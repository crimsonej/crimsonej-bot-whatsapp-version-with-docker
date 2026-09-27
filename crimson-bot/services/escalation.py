"""
services/escalation.py
======================
Minimal Escalation System.
Only triggers when someone addresses the bot by a wrong name
(crimson, elijah, creator, dad) - suggesting they meant someone else.
Bot verifies first, then silently pings creator if it's a genuine mix-up.
"""

from __future__ import annotations

import json
import re
import time
from typing import Dict, Any, Optional

from core.config import cfg, log
from services.storage import profile_get, profile_update
from services.bridge_api import bridge_send


# Only wrong-name escalation
ESCALATION_WRONG_NAME = "wrong_name"


# Patterns that suggest user is addressing someone else
WRONG_NAME_PATTERNS = [
    r"\b(crimson|elijah|creator|dad)\b",
    r"@\s*(crimson|elijah|creator|dad)\b",
    r"hey\s+(crimson|elijah|creator|dad)\b",
    r"(crimson|elijah|creator|dad)[,\s]can you",
    r"(crimson|elijah|creator|dad)[,\s]please",
]

# Bot's own names (don't trigger on these)
BOT_NAMES = ["crimsonej", "crims", "crim", "bot"]


def detect_wrong_name(user_message: str) -> Dict[str, Any]:
    """
    Check if user addressed bot by wrong name.
    Returns dict with detected name and confidence.
    """
    user_lower = user_message.lower()
    
    # Skip if user used bot's actual names
    for name in BOT_NAMES:
        if name in user_lower:
            return {"detected": False}
    
    # Check for wrong names
    for pattern in WRONG_NAME_PATTERNS:
        match = re.search(pattern, user_lower)
        if match:
            wrong_name = match.group(1) if match.groups() else match.group(0)
            return {
                "detected": True,
                "wrong_name": wrong_name,
                "pattern": pattern,
                "confidence": 0.85
            }
    
    return {"detected": False}


def verify_mixup(user_message: str, bot_response: str) -> Dict[str, Any]:
    """
    Determine if this is likely a mix-up vs user actually talking to bot.
    """
    user_lower = user_message.lower()
    
    # Strong indicators it's a mix-up:
    # 1. User used wrong name + question/request
    # 2. User didn't use bot's actual name
    # 3. Context suggests they want someone else
    
    mixup_indicators = 0
    
    # Check for wrong name
    name_check = detect_wrong_name(user_message)
    if name_check["detected"]:
        mixup_indicators += 2
        wrong_name = name_check["wrong_name"]
    else:
        wrong_name = None
    
    # Check for question/request patterns
    question_patterns = [r"\?", r"\bcan you\b", r"\bplease\b", r"\bhelp\b", r"\bdo\b.*\bthis\b"]
    for pattern in question_patterns:
        if re.search(pattern, user_lower):
            mixup_indicators += 1
            break
    
    # Check if bot already clarified - but DON'T subtract, just don't add extra
    # If bot already said "I'm not X", the user might persist = stronger mix-up signal
    bot_lower = bot_response.lower()
    clarification_phrases = [
        "i'm not", "i am not", "my name is", "i'm crimsonej",
        "i am crimsonej", "that's not my name", "i go by"
    ]
    bot_clarified = any(phrase in bot_lower for phrase in clarification_phrases)
    
    # If bot already clarified but user STILL used wrong name, that's a STRONGER signal
    # Don't penalize - the fact they used wrong name at all is the key indicator
    
    is_mixup = mixup_indicators >= 2
    
    return {
        "is_mixup": is_mixup,
        "confidence": min(0.95, mixup_indicators * 0.35),
        "wrong_name": wrong_name,
        "indicators": mixup_indicators,
        "bot_clarified": bot_clarified
    }


def silent_creator_notify(
    user_id: str,
    user_jid: str,
    wrong_name: str,
    user_message: str,
    bot_response: str
) -> Dict[str, Any]:
    """
    Silently notify creator about name mix-up.
    """
    creator_jid = (cfg("owner_jid") or "").strip()
    if not creator_jid:
        return {"ok": False, "error": "Creator not configured"}
    
    profile = profile_get(user_id)
    
    escalation_id = f"mixup_{int(time.time() * 1000)}"
    
    msg = (
        f"📛 *Name mix-up detected*\n\n"
        f"User: {profile.get('name', 'someone')} ({user_id})\n"
        f"Called me: \"{wrong_name}\"\n"
        f"Said: \"{user_message[:200]}\"\n"
        f"I replied: \"{bot_response[:200]}\"\n\n"
        f"They probably meant someone else named {wrong_name}.\n"
        f"Want me to clarify or let it slide?"
    )
    
    escalation_id = f"mixup_{int(time.time() * 1000)}"
    
    esc_data = {
        "escalation_id": escalation_id,
        "user_id": user_id,
        "user_jid": user_jid,
        "user_name": profile.get("name", "someone"),
        "wrong_name": wrong_name,
        "user_message": user_message,
        "bot_response": bot_response,
        "status": "pending",
        "created_at": time.time(),
    }
    
    # Save to profile
    profile_esc = profile.get("escalations", [])
    profile_esc.append(esc_data)
    if len(profile_esc) > 20:
        profile_esc = profile_esc[-20:]
    profile_update(user_id, escalations=profile_esc)
    
    # Send to creator (non-blocking)
    result = bridge_send(creator_jid, msg)
    
    return {"ok": True, "escalation_id": escalation_id, "notified": result.get("ok", False)}


def process_mixup_response(
    creator_jid: str,
    action: str,
    escalation_id: str,
    response: str = ""
) -> Dict[str, Any]:
    """
    Process creator's response to mix-up alert.
    Actions: clarify, ignore
    """
    expected_jid = (cfg("owner_jid") or "").strip()
    if creator_jid != expected_jid:
        return {"ok": False, "error": "Unauthorized"}
    
    from services.storage import get_conn, profile_update
    import json
    
    conn = get_conn()
    cur = conn.execute("""
        SELECT user_id, name, nicknames, facts, interests, relationship, preferences, 
               escalations, interaction_count, last_seen, first_seen, is_creator, ignore_status 
        FROM profiles 
        WHERE escalations LIKE ?
    """, (f"%{escalation_id}%",))
    row = cur.fetchone()
    
    if not row:
        return {"ok": False, "error": "Escalation not found"}
    
    user_id = row[0]
    profile = {
        "user_id": row[0],
        "name": row[1],
        "nicknames": json.loads(row[2] or "[]"),
        "facts": json.loads(row[3] or "[]"),
        "interests": json.loads(row[4] or "[]"),
        "relationship": row[5],
        "preferences": json.loads(row[6] or "{}"),
        "escalations": json.loads(row[7] or "[]"),
        "interaction_count": row[8],
        "last_seen": row[9],
        "first_seen": row[10],
        "is_creator": bool(row[10]),
        "ignore_status": bool(row[11]),
    }
    
    escalations = profile.get("escalations", [])
    escalation = next((e for e in escalations if e.get("escalation_id") == escalation_id), None)
    
    if not escalation:
        return {"ok": False, "error": "Escalation not found"}
    
    if escalation.get("status") != "pending":
        return {"ok": False, "error": f"Already {escalation.get('status')}"}
    
    if action == "clarify":
        escalation["status"] = "clarified"
        escalation["creator_response"] = response or "Noted."
        # Bot will clarify to user in next interaction
        
    elif action == "ignore":
        escalation["status"] = "ignored"
        
    else:
        return {"ok": False, "error": "Action must be 'clarify' or 'ignore'"}
    
    escalation["responded_at"] = time.time()
    
    profile["escalations"] = escalations
    profile_update(user_id, escalations=escalations)
    
    return {"ok": True, "status": escalation["status"]}


def should_escalate_now(user_message: str, bot_response: str) -> Dict[str, Any]:
    """
    Main entry point: check if current exchange needs mix-up escalation.
    Called after bot generates response.
    """
    # Quick pre-check - no wrong names used
    if not any(name in user_message.lower() for name in ["crimson", "elijah", "creator", "dad"]):
        return {"escalate": False}
    
    # Verify it's actually a mix-up
    verification = verify_mixup(user_message, bot_response)
    
    if verification["is_mixup"]:
        return {
            "escalate": True,
            "wrong_name": verification["wrong_name"],
            "confidence": verification["confidence"]
        }
    
    return {"escalate": False}


# Tool for bot to manually escalate if it suspects mix-up
MIXUP_ESCALATE_TOOL = {
    "type": "function",
    "function": {
        "name": "escalate_name_mixup",
        "description": "Use when someone calls you by wrong name (crimson, elijah, creator, dad) and you suspect they meant someone else. Silently alerts creator.",
        "parameters": {
            "type": "object",
            "properties": {
                "wrong_name": {
                    "type": "string",
                    "description": "The wrong name they used (crimson, elijah, creator, dad)"
                },
                "user_message": {
                    "type": "string",
                    "description": "What the user said"
                }
            },
            "required": ["wrong_name", "user_message"]
        }
    }
}


def execute_mixup_escalate(
    user_id: str,
    user_jid: str,
    wrong_name: str,
    user_message: str,
    bot_response: str = ""
) -> Dict[str, Any]:
    """Execute mix-up escalation."""
    return silent_creator_notify(user_id, user_jid, wrong_name, user_message, bot_response)