"""
services/bridge_api.py
======================
Thin HTTP wrapper around the WhatsApp bridge's outbound endpoints.

The bridge exposes three POST endpoints we care about:
  /send_message   {jid, text}        → {ok, message_id, message_key, ts}
  /edit_message   {jid, message_id,
                   new_text}         → {ok, error?}
  /delete_message {jid, message_id}  → {ok, error?}

This module is the single place the bot uses to talk to the bridge for
outbound WhatsApp messaging.  Every other service (reporter, tasks,
progress) should call these helpers instead of issuing raw requests.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from core.config import log

BRIDGE_BASE = os.environ.get("BRIDGE_BASE_URL", "http://127.0.0.1:7860")


def _post(path: str, payload: dict, *, timeout: int) -> dict:
    try:
        r = requests.post(f"{BRIDGE_BASE}{path}", json=payload, timeout=timeout)
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
    """Send a WhatsApp text message. Returns {ok, message_id, message_key, ts}
    on success or {ok:False, error} on failure.

    Optional `media_path` (local file, shared filesystem with the bridge)
    delivers the file as an audio/video/message instead of text."""
    # Use longer timeout for media uploads (up to 5 min for large files)
    if media_path:
        timeout = max(timeout, 300)
    return _post("/send_message", {"jid": jid, "text": text,
                                    "path": media_path, "media_type": media_type,
                                    "filename": filename}, timeout=timeout)


def bridge_edit(jid: str, message_id: str, new_text: str, *, timeout: int = 6) -> dict[str, Any]:
    """Edit an existing message in place. Requires `message_id` returned from
    a prior bridge_send()."""
    return _post("/edit_message", {"jid": jid, "message_id": message_id,
                                    "new_text": new_text}, timeout=timeout)


def bridge_delete(jid: str, message_id: str, *, timeout: int = 6) -> dict[str, Any]:
    """Delete a message by id. Idempotent within WhatsApp's 24h window."""
    return _post("/delete_message", {"jid": jid, "message_id": message_id}, timeout=timeout)


def bridge_get_group_admins(group_jid: str, *, timeout: int = 10) -> dict[str, Any]:
    """
    Fetch group admin list from bridge.
    Returns {ok: True, admins: [jid1, jid2, ...]} or {ok: False, error}.
    
    Note: Requires bridge support for /group_admins endpoint.
    """
    try:
        r = requests.post(f"{BRIDGE_BASE}/group_admins", json={"jid": group_jid}, timeout=timeout)
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
        else:
            data = {"ok": r.status_code == 200, "raw": r.text}
        return data if isinstance(data, dict) else {"ok": False, "error": "bad_payload"}
    except requests.exceptions.Timeout:
        log.warning("[bridge_api] group_admins timed out after %ss", timeout)
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        log.warning("[bridge_api] group_admins failed: %s", exc)
        return {"ok": False, "error": str(exc)}
