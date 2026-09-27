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

BRIDGE_BASE = os.environ.get("BRIDGE_BASE_URL", "http://127.0.0.1:7860")


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


def bridge_list_groups(*, timeout: int = 10) -> dict[str, Any]:
    """Fetch list of all participating groups, admin status, and member counts."""
    return _post("/list_groups", {}, timeout=timeout)


def bridge_group_setting(jids: list[str] | str, setting: str, *, timeout: int = 10) -> dict[str, Any]:
    """
    Update group setting ('announcement' | 'not_announcement' | 'locked' | 'unlocked').
    'announcement' locks message sending to admins only.
    """
    target_jids = [jids] if isinstance(jids, str) else jids
    return _post("/group_setting", {"jids": target_jids, "setting": setting}, timeout=timeout)


def bridge_get_group_admins(group_jid: str, *, timeout: int = 10) -> dict[str, Any]:
    """Fetch admin list for a group."""
    return _post("/group_admins", {"jid": group_jid}, timeout=timeout)


def bridge_post_status(text: str, media_base64: str | None = None, mimetype: str | None = None, *, timeout: int = 10) -> dict[str, Any]:
    """Post a WhatsApp status update."""
    return _post("/post_status", {"text": text, "media_base64": media_base64, "mimetype": mimetype}, timeout=timeout)


def bridge_send_reaction(jid: str, message_id: str, emoji: str, *, timeout: int = 6) -> dict[str, Any]:
    """Send an emoji reaction to a message."""
    return _post("/send_reaction", {"jid": jid, "message_id": message_id, "emoji": emoji}, timeout=timeout)


def bridge_send_document(jid: str, document_path: str, caption: str = "", filename: str = "", *, timeout: int = 30) -> dict[str, Any]:
    """Send a document or PDF file."""
    return bridge_send(jid, caption, media_path=document_path, media_type="document", filename=filename, timeout=timeout)
