"""
services/queue.py
=================
Redis-backed job queue using RQ (Redis Queue).
Replaces in-memory ThreadPoolExecutor with persistent, scalable job processing.
"""

from __future__ import annotations

import os
import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import redis
from rq import Queue, Worker, job
from rq.job import JobStatus
from rq.registry import StartedJobRegistry, FinishedJobRegistry, FailedJobRegistry

from core.config import cfg, log


# ─────────────────────────────────────────────────────────────────────────────
# REDIS CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

_redis_client: redis.Redis | None = None
_queues: Dict[str, Queue] = {}


def get_redis() -> redis.Redis:
    """Get or create Redis connection."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.environ.get("REDIS_URL") or cfg("redis_url") or "redis://localhost:6379/0"
        _redis_client = redis.from_url(redis_url, decode_responses=True)
        # Test connection
        _redis_client.ping()
        log.info("[Queue] Connected to Redis at %s", redis_url)
    return _redis_client


def get_queue(name: str = "default") -> Queue:
    """Get or create a named queue."""
    if name not in _queues:
        _queues[name] = Queue(name, connection=get_redis())
    return _queues[name]


# ─────────────────────────────────────────────────────────────────────────────
# JOB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_job(
    func: Callable,
    *args,
    queue_name: str = "default",
    timeout: int = 300,
    job_id: str | None = None,
    meta: Dict | None = None,
    **kwargs
) -> str:
    """
    Enqueue a job for background execution.
    
    Returns:
        Job ID (string)
    """
    queue = get_queue(queue_name)
    
    job_id = job_id or str(uuid.uuid4())
    job = queue.enqueue_call(
        func=func,
        args=args,
        kwargs=kwargs,
        timeout=timeout,
        job_id=job_id,
        meta=meta or {},
        on_failure=_job_failed,
        on_success=_job_succeeded,
    )
    log.info("[Queue] Enqueued job %s in %s queue", job.id, queue_name)
    return job.id


def get_job_status(job_id: str, queue_name: str = "default") -> Dict[str, Any]:
    """Get job status and result."""
    queue = get_queue(queue_name)
    job = queue.fetch_job(job_id)
    if not job:
        return {"status": "not_found", "job_id": job_id}
    
    status = job.get_status()
    result = {
        "job_id": job.id,
        "status": status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "ended_at": job.ended_at,
    }
    
    if status == JobStatus.FINISHED:
        result["result"] = job.result
    elif status == JobStatus.FAILED:
        result["error"] = str(job.exc_info) if job.exc_info else "Unknown error"
    
    return result


def get_job_result(job_id: str, queue_name: str = "default", timeout: int = 0) -> Any:
    """Get job result, optionally waiting for completion."""
    queue = get_queue(queue_name)
    job = queue.fetch_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    
    if timeout > 0:
        start = time.time()
        while job.get_status() not in (JobStatus.FINISHED, JobStatus.FAILED):
            if time.time() - start > timeout:
                raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")
            time.sleep(0.5)
        job.refresh()
    
    if job.get_status() == JobStatus.FINISHED:
        return job.result
    elif job.get_status() == JobStatus.FAILED:
        raise RuntimeError(f"Job failed: {job.exc_info}")
    raise RuntimeError(f"Job not finished: {job.get_status()}")


def cancel_job(job_id: str, queue_name: str = "default") -> bool:
    """Cancel a pending/running job."""
    queue = get_queue(queue_name)
    job = queue.fetch_job(job_id)
    if not job:
        return False
    try:
        job.cancel()
        return True
    except Exception:
        return False


def requeue_job(job_id: str, queue_name: str = "default") -> str | None:
    """Requeue a failed job."""
    queue = get_queue(queue_name)
    job = queue.fetch_job(job_id)
    if not job or job.get_status() != "failed":
        return None
    new_job = job.requeue()
    return new_job.id if new_job else None


# ─────────────────────────────────────────────────────────────────────────────
# QUEUE MONITORING
# ─────────────────────────────────────────────────────────────────────────────

def get_queue_stats(queue_name: str = "default") -> Dict[str, Any]:
    """Get queue statistics."""
    queue = get_queue(queue_name)
    redis = get_redis()
    
    started = StartedJobRegistry(queue.name, connection=get_redis())
    finished = FinishedJobRegistry(queue.name, connection=get_redis())
    failed = FailedJobRegistry(queue.name, connection=get_redis())
    
    return {
        "name": queue.name,
        "pending": len(queue),
        "started": len(started),
        "finished": len(finished),
        "failed": len(failed),
        "workers": _count_workers(),
    }


def _count_workers() -> int:
    """Count active RQ workers."""
    try:
        redis = get_redis()
        worker_keys = redis.keys("rq:worker:*")
        return len(worker_keys)
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

def _job_succeeded(job: Job, *args, **kwargs):
    log.info("[Queue] Job %s succeeded in %.2fs", job.id, job.ended_at - job.started_at if job.ended_at and job.started_at else 0)


def _job_failed(job: Job, *args, **kwargs):
    log.error("[Queue] Job %s failed: %s", job.id, job.exc_info)


# ─────────────────────────────────────────────────────────────────────────────
# HIGH-LEVEL HELPERS FOR COMMON PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_download(job_id: str, url: str, media_type: str, owner_jid: str, owner_user_id: str):
    """Enqueue a media download job."""
    from services.media import download_youtube_task
    return enqueue_job(
        download_youtube_task,
        url=url,
        media_type=media_type,
        owner_jid=owner_jid,
        owner_user_id=owner_user_id,
        task_id=job_id,
        queue_name="media",
        timeout=600,  # 10 min for downloads
        meta={"url": url, "media_type": media_type},
    )


def enqueue_deep_research(topic: str, export_doc: bool = False, format: str = "pdf") -> str:
    """Enqueue a deep research task."""
    from services.deep_research import run_deep_research_task
    return enqueue_job(
        run_deep_research_task,
        topic=topic,
        export_doc=export_doc,
        format=format,
        queue_name="research",
        timeout=3600,  # 1 hour max
        meta={"topic": topic, "export_doc": export_doc},
    )


def enqueue_media_task(tool_name: str, query: str, media_type: str, user_id: str, sender_jid: str) -> str:
    """Enqueue a media search/download task."""
    from services.tools import _enqueue_download_task
    from services.media import MediaService
    
    media_svc = MediaService()
    return _enqueue_download_task(
        tool_name, query, media_type, 
        user_id, sender_jid, media_svc, None, None
    )


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULED JOBS (CRON-like)
# ─────────────────────────────────────────────────────────────────────────────

from rq_scheduler import Scheduler

_scheduler: "Scheduler | None" = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(connection=get_redis())
    return _scheduler


def schedule_recurring(
    func: Callable,
    interval_seconds: int,
    *args,
    queue_name: str = "default",
    **kwargs
) -> str:
    """Schedule a recurring job (like cron)."""
    scheduler = get_scheduler()
    job = scheduler.schedule(
        scheduled_time=time.time() + 1,  # start in 1 second
        func=func,
        args=args,
        kwargs=kwargs,
        interval=interval_seconds,
        queue_name=queue_name,
        repeat=None,  # repeat forever
    )
    return job.id


def schedule_cron(
    func: Callable,
    cron_string: str,
    *args,
    queue_name: str = "default",
    **kwargs
) -> str:
    """Schedule a cron-like job (e.g., '0 2 * * *' for 2am daily)."""
    scheduler = get_scheduler()
    job = scheduler.cron(
        cron_string=cron_string,
        func=func,
        args=args,
        kwargs=kwargs,
        queue_name=queue_name,
    )
    return job.id


def cancel_scheduled(job_id: str) -> bool:
    """Cancel a scheduled job."""
    scheduler = get_scheduler()
    try:
        scheduler.cancel(job_id)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────────────

def init_queues() -> Dict[str, Queue]:
    """Initialize all standard queues."""
    queues = {}
    for name in ["default", "media", "research", "trading", "maintenance"]:
        queues[name] = get_queue(name)
    log.info("[Queue] Initialized queues: %s", list(queues.keys()))
    return queues


def start_worker(queue_names: List[str] = None, burst: bool = False):
    """Start an RQ worker (call from separate process)."""
    if queue_names is None:
        queue_names = ["default", "media", "research", "trading", "maintenance"]
    
    redis = get_redis()
    queues = [Queue(name, connection=get_redis()) for name in queue_names]
    worker = Worker(queues, connection=get_redis())
    log.info("[Queue] Starting worker for queues: %s", queue_names)
    worker.work(burst=burst)


# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────────────

def init_queue_system():
    """Initialize queue system (call at startup)."""
    get_redis()  # test connection
    init_queues()
    log.info("[Queue] Queue system initialized")


# Auto-init on import
try:
    init_queue_system()
except Exception as e:
    log.warning("[Queue] Auto-init failed (Redis not available?): %s", e)