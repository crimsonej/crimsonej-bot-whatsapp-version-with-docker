"""
services/progress.py
====================
Progress-message lifecycle for long-running tasks.

Each `ProgressSession` owns one WhatsApp text message per user chat:

  start(label)     — sends "label…" and captures the WhatsApp message_id
  update(pct, msg) — edits the message in place with "label… <msg> N%"
                     throttled to ~1.5s between edits so we don't hammer
                     the bridge for a 10s download
  finish()         — deletes the progress message; no-op if never started
                     or bridge is down

The session is intentionally in-memory only — if the bot restarts mid-task,
the progress message becomes orphaned in WhatsApp. That's acceptable; the
cancel route can also delete it explicitly.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from core.config import log
import services.bridge_api as bridge_api


class ProgressSession:
    """Thread-safe progress message bound to one user chat."""

    def __init__(self, jid: str, *, throttle_secs: float = 1.5) -> None:
        self.jid = jid
        self.throttle = throttle_secs
        self._message_id: str | None = None
        self._last_update_ts: float = 0.0
        self._last_text: str = ""
        self._label: str = ""
        self._lock = threading.Lock()
        self._finished = False
        self.started_at: float | None = None

    # ── Public API ───────────────────────────────────────────────────────────
    def start(self, label: str) -> None:
        """Send the initial progress message. Safe to call multiple times —
        only the first call actually sends."""
        with self._lock:
            if self._message_id or self._finished:
                return
            self._label = label.strip()
            text = f"{self._label}…"
            r = bridge_api.bridge_send(self.jid, text)
            if r.get("ok") and r.get("message_id"):
                self._message_id = str(r["message_id"])
                self._last_text = text
                self.started_at = time.time()
                log.debug("[Progress] started: jid=%s mid=%s text=%r",
                          self.jid.split("@")[0], self._message_id, text)
            else:
                log.debug("[Progress] start send failed: %s", r.get("error"))

    def update(self, pct: int, message: str | None = None) -> None:
        """Edit the message in place. Throttled."""
        if not (0 <= pct <= 100):
            pct = max(0, min(100, pct))
        with self._lock:
            if self._finished or not self._message_id or not self._label:
                return
            now = time.time()
            if now - self._last_update_ts < self.throttle:
                return
            tail = f"{message} " if message else ""
            text = f"{self._label}… {tail}{pct}%"
            if text == self._last_text:
                return
            self._last_text = text
            self._last_update_ts = now  # update inside lock to prevent race
            mid = self._message_id
        bridge_api.bridge_edit(self.jid, mid, text)

    def finish(self) -> None:
        """Delete the progress message. Idempotent and lock-safe. Retries on failure."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            mid = self._message_id
            self._message_id = None
        if mid:
            for attempt in range(3):
                r = bridge_api.bridge_delete(self.jid, mid)
                if r.get("ok"):
                    log.debug("[Progress] finish delete mid=%s ok=True attempt=%d", mid, attempt + 1)
                    break
                log.debug("[Progress] finish delete mid=%s attempt=%d err=%s", mid, attempt + 1, r.get("error"))
                if attempt < 2:
                    time.sleep(0.5)

    def age(self) -> float:
        """Return seconds since start; 0 if never started."""
        if not self.started_at:
            return 0.0
        return time.time() - self.started_at

    # ── Introspection ────────────────────────────────────────────────────────
    @property
    def message_id(self) -> str | None:
        with self._lock:
            return self._message_id

    @property
    def is_finished(self) -> bool:
        with self._lock:
            return self._finished

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"message_id": self._message_id, "label": self._label,
                    "finished": self._finished}
