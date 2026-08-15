"""
services/natural_cron.py
========================
NL → schedule parser for the self-awareness task engine.

Supports the patterns the user asked for plus common variants:
  "every 30 minutes"            → interval (minutes)
  "every 2 hours"               → interval (hours)
  "every day at 8:00"           → daily at HH:MM
  "every weekday at 18:30"      → weekdays at HH:MM
  "every monday at 9am"         → weekly (single weekday)
  "in 10 minutes"               → one-shot offset
  "at 14:00"                    → one-shot at HH:MM today or tomorrow
  "at 2026-08-15 09:00"         → one-shot absolute

Returns: (schedule_dict, next_run_at_datetime)
schedule_dict["next_run_at"] is the unix-ts the dispatcher should use.
For recurring schedules the dispatcher rebinds next_run_at after each fire
using the same dict.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from core.config import TZ


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _now_in_tz() -> datetime:
    return datetime.now(TZ)


def _parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    """Accept '8:00', '08:00', '8am', '8 pm', '14h00', '9:30 PM'."""
    s = s.strip().lower().replace("h", ":").replace(" ", "")
    # 14:00 / 8:00
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 8am / 8pm / 9:30am / 11 am
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", s)
    if m:
        h = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        return h, mm
    return None


def parse(text: str) -> Optional[Tuple[dict, datetime]]:
    """Return (schedule, next_run_dt) on success, None on no-match."""
    if not text:
        return None
    s = text.lower().strip().rstrip(".!?")

    # ── Absolute timestamp: 2026-08-15 09:00 / 2026-08-15 ────────────────────
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})", s)
    if m:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if dt <= _now_in_tz():
            return None
        return ({"type": "at", "value": s, "next_run_at": dt.timestamp()}, dt)

    # ── In N minutes / hours ────────────────────────────────────────────────
    m = re.fullmatch(r"in\s+(\d+)\s+(minute|minutes|min|hour|hours|hr|h)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        dt = _now_in_tz() + (timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n))
        return ({"type": "at", "value": s, "next_run_at": dt.timestamp()}, dt)

    # ── At HH:MM (today or tomorrow if past) ─────────────────────────────────
    m = re.fullmatch(r"at\s+(.+)", s)
    if m:
        hm = _parse_hhmm(m.group(1))
        if hm:
            h, mm = hm
            now = _now_in_tz()
            tgt = now.replace(hour=h, minute=mm, second=0, microsecond=0)
            if tgt <= now:
                tgt += timedelta(days=1)
            sched = {"type": "at", "value": m.group(1), "next_run_at": tgt.timestamp()}
            # Also expose HH:MM for re-scheduling if needed.
            sched["hh"] = h
            sched["mm"] = mm
            return sched, tgt

    # ── every weekday|day at HH:MM ────────────────────────────────────────────
    m = re.fullmatch(r"every\s+(day|weekday|monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+(.+))?", s)
    if m:
        when_word = m.group(1)
        time_part = (m.group(2) or "").strip()
        hm = _parse_hhmm(time_part) if time_part else (8, 0)  # default 8am
        if not hm:
            return None
        h, mm = hm
        sched_value = {"type": "cron_nl", "when": when_word, "h": h, "m": mm}
        next_dt = _next_occurrence(when_word, h, mm)
        sched_value["next_run_at"] = next_dt.timestamp()
        return sched_value, next_dt

    # ── every N minutes / hours (interval) ──────────────────────────────────
    m = re.fullmatch(r"every\s+(\d+)\s+(minute|minutes|min|hour|hours|hr|h)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        is_hours = unit.startswith("h")
        sched_value = {"type": "interval", "value": s}
        if is_hours:
            sched_value["hours"] = n
            secs = n * 3600
        else:
            sched_value["minutes"] = n
            secs = n * 60
        next_dt = _now_in_tz() + timedelta(seconds=secs)
        sched_value["next_run_at"] = next_dt.timestamp()
        sched_value["interval_secs"] = secs
        return sched_value, next_dt

    return None


def _next_occurrence(when: str, h: int, m: int) -> datetime:
    """Next datetime at H:M matching `when` (`day`, `weekday`, or `monday`…)."""
    now = _now_in_tz()
    today = now.replace(hour=h, minute=m, second=0, microsecond=0)

    if when == "day":
        if today <= now:
            today += timedelta(days=1)
        return today

    if when == "weekday":
        # Mon-Fri only
        while True:
            if today > now and today.weekday() < 5:
                return today
            today += timedelta(days=1)
            today = today.replace(hour=h, minute=m, second=0, microsecond=0)

    # single weekday
    target = WEEKDAYS.index(when)
    delta_days = (target - now.weekday()) % 7
    today += timedelta(days=delta_days)
    today = today.replace(hour=h, minute=m, second=0, microsecond=0)
    if today <= now:
        today += timedelta(days=7)
    return today


def next_after(schedule: dict, *, fired_at: datetime | None = None) -> datetime | None:
    """Return the next run-time AFTER `fired_at` for a recurring schedule.
    Returns None for one-shot (`type == "at"`)."""
    fired_at = fired_at or datetime.now(TZ)
    t = schedule.get("type")
    if t == "at":
        return None
    if t == "interval":
        secs = int(schedule.get("interval_secs") or 3600)
        return fired_at + timedelta(seconds=secs)
    if t == "cron_nl":
        when = schedule.get("when") or "day"
        h = int(schedule.get("h") or 8)
        m = int(schedule.get("m") or 0)
        # Probe forward one minute to make sure we don't recompute the same fire.
        return _next_occurrence(when, h, m)
    return None
