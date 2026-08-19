"""
services/trading_scheduler.py
=============================
Daily trading briefing scheduler. Runs at 07:30 UTC (pre-London) and 21:30 UTC (EOD).
Posts to owner's WhatsApp via bridge API.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any

from core.config import cfg, log, TZ
from core.eventlog import event_log
from services.trading import generate_daily_briefing, post_briefing_to_owner


class TradingBriefingScheduler:
    """Scheduler for daily trading briefings."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._enabled = False

    def start(self) -> None:
        """Start the scheduler thread."""
        if self._thread and self._thread.is_alive():
            log.info("[TradingScheduler] Already running")
            return

        self._enabled = cfg("trading_briefing_enabled")
        if not self._enabled:
            log.info("[TradingScheduler] Disabled in config")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="TradingBriefingScheduler")
        self._thread.start()
        log.info("[TradingScheduler] Started")

    def stop(self) -> None:
        """Stop the scheduler thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("[TradingScheduler] Stopped")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        """Main scheduler loop."""
        while not self._stop_event.is_set():
            try:
                now = datetime.now(TZ)
                next_run = self._next_run_time(now)

                # Sleep until next run
                wait_seconds = (next_run - now).total_seconds()
                if wait_seconds > 0:
                    log.debug(f"[TradingScheduler] Next briefing at {next_run.strftime('%H:%M %Z')} (in {wait_seconds:.0f}s)")
                    self._stop_event.wait(timeout=wait_seconds)

                if self._stop_event.is_set():
                    break

                # Determine session based on config times
                pre_london_str = cfg("trading_briefing_pre_london")
                pre_hour = int(pre_london_str.split(":")[0])
                session = "pre_london" if next_run.hour == pre_hour else "eod"
                self._execute_briefing(session)

            except Exception as exc:
                log.error(f"[TradingScheduler] Error in scheduler loop: {exc}")
                time.sleep(60)  # Back off on error

    def _next_run_time(self, now: datetime) -> datetime:
        """Calculate next briefing time based on config (EAT timezone)."""
        # Config times are in EAT (Africa/Kampala)
        pre_london_str = cfg("trading_briefing_pre_london")
        eod_str = cfg("trading_briefing_eod")

        pre_hour, pre_min = map(int, pre_london_str.split(":"))
        eod_hour, eod_min = map(int, eod_str.split(":"))

        # Target times in local TZ
        pre_london = now.replace(hour=pre_hour, minute=pre_min, second=0, microsecond=0)
        eod = now.replace(hour=eod_hour, minute=eod_min, second=0, microsecond=0)

        # If we're past EOD, next is tomorrow's pre-London
        if now >= eod:
            next_run = pre_london + timedelta(days=1)
        elif now >= pre_london:
            next_run = eod
        else:
            next_run = pre_london

        return next_run

    def _execute_briefing(self, session: str) -> None:
        """Execute the briefing and post to owner."""
        try:
            log.info(f"[TradingScheduler] Running {session} briefing")

            # Import bridge_api here to avoid circular imports
            import services.bridge_api as bridge_api

            success = post_briefing_to_owner(bridge_api, session)

            if success:
                event_log.append("trading", "daily_briefing_sent",
                                 summary=f"Daily {session} briefing posted to owner",
                                 payload={"session": session})
            else:
                log.warning(f"[TradingScheduler] Failed to post {session} briefing")

        except Exception as exc:
            log.error(f"[TradingScheduler] Briefing execution failed: {exc}")

    def trigger_now(self, session: str = "pre_london") -> bool:
        """Manually trigger a briefing (for testing or /brief command)."""
        try:
            import services.bridge_api as bridge_api
            return post_briefing_to_owner(bridge_api, session)
        except Exception as exc:
            log.error(f"[TradingScheduler] Manual trigger failed: {exc}")
            return False


# Global instance
_scheduler: TradingBriefingScheduler | None = None


def start_briefing_scheduler() -> None:
    """Start the daily briefing scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = TradingBriefingScheduler()
    _scheduler.start()


def stop_briefing_scheduler() -> None:
    """Stop the daily briefing scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None


def restart_briefing_scheduler() -> None:
    """Restart the daily briefing scheduler."""
    stop_briefing_scheduler()
    start_briefing_scheduler()


def trigger_briefing_now(session: str = "pre_london") -> bool:
    """Manually trigger a briefing now."""
    if _scheduler:
        return _scheduler.trigger_now(session)
    return False


def briefing_scheduler_alive() -> bool:
    """Check if scheduler is running."""
    return _scheduler is not None and _scheduler.is_alive()