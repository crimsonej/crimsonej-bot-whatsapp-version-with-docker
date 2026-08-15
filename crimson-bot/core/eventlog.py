"""
core/eventlog.py
================
Append-only JSONL event log for the Crimsonej self-awareness layer.

Every meaningful action the bot takes (or detects) lands here as a single
line in `~/.crimson/events.jsonl`. Components subscribe to this stream by
calling `EventLog.recent()` or `EventLog.stream()`.

Thread-safe: a single `threading.Lock` guards both in-memory deque and
file writes. The on-disk file is rotated by `truncate_to_n()` when it
exceeds `max_entries`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Iterator

from core.config import EVENTS_FILE, log


class EventLog:
    def __init__(self, path: str = EVENTS_FILE, max_entries: int = 2000) -> None:
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._buffer: deque[dict] = deque(maxlen=max_entries)
        self._last_seq: int = 0
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception as exc:
            log.warning("[EventLog] could not load %s: %s", self.path, exc)
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            seq = int(evt.get("seq") or 0)
            if seq > self._last_seq:
                self._last_seq = seq
            if len(self._buffer) >= self.max_entries:
                break
            self._buffer.append(evt)

    def _flush(self) -> None:
        # Caller must hold the lock.
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                for evt in self._buffer:
                    fh.write(json.dumps(evt, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("[EventLog] flush failed: %s", exc)

    def truncate_to_n(self, n: int) -> None:
        with self._lock:
            if len(self._buffer) <= n:
                return
            # Drop oldest entries, keep the most recent `n`.
            tail = list(self._buffer)[-n:]
            self._buffer.clear()
            for evt in tail:
                self._buffer.append(evt)
            self._flush()

    # ── Public API ────────────────────────────────────────────────────────────
    def append(self, source: str, kind: str, *, summary: str,
               user_id: str | None = None, jid: str | None = None,
               payload: dict | None = None, ts: float | None = None) -> dict:
        with self._lock:
            self._last_seq += 1
            evt = {
                "ts": ts or time.time(),
                "iso": _iso(ts or time.time()),
                "seq": self._last_seq,
                "source": source,
                "kind": kind,
                "summary": summary,
            }
            if user_id:
                evt["user_id"] = user_id
            if jid:
                evt["jid"] = jid
            if payload:
                evt["payload"] = payload
            self._buffer.append(evt)
            self._flush()
            return evt

    def recent(self, n: int = 20, source: str | None = None,
               kind: str | None = None, since_seq: int = 0) -> list[dict]:
        with self._lock:
            data = list(self._buffer)
        # Filter then slice from the tail.
        filtered = [e for e in data if (not source or e.get("source") == source)
                                  and (not kind or e.get("kind") == kind)
                                  and int(e.get("seq") or 0) > since_seq]
        return filtered[-n:]

    def stream(self, since_seq: int = 0) -> Iterator[dict]:
        with self._lock:
            data = list(self._buffer)
        for evt in data:
            if int(evt.get("seq") or 0) > since_seq:
                yield evt

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq


def _iso(ts: float) -> str:
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ── Module singleton ──────────────────────────────────────────────────────────
event_log = EventLog()
