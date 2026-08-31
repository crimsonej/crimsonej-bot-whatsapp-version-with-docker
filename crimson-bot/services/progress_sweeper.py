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


def _clean_temp_media_files() -> None:
    """Clean up temporary media files older than 1 hour in /tmp and /dev/shm."""
    import os
    now = time.time()
    max_age_sec = 3600  # 1 hour
    prefixes = ("song_", "orig_", "stk_", "whatsapp_", "tmp_")
    dirs_to_clean = ["/tmp", "/dev/shm"]

    for d in dirs_to_clean:
        if not os.path.exists(d):
            continue
        try:
            for f in os.listdir(d):
                if any(f.startswith(p) for p in prefixes) or f.endswith(("_whatsapp.mp4", "_whatsapp.ogg", "_aac.mp4")):
                    filepath = os.path.join(d, f)
                    try:
                        if os.path.isfile(filepath):
                            mtime = os.path.getmtime(filepath)
                            if now - mtime > max_age_sec:
                                os.remove(filepath)
                                log.info("[Sweeper] Removed stale temp media: %s", filepath)
                    except Exception:
                        pass
        except Exception as e:
            log.debug("[Sweeper] Temp cleanup error for %s: %s", d, e)


def _sweep_loop():
    interval = int(cfg("progress_sweeper_interval_sec") or 60)
    stale_secs = int(cfg("progress_stale_seconds") or 3600)
    log.info("[Sweeper] started; interval=%ds stale_secs=%ds", interval, stale_secs)
    while not _sweeper_stop.is_set():
        try:
            _clean_temp_media_files()
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
