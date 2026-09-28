"""
services/bridge_api.py
======================
HTTP wrapper around the WhatsApp bridge's outbound endpoints.
Provides standard methods for sending, editing, deleting, pinning, forwarding,
group management, status posting, and group listing.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from core.config import log

BRIDGE_BASE = os.environ.get("BRIDGE_BASE_URL") or (
    f"http://127.0.0.1:{os.environ.get('BRIDGE_PORT') or os.environ.get('PORT', '7860')}"
)
BRIDGE_BASE = BRIDGE_BASE.rstrip("/")


def _post(path: str, payload: dict, *, timeout: int) -> dict:
    try:
        r = requests.post(f"{BRIDGE_BASE}{path}", json=payload, headers={}, timeout=timeout)
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
        else:
            data = {"ok": r.status_code == 200, "raw": r.text}
        return data if isinstance(data, dict) else {"ok": False, "error": "bad_payload"}
    except requests.exceptions.Timeout:
        log.warning("[bridge_api] %s timed out after %ss", path, timeout)
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        log.warning("[bridge_api] %s failed: %s", path, exc)
        return {"ok": False, "error": str(exc)}


def bridge_send(jid: str, text: str, *, timeout: int = 8, media_path: str = "",
                media_type: str = "audio", filename: str = "") -> dict[str, Any]:
    """Send a WhatsApp text/media message. Returns {ok, message_id, message_key, ts}."""
    if media_path:
        timeout = max(timeout, 300)
    return _post("/send_message", {
        "jid": jid,
        "text": text,
        "path": media_path,
        "media_type": media_type,
        "filename": filename
    }, timeout=timeout)


def bridge_edit(jid: str, message_id: str, new_text: str, *, timeout: int = 6) -> dict[str, Any]:
    """Edit an existing bot message in place."""
    return _post("/edit_message", {"jid": jid, "message_id": message_id, "new_text": new_text}, timeout=timeout)


def bridge_delete(jid: str, message_id: str, *, from_me: bool = True, participant: str | None = None, timeout: int = 6) -> dict[str, Any]:
    """Delete a message by id (bot's own message or user message if bot is admin)."""
    payload = {"jid": jid, "message_id": message_id, "from_me": from_me}
    if participant:
        payload["participant"] = participant
    return _post("/delete_message", payload, timeout=timeout)


def bridge_delete_message(jid: str, message_id: str, *, timeout: int = 6) -> dict[str, Any]:
    """Compatibility wrapper used by tool dispatch and moderation."""
    return bridge_delete(jid, message_id, timeout=timeout)


def bridge_pin(jid: str, message_id: str, *, pin: bool = True, from_me: bool = True, participant: str | None = None, duration_sec: int = 2592000, timeout: int = 6) -> dict[str, Any]:
    """Pin or unpin a message in chat."""
    payload = {"jid": jid, "message_id": message_id, "pin": pin, "from_me": from_me, "duration_sec": duration_sec}
    if participant:
        payload["participant"] = participant
    return _post("/pin_message", payload, timeout=timeout)


def bridge_forward(target_jid: str, message_id: str, from_jid: str, *, from_me: bool = False, participant: str | None = None, timeout: int = 10) -> dict[str, Any]:
    """Forward a message to another chat."""
    payload = {"target_jid": target_jid, "message_id": message_id, "from_jid": from_jid, "from_me": from_me}
    if participant:
        payload["participant"] = participant
    return _post("/forward_message", payload, timeout=timeout)


def bridge_forward_message(from_jid: str, message_id: str, target_jid: str, *, timeout: int = 10) -> dict[str, Any]:
    """Compatibility wrapper matching the message tool's argument order."""
    return bridge_forward(target_jid, message_id, from_jid, timeout=timeout)


def bridge_pin_message(jid: str, message_id: str, *, timeout: int = 6) -> dict[str, Any]:
    return bridge_pin(jid, message_id, pin=True, timeout=timeout)


def bridge_unpin_message(jid: str, message_id: str = "", *, timeout: int = 6) -> dict[str, Any]:
    return bridge_pin(jid, message_id, pin=False, timeout=timeout)


def resolve_group_jid(group_identifier: str, *, timeout: int = 10) -> str:
    """
    Resolve a group name or partial JID to a full @g.us WhatsApp group JID.
    If already a valid group JID, returns it as-is.
    """
    if not group_identifier:
        return group_identifier
    group_identifier = group_identifier.strip()
    if group_identifier.endswith("@g.us"):
        return group_identifier
    
    # Try fetching group list to match subject
    try:
        res = bridge_list_groups(timeout=timeout)
        groups = res.get("groups", [])
        ident_lower = group_identifier.lower()
        
        # 1. Exact subject match
        for g in groups:
            subject = (g.get("subject") or "").strip().lower()
            if subject == ident_lower:
                return g["jid"]
        
        # 2. Substring / fuzzy match
        for g in groups:
            subject = (g.get("subject") or "").strip().lower()
            if ident_lower in subject or subject in ident_lower:
                return g["jid"]
    except Exception as exc:
        log.warning("[bridge_api] Failed to resolve group name %r: %s", group_identifier, exc)
    
    return group_identifier


def bridge_list_groups(*, timeout: int = 10) -> dict[str, Any]:
    """Fetch list of all participating groups, admin status, and member counts."""
    return _post("/list_groups", {}, timeout=timeout)


def bridge_get_user_groups(*, timeout: int = 10) -> dict[str, Any]:
    """Compatibility alias used by the group-list tool."""
    return bridge_list_groups(timeout=timeout)


def bridge_group_admin_action(group_jid: str, action: str, target_jid: str | None = None, *, timeout: int = 10) -> dict[str, Any]:
    resolved_jid = resolve_group_jid(group_jid, timeout=timeout)
    payload = {"jid": resolved_jid, "action": action}
    if target_jid:
        payload["target"] = target_jid
    return _post("/group_admin_action", payload, timeout=timeout)


def bridge_get_group_participants(group_jid: str, *, timeout: int = 10) -> dict[str, Any]:
    resolved_jid = resolve_group_jid(group_jid, timeout=timeout)
    return _post("/group_participants", {"jid": resolved_jid}, timeout=timeout)


def bridge_set_group_settings(group_jid: str, settings: dict[str, Any] | str, *, timeout: int = 10) -> dict[str, Any]:
    resolved_jid = resolve_group_jid(group_jid, timeout=timeout)
    if isinstance(settings, dict):
        mapping = {
            "announcement": "announcement",
            "announce": "announcement",
            "not_announcement": "not_announcement",
            "unannounce": "not_announcement",
            "locked": "locked",
            "lock": "locked",
            "unlocked": "unlocked",
            "unlock": "unlocked",
        }
        for key, value in settings.items():
            if isinstance(value, bool) and key in {"announcement", "announce"}:
                setting = "announcement" if value else "not_announcement"
            elif isinstance(value, bool) and key in {"locked", "lock"}:
                setting = "locked" if value else "unlocked"
            else:
                setting = mapping.get(str(key).lower())
            if setting:
                return _post("/group_setting", {"jid": resolved_jid, "setting": setting}, timeout=timeout)
        return {"ok": False, "error": "no_valid_group_setting"}
    return _post("/group_setting", {"jid": resolved_jid, "setting": str(settings)}, timeout=timeout)


def bridge_lock_group(group_jid: str, duration_seconds: int = 0, *, timeout: int = 10) -> dict[str, Any]:
    resolved_jid = resolve_group_jid(group_jid, timeout=timeout)
    res = bridge_group_setting(resolved_jid, "announcement", timeout=timeout)
    if not res.get("ok"):
        return res
    if duration_seconds > 0:
        try:
            import time
            from services.storage import get_conn
            revert_at = time.time() + duration_seconds
            conn = get_conn()
            conn.execute("""
                INSERT INTO scheduled_messages (jid, text, media_path, media_type, filename, scheduled_at, status, created_at)
                VALUES (?, 'UNLOCK_GROUP', 'ACTION:UNLOCK_GROUP', 'system_action', '', ?, 'pending', ?)
            """, (resolved_jid, revert_at, time.time()))
            conn.commit()
            res["scheduled_unlock"] = True
            res["duration_seconds"] = duration_seconds
            log.info("[bridge_lock_group] Locked %s for %ds (revert scheduled)", resolved_jid, duration_seconds)
        except Exception as exc:
            log.warning("[bridge_lock_group] Failed to schedule unlock: %s", exc)
    return res


def bridge_unlock_group(group_jid: str, *, timeout: int = 10) -> dict[str, Any]:
    resolved_jid = resolve_group_jid(group_jid, timeout=timeout)
    return bridge_group_setting(resolved_jid, "not_announcement", timeout=timeout)


def bridge_group_setting(jids: list[str] | str, setting: str, *, timeout: int = 10) -> dict[str, Any]:
    """
    Update group setting ('announcement' | 'not_announcement' | 'locked' | 'unlocked').
    'announcement' locks message sending to admins only.
    """
    target_jids = [jids] if isinstance(jids, str) else jids
    resolved_jids = [resolve_group_jid(j, timeout=timeout) for j in target_jids]
    return _post("/group_setting", {"jids": resolved_jids, "setting": setting}, timeout=timeout)


def bridge_get_group_admins(group_jid: str, *, timeout: int = 10) -> dict[str, Any]:
    """Fetch admin list for a group."""
    resolved_jid = resolve_group_jid(group_jid, timeout=timeout)
    return _post("/group_admins", {"jid": resolved_jid}, timeout=timeout)


def bridge_post_status(text: str, media_base64: str | None = None, mimetype: str | None = None, *, timeout: int = 10) -> dict[str, Any]:
    """Post a WhatsApp status update."""
    return _post("/post_status", {"text": text, "media_base64": media_base64, "mimetype": mimetype}, timeout=timeout)


def bridge_send_reaction(jid: str, message_id: str, emoji: str, *, timeout: int = 6) -> dict[str, Any]:
    """Send an emoji reaction to a message."""
    return _post("/send_reaction", {"jid": jid, "message_id": message_id, "emoji": emoji}, timeout=timeout)


def bridge_send_document(jid: str, document_path: str, caption: str = "", filename: str = "", *, timeout: int = 30) -> dict[str, Any]:
    """Send a document or PDF file."""
    return bridge_send(jid, caption, media_path=document_path, media_type="document", filename=filename, timeout=timeout)
