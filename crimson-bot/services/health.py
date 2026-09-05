"""
services/health.py
===================
Lightweight health checks, heartbeat, and status aggregation for the bot.

Provides `get_status()` which composes dispatcher, task, bridge, and reporter
health into a structured dict. Also provides a heartbeat thread to periodically
emit an event to `event_log` and refresh an in-memory `last_status` snapshot.
"""

from __future__ import annotations

import threading
import time
import requests
from typing import Any

from core.config import log, cfg
from core.eventlog import event_log
from services.tasks import task_store
from services.dispatcher import dispatcher_is_alive, get_dispatcher
import services.progress as progress_module
import services.bridge_api as bridge_api
from services.autofix import inspect_task_health, safe_auto_heal

# Simple module-level snapshot updated by the heartbeat
_last_status: dict[str, Any] = {}
_hb_thread: threading.Thread | None = None
_hb_stop = threading.Event()


def _probe_bridge(timeout: float = 2.0) -> dict:
    try:
        # Prefer an actual health endpoint, fall back to root
        base = getattr(bridge_api, 'BRIDGE_BASE', None) or bridge_api.BRIDGE_BASE
        r = requests.get(base, timeout=timeout)
        return {"ok": True, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_status() -> dict:
    """Return a structured status snapshot for use by tools and /health.

    Keys: dispatcher_alive, task_stats, running_count, bridge, recent_events
    """
    try:
        disp_alive = dispatcher_is_alive()
    except Exception:
        disp_alive = False

    stats = task_store.stats() if task_store else {}
    running = task_store.list(status="running", limit=20) if task_store else []
    bridge = _probe_bridge()
    recent = []
    try:
        recent = [e for e in event_log.recent(10)]
    except Exception:
        recent = []

    status = {
        "ts": int(time.time()),
        "dispatcher_alive": bool(disp_alive),
        "task_stats": stats,
        "running_tasks": len(running),
        "bridge": bridge,
        "recent_events": recent,
    }
    return status


def _maybe_autofix():
    try:
        status = get_status()
        dead_dispatcher = not bool(status.get("dispatcher_alive"))
        if dead_dispatcher:
            log.warning("[Health] auto-heal triggered: dispatcher dead")
            safe_auto_heal("health_monitor")
        else:
            from services.autofix import requeue_stale_tasks
            result = requeue_stale_tasks()
            if result.get("requeued"):
                log.warning("[Health] requeued stale tasks: %s", result["requeued"])
    except Exception as e:
        log.warning("[Health] auto-heal check failed: %s", e)


def _heartbeat_loop():
    interval = int(cfg("health_heartbeat_interval_sec") or 60)
    log.info("[Health] heartbeat starting; interval=%ds", interval)
    while not _hb_stop.is_set():
        try:
            s = get_status()
            _last_status.update(s)
            # add a condensed event
            event_log.append("health", "heartbeat", summary=f"dispatcher={s['dispatcher_alive']} running={s['running_tasks']} tasks_total={s['task_stats'].get('total')}", payload={"bridge": s['bridge']})
            _maybe_autofix()
        except Exception as e:
            log.warning("[Health] heartbeat error: %s", e)
        _hb_stop.wait(timeout=interval)
    log.info("[Health] heartbeat stopped")


def start_heartbeat() -> None:
    global _hb_thread, _hb_stop
    if _hb_thread and _hb_thread.is_alive():
        return
    _hb_stop = threading.Event()
    _hb_thread = threading.Thread(target=_heartbeat_loop, name="HealthHeartbeat", daemon=True)
    _hb_thread.start()


def stop_heartbeat() -> None:
    global _hb_stop
    if _hb_stop:
        _hb_stop.set()


def last_status() -> dict:
    return dict(_last_status)
