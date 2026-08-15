"""
profiles.py
===========
Persistent user profile & relationship memory for Crimsonej.

Each contact gets a profile keyed by phone number. The profile stores:
  - name (WhatsApp push name, auto-updated)
  - nicknames (names the bot has given or learned)
  - facts (list of things learned about this person)
  - interests (topics they talk about frequently)
  - relationship (how the bot relates to this person)
  - interaction_count (how many times they've chatted)
  - last_seen / first_seen timestamps
  - is_creator flag
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_FACTS = 50      # Keep profile data bounded
MAX_INTERESTS = 20


class ProfileManager:
    def __init__(self, filename="user_profiles.json"):
        self.filename = filename
        self.profiles: dict[str, dict] = {}
        self.load()

    def load(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    self.profiles = json.load(f)
            except Exception as e:
                logger.error(f"Error loading profiles: {e}")
                self.profiles = {}

    def save(self):
        try:
            with open(self.filename, 'w') as f:
                json.dump(self.profiles, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving profiles: {e}")

    def get_profile(self, user_id: str) -> dict:
        """Get or create a profile for user_id (phone number string)."""
        if user_id not in self.profiles:
            self.profiles[user_id] = {
                "phone": user_id,
                "name": None,
                "nicknames": [],
                "facts": [],
                "interests": [],
                "relationship": None,   # e.g. "friend", "acquaintance", "close friend"
                "preferences": {},
                "interaction_count": 0,
                "last_seen": None,
                "first_seen": datetime.now().isoformat(),
                "is_creator": False,
            }
        return self.profiles[user_id]

    def touch(self, user_id: str, push_name: str | None = None) -> dict:
        """
        Record an interaction: bump interaction_count, update last_seen,
        and auto-learn the WhatsApp push_name if provided.
        Returns the profile dict.
        """
        profile = self.get_profile(user_id)
        profile["interaction_count"] = profile.get("interaction_count", 0) + 1
        profile["last_seen"] = datetime.now().isoformat()

        # Auto-learn WhatsApp display name
        if push_name and push_name.strip():
            clean_name = push_name.strip()
            current_name = profile.get("name")
            if not current_name:
                profile["name"] = clean_name
                logger.info("[Profile] Learned name for %s: %s", user_id, clean_name)
            elif current_name != clean_name:
                # Name changed — store old one as a nickname, update current
                nicks = profile.get("nicknames", [])
                if current_name not in nicks:
                    nicks.append(current_name)
                    profile["nicknames"] = nicks[-5:]   # keep last 5 old names
                profile["name"] = clean_name
                logger.info("[Profile] Updated name for %s: %s → %s", user_id, current_name, clean_name)

        # Auto-save periodically (every 5 interactions)
        if profile["interaction_count"] % 5 == 0:
            self.save()

        return profile

    def update_profile(self, user_id: str, **kwargs):
        profile = self.get_profile(user_id)
        profile.update(kwargs)
        profile["last_seen"] = datetime.now().isoformat()
        self.save()

    def add_fact(self, user_id: str, fact: str):
        """Add a fact about this user (deduped, bounded)."""
        profile = self.get_profile(user_id)
        facts = profile.get("facts", [])
        # Simple dedup: skip if already present or very similar
        fact_lower = fact.lower().strip()
        if any(fact_lower in f.lower() or f.lower() in fact_lower for f in facts):
            return
        facts.append(fact.strip())
        if len(facts) > MAX_FACTS:
            facts = facts[-MAX_FACTS:]
        profile["facts"] = facts
        self.save()

    def add_interest(self, user_id: str, interest: str):
        """Track a topic this user talks about."""
        profile = self.get_profile(user_id)
        interests = profile.get("interests", [])
        interest_lower = interest.lower().strip()
        if interest_lower not in [i.lower() for i in interests]:
            interests.append(interest.strip())
            if len(interests) > MAX_INTERESTS:
                interests = interests[-MAX_INTERESTS:]
            profile["interests"] = interests
            self.save()

    def set_name(self, user_id: str, name: str):
        profile = self.get_profile(user_id)
        profile["name"] = name
        self.save()

    def set_relationship(self, user_id: str, relationship: str):
        profile = self.get_profile(user_id)
        profile["relationship"] = relationship
        self.save()

    def get_context_string(self, user_id: str) -> str:
        """
        Build a rich context string for the system prompt,
        so the AI knows exactly who it's talking to.
        """
        profile = self.get_profile(user_id)
        parts = []

        name = profile.get("name")
        if name:
            parts.append(f"Name: {name}")
            nicks = profile.get("nicknames", [])
            if nicks:
                parts.append(f"Also known as: {', '.join(nicks)}")

        phone = profile.get("phone", "")
        if phone:
            parts.append(f"Phone: {phone}")

        relationship = profile.get("relationship")
        if relationship:
            parts.append(f"Relationship: {relationship}")

        count = profile.get("interaction_count", 0)
        if count > 0:
            # Describe familiarity level
            if count < 5:
                parts.append("Familiarity: New contact (just met)")
            elif count < 20:
                parts.append("Familiarity: Getting to know each other")
            elif count < 100:
                parts.append("Familiarity: Regular contact")
            else:
                parts.append("Familiarity: Close / frequent contact")

        facts = profile.get("facts", [])
        if facts:
            parts.append(f"Known facts: {' | '.join(facts[-15:])}")

        interests = profile.get("interests", [])
        if interests:
            parts.append(f"Interests: {', '.join(interests[-10:])}")

        first_seen = profile.get("first_seen")
        if first_seen:
            parts.append(f"First interaction: {first_seen[:10]}")

        if not parts:
            return ""

        return "\n[USER PROFILE]\n" + "\n".join(parts) + "\n[/USER PROFILE]\n"

    def get_all_known_names(self) -> dict[str, str]:
        """Return a dict of {phone: name} for all contacts with known names."""
        return {
            uid: p.get("name", uid)
            for uid, p in self.profiles.items()
            if p.get("name")
        }
