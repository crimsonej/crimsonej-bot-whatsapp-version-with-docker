"""
services/contact_relay.py
=========================
Contact Relay Service.
Allows users to request contact with the creator.
Creator gets notified with user info and can choose to:
- Share their contact (phone/username)
- Send a custom message
- Decline
Bot relays creator's response back to the user.
"""

from __future__ import annotations

import json
import time
from typing import Dict, Any, Optional

from core.config import cfg, log
from services.storage import profile_get, profile_update
from services.tasks import task_store


# Relay request status
RELAY_PENDING = "pending"
RELAY_APPROVED = "approved"
RELAY_DECLINED = "declined"
RELAY_EXPIRED = "expired"


def create_relay_request(
    user_id: str,
    user_jid: str,
    message: str = "",
    user_name: str = ""
) -> Dict[str, Any]:
    """
    Create a contact relay request from a user to the creator.
    """
    creator_jid = (cfg("owner_jid") or "").strip()
    if not creator_jid:
        return {"ok": False, "error": "Creator not configured"}
    
    profile = profile_get(user_id)
    
    request_data = {
        "request_id": f"relay_{int(time.time() * 1000)}",
        "user_id": user_id,
        "user_jid": user_jid,
        "user_name": user_name or profile.get("name") or "someone",
        "user_facts": profile.get("facts", []),
        "user_interests": profile.get("interests", []),
        "interaction_count": profile.get("interaction_count", 0),
        "message": message,
        "status": RELAY_PENDING,
        "created_at": time.time(),
        "expires_at": time.time() + (24 * 3600),
        "creator_response": None,
    }
    
    profile_relay = profile.get("relay_requests", [])
    profile_relay.append(request_data)
    if len(profile_relay) > 10:
        profile_relay = profile_relay[-10:]
    profile_update(user_id, relay_requests=profile_relay)
    
    task_store.create(
        kind="one_shot",
        name="contact_relay_notify",
        action={
            "module": "services.contact_relay",
            "fn": "notify_creator_of_relay",
            "kwargs": {"request_id": request_data["request_id"], "user_id": user_id}
        },
        owner_user_id=user_id,
        owner_jid=creator_jid,
        notify_on="done",
        metadata={"relay_request_id": request_data["request_id"]}
    )
    
    log.info("[ContactRelay] Created relay request %s from %s (%s)", 
             request_data["request_id"], user_id, user_jid)
    
    return {
        "ok": True,
        "request_id": request_data["request_id"],
        "message": "Sent it to my dad. He'll get back to you when he can 🤙"
    }


def notify_creator_of_relay(request_id: str, user_id: str) -> Dict[str, Any]:
    """Notify creator (dad) about a contact request."""
    creator_jid = (cfg("owner_jid") or "").strip()
    if not creator_jid:
        return {"ok": False, "error": "Creator not configured"}
    
    profile = profile_get(user_id)
    relay_requests = profile.get("relay_requests", [])
    request = next((r for r in relay_requests if r.get("request_id") == request_id), None)
    
    if not request:
        return {"ok": False, "error": "Request not found"}
    
    # Natural, casual notification
    msg = (
        f"Yo dad, someone wants to talk to you 📞\n\n"
        f"Name: {request.get('user_name', 'someone')} ({request.get('user_id', 'N/A')})\n"
        f"Chats: {request.get('interaction_count', 0)} times\n"
    )
    
    facts = request.get('user_facts', [])
    if facts:
        msg += f"Knows: {', '.join(facts[-3:])}\n"
    
    interests = request.get('user_interests', [])
    if interests:
        msg += f"Into: {', '.join(interests[-3:])}\n"
    
    if request.get('message'):
        msg += f"\nSays: \"{request['message']}\"\n"
    
    msg += (
        f"\nReply:\n"
        f"• `relay approve {request_id} [your reply]` — send them something\n"
        f"• `relay decline {request_id} [reason]` — pass\n"
        f"• Ignore — expires in 24h"
    )
    
    from services.bridge_api import bridge_send
    result = bridge_send(creator_jid, msg)
    
    return {"ok": result.get("ok", False), "message_id": result.get("message_id")}


def process_creator_response(
    creator_jid: str,
    action: str,
    request_id: str,
    response_text: str = ""
) -> Dict[str, Any]:
    """
    Process creator's response to a relay request.
    
    Args:
        creator_jid: Creator's JID (must match owner_jid)
        action: 'approve' or 'decline'
        request_id: The relay request ID
        response_text: Optional contact info or message to share
    
    Returns:
        Result dict
    """
    expected_jid = (cfg("owner_jid") or "").strip()
    if creator_jid != expected_jid:
        return {"ok": False, "error": "Unauthorized"}
    
    # Find the request across all profiles
    from services.storage import get_conn, profile_update
    import json
    
    conn = get_conn()
    cur = conn.execute("""
        SELECT user_id, name, nicknames, facts, interests, relationship, preferences, relay_requests,
               interaction_count, last_seen, first_seen, is_creator, ignore_status 
        FROM profiles 
        WHERE relay_requests LIKE ?
    """, (f"%{request_id}%",))
    row = cur.fetchone()
    
    if not row:
        return {"ok": False, "error": "Request not found"}
    
    user_id = row[0]
    profile = {
        "user_id": row[0],
        "name": row[1],
        "nicknames": json.loads(row[2] or "[]"),
        "facts": json.loads(row[3] or "[]"),
        "interests": json.loads(row[4] or "[]"),
        "relationship": row[5],
        "preferences": json.loads(row[6] or "{}"),
        "relay_requests": json.loads(row[7] or "[]"),
        "interaction_count": row[8],
        "last_seen": row[9],
        "first_seen": row[10],
        "is_creator": bool(row[11]),
        "ignore_status": bool(row[12]),
    }
    relay_requests = profile.get("relay_requests", [])
    
    # Find and update the request
    request = None
    for r in relay_requests:
        if r.get("request_id") == request_id:
            request = r
            break
    
    if not request:
        return {"ok": False, "error": "Request not found in profile"}
    
    if request.get("status") != RELAY_PENDING:
        return {"ok": False, "error": f"Request already {request.get('status')}"}
    
    if action == "approve":
        request["status"] = RELAY_APPROVED
        request["creator_response"] = response_text or "My dad said to reach out."
        reply_to_user = (
            f"My dad got back to you:\n\n{response_text or 'He said to reach out.'}"
        )
    elif action == "decline":
        request["status"] = RELAY_DECLINED
        request["creator_response"] = response_text or "My dad said he's not available."
        reply_to_user = (
            f"My dad said: {response_text or 'Not right now.'}"
        )
    else:
        return {"ok": False, "error": "Action must be 'approve' or 'decline'"}
    
    request["responded_at"] = time.time()
    
    # Save updated profile
    profile["relay_requests"] = relay_requests
    profile_update(user_id, relay_requests=relay_requests)
    
    # Send response to user via bridge
    from services.bridge_api import bridge_send
    user_jid = request.get("user_jid") or f"{user_id}@s.whatsapp.net"
    result = bridge_send(user_jid, reply_to_user)
    
    return {
        "ok": True,
        "status": request["status"],
        "delivered": result.get("ok", False)
    }


def get_pending_relay_requests() -> list[Dict[str, Any]]:
    """Get all pending relay requests for creator dashboard."""
    from services.storage import get_conn
    conn = get_conn()
    # Query profiles that have relay_requests (non-empty JSON array)
    cur = conn.execute("""
        SELECT user_id, name, nicknames, facts, interests, relationship, preferences, relay_requests, 
               interaction_count, last_seen, first_seen, is_creator, ignore_status 
        FROM profiles 
        WHERE relay_requests IS NOT NULL AND relay_requests != '[]'
    """)
    
    pending = []
    for row in cur.fetchall():
        import json
        user_id = row[0]
        profile = {
            "user_id": row[0],
            "name": row[1],
            "nicknames": json.loads(row[2] or "[]"),
            "facts": json.loads(row[3] or "[]"),
            "interests": json.loads(row[4] or "[]"),
            "relationship": row[5],
            "preferences": json.loads(row[6] or "{}"),
            "relay_requests": json.loads(row[7] or "[]"),
            "interaction_count": row[8],
            "last_seen": row[9],
            "first_seen": row[10],
            "is_creator": bool(row[11]),
            "ignore_status": bool(row[12]),
        }
        for r in profile.get("relay_requests", []):
            if r.get("status") == RELAY_PENDING:
                pending.append(r)
    return pending


def cleanup_expired_relays() -> int:
    """Clean up expired relay requests (run periodically)."""
    from services.storage import get_conn, profile_update
    import json
    
    conn = get_conn()
    cur = conn.execute("""
        SELECT user_id, name, nicknames, facts, interests, relationship, preferences, relay_requests,
               interaction_count, last_seen, first_seen, is_creator, ignore_status 
        FROM profiles 
        WHERE relay_requests IS NOT NULL AND relay_requests != '[]'
    """)
    
    cleaned = 0
    now = time.time()
    
    for row in cur.fetchall():
        user_id = row[0]
        profile = {
            "user_id": row[0],
            "name": row[1],
            "nicknames": json.loads(row[2] or "[]"),
            "facts": json.loads(row[3] or "[]"),
            "interests": json.loads(row[4] or "[]"),
            "relationship": row[5],
            "preferences": json.loads(row[6] or "{}"),
            "relay_requests": json.loads(row[7] or "[]"),
            "interaction_count": row[8],
            "last_seen": row[9],
            "first_seen": row[10],
            "is_creator": bool(row[11]),
            "ignore_status": bool(row[12]),
        }
        relay_requests = profile.get("relay_requests", [])
        
        updated = []
        for r in relay_requests:
            if r.get("status") == RELAY_PENDING and r.get("expires_at", 0) < now:
                r["status"] = RELAY_EXPIRED
                cleaned += 1
            updated.append(r)
        
        if len(updated) != len(relay_requests):
            profile["relay_requests"] = updated
            profile_update(user_id, relay_requests=updated)
    
    return cleaned


# Tool function for LLM
RELAY_REQUEST_TOOL = {
    "type": "function",
    "function": {
        "name": "request_creator_contact",
        "description": "User wants to contact the creator (Crimson/Elijah). Creates a relay request that notifies the creator with user's info. Creator can then choose to share contact or send a message.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Optional message from the user explaining why they want to contact the creator"
                }
            },
            "required": []
        }
    }
}


def execute_relay_request(user_id: str, user_jid: str, message: str = "", user_name: str = "") -> Dict[str, Any]:
    """Execute the relay request tool."""
    return create_relay_request(user_id, user_jid, message, user_name)