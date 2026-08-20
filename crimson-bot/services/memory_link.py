"""
services/memory_link.py
=======================
Cross-session memory linking for Crimsonej.
Connects facts, topics, and context across different conversation sessions.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from core.config import BASE_DIR, TZ, log
from services.memory import profile_mgr, sessions, get_vault_context


# ─── Persistent Memory Links ────────────────────────────────────────────────

_LINKS_FILE = os.path.join(BASE_DIR, "memory_links.json")
_link_lock = threading.Lock()
_memory_links: dict[str, dict] = {}  # user_id -> {topics, entities, cross_refs, last_updated}

def _load_links() -> None:
    global _memory_links
    if os.path.exists(_LINKS_FILE):
        try:
            with open(_LINKS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Reconstruct proper data structure
            for user_id, links in data.items():
                # Convert sessions lists back to sets
                if "topics" in links:
                    for topic, topic_data in links.get("topics", {}).items():
                        if "sessions" in topic_data and isinstance(topic_data["sessions"], list):
                            topic_data["sessions"] = set(topic_data["sessions"])
                # Ensure entities is a dict
                if "entities" in links and not isinstance(links.get("entities"), dict):
                    links["entities"] = {}
                # Ensure cross_refs is a list
                if "cross_refs" in links and not isinstance(links.get("cross_refs"), list):
                    links["cross_refs"] = []
            _memory_links = data
        except Exception:
            _memory_links = {}

def _save_links() -> None:
    with _link_lock:
        # Convert sets to lists for JSON serialization
        serializable = {}
        for user_id, links in _memory_links.items():
            serializable[user_id] = {
                "topics": {},
                "entities": links.get("entities", {}),
                "cross_refs": links.get("cross_refs", []),
                "last_updated": links.get("last_updated", time.time()),
            }
            for topic, data in links.get("topics", {}).items():
                serializable[user_id]["topics"][topic] = {
                    "count": data.get("count", 0),
                    "last_seen": data.get("last_seen", 0),
                    "sessions": list(data.get("sessions", [])),  # Convert set to list
                }
            serializable[user_id]["entities"] = links.get("entities", {})
        with open(_LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)

_load_links()


# ─── Entity Extraction ──────────────────────────────────────────────────────

ENTITY_PATTERNS = {
    "symbol": r"\b(BTC|ETH|SOL|BNB|XRP|ADA|DOGE|MATIC|AVAX|DOT|LINK|UNI|AAVE|USDT|USDC|EURUSD|GBPUSD|USDJPY|GOLD|SPX|DXY|OIL|AAPL|TSLA|NVDA|MSFT|GOOGL|AMZN)\b",
    "person": r"\b([A-Z][a-z]+)\b",
    "topic": r"\b(trading|crypto|forex|stocks|technical analysis|fundamental analysis|risk management|psychology|journaling|market structure|support|resistance|rsi|macd|moving average|volume|liquidity|order flow|wyckoff|elliott wave|fibonacci)\b",
    "timeframe": r"\b(1m|5m|15m|30m|1h|4h|1d|1w|1M)\b",
    "price": r"\$?\d{1,3}(?:,\d{3})*(?:\.\d+)?",
    "percentage": r"\d+(?:\.\d+)?%",
}

def extract_entities(text: str) -> dict[str, list[str]]:
    """Extract entities from text using patterns."""
    import re
    entities = defaultdict(list)
    for entity_type, pattern in ENTITY_PATTERNS.items():
        matches = re.findall(pattern, text, re.IGNORECASE)
        entities[entity_type] = list(set(matches))  # dedupe
    return dict(entities)


# ─── Cross-Session Linking ──────────────────────────────────────────────────

def link_session_memory(user_id: str, session_key: str, message: str, context: dict = None) -> dict:
    """Link entities and topics from current message to user's memory graph."""
    with _link_lock:
        if user_id not in _memory_links:
            _memory_links[user_id] = {
                "topics": {},           # topic -> {count, last_seen, sessions}
                "entities": {},         # entity -> {count, last_seen, contexts}
                "cross_refs": [],       # cross-session references
                "last_updated": time.time(),
            }
        
        links = _memory_links[user_id]
        entities = extract_entities(message)
        
        # Update topic tracking
        if context and context.get("topic"):
            topic = context["topic"]
            if topic not in links["topics"]:
                links["topics"][topic] = {"count": 0, "last_seen": time.time(), "sessions": set()}
            links["topics"][topic]["count"] += 1
            links["topics"][topic]["last_seen"] = time.time()
            links["topics"][topic]["sessions"].add(session_key)
        
        # Update entity tracking
        for entity_type, entity_list in entities.items():
            for entity in entity_list:
                key = f"{entity_type}:{entity}"
                if key not in links["entities"]:
                    links["entities"][key] = {"count": 0, "last_seen": time.time(), "contexts": []}
                links["entities"][key]["count"] += 1
                links["entities"][key]["last_seen"] = time.time()
                if context and context.get("topic"):
                    links["entities"][key]["contexts"].append(context["topic"])
        
        # Cross-reference: find related previous sessions
        current_entities = set()
        for ent_list in entities.values():
            current_entities.update(ent_list)
        
        for prev_session_key, session in sessions._store.items():
            if session_key != prev_session_key:
                # Check if same user (approximate by session key prefix)
                pass  # Simplified for now
        
        links["last_updated"] = time.time()
    
    _save_links()
    return {"entities": entities, "linked": True}


def get_cross_session_context(user_id: str, current_topic: str = None) -> str:
    """Get relevant cross-session context for current conversation."""
    with _link_lock:
        links = _memory_links.get(user_id, {})
    
    if not links:
        return ""
    
    parts = ["\n[CROSS-SESSION MEMORY]"]
    
    # Relevant topics
    if current_topic and current_topic in links.get("topics", {}):
        topic_data = links["topics"][current_topic]
        parts.append(f"Previous chats on {current_topic}: {topic_data['count']} mentions across {len(topic_data.get('sessions', []))} sessions")
    
    # Top entities
    entities = links.get("entities", {})
    if entities:
        top_entities = sorted(entities.items(), key=lambda x: x[1]["count"], reverse=True)[:5]
        entity_strs = [f"{k.split(':', 1)[1]} ({v['count']}x)" for k, v in top_entities]
        parts.append(f"Frequent entities: {', '.join(entity_strs)}")
    
    # Related topics
    topics = links.get("topics", {})
    if topics:
        related = sorted(topics.items(), key=lambda x: x[1]["count"], reverse=True)[:3]
        related_strs = [f"{t} ({d['count']}x)" for t, d in related if t != current_topic]
        if related_strs:
            parts.append(f"Related topics: {', '.join(related_strs)}")
    
    if len(parts) > 1:
        return "\n".join(parts) + "\n"
    return ""


def get_user_memory_graph(user_id: str) -> dict:
    """Get full memory graph for user."""
    with _link_lock:
        return dict(_memory_links.get(user_id, {}))


def cleanup_old_links(max_age_days: int = 90) -> int:
    """Remove old, low-count entities and topics."""
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    
    with _link_lock:
        for user_id, links in _memory_links.items():
            # Clean topics
            old_topics = [t for t, d in links.get("topics", {}).items() 
                         if d["last_seen"] < cutoff and d["count"] < 3]
            for t in old_topics:
                del links["topics"][t]
                removed += 1
            
            # Clean entities
            old_entities = [e for e, d in links.get("entities", {}).items() 
                           if d["last_seen"] < cutoff and d["count"] < 2]
            for e in old_entities:
                del links["entities"][e]
                removed += 1
    
    _save_links()
    return removed


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test entity extraction
    test_text = "BTC is at $69,000 on the 4h timeframe. RSI shows 65. Thinking about EURUSD too."
    entities = extract_entities(test_text)
    print("Entities:", entities)
    
    # Test linking
    link_session_memory("test_user", "session_1", test_text, {"topic": "trading"})
    print("\nLinked memory:", get_user_memory_graph("test_user"))
    
    # Test cross-session context
    context = get_cross_session_context("test_user", "trading")
    print("\nCross-session context:", context)