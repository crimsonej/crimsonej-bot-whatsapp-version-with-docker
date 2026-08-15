"""
services/reporter.py
====================
Watches the event log for actionable transitions and posts a WhatsApp
message to the task owner (or the creator JID for system tasks) when
something needs attention.

Events handled:
  task_fail             → owner of the task
  bridge_lost           → creator JID
  bridge_silence        → creator JID

Outbound alerts go through the existing bridge POST /send_message route
(bridge.js). The bridge is therefore the channel even for self-alerts.
"""

from __future__ import annotations

import os
import requests
import threading
import time

from core.config import cfg, log
from core.eventlog import event_log
from services.tasks import task_store


_reporter_thread: threading.Thread | None = None
_reporter_stop = threading.Event()


class Reporter:
    BRIDGE_URL = "http://127.0.0.1:7860/send_message"
    BRIDGE_HEALTH_URL = "http://127.0.0.1:7860/health/full"

    def __init__(self) -> None:
        self._poll_secs = max(5, int(cfg("bridge_health_interval_sec") or 30))
        self._silence_secs = int(cfg("bridge_silence_alert_sec") or 120)
        self._last_seq = 0
        self._last_bridge_seen = time.time()
        self._bridge_ok = False
        self._bridge_alerted_for = 0  # silence_ms at last alert

    def start(self) -> None:
        global _reporter_thread, _reporter_stop

        if _reporter_thread and _reporter_thread.is_alive():
            return

        _reporter_stop = threading.Event()
        self._last_seq = event_log.last_seq
        _reporter_thread = threading.Thread(target=self._loop, name="CrimsonReporter", daemon=True)
        _reporter_thread.start()
        # Wire our sender into the dispatcher too.
        try:
            from services.dispatcher import set_reporter_sender
            set_reporter_sender(self._send_task_notification)
        except Exception as exc:
            log.warning("[Reporter] could not plug into dispatcher: %s", exc)
        log.info("[Reporter] started — poll=%ds silence-alert=%ds", self._poll_secs, self._silence_secs)

    def stop(self) -> None:
        if _reporter_stop:
            _reporter_stop.set()

    # ── Loop ─────────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not _reporter_stop.is_set():
            try:
                self._process_events()
                self._check_bridge()
            except Exception as exc:
                log.debug("[Reporter] tick error: %s", exc)
            _reporter_stop.wait(timeout=self._poll_secs)
        log.info("[Reporter] stopped")

    def _process_events(self) -> None:
        # Send buffered events from after the last sequence we acted on.
        for evt in event_log.stream(since_seq=self._last_seq):
            self._handle(evt)
            try:
                self._last_seq = max(self._last_seq, int(evt.get("seq") or 0))
            except Exception:
                pass

    def _handle(self, evt: dict) -> None:
        kind = evt.get("kind")
        payload = evt.get("payload") or {}
        if kind == "task_fail":
            task_id = payload.get("task_id")
            err = payload.get("error") or "no error message"
            name = payload.get("name") or "task"
            user_id = evt.get("user_id") or ""
            jid = self._jid_for_user(user_id)
            if not jid:
                return
            msg = (f"⚠️ task #{task_id} ({name}) flopped: {err}\n"
                   f"/status to inspect, or /tasks cancel {task_id}")
            self._send_wa(jid, msg)

    # ── Bridge health ────────────────────────────────────────────────────────
    def _check_bridge(self) -> None:
        try:
            r = requests.get(self.BRIDGE_HEALTH_URL, timeout=4)
            if r.status_code == 200:
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                self._bridge_ok = bool(data.get("connected") if isinstance(data, dict) else False)
                if self._bridge_ok:
                    self._last_bridge_seen = time.time()
                    self._bridge_alerted_for = 0
                    return
        except Exception:
            self._bridge_ok = False

        # Either down or socket disconnected
        now = time.time()
        silence_ms = int((now - self._last_bridge_seen) * 1000)
        if silence_ms >= self._silence_secs * 1000:
            # Avoid re-alerting for the same outage window
            if abs(silence_ms - self._bridge_alerted_for) < 60_000:
                return
            self._bridge_alerted_for = silence_ms
            jid = cfg("owner_jid") or ""
            if not jid:
                return
            self._send_wa(
                jid,
                f"⚠️ bridge looks dead — silence {silence_ms // 1000}s. "
                f"check `/health/full` on port 7860",
            )
            event_log.append("reporter", "bridge_lost",
                             summary=f"bridge silence {silence_ms // 1000}s",
                             payload={"silence_ms": silence_ms})

    # ── Send helpers ─────────────────────────────────────────────────────────
    def _send_wa(self, jid: str, text: str) -> None:
        """Outbound channel: POST to bridge via the shared helper. If the bridge
        is also down we just log; nothing else we can do."""
        import services.bridge_api as bridge_api
        r = bridge_api.bridge_send(jid, text)
        if not r.get("ok"):
            log.warning("[Reporter] send_wa failed: %s", r.get("error"))
            event_log.append("reporter", "alert_send_failed",
                             summary=f"couldn't deliver to {jid}: {r.get('error')}",
                             payload={"jid": jid, "text": text[:120]})

    def _send_task_notification(self, task: dict, status: str, result) -> None:
        """Called by dispatcher after task_done or task_fail when notify_on allows."""
        if status not in ("done", "failed"):
            return
        user_id = task.get("owner_user_id") or ""
        jid = task.get("owner_jid") or self._jid_for_user(user_id) or ""
        if not jid:
            return
        task_id = task.get("id", "?")
        name = task.get("name") or "task"
        if status == "done":
            body = _format_done(task_id, name, result)
        else:
            body = _format_fail(task_id, name, result)
        self._send_wa(jid, body)

    def _jid_for_user(self, user_id: str) -> str | None:
        """Return a 'xxx@s.whatsapp.net' JID for a user_phone. None if we don't know."""
        if not user_id:
            return None
        # Default: use CREATOR_PHONE if user_id matches; otherwise strip non-digits.
        digits = "".join(ch for ch in str(user_id) if ch.isdigit())
        if not digits:
            return None
        return f"{digits}@s.whatsapp.net"


# ── Helpers ──────────────────────────────────────────────────────────────────
def _format_done(task_id: str, name: str, result) -> str:
    summary = ""
    if isinstance(result, dict):
        if result.get("url"):
            summary = f"\nfile: {result.get('filename') or result.get('url')}"
        elif result.get("reply"):
            summary = f"\n{result['reply']}"
        else:
            summary = ""
    elif result:
        summary = f"\n{str(result)[:200]}"
    return f"✅ task #{task_id} ({name}) done{summary}"


def _format_fail(task_id: str, name: str, err) -> str:
    return f"⚠️ task #{task_id} ({name}) flopped: {err}"


# ── Module singleton ──────────────────────────────────────────────────────────
_reporter: Reporter | None = None


def start_reporter() -> None:
    global _reporter
    if _reporter is None:
        _reporter = Reporter()
    _reporter.start()


def stop_reporter() -> None:
    if _reporter:
        _reporter.stop()
