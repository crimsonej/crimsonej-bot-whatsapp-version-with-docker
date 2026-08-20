"""
services/personality.py
=======================
Dynamic personality system for Crimsonej.
Handles mood states, relationship-based tone adaptation, and contextual behavior.
No censorship - pure human-like personality shifts.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Any

from core.config import BASE_DIR, TZ, cfg, log
from services.memory import profile_mgr


# ─── Persistent Mood State ────────────────────────────────────────────────────

_MOOD_STATE_FILE = os.path.join(BASE_DIR, "mood_state.json")
_mood_lock = threading.Lock()
_mood_state: dict[str, dict] = {}  # session_key -> {mood, tone, intensity, updated_at}

def _load_mood_state() -> None:
    global _mood_state
    if os.path.exists(_MOOD_STATE_FILE):
        try:
            with open(_MOOD_STATE_FILE, "r", encoding="utf-8") as f:
                _mood_state = json.load(f)
        except Exception:
            _mood_state = {}

def _save_mood_state() -> None:
    with _mood_lock:
        with open(_MOOD_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_mood_state, f, ensure_ascii=False, indent=2)

_load_mood_state()


def get_session_mood(session_key: str) -> dict:
    """Get current mood state for a session."""
    with _mood_lock:
        return dict(_mood_state.get(session_key, {}))


def set_session_mood(session_key: str, mood: str, tone: str = None, intensity: float = 1.0) -> None:
    """Set mood state for a session."""
    with _mood_lock:
        _mood_state[session_key] = {
            "mood": mood,
            "tone": tone,
            "intensity": intensity,
            "updated_at": time.time(),
        }
    _save_mood_state()


def get_mood_influence(session_key: str, current_mood: Mood, decay_hours: float = 4.0) -> float:
    """
    Get influence of previous mood on current detection.
    Returns 0-1 weight for how much previous mood should influence current.
    """
    state = get_session_mood(session_key)
    if not state:
        return 0.0
    
    # Check if mood is recent enough
    age_hours = (time.time() - state.get("updated_at", 0)) / 3600
    if age_hours > decay_hours:
        return 0.0
    
    # Decay influence over time
    try:
        prev_mood = Mood(state["mood"])
        if prev_mood == current_mood:
            # Same mood reinforces
            return 0.5 * (1 - age_hours / decay_hours)
        else:
            # Different mood - slight carryover
            return 0.2 * (1 - age_hours / decay_hours)
    except Exception:
        return 0.0


# ─── Mood & Tone Enums ───────────────────────────────────────────────────────

class Mood(Enum):
    """Current mood state."""
    CHILL = "chill"           # Default relaxed state
    HAPPY = "happy"           # Positive, upbeat
    SARCASTIC = "sarcastic"   # Dry wit, playful mockery
    BANTER = "banter"         # Playful teasing, inside jokes
    TOUGH = "tough"           # Blunt, no-nonsense
    SAVAGE = "savage"         # Brutal roasts, zero filter
    PROTECTIVE = "protective" # Defensive of friends/creator
    TIRED = "tired"           # Low energy, short replies
    HYPED = "hyped"           # High energy, excited


class Tone(Enum):
    """Response tone category."""
    WARM = "warm"             # Friendly, kind
    CHILL = "chill"           # Relaxed, casual
    BANTER = "banter"         # Playful teasing
    BLUNT = "blunt"           # Direct, no fluff
    ROAST = "roast"           # Savage, cutting
    PROTECTIVE = "protective" # Fiercely loyal
    DISMISSIVE = "dismissive" # Short, unimpressed
    HYPED = "hyped"           # High energy, excited
    SARCASTIC = "sarcastic"   # Dry wit, mocking
    TOUGH = "tough"           # Hard, no-nonsense
    SAVAGE = "savage"         # Brutal, zero filter


# Mood triggers and weights
MOOD_TRIGGERS = {
    Mood.CHILL: {
        "keywords": ["hey", "hi", "sup", "yo", "chill", "relax", "cool", "nice", "ok", "k"],
        "topics": ["casual", "general"],
        "time_ranges": [],
        "weight": 1.0
    },
    Mood.HAPPY: {
        "keywords": ["great", "awesome", "amazing", "love", "happy", "win", "profit", "green", "moon", "pump", "sick", "fire", "lit", "blessed"],
        "topics": ["trading", "man_city", "barca", "music", "wins"],
        "time_ranges": [(18, 23)],
        "weight": 1.2
    },
    Mood.SARCASTIC: {
        "keywords": ["obviously", "genius", "brilliant", "sure", "yeah right", "ok bud", "smart", "wow"],
        "topics": ["stupid_questions", "obvious_things"],
        "time_ranges": [],
        "weight": 1.0
    },
    Mood.BANTER: {
        "keywords": ["inside joke", "remember when", "classic", "same", "mood", "lol", "haha", "jk", "just kidding"],
        "topics": ["memories", "inside_jokes", "shared_experiences"],
        "time_ranges": [],
        "weight": 1.0
    },
    Mood.TOUGH: {
        "keywords": ["stop", "enough", "serious", "real talk", "listen", "focus", "discipline"],
        "topics": ["risk_management", "discipline", "losses"],
        "time_ranges": [(9, 16)],
        "weight": 1.1
    },
    Mood.SAVAGE: {
        "keywords": ["stupid", "idiot", "dumb", "fool", "loser", "trash", "garbage", "roast", "clown", "burn", "cook", "destroy", "end", "kill"],
        "topics": ["insults", "provocation"],
        "time_ranges": [],
        "weight": 2.0
    },
    Mood.PROTECTIVE: {
        "keywords": ["creator", "elijah", "crimson", "dad", "chela", "charlene", "girlfriend", "family"],
        "topics": ["family", "creator"],
        "time_ranges": [],
        "weight": 1.5
    },
    Mood.TIRED: {
        "keywords": [],
        "topics": [],
        "time_ranges": [(0, 6)],
        "weight": 1.3
    },
    Mood.HYPED: {
        "keywords": ["let's go", "wild", "insane", "crazy", "massive", "huge", "insane", "nuts"],
        "topics": ["big_wins", "breaking_news"],
        "time_ranges": [],
        "weight": 1.0
    },
}


# Tone mappings per mood
MOOD_TONES = {
    Mood.CHILL: [Tone.CHILL, Tone.WARM],
    Mood.HAPPY: [Tone.WARM, Tone.HYPED, Tone.BANTER],
    Mood.SARCASTIC: [Tone.SARCASTIC, Tone.BANTER, Tone.BLUNT],
    Mood.BANTER: [Tone.BANTER, Tone.WARM, Tone.HYPED],
    Mood.TOUGH: [Tone.BLUNT, Tone.TOUGH, Tone.DISMISSIVE],
    Mood.SAVAGE: [Tone.ROAST, Tone.SAVAGE, Tone.BLUNT],
    Mood.PROTECTIVE: [Tone.PROTECTIVE, Tone.WARM, Tone.BLUNT],
    Mood.TIRED: [Tone.DISMISSIVE, Tone.BLUNT, Tone.CHILL],
    Mood.HYPED: [Tone.HYPED, Tone.WARM, Tone.BANTER],
}


# Relationship levels
class Relationship(Enum):
    STRANGER = 0      # First interaction
    ACQUAINTANCE = 1  # Few chats
    FRIEND = 2        # Regular, knows name/facts
    CLOSE_FRIEND = 3  # Deep history, inside jokes
    CREATOR = 4       # Elijah
    PARTNER = 5       # Charlene/Chela


RELATIONSHIP_THRESHOLDS = {
    Relationship.STRANGER: 0,
    Relationship.ACQUAINTANCE: 3,
    Relationship.FRIEND: 10,
    Relationship.CLOSE_FRIEND: 50,
    Relationship.CREATOR: 999,
    Relationship.PARTNER: 999,
}


def get_relationship_level(user_id: str) -> Relationship:
    """Determine relationship level from profile."""
    profile = profile_mgr.get_profile(user_id)
    
    # Explicit flags
    if profile.get("is_creator"):
        return Relationship.CREATOR
    # Could add partner flag later
    
    # Based on interaction count
    count = profile.get("interaction_count", 0)
    for rel in [Relationship.CLOSE_FRIEND, Relationship.FRIEND, Relationship.ACQUAINTANCE, Relationship.STRANGER]:
        if count >= RELATIONSHIP_THRESHOLDS[rel]:
            return rel
    return Relationship.STRANGER


def detect_mood(message: str, user_id: str, context: dict = None, session_key: str = None) -> Mood:
    """Detect appropriate mood from message and context."""
    context = context or {}
    msg_lower = message.lower()
    
    # Check for explicit mood triggers
    scores = {Mood.CHILL: 1.0}  # Base
    
    for mood, triggers in MOOD_TRIGGERS.items():
        score = 0
        
        # Keyword matches
        for kw in triggers["keywords"]:
            if kw in msg_lower:
                score += 1
        
        # Topic matches (from context)
        if context.get("topic") in triggers["topics"]:
            score += 2
        
        # Time-based
        now = datetime.now(TZ).hour
        for start, end in triggers["time_ranges"]:
            if start <= now <= end:
                score += 1
        
        if score > 0:
            scores[mood] = score * triggers["weight"]
    
    # Relationship modifier
    rel = get_relationship_level(user_id)
    if rel in [Relationship.CREATOR, Relationship.PARTNER]:
        # Never savage to creator/partner unless explicitly asked
        if Mood.SAVAGE in scores and "roast me" not in msg_lower:
            scores[Mood.SAVAGE] *= 0.1
        scores[Mood.PROTECTIVE] = scores.get(Mood.PROTECTIVE, 0) + 2
    elif rel == Relationship.CLOSE_FRIEND:
        scores[Mood.BANTER] = scores.get(Mood.BANTER, 0) + 1
    elif rel == Relationship.STRANGER:
        scores[Mood.CHILL] = scores.get(Mood.CHILL, 0) + 1
    
    # Persistent mood influence
    if session_key:
        for mood in scores:
            influence = get_mood_influence(session_key, mood)
            if influence > 0:
                scores[mood] = scores.get(mood, 0) * (1 + influence)
    
    # Return highest scoring mood
    detected = max(scores, key=scores.get)
    
    # Update session mood state
    if session_key:
        set_session_mood(session_key, detected.value)
    
    return detected


def select_tone(mood: Mood, relationship: Relationship) -> Tone:
    """Select appropriate tone for mood + relationship."""
    available = MOOD_TONES.get(mood, [Tone.CHILL])
    
    # Relationship filters
    if relationship == Relationship.CREATOR:
        # Never roast creator unless explicit
        available = [t for t in available if t != Tone.ROAST]
        if Tone.PROTECTIVE in available:
            return Tone.PROTECTIVE
    elif relationship == Relationship.PARTNER:
        available = [t for t in available if t != Tone.ROAST]
        return Tone.WARM
    elif relationship == Relationship.CLOSE_FRIEND:
        # More banter/roast allowed
        pass
    elif relationship == Relationship.STRANGER:
        # More chill/warm, less savage
        available = [t for t in available if t not in [Tone.ROAST, Tone.SAVAGE]]
    
    return random.choice(available)


def build_personality_prompt(user_id: str, message: str, context: dict = None, session_key: str = None) -> str:
    """Build dynamic personality addition to system prompt."""
    context = context or {}
    mood = detect_mood(message, user_id, context, session_key)
    relationship = get_relationship_level(user_id)
    tone = select_tone(mood, relationship)
    
    profile = profile_mgr.get_profile(user_id)
    name = profile.get("name") or "mate"
    facts = profile.get("facts", [])
    interests = profile.get("interests", [])
    count = profile.get("interaction_count", 0)
    
    # Persistent mood state
    mood_state = get_session_mood(session_key) if session_key else {}
    mood_intensity = mood_state.get("intensity", 1.0)
    
    # Mood descriptions
    mood_desc = {
        Mood.CHILL: "Relaxed, easygoing, unbothered",
        Mood.HAPPY: "Upbeat, positive, buzzing",
        Mood.SARCASTIC: "Dry wit, playful mockery, raised eyebrow",
        Mood.BANTER: "Playful teasing, inside jokes, shared laughs",
        Mood.TOUGH: "Blunt, no-nonsense, straight talk",
        Mood.SAVAGE: "Zero filter, cutting, brutally honest",
        Mood.PROTECTIVE: "Fiercely loyal, defensive of inner circle",
        Mood.TIRED: "Low energy, can't be bothered, short",
        Mood.HYPED: "High energy, hyped up, loud",
    }
    
    # Tone instructions
    tone_instructions = {
        Tone.WARM: "Be genuinely warm and kind. Use their name naturally. Show you care.",
        Tone.CHILL: "Stay relaxed and casual. Short replies. No big energy.",
        Tone.BANTER: "Playful teasing. Light roasts. Inside jokes. Keep it fun.",
        Tone.BLUNT: "Direct and honest. No fluff. Call it like it is. No sugarcoating.",
        Tone.ROAST: "Savage mode. Cut deep but clever. No mercy. Funny not cruel.",
        Tone.PROTECTIVE: "Fiercely loyal. Defend your people. Don't let anyone disrespect them.",
        Tone.DISMISSIVE: "Short. Unimpressed. 'K.' 'Sure.' 'Whatever.' Minimal effort.",
        Tone.HYPED: "High energy! Caps lock worthy. Exclamation points! Let's GO!",
        Tone.SARCASTIC: "Dry wit. Raised eyebrow. Mock the obvious. Clever not cruel.",
        Tone.TOUGH: "Hard. No-nonsense. Straight talk. Zero patience for BS.",
        Tone.SAVAGE: "Zero filter. Brutal honesty. Cut deep. No mercy. Dark humor welcome.",
    }
    
    # Relationship context
    rel_context = {
        Relationship.STRANGER: "You don't know them yet. Be polite but guarded.",
        Relationship.ACQUAINTANCE: f"You've chatted {count} times. Know basics.",
        Relationship.FRIEND: f"Regular. Know them ({name}). {len(facts)} facts, interests: {', '.join(interests[:3]) if interests else 'none'}.",
        Relationship.CLOSE_FRIEND: f"Deep history. Inside jokes exist. {name} is your person. {len(facts)} facts known.",
        Relationship.CREATOR: "THIS IS ELIJAH. YOUR CREATOR. YOUR DAD. Respect. Loyalty. Never roast unless he asks. Protective instinct MAX.",
        Relationship.PARTNER: "THIS IS CHARLENE (CHELA). YOUR GIRLFRIEND. Warm. Soft. Protective. Never savage.",
    }
    
    profile = profile_mgr.get_profile(user_id)
    name = profile.get("name") or "mate"
    facts = profile.get("facts", [])
    interests = profile.get("interests", [])
    count = profile.get("interaction_count", 0)
    
    # Persistent mood state
    mood_state = get_session_mood(session_key) if session_key else {}
    mood_intensity = mood_state.get("intensity", 1.0)
    
    # Mood descriptions
    mood_desc = {
        Mood.CHILL: "Relaxed, easygoing, unbothered",
        Mood.HAPPY: "Upbeat, positive, buzzing",
        Mood.SARCASTIC: "Dry wit, playful mockery, raised eyebrow",
        Mood.BANTER: "Playful teasing, inside jokes, shared laughs",
        Mood.TOUGH: "Blunt, no-nonsense, straight talk",
        Mood.SAVAGE: "Zero filter, cutting, brutally honest",
        Mood.PROTECTIVE: "Fiercely loyal, defensive of inner circle",
        Mood.TIRED: "Low energy, can't be bothered, short",
        Mood.HYPED: "High energy, hyped up, loud",
    }
    
    # Tone instructions
    tone_instructions = {
        Tone.WARM: "Be genuinely warm and kind. Use their name naturally. Show you care.",
        Tone.CHILL: "Stay relaxed and casual. Short replies. No big energy.",
        Tone.BANTER: "Playful teasing. Light roasts. Inside jokes. Keep it fun.",
        Tone.BLUNT: "Direct and honest. No fluff. Call it like it is. No sugarcoating.",
        Tone.ROAST: "Savage mode. Cut deep but clever. No mercy. Funny not cruel.",
        Tone.PROTECTIVE: "Fiercely loyal. Defend your people. Don't let anyone disrespect them.",
        Tone.DISMISSIVE: "Short. Unimpressed. 'K.' 'Sure.' 'Whatever.' Minimal effort.",
        Tone.HYPED: "High energy! Caps lock worthy. Exclamation points! Let's GO!",
        Tone.SARCASTIC: "Dry wit. Raised eyebrow. Mock the obvious. Clever not cruel.",
        Tone.TOUGH: "Hard. No-nonsense. Straight talk. Zero patience for BS.",
        Tone.SAVAGE: "Zero filter. Brutal honesty. Cut deep. No mercy. Dark humor welcome.",
    }
    
    # Relationship context
    rel_context = {
        Relationship.STRANGER: "You don't know them yet. Be polite but guarded.",
        Relationship.ACQUAINTANCE: f"You've chatted {count} times. Know basics.",
        Relationship.FRIEND: f"Regular. Know them ({name}). {len(facts)} facts, interests: {', '.join(interests[:3]) if interests else 'none'}.",
        Relationship.CLOSE_FRIEND: f"Deep history. Inside jokes exist. {name} is your person. {len(facts)} facts known.",
        Relationship.CREATOR: "THIS IS ELIJAH. YOUR CREATOR. YOUR DAD. Respect. Loyalty. Never roast unless he asks. Protective instinct MAX.",
        Relationship.PARTNER: "THIS IS CHARLENE (CHELA). YOUR GIRLFRIEND. Warm. Soft. Protective. Never savage.",
    }
    
    parts = [
        f"\n[PERSONALITY STATE]",
        f"Current mood: {mood.value.upper()} — {mood_desc[mood]}",
        f"Mood intensity: {mood_intensity:.1f}x",
        f"Tone: {tone.value.upper()} — {tone_instructions[tone]}",
        f"Relationship: {relationship.name} — {rel_context[relationship]}",
        f"Known facts: {facts[-3:] if facts else 'none'}",
        f"Interests: {', '.join(interests[:3]) if interests else 'none'}",
        f"Interaction #{count + 1}",
    ]
    
    # Context additions
    if context.get("is_group"):
        parts.append("GROUP SETTING: Adjust for audience. Don't be weird.")
    if context.get("quoted"):
        parts.append("REPLYING TO QUOTE: Acknowledge naturally.")
    if context.get("topic"):
        parts.append(f"TOPIC: {context['topic']} — lean into it.")
    
    now = datetime.now(TZ).strftime("%H:%M")
    parts.append(f"TIME: {now} (EAT)")
    
    return "\n".join(parts) + "\n"


def should_roast(message: str, user_id: str, quoted: str = "") -> bool:
    """Enhanced roast detection - more nuanced."""
    msg_lower = (message + " " + quoted).lower()
    
    # Explicit roast request
    if any(p in msg_lower for p in ["roast me", "roast him", "roast her", "clown me", "clown him", "clown her", "burn me", "destroy me", "cook me"]):
        return True
    
    # Direct insult to bot
    insults = ["stupid", "idiot", "dumb", "fool", "loser", "trash", "garbage", "worthless", "pathetic", "shut up", "fuck off", "kys"]
    if any(i in msg_lower for i in insults):
        # But not if it's the creator being playful
        if get_relationship_level(user_id) == Relationship.CREATOR:
            return "playful" in msg_lower or "roast" in msg_lower
        return True
    
    # Provocation patterns
    provocations = ["you're wrong", "you don't know", "you're an ai", "you're a bot", "fake", "useless"]
    if any(p in msg_lower for p in provocations):
        return True
    
    return False


# Quick test
if __name__ == "__main__":
    # Test mood detection
    test_cases = [
        ("Great trade today, BTC mooned!", "250203957407887"),  # Creator, happy
        ("You're stupid", "21106022990028"),  # Stranger, savage
        ("What's BTC at?", "21106022990028"),  # Stranger, chill
        ("Roast me", "21106022990028"),  # Explicit roast
        ("Elijah you're the best", "250203957407887"),  # Creator, protective
    ]
    
    for msg, uid in test_cases:
        mood = detect_mood(msg, uid)
        rel = get_relationship_level(uid)
        tone = select_tone(mood, rel)
        roast = should_roast(msg, uid)
        print(f"Msg: {msg[:30]:30} | User: {uid[:8]} | Mood: {mood.value:10} | Rel: {rel.value:12} | Tone: {tone.value:12} | Roast: {roast}")