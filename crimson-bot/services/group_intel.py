"""
services/group_intel.py
=======================
Group chat intelligence: mention detection, admin checks, context awareness,
rate limiting, and group memory.
"""

from __future__ import annotations

import re
import time
import threading
from typing import Any

from core.config import cfg, log, load_json, save_json
from services.memory import profile_mgr

# ─── In-memory group state ────────────────────────────────────────────────────
_group_lock = threading.Lock()
_group_state: dict[str, dict] = {}  # group_jid -> {name, topic, admins, last_active, msg_count, rate_limit}

_GROUP_STATE_FILE = "group_state.json"  # persisted in BASE_DIR

def _load_group_state() -> None:
    global _group_state
    from core.config import BASE_DIR
    path = BASE_DIR + "/" + _GROUP_STATE_FILE
    data = load_json(path, {})
    if isinstance(data, dict):
        _group_state = data

def _save_group_state() -> None:
    from core.config import BASE_DIR
    path = BASE_DIR + "/" + _GROUP_STATE_FILE
    save_json(path, _group_state)

_load_group_state()


# ─── Mention Detection ────────────────────────────────────────────────────────

MENTION_PATTERNS = [
    r"@(\d{5,15})",           # @phone_number
    r"@([a-zA-Z0-9_.-]+)",    # @username (if bridge provides)
    r"crimsonej\b",           # name mention (case-insensitive)
]

_mention_regex = re.compile("|".join(f"({p})" for p in MENTION_PATTERNS), re.IGNORECASE)


def is_mentioned(text: str, bot_jid: str, bot_phone: str) -> bool:
    """Check if the bot is mentioned in the message."""
    if not text:
        return False
    
    # Direct JID mention
    if bot_jid and bot_jid in text:
        return True
    # Phone number mention
    if bot_phone and f"@{bot_phone}" in text:
        return True
    # Name mentions
    text_lower = text.lower()
    if "crimsonej" in text_lower:
        return True
    
    return False


# ─── Multi-Bot Conflict Avoidance ──────────────────────────────────────────────

OTHER_BOT_PATTERNS = [
    r"@(\d{5,15})",           # @phone_number (generic)
    r"@([a-zA-Z0-9_.-]+bot)", # @username ending in 'bot'
    r"\b(other|another)\s+bot\b",
]


def detect_other_bot_mentions(text: str, bot_jid: str, bot_phone: str) -> list[str]:
    """Detect if message mentions other bots."""
    if not text:
        return []
    
    text_lower = text.lower()
    other_bots = []
    
    # Check for @mentions that aren't this bot
    mentions = extract_mentions(text)
    for mention in mentions:
        # Skip if it's this bot
        if bot_phone and mention == bot_phone:
            continue
        if bot_jid and mention in bot_jid:
            continue
        if "crimsonej" in mention.lower():
            continue
        other_bots.append(mention)
    
    return other_bots


def should_respond_in_multi_bot_context(text: str, bot_jid: str, bot_phone: str, is_group: bool) -> tuple[bool, str | None]:
    """
    Determine if bot should respond when other bots might be present.
    Returns (should_respond, reason_if_not).
    """
    if not is_group:
        return True, None  # Always respond in DMs
    
    # If directly mentioned, always respond
    if is_mentioned(text, bot_jid, bot_phone):
        return True, None
    
    # If command, always respond
    if text.strip().startswith("/"):
        return True, None
    
    # Check for other bot mentions
    other_bots = detect_other_bot_mentions(text, bot_jid, bot_phone)
    if other_bots:
        # Someone is talking to another bot - stay quiet
        return False, f"Other bot mentioned: {', '.join(other_bots)}"
    
    return True, None


def extract_mentions(text: str) -> list[str]:
    """Extract all @mentions from text."""
    if not text:
        return []
    matches = re.findall(r"@(\d{5,15}|[a-zA-Z0-9_.-]+)", text)
    return list(set(matches))  # dedupe


# ─── Group Admin Detection ────────────────────────────────────────────────────

def is_group_admin(group_jid: str, user_jid: str, bridge_api=None) -> bool:
    """Check if user is admin of group. Uses cached state, falls back to bridge."""
    with _group_lock:
        state = _group_state.get(group_jid, {})
        admins = state.get("admins", [])
        if user_jid in admins:
            return True
    
    # Try fetching from bridge if not cached
    if not admins:
        fetched = fetch_and_cache_group_admins(group_jid)
        if fetched and user_jid in fetched:
            return True
    
    # Fallback: creator is always admin
    profile = profile_mgr.get_profile(user_jid)
    if profile.get("is_creator"):
        return True
    
    return False


def update_group_admins(group_jid: str, admins: list[str]) -> None:
    """Update cached admin list for group."""
    with _group_lock:
        if group_jid not in _group_state:
            _group_state[group_jid] = {}
        _group_state[group_jid]["admins"] = admins
        _group_state[group_jid]["last_active"] = time.time()
    _save_group_state()


def fetch_and_cache_group_admins(group_jid: str) -> list[str] | None:
    """
    Fetch group admins from bridge and cache them.
    Returns admin list on success, None on failure.
    """
    try:
        import services.bridge_api as bridge_api
        result = bridge_api.bridge_get_group_admins(group_jid)
        if result.get("ok") and result.get("admins"):
            admins = result["admins"]
            update_group_admins(group_jid, admins)
            return admins
    except Exception as e:
        log.warning("[GroupIntel] Failed to fetch admins from bridge for %s: %s", group_jid, e)
    return None


# ─── Group Context/Topic Awareness ────────────────────────────────────────────

def get_group_context(group_jid: str) -> dict[str, Any]:
    """Get cached group context (topic, vibe, rules, etc.)."""
    with _group_lock:
        return dict(_group_state.get(group_jid, {}))


def update_group_context(group_jid: str, **kwargs) -> None:
    """Update group context (topic, description, rules, custom fields)."""
    with _group_lock:
        if group_jid not in _group_state:
            _group_state[group_jid] = {"created_at": time.time()}
        _group_state[group_jid].update(kwargs)
        _group_state[group_jid]["last_active"] = time.time()
    _save_group_state()


def learn_group_topic(group_jid: str, message: str, sender_name: str) -> None:
    """Passively learn group topic from conversation patterns."""
    # Simple heuristic: track most discussed subjects
    state = get_group_context(group_jid)
    topics = state.get("topic_keywords", {})
    
    # Extract potential topic keywords (nouns, proper nouns)
    words = re.findall(r"\b[A-Z][a-z]{2,}\b|\b[crypto|trading|btc|eth|forex|stocks|memes|tech|ai|gaming|sports|music|movies]\b", message, re.IGNORECASE)
    for w in words:
        w_lower = w.lower()
        topics[w_lower] = topics.get(w_lower, 0) + 1
    
    # Keep top 20
    sorted_topics = sorted(topics.items(), key=lambda x: x[1], reverse=True)[:20]
    update_group_context(group_jid, topic_keywords=dict(sorted_topics))


def get_group_vibe(group_jid: str) -> str:
    """Get a short description of the group's vibe/topic for system prompt."""
    state = get_group_context(group_jid)
    topics = state.get("topic_keywords", {})
    if not topics:
        return ""
    
    top = [k for k, _ in sorted(topics.items(), key=lambda x: x[1], reverse=True)[:5]]
    return f" This group often discusses: {', '.join(top)}."


# ─── Group Rate Limiting ──────────────────────────────────────────────────────

def check_group_rate_limit(group_jid: str, max_per_minute: int = 10) -> tuple[bool, dict]:
    """
    Check if group is within rate limits.
    Returns (allowed, rate_info).
    """
    now = time.time()
    with _group_lock:
        state = _group_state.get(group_jid, {})
        minute_bucket = int(now // 60)
        
        # Reset if new minute
        if state.get("rate_minute_bucket") != minute_bucket:
            state["rate_minute_bucket"] = minute_bucket
            state["rate_count"] = 0
        
        count = state.get("rate_count", 0)
        allowed = count < max_per_minute
        
        if allowed:
            state["rate_count"] = count + 1
        
        _group_state[group_jid] = state
    
    rate_info = {
        "count": state.get("rate_count", 0),
        "limit": max_per_minute,
        "reset_at": (minute_bucket + 1) * 60
    }
    
    return allowed, rate_info


def get_group_message_count(group_jid: str) -> int:
    """Get total message count for group (for analytics)."""
    with _group_lock:
        return _group_state.get(group_jid, {}).get("total_messages", 0)


def increment_group_messages(group_jid: str) -> None:
    """Increment group message counter."""
    with _group_lock:
        if group_jid not in _group_state:
            _group_state[group_jid] = {}
        _group_state[group_jid]["total_messages"] = _group_state[group_jid].get("total_messages", 0) + 1
        _group_state[group_jid]["last_active"] = time.time()
    # Save periodically, not every message
    if _group_state[group_jid]["total_messages"] % 50 == 0:
        _save_group_state()


# ─── Group Join/Leave Events ──────────────────────────────────────────────────

def handle_group_join(group_jid: str, user_jid: str, user_name: str, is_bot: bool = False) -> str | None:
    """Handle user joining group. Returns welcome message or None."""
    if is_bot:
        # Bot joined - introduce self
        state = get_group_context(group_jid)
        member_count = state.get("member_count", 0)
        update_group_context(group_jid, member_count=member_count + 1, joined_at=time.time())
        return (
            "Yo! Crimsonej in the building 😎\n"
            "Trading, memes, songs, images, roasts — I got you.\n"
            "Tag me with @Crimsonej.\n"
            "Type `/help` for commands."
        )
    
    # User joined - track member count
    state = get_group_context(group_jid)
    member_count = state.get("member_count", 0)
    update_group_context(group_jid, member_count=member_count + 1)
    
    # Only welcome if group is small or configured
    if member_count < 20:
        return f"Welcome {user_name}! 👋 Crimsonej's here if you need anything — just @ me."
    
    return None


def handle_group_leave(group_jid: str, user_jid: str, user_name: str) -> None:
    """Handle user leaving group."""
    state = get_group_context(group_jid)
    member_count = max(0, state.get("member_count", 1) - 1)
    update_group_context(group_jid, member_count=member_count)


# ─── Reply Thread Awareness ────────────────────────────────────────────────────

def is_bot_quoted(quoted_author: str, bot_jid: str, bot_phone: str) -> bool:
    """Check if the quoted message author is the bot."""
    if not quoted_author:
        return False
    # Check JID match
    if bot_jid and bot_jid in quoted_author:
        return True
    # Check phone match
    if bot_phone and bot_phone in quoted_author:
        return True
    # Check name match
    if "crimsonej" in quoted_author.lower():
        return True
    return False


def build_thread_context(quoted_text: str, quoted_author: str, quoted_author_name: str | None = None) -> str:
    """Build context string for quoted message thread."""
    if not quoted_text:
        return ""
    
    author_display = quoted_author_name or quoted_author or "someone"
    # Truncate long quoted messages
    quoted_preview = quoted_text[:300] + ("..." if len(quoted_text) > 300 else "")
    
    return (
        f"\n[THREAD CONTEXT: Replying to {author_display}'s message]\n"
        f"They said: \"{quoted_preview}\"\n"
        f"[END THREAD CONTEXT]\n"
    )


def extract_mentions_from_text(text: str) -> list[str]:
    """Extract @mentions from text for reply targeting."""
    if not text:
        return []
    matches = re.findall(r"@(\d{5,15}|[a-zA-Z0-9_.-]+)", text)
    return list(set(matches))  # dedupe


def format_reply_with_mentions(reply_text: str, target_jid: str, target_name: str | None = None) -> str:
    """Prepend @mention to reply if replying to a specific user in a group."""
    if not reply_text or not target_jid:
        return reply_text
    # Extract phone number from JID if needed
    target_phone = ''.join(ch for ch in target_jid if ch.isdigit())
    if not target_phone:
        target_phone = target_jid.split('@')[0] if '@' in target_jid else target_jid
    
    # Only add mention if not already present
    if f"@{target_phone}" not in reply_text and (not target_name or f"@{target_name}" not in reply_text):
        mention = f"@{target_phone} "
        return mention + reply_text
    return reply_text


# ─── Group Session Key ────────────────────────────────────────────────────────

def get_group_session_key(group_jid: str, user_jid: str | None = None) -> str:
    """
    Get session key for group conversations.
    Uses group JID for shared context, but can include user for per-user threads.
    """
    return group_jid  # Shared session by default


# ─── Group-Aware System Prompt Enhancement ────────────────────────────────────

def build_group_system_prompt_addition(
    group_jid: str, 
    sender_name: str, 
    sender_jid: str, 
    is_admin: bool,
    quoted_text: str = "",
    quoted_author: str = "",
    quoted_author_name: str | None = None,
    is_bot_quoted_flag: bool = False
) -> str:
    """Build the group-aware addition to system prompt."""
    vibe = get_group_vibe(group_jid)
    state = get_group_context(group_jid)
    member_count = state.get("member_count", "?")
    
    admin_note = " (GROUP ADMIN)" if is_admin else ""
    
    parts = [
        f"\n[GROUP CHAT: You're in a group with ~{member_count} members.{vibe}]",
        f"The person messaging you is {sender_name}{admin_note}.",
        "You know their name but do NOT use it in every reply — that's weird and robotic.",
        "Use names only when natural (greeting someone new, calling someone out, replying to a direct question).",
        "Talk like a real person in a group chat. Keep replies concise.",
        "To tag someone, use @phone_number format.",
    ]
    
    # Add thread context if replying to a quoted message
    if quoted_text:
        thread_ctx = build_thread_context(quoted_text, quoted_author, quoted_author_name)
        parts.append(thread_ctx)
        
        if is_bot_quoted_flag:
            parts.append(
                "NOTE: The quoted message was YOURS. The user is responding to something you said earlier. "
                "Acknowledge the continuity naturally."
            )
    
    return "\n".join(parts) + "\n"


# ─── Command Restrictions ─────────────────────────────────────────────────────

ADMIN_ONLY_COMMANDS = {
    "master control",
    "wipe",
    "briefing_subscribe",
    "briefing_unsubscribe",
    "brief_sub",
    "brief_unsub",
    "status_posting",
    "status_reply",
    "scheduler",
    "interval",
    "topic",
    "status_now",
    "config",
}

GROUP_ADMIN_COMMANDS = {
    "/briefing_subscribe",
    "/briefing_unsubscribe",
    "/brief_sub",
    "/brief_unsub",
    "/briefing_list",
    "/brief_list",
}


def is_command_admin_only(command: str) -> bool:
    """Check if command requires creator (master control) access."""
    cmd_lower = command.lower().strip()
    return any(cmd_lower.startswith(c) for c in ADMIN_ONLY_COMMANDS)


def is_command_group_admin_only(command: str) -> bool:
    """Check if command requires group admin access."""
    cmd_lower = command.lower().strip()
    return any(cmd_lower.startswith(c) for c in GROUP_ADMIN_COMMANDS)


def check_command_permissions(command: str, user_jid: str, group_jid: str | None, is_group: bool) -> tuple[bool, str | None]:
    """
    Check if user can run command.
    Returns (allowed, error_message).
    """
    if is_command_admin_only(command):
        profile = profile_mgr.get_profile(user_jid)
        if not profile.get("is_creator"):
            return False, "🔒 Creator-only command. Run `master control chela` to authenticate."
        return True, None
    
    if is_group and is_command_group_admin_only(command):
        if not is_group_admin(group_jid, user_jid):
            return False, "🔒 Group admin only command."
        return True, None
    
    return True, None


# ─── Group Analytics ──────────────────────────────────────────────────────────

def get_group_stats(group_jid: str) -> dict:
    """Get group statistics for reporting."""
    state = get_group_context(group_jid)
    return {
        "group_jid": group_jid,
        "name": state.get("name", "Unknown"),
        "member_count": state.get("member_count", 0),
        "total_messages": state.get("total_messages", 0),
        "top_topics": list(state.get("topic_keywords", {}).keys())[:10],
        "created_at": state.get("created_at"),
        "last_active": state.get("last_active"),
        "admins": state.get("admins", []),
    }


def list_active_groups(min_messages: int = 10) -> list[dict]:
    """List groups with activity above threshold."""
    with _group_lock:
        return [
            get_group_stats(gid) 
            for gid, state in _group_state.items() 
            if state.get("total_messages", 0) >= min_messages
        ]


# ─── Group Vault (Persistent Group Memory) ────────────────────────────────────

import os
from core.config import BASE_DIR

_GROUP_VAULTS_DIR = os.path.join(BASE_DIR, "group_vaults")
os.makedirs(_GROUP_VAULTS_DIR, exist_ok=True)


def _get_group_vault_path(group_jid: str) -> str:
    """Get the vault file path for a group."""
    # Sanitize JID for filename
    safe_name = group_jid.replace("@", "_at_").replace(".", "_dot_").replace(":", "_colon_")
    return os.path.join(_GROUP_VAULTS_DIR, f"vault_{safe_name}.txt")


def get_group_vault_context(group_jid: str) -> str:
    """Retrieve permanent group vault context for system prompt."""
    vault_path = _get_group_vault_path(group_jid)
    if not os.path.exists(vault_path):
        return ""
    
    try:
        with open(vault_path, "r", encoding="utf-8") as f:
            vdata = f.read()
        if len(vdata) > 30000:
            vdata = "[...older facts truncated...]\n" + vdata[-30000:]
        if vdata.strip():
            return f"\n\n--- GROUP MEMORY VAULT ---\nShared facts for this group:\n{vdata}\n--------------------------\n"
    except Exception as e:
        log.error("[GroupVault] Error reading vault for %s: %s", group_jid, e)
    return ""


def learn_group_fact(group_jid: str, fact: str, source: str = "conversation") -> None:
    """Save a learned fact to the group vault."""
    if not fact or not fact.strip():
        return
    
    vault_path = _get_group_vault_path(group_jid)
    from datetime import datetime
    from core.config import TZ
    
    timestamp = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    entry = f"\n--- Learned on {timestamp} (Source: {source}) ---\n{fact.strip()}\n"
    
    try:
        with open(vault_path, "a", encoding="utf-8") as f:
            f.write(entry)
        log.info("[GroupVault] Learned fact saved for group %s", group_jid)
    except Exception as e:
        log.error("[GroupVault] Error saving fact for %s: %s", group_jid, e)


def get_group_vault_raw(group_jid: str) -> str:
    """Get raw vault content for viewing/editing."""
    vault_path = _get_group_vault_path(group_jid)
    if not os.path.exists(vault_path):
        return ""
    try:
        with open(vault_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def clear_group_vault(group_jid: str) -> bool:
    """Clear the group vault (creator only)."""
    vault_path = _get_group_vault_path(group_jid)
    try:
        if os.path.exists(vault_path):
            os.remove(vault_path)
        return True
    except Exception as e:
        log.error("[GroupVault] Error clearing vault for %s: %s", group_jid, e)
        return False


# ─── Initialization ───────────────────────────────────────────────────────────

def init_group_intel() -> None:
    """Call at bot startup to ensure group state is loaded."""
    _load_group_state()
    log.info("[GroupIntel] Loaded state for %d groups", len(_group_state))