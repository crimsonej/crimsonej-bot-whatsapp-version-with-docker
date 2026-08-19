"""
services/autofix.py
===================
Safe self-healing actions for the bot. These are intentionally conservative:
- restart dispatcher if it is dead
- requeue or fail obviously stale running tasks
- delete stale progress messages for tasks that have no real activity anymore

This module never touches user messages other than progress messages and task
state; it does not delete bot replies or arbitrary WhatsApp content.
"""

from __future__ import annotations

import time
from typing import Any

from core.config import log
from core.eventlog import event_log
from services.tasks import task_store
import services.bridge_api as bridge_api


SAFE_HEAL_ACTIONS = (
    "restart_dispatcher",
    "requeue_stale_tasks",
    "clear_stale_progress",
)


def _task_is_stale(task: dict, now: float | None = None, stale_seconds: int = 1800) -> bool:
    now = now or time.time()
    started_at = task.get("started_at")
    if not started_at:
        return False
    age = now - float(started_at)
    return age >= stale_seconds


def restart_dispatcher() -> dict:
    """Restart the dispatcher if it is not alive. Safe and idempotent."""
    try:
        from services.dispatcher import start_dispatcher, dispatcher_is_alive
        if dispatcher_is_alive():
            return {"ok": True, "action": "restart_dispatcher", "result": "already_alive"}
        start_dispatcher()
        event_log.append("autofix", "dispatcher_restarted", summary="dispatcher restarted by safe auto-heal", payload={"action": "restart_dispatcher"})
        return {"ok": True, "action": "restart_dispatcher", "result": "restarted"}
    except Exception as exc:
        log.warning("[Autofix] restart_dispatcher failed: %s", exc)
        return {"ok": False, "action": "restart_dispatcher", "error": str(exc)}


def clear_stale_progress(stale_seconds: int = 1800) -> dict:
    """Delete stale WhatsApp progress messages and clear task progress metadata."""
    cleared = []
    now = time.time()
    for task in task_store.list(status="running", limit=200):
        prog = task.get("progress") or {}
        mid = prog.get("message_id")
        if not mid:
            continue
        if not _task_is_stale(task, now=now, stale_seconds=stale_seconds):
            continue
        jid = task.get("owner_jid") or ""
        try:
            if jid:
                bridge_api.bridge_delete(jid, mid)
            task_store.update(task["id"], progress={"message": "stale progress cleaned", "pct": prog.get("pct", 0), "message_id": None})
            cleared.append({"task_id": task["id"], "jid": jid, "message_id": mid})
            event_log.append("autofix", "stale_progress_cleared", summary=f"cleared stale progress for task {task['id']}", payload={"task_id": task["id"], "message_id": mid})
        except Exception as exc:
            log.warning("[Autofix] deleting stale progress failed for task %s: %s", task.get("id"), exc)
    return {"ok": True, "action": "clear_stale_progress", "cleared": cleared}


def requeue_stale_tasks(stale_seconds: int = 1800) -> dict:
    """Requeue stale running tasks that have not progressed. Safe because it only
    affects running tasks proven stale by elapsed time and leaves task state visible."""
    requeued = []
    now = time.time()
    for task in task_store.list(status="running", limit=200):
        if not _task_is_stale(task, now=now, stale_seconds=stale_seconds):
            continue
        task_id = task.get("id")
        try:
            task_store.update(
                task_id,
                expected_status="running",
                status="pending",
                error="stale-running-task-requeued-by-autofix",
                schedule={"next_run_at": now + 30},
                progress={"pct": 0, "message": "requeued by autofix"},
            )
            requeued.append(task_id)
            event_log.append("autofix", "stale_task_requeued", summary=f"task {task_id} requeued by safe auto-heal", payload={"task_id": task_id})
        except Exception as exc:
            log.warning("[Autofix] requeue_stale_tasks failed for %s: %s", task_id, exc)
    return {"ok": True, "action": "requeue_stale_tasks", "requeued": requeued}


def safe_auto_heal(reason: str = "health_check") -> dict:
    """Run the safe, conservative auto-heal pass.

    It does not mutate user data or arbitrary WhatsApp messages. It only heats:
    - restart the dispatcher if dead
    - clear stale progress messages
    - requeue stale running tasks
    """
    result = {"ok": True, "actions": []}
    disp = restart_dispatcher()
    result["actions"].append(disp)

    stale = clear_stale_progress()
    result["actions"].append(stale)

    rq = requeue_stale_tasks()
    result["actions"].append(rq)

    event_log.append("autofix", "safe_auto_heal_run", summary=f"safe auto-heal triggered: {reason}", payload={"actions": result["actions"]})
    return result


def inspect_task_health() -> dict:
    """Return a simple health report of tasks with stale/inflight issues."""
    now = time.time()
    report = {"stale_running": [], "stale_progress": []}
    for task in task_store.list(status="running", limit=200):
        if _task_is_stale(task, now=now, stale_seconds=1800):
            report["stale_running"].append(task["id"])
        prog = task.get("progress") or {}
        if prog.get("message_id") and _task_is_stale(task, now=now, stale_seconds=1800):
            report["stale_progress"].append({"task_id": task["id"], "message_id": prog.get("message_id")})
    return report
