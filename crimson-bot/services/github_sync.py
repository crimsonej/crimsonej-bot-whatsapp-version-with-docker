"""
services/github_sync.py
========================
GitHub Backup Sync Service.
On startup: pulls data from configured backup repo.
Periodically: pushes local SQLite data to backup repo.
"""

from __future__ import annotations

import json
import base64
import os
import sqlite3
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.config import cfg, log
from services.github_search import (
    github_get_file,
    github_list_files,
    github_upsert_file,
    _github_api_request,
)
from services.storage import (
    DATA_DIR,
    session_get,
    session_get_all_active,
    profile_get_all,
    vector_get_all,
    vault_get,
    profile_get,
    vault_append,
    vector_add,
    session_save,
    profile_update,
)

# Backup file paths in the repo
BACKUP_PATHS = {
    "profiles": "backup/profiles.json",
    "sessions": "backup/sessions.json",
    "vaults": "backup/vaults.json",
    "vectors": "backup/vectors.json",
    "meta": "backup/meta.json",  # timestamp, version info
}

# In-memory state
_sync_thread: Optional[threading.Thread] = None
_sync_stop = threading.Event()
_last_pull_time: float = 0
_last_push_time: float = 0
_last_push_stats: Optional[Dict[str, int]] = None


def get_backup_repo() -> str:
    """Get backup repo from config/env."""
    repo = (cfg("github_backup_repo") or os.getenv("GITHUB_BACKUP_REPO") or "").strip()
    if not repo:
        return ""
    if repo.startswith("git@github.com:"):
        repo = repo.split(":", 1)[1]
    elif "://" in repo:
        parsed = urlparse(repo)
        if parsed.hostname == "github.com":
            repo = parsed.path
    repo = repo.removeprefix("github.com/").strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


def is_sync_enabled() -> bool:
    """Check if GitHub sync is configured."""
    repo = get_backup_repo()
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    return bool(repo and token)


def _read_json_file(repo: str, path: str) -> Optional[Dict[str, Any]]:
    """Read and parse a JSON file from GitHub."""
    result = github_get_file(repo, path)
    if not result.get("ok"):
        return None
    try:
        return json.loads(result.get("content", "{}"))
    except json.JSONDecodeError:
        log.warning("[GitHubSync] Failed to parse JSON from %s/%s", repo, path)
        return None


def _write_json_file(repo: str, path: str, data: Dict[str, Any], message: str) -> bool:
    """Write JSON data to GitHub file."""
    content = json.dumps(data, indent=2, ensure_ascii=False)
    result = github_upsert_file(repo, path, content, message)
    return result.get("ok", False)


def _write_database_snapshot(repo: str) -> bool:
    """Upload a consistent SQLite snapshot to the private backup repository."""
    from services.storage import DB_PATH

    if not os.path.isfile(DB_PATH):
        return False
    fd, snapshot_path = tempfile.mkstemp(prefix="crimson-backup-", suffix=".db")
    os.close(fd)
    try:
        source = sqlite3.connect(DB_PATH, timeout=30)
        snapshot = sqlite3.connect(snapshot_path)
        try:
            source.backup(snapshot)
        finally:
            snapshot.close()
            source.close()
        size = os.path.getsize(snapshot_path)
        if not size or size > 80 * 1024 * 1024:
            log.error("[GitHubSync] SQLite snapshot size is not supported by Contents API: %d", size)
            return False
        with open(snapshot_path, "rb") as database_file:
            encoded = base64.b64encode(database_file.read()).decode("ascii")

        path = "backup/crimson.db"
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        existing = _github_api_request("GET", url)
        payload: Dict[str, str] = {
            "message": f"Backup SQLite database ({size} bytes) - {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "content": encoded,
        }
        if existing.get("ok"):
            payload["sha"] = existing["data"].get("sha", "")
        elif "404" not in existing.get("error", "") and "Not Found" not in existing.get("error", ""):
            log.warning("[GitHubSync] Could not read existing SQLite snapshot: %s", existing.get("error"))
            return False
        result = _github_api_request("PUT", url, json=payload)
        if not result.get("ok"):
            log.warning("[GitHubSync] SQLite snapshot upload failed: %s", result.get("error"))
            return False
        log.info("[GitHubSync] SQLite snapshot uploaded (%d bytes)", size)
        return True
    except Exception as exc:
        log.warning("[GitHubSync] SQLite snapshot failed: %s", exc)
        return False
    finally:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass


def pull_from_github() -> Dict[str, int]:
    """
    Pull data from GitHub backup repo and merge into local SQLite.
    Returns stats of what was synced.
    """
    repo = get_backup_repo()
    if not repo:
        return {"error": "No backup repo configured"}

    stats = {"profiles": 0, "sessions": 0, "vaults": 0, "vectors": 0, "errors": 0}

    # Pull profiles
    profiles_data = _read_json_file(repo, BACKUP_PATHS["profiles"])
    if profiles_data:
        for user_id, profile in profiles_data.items():
            if not isinstance(profile, dict):
                continue
            # Only update if GitHub version is newer or local doesn't exist
            local = profile_get(user_id)
            gh_updated = profile.get("_synced_at", 0)
            local_updated = local.get("_synced_at", 0) if isinstance(local, dict) else 0
            if gh_updated > local_updated:
                # Merge facts/interests (union)
                existing_facts = set(local.get("facts", []))
                existing_interests = set(local.get("interests", []))
                gh_facts = set(profile.get("facts", []))
                gh_interests = set(profile.get("interests", []))
                merged_facts = list(existing_facts | gh_facts)[-50:]
                merged_interests = list(existing_interests | gh_interests)[-20:]

                profile_update(user_id, **{
                    "name": profile.get("name") or local.get("name"),
                    "nicknames": profile.get("nicknames", []),
                    "facts": merged_facts,
                    "interests": merged_interests,
                    "relationship": profile.get("relationship") or local.get("relationship"),
                    "preferences": {**local.get("preferences", {}), **profile.get("preferences", {})},
                    "interaction_count": max(local.get("interaction_count", 0), profile.get("interaction_count", 0)),
                    "last_seen": profile.get("last_seen") or local.get("last_seen"),
                    "first_seen": profile.get("first_seen") or local.get("first_seen"),
                    "is_creator": int(profile.get("is_creator", 0) or local.get("is_creator", 0)),
                    "ignore_status": int(profile.get("ignore_status", 0) or local.get("ignore_status", 0)),
                })
                stats["profiles"] += 1

    # Pull sessions
    sessions_data = _read_json_file(repo, BACKUP_PATHS["sessions"])
    if sessions_data:
        for sender, session in sessions_data.items():
            if not isinstance(session, dict):
                continue
            turns = session.get("turns", [])
            last_active = session.get("last_active", time.time())
            # Only restore if we don't have a newer local session
            local = session_get(sender)
            if local and local.get("last_active", 0) > last_active:
                continue
            session_save(sender, turns, last_active)
            stats["sessions"] += 1

    # Pull vaults
    vaults_data = _read_json_file(repo, BACKUP_PATHS["vaults"])
    if vaults_data:
        for vault_key, content in vaults_data.items():
            if not isinstance(content, str):
                continue
            local_content = vault_get(vault_key)
            # Simple merge: append GitHub content if not already present
            if local_content and content not in local_content:
                vault_append(vault_key, content)
            elif not local_content:
                # First time - append with note
                vault_append(vault_key, content)
            stats["vaults"] += 1

    # Pull vectors
    vectors_data = _read_json_file(repo, BACKUP_PATHS["vectors"])
    if vectors_data and isinstance(vectors_data, list):
        existing = vector_get_all()
        existing_texts = {v["text"] for v in existing}
        for chunk in vectors_data:
            if not isinstance(chunk, dict):
                continue
            text = chunk.get("text", "")
            if text and text not in existing_texts:
                vector_add(
                    text,
                    owner=chunk.get("owner", ""),
                    group_jid=chunk.get("group", ""),
                    source=chunk.get("source", "github_sync"),
                )
                stats["vectors"] += 1
                existing_texts.add(text)

    global _last_pull_time
    _last_pull_time = time.time()
    log.info("[GitHubSync] Pull complete: %s", stats)
    return stats


def push_to_github() -> Dict[str, int]:
    """
    Push local SQLite data to GitHub backup repo.
    Returns stats of what was pushed.
    """
    repo = get_backup_repo()
    if not repo:
        return {"error": "No backup repo configured"}

    stats = {"profiles": 0, "sessions": 0, "vaults": 0, "vectors": 0, "errors": 0}

    # Push profiles
    profiles_data = profile_get_all()
    for profile in profiles_data.values():
        profile["_synced_at"] = time.time()
    if profiles_data:
        ok = _write_json_file(
            repo, BACKUP_PATHS["profiles"], profiles_data,
            f"Backup profiles ({len(profiles_data)} users) - {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if ok:
            stats["profiles"] = len(profiles_data)
        else:
            stats["errors"] += 1

    # Push sessions (only active ones)
    sessions_data = {}
    ttl = int(cfg("session_ttl") or 7200)
    active_sessions = session_get_all_active(ttl)
    for sender, session in active_sessions:
        sessions_data[sender] = {
            "turns": session.get("turns", []),
            "last_active": session.get("last_active", time.time()),
            "_synced_at": time.time(),
        }
    if sessions_data:
        ok = _write_json_file(
            repo, BACKUP_PATHS["sessions"], sessions_data,
            f"Backup sessions ({len(sessions_data)} active) - {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if ok:
            stats["sessions"] = len(sessions_data)
        else:
            stats["errors"] += 1

    # Push vaults
    vaults_data = {}
    # Personal vaults
    for user_id in profiles_data:
        content = vault_get(f"user:{user_id}")
        if content:
            vaults_data[f"user:{user_id}"] = content
    # Global vault
    global_content = vault_get("global")
    if global_content:
        vaults_data["global"] = global_content
    if vaults_data:
        ok = _write_json_file(
            repo, BACKUP_PATHS["vaults"], vaults_data,
            f"Backup vaults ({len(vaults_data)} vaults) - {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if ok:
            stats["vaults"] = len(vaults_data)
        else:
            stats["errors"] += 1

    # Push vectors (limit to recent 5000 to keep repo size manageable)
    vectors = vector_get_all()
    vectors_data = []
    for v in vectors[-5000:]:
        vectors_data.append({
            "text": v.get("text", ""),
            "owner": v.get("owner", ""),
            "group": v.get("group_jid", ""),
            "source": v.get("source", ""),
            "created_at": v.get("created_at", time.time()),
        })
    if vectors_data:
        ok = _write_json_file(
            repo, BACKUP_PATHS["vectors"], vectors_data,
            f"Backup vectors ({len(vectors_data)} chunks) - {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if ok:
            stats["vectors"] = len(vectors_data)
        else:
            stats["errors"] += 1

    if _write_database_snapshot(repo):
        stats["database"] = 1
    else:
        stats["errors"] += 1

    # Push meta
    meta = {
        "last_push": time.time(),
        "last_pull": _last_pull_time,
        "version": 1,
        "stats": stats,
    }
    if not _write_json_file(repo, BACKUP_PATHS["meta"], meta, "Update sync metadata"):
        stats["errors"] += 1

    global _last_push_time, _last_push_stats
    _last_push_time = time.time()
    _last_push_stats = stats.copy()
    log.info("[GitHubSync] Push complete: %s", stats)
    return stats


def sync_now() -> Dict[str, Any]:
    """Run a full pull + push cycle."""
    if not is_sync_enabled():
        return {"ok": False, "error": "GitHub sync not configured (need GITHUB_TOKEN and GITHUB_BACKUP_REPO)"}

    log.info("[GitHubSync] Starting sync cycle...")
    pull_stats = pull_from_github()
    push_stats = push_to_github()
    return {"ok": True, "pull": pull_stats, "push": push_stats}


def _sync_loop():
    """Background loop for periodic pushes."""
    interval = int(cfg("github_sync_interval_sec") or 3600)  # default 1 hour
    log.info("[GitHubSync] Background sync started, interval=%ds", interval)

    # Restore existing state and establish a fresh backup on startup.
    if is_sync_enabled():
        sync_now()

    while not _sync_stop.is_set():
        _sync_stop.wait(timeout=interval)
        if _sync_stop.is_set():
            break
        if is_sync_enabled():
            push_to_github()


def start_github_sync() -> None:
    """Start the background sync thread."""
    global _sync_thread
    if _sync_thread and _sync_thread.is_alive():
        log.warning("[GitHubSync] Already running")
        return

    if not is_sync_enabled():
        log.info("[GitHubSync] Not enabled (missing GITHUB_TOKEN or GITHUB_BACKUP_REPO)")
        return

    _sync_stop.clear()
    _sync_thread = threading.Thread(target=_sync_loop, name="GitHubSync", daemon=True)
    _sync_thread.start()


def stop_github_sync() -> None:
    """Stop the background sync thread."""
    _sync_stop.set()
    if _sync_thread:
        _sync_thread.join(timeout=5)


def get_sync_status() -> Dict[str, Any]:
    """Get current sync status."""
    return {
        "enabled": is_sync_enabled(),
        "repo": get_backup_repo() if is_sync_enabled() else None,
        "running": _sync_thread is not None and _sync_thread.is_alive(),
        "interval_seconds": int(cfg("github_sync_interval_sec") or 3600),
        "data_dir_mounted": os.path.ismount(DATA_DIR),
        "last_pull": _last_pull_time,
        "last_push": _last_push_time,
        "last_push_stats": _last_push_stats.copy() if _last_push_stats else None,
    }