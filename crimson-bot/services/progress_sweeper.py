"""
services/progress_sweeper.py
============================
Background sweeper that cleans up stale progress messages left by long-running
tasks and marks progress entries as cleaned to prevent orphaned messages.

Runs in a daemon thread and periodically checks the `task_store` for running
tasks with `progress.message_id` older than a configured threshold.
"""

from __future__ import annotations

import threading
import time

from core.config import cfg, log
from services.tasks import task_store
import services.bridge_api as bridge_api

_sweeper_thread: threading.Thread | None = None
_sweeper_stop = threading.Event()


def _sweep_loop():
    interval = int(cfg("progress_sweeper_interval_sec") or 60)
    stale_secs = int(cfg("progress_stale_seconds") or 3600)
    log.info("[Sweeper] started; interval=%ds stale_secs=%ds", interval, stale_secs)
    while not _sweeper_stop.is_set():
        try:
            now = time.time()
            running = task_store.list(status="running", limit=200)
            for t in running:
                prog = t.get("progress") or {}
                mid = prog.get("message_id")
                if not mid:
                    continue
                started_at = t.get("started_at") or 0
                age = now - started_at
                if age >= stale_secs:
                    jid = t.get("owner_jid") or ""
                    try:
                        bridge_api.bridge_delete(jid, mid)
                        log.info("[Sweeper] deleted stale progress mid=%s for task=%s jid=%s", mid, t.get("id"), jid)
                        # annotate task progress so UI knows we cleaned it
                        task_store.update(t["id"], progress={"message": "stale progress cleaned", "pct": prog.get("pct", 0), "message_id": None})
                    except Exception as exc:
                        log.warning("[Sweeper] failed to delete mid=%s: %s", mid, exc)
        except Exception as e:
            log.warning("[Sweeper] loop error: %s", e)
        _sweeper_stop.wait(timeout=interval)
    log.info("[Sweeper] stopped")


def start_sweeper() -> None:
    global _sweeper_thread, _sweeper_stop
    if _sweeper_thread and _sweeper_thread.is_alive():
        return
    _sweeper_stop = threading.Event()
    _sweeper_thread = threading.Thread(target=_sweep_loop, name="ProgressSweeper", daemon=True)
    _sweeper_thread.start()


def stop_sweeper() -> None:
    global _sweeper_stop
    if _sweeper_stop:
        _sweeper_stop.set()
