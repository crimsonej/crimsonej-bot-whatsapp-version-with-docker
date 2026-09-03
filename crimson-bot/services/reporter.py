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
        self._last_autofix_ts = 0
        self._autofix_dedupe_sec = int(cfg("autofix_alert_dedupe_sec") or 300)

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
        self._retry_pending_task_notifications()

    def _retry_pending_task_notifications(self) -> None:
        """Deliver task results missed while the bridge was unavailable."""
        for task in task_store.list(limit=200):
            if not task.get("notification_pending"):
                continue
            status = task.get("status")
            if status == "done" and task.get("notify_on") in ("done", "always"):
                delivered = self._send_task_notification(task, "done", task.get("result"))
            elif status == "pending" and task.get("kind") == "recurring" and task.get("result"):
                delivered = self._send_task_notification(task, "done", task.get("result"))
            elif status == "failed" and task.get("notify_on") in ("failed", "always"):
                delivered = self._send_task_notification(task, "failed", task.get("error"))
            else:
                delivered = True
            if delivered:
                task_store.mark_notification(task["id"], sent=True)

    def _handle(self, evt: dict) -> None:
        kind = evt.get("kind")
        payload = evt.get("payload") or {}
        src = evt.get("source") or ""
        if kind == "task_fail":
            task_id = payload.get("task_id")
            err = payload.get("error") or "no error message"
            name = payload.get("name") or "task"
            user_id = evt.get("user_id") or ""
            # Prefer the actual owner_jid stored on the task; fall back to
            # reconstructing from user_phone. Avoid the broken strip-digits
            # heuristic when the JID is already on the payload.
            jid = (payload.get("owner_jid")
                   or self._jid_for_user(user_id))
            if not jid:
                return
            msg = (f"⚠️ task #{task_id} ({name}) flopped: {err}\n"
                   f"/status to inspect, or /tasks cancel {task_id}")
            self._send_wa(jid, msg)

        # Autofix or health events should be notified to owner (summary only)
        elif src == "autofix" or kind.startswith("autofix"):
            now = time.time()
            if now - self._last_autofix_ts < self._autofix_dedupe_sec:
                return
            self._last_autofix_ts = now
            jid = cfg("owner_jid") or ""
            if not jid:
                return
            summary = evt.get("summary") or f"Autofix event: {kind}"
            body = f"🔧 Autofix: {summary}\nDetails: {payload or {}}"
            self._send_wa(jid, body)

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
    def _send_wa(self, jid: str, text: str) -> bool:
        """Outbound channel: POST to bridge via the shared helper. If the bridge
        is also down we just log; nothing else we can do."""
        import services.bridge_api as bridge_api
        r = bridge_api.bridge_send(jid, text)
        if not r.get("ok"):
            log.warning("[Reporter] send_wa failed: %s", r.get("error"))
            event_log.append("reporter", "alert_send_failed",
                             summary=f"couldn't deliver to {jid}: {r.get('error')}",
                             payload={"jid": jid, "text": text[:120]})
            return False
        return True

    def _send_task_notification(self, task: dict, status: str, result) -> bool:
        """Called by dispatcher after task_done or task_fail when notify_on allows."""
        if status not in ("done", "failed"):
            return True
        user_id = task.get("owner_user_id") or ""
        jid = task.get("owner_jid") or self._jid_for_user(user_id) or ""
        if not jid:
            return True
        task_id = task.get("id", "?")
        name = task.get("name") or "task"
        if status == "done":
            body = _format_done(task_id, name, result)
        else:
            body = _format_fail(task_id, name, result)
        # Deliver the actual file for download tasks: the bridge reads the
        # shared filesystem path directly (no base64 over HTTP).
        if status == "done" and isinstance(result, dict):
            path = result.get("path") or ""
            log.info("[Reporter] notify done for %s: path=%s ispublic=%s", name, path, result.get("is_public"))
            if path and not str(path).startswith("http"):
                import os as _os
                if _os.path.isfile(path):
                    import services.bridge_api as bridge_api
                    r = bridge_api.bridge_send(
                        jid, body,
                        media_path=path,
                        media_type=result.get("media_type") or "audio",
                        filename=result.get("filename") or "file",
                        timeout=300,
                    )
                    if r.get("ok"):
                        log.info("[Reporter] media delivered for %s: mid=%s", name, r.get("message_id"))
                        return True
                    else:
                        log.warning("[Reporter] media delivery FAILED for %s: %s", name, r.get("error"))
                        return False
                else:
                    log.info("[Reporter] path missing on disk, falling back to text")
        return self._send_wa(jid, body)

    def _jid_for_user(self, user_id: str) -> str | None:
        """Return a valid WhatsApp JID for a user_phone.

        - If user_id already looks like a JID ('xxx@domain'), pass it through.
        - Otherwise strip non-digits and assume @s.whatsapp.net.
        Returns None if there's nothing to send to.
        """
        if not user_id:
            return None
        s = str(user_id).strip()
        if "@" in s:
            # Already a JID. If the domain isn't a WhatsApp domain, leave it
            # alone — the bridge will reject it if it's truly invalid.
            return s
        digits = "".join(ch for ch in s if ch.isdigit())
        if not digits:
            return None
        return f"{digits}@s.whatsapp.net"


# ── Helpers ──────────────────────────────────────────────────────────────────
def _format_done(task_id: str, name: str, result) -> str:
    summary = ""
    if isinstance(result, dict):
        if result.get("filename"):
            summary = f"\nfile: {result.get('filename')}"
        elif result.get("url"):
            summary = f"\nfile: {result.get('filename') or result.get('url')}"
        elif result.get("reply"):
            summary = f"\n{result['reply']}"
        else:
            summary = ""
    elif result:
        summary = f"\n{str(result)[:200]}"
    if isinstance(result, dict) and result.get("report"):
        summary = f"\n{str(result['report'])[:12000]}"
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
