"""
services/tasks.py
=================
Task storage & state machine for the self-awareness layer.

A Task is a unit of deferred or recurring work that the dispatcher will pick up
on a schedule, run with retry+backoff, and emit events for. Tasks persist to
`~/.crimson/tasks.json` so they survive bot restarts.

Status transitions:
  pending  → running   → done  | failed
  pending  → cancelled                    (any time before running)
  failed   → pending                      (retry bumps `attempts`)
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from core.config import TASKS_FILE, cfg, log, save_json, load_json

VALID_STATUSES = ("pending", "running", "done", "failed", "cancelled")
VALID_KINDS = ("one_shot", "recurring", "background")


class TaskStore:
    def __init__(self, path: str = TASKS_FILE) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────
    def _load(self) -> None:
        raw = load_json(self.path, {})
        # Normalise: accept either {id: task} or [task]
        if isinstance(raw, list):
            raw = {t.get("id"): t for t in raw if t.get("id")}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                v.setdefault("id", k)
                self._tasks[str(k)] = v

    def _save_locked(self) -> None:
        save_json(self.path, self._tasks)

    # ── CRUD ──────────────────────────────────────────────────────────────────
    def create(
        self,
        *,
        kind: str,
        name: str,
        action: dict[str, Any],
        owner_user_id: str | None = None,
        owner_jid: str | None = None,
        schedule: dict[str, Any] | None = None,
        notify_on: str = "done",
        max_attempts: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid task kind: {kind}")
        if notify_on not in ("none", "done", "failed", "always"):
            notify_on = "done"

        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        max_attempts = max_attempts or int(cfg("task_default_max_attempts") or 3)

        next_run_at = (schedule or {}).get("next_run_at") or now
        task: dict[str, Any] = {
            "id": task_id,
            "kind": kind,
            "name": name,
            "owner_user_id": owner_user_id or "",
            "owner_jid": owner_jid or "",
            "status": "pending",
            "schedule": schedule or {"type": "at", "next_run_at": next_run_at},
            "action": action,                          # {module, fn, args}
            "result": None,
            "error": None,
            "attempts": 0,
            "max_attempts": max_attempts,
            "progress": {"pct": 0, "message": ""},
            "notify_on": notify_on,
            "metadata": metadata or {},
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "last_event_seq": 0,
        }
        with self._lock:
            self._tasks[task_id] = task
            self._save_locked()
        return task

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            t = dict(self._tasks.get(task_id)) if task_id in self._tasks else None
        return t

    def list(
        self,
        owner_user_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        with self._lock:
            data = list(self._tasks.values())
        out: list[dict] = []
        for t in data:
            if owner_user_id and t.get("owner_user_id") != owner_user_id:
                continue
            if status and t.get("status") != status:
                continue
            if kind and t.get("kind") != kind:
                continue
            out.append(t)
        out.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
        return out[:limit]

    def update(self, task_id: str, *, expected_status: str | None = None, **changes) -> dict | None:
        """Update a task with an optional optimistic status-transition guard."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            if expected_status and task.get("status") != expected_status:
                log.debug("[TaskStore] update on %s skipped: status=%s expected=%s",
                          task_id, task.get("status"), expected_status)
                return task
            for k, v in changes.items():
                if k == "schedule" and isinstance(v, dict):
                    # Merge schedule dict
                    merged = dict(task.get("schedule") or {})
                    merged.update(v)
                    task["schedule"] = merged
                elif k == "progress" and isinstance(v, dict):
                    merged = dict(task.get("progress") or {})
                    merged.update(v)
                    task["progress"] = merged
                else:
                    task[k] = v
            self._save_locked()
            return dict(task)

    def cancel(self, task_id: str) -> dict | None:
        return self.update(task_id, expected_status="pending", status="cancelled",
                           finished_at=time.time())

    def done(self, task_id: str, result: Any) -> dict | None:
        return self.update(task_id, expected_status="running", status="done",
                           result=result, finished_at=time.time(),
                           progress={"pct": 100, "message": "done"})

    def fail(self, task_id: str, error: str) -> dict | None:
        return self.update(task_id, expected_status="running", status="failed",
                           error=error, finished_at=time.time(),
                           progress={"pct": 100, "message": "failed"})

    def requeue_for_retry(self, task_id: str, *, backoff_secs: float,
                          error: str) -> dict | None:
        return self.update(
            task_id,
            expected_status="running",
            status="pending",
            error=error,
            attempts=int(self.get(task_id).get("attempts") or 0) + 1,
            schedule={
                "next_run_at": time.time() + backoff_secs,
                "last_backoff_secs": backoff_secs,
            },
        )

    def due(self, now: float | None = None, *, limit: int = 25) -> list[dict]:
        now = now or time.time()
        with self._lock:
            data = list(self._tasks.values())
        out: list[dict] = []
        for t in data:
            if t.get("status") != "pending":
                continue
            sched = t.get("schedule") or {}
            run_at = sched.get("next_run_at") or sched.get("run_at")
            if run_at is None:
                continue
            if run_at <= now:
                out.append(t)
        out.sort(key=lambda t: (t.get("schedule") or {}).get("next_run_at") or 0)
        return out[:limit]

    def stats(self) -> dict:
        with self._lock:
            counts = {s: 0 for s in VALID_STATUSES}
            for t in self._tasks.values():
                counts[t.get("status") or "pending"] = counts.get(t.get("status") or "pending", 0) + 1
            total = len(self._tasks)
        return {"total": total, **counts}


# ── Module singleton ──────────────────────────────────────────────────────────
task_store = TaskStore()


# ── Built-in actions callable by the dispatcher ───────────────────────────────
def run_user_reminder(*, owner_jid: str = "", owner_user_id: str = "",
                      what: str = "", task_name: str = "reminder",
                      progress=None) -> dict:
    """Notify the user that a scheduled reminder fired.

    All keyword args are optional with safe defaults so that legacy/stale
    tasks persisted before a schema change won't crash the dispatcher
    (they'll just produce a degraded reminder instead of a retry storm).
    """
    if not owner_jid:
        # No recipient on file — fail loudly so the task is marked failed
        # and won't be requeued infinitely.
        raise RuntimeError("reminder has no owner_jid")

    msg = f"⏰ reminder: {task_name}\n{what}".strip() if what else f"⏰ reminder: {task_name}"
    import services.bridge_api as bridge_api
    r = bridge_api.bridge_send(owner_jid, msg)
    if r.get("ok"):
        return {"delivered": True, "message_id": r.get("message_id")}
    # Bridge down — log only, don't fail the task
    from core.eventlog import event_log
    event_log.append("task", "reminder_bridge_unreachable",
                     summary=f"could not deliver reminder: {r.get('error')}",
                     user_id=owner_user_id or None, jid=owner_jid,
                     payload={"task_name": task_name, "error": r.get("error")})
    return {"delivered": False, "error": r.get("error")}
