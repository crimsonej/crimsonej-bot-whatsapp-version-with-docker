"""
services/events.py
==================
Holiday, event, and cultural awareness for Crimsonej.
Provides contextual awareness of dates, holidays, market events, and cultural moments.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, date, timedelta
from typing import Any

from core.config import BASE_DIR, TZ, log


# ─── Configuration ──────────────────────────────────────────────────────────

_EVENTS_FILE = os.path.join(BASE_DIR, "events_calendar.json")
_events_lock = threading.Lock()
_events_cache: dict[str, Any] = {}


# ─── Static Holiday Data ────────────────────────────────────────────────────

# Fixed date holidays (month, day)
FIXED_HOLIDAYS = {
    (1, 1): "New Year's Day",
    (2, 14): "Valentine's Day",
    (3, 17): "St. Patrick's Day",
    (4, 1): "April Fools' Day",
    (5, 1): "Labour Day / May Day",
    (5, 4): "Star Wars Day",
    (6, 19): "Juneteenth",
    (7, 4): "US Independence Day",
    (8, 15): "Assumption / Ferragosto",
    (10, 31): "Halloween",
    (11, 11): "Veterans Day / Armistice Day",
    (12, 25): "Christmas Day",
    (12, 26): "Boxing Day",
    (12, 31): "New Year's Eve",
}

# Variable holidays (computed per year)
def get_variable_holidays(year: int) -> dict[tuple[int, int], str]:
    """Calculate variable date holidays for a given year."""
    import calendar
    
    holidays = {}
    
    # Easter calculation (Western)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = date(year, month, day)
    holidays[(month, day)] = "Easter Sunday"
    
    # Good Friday
    good_friday = easter - timedelta(days=2)
    holidays[(good_friday.month, good_friday.day)] = "Good Friday"
    
    # Easter Monday
    easter_monday = easter + timedelta(days=1)
    holidays[(easter_monday.month, easter_monday.day)] = "Easter Monday"
    
    # US Thanksgiving (4th Thursday of November)
    nov_cal = calendar.monthcalendar(year, 11)
    thanksgiving = nov_cal[3][calendar.THURSDAY] if nov_cal[0][calendar.THURSDAY] else nov_cal[4][calendar.THURSDAY]
    holidays[(11, thanksgiving)] = "Thanksgiving"
    
    # Black Friday
    black_friday = thanksgiving + 1
    holidays[(11, black_friday)] = "Black Friday"
    
    # Cyber Monday
    cyber_monday = thanksgiving + 4
    holidays[(11, cyber_monday)] = "Cyber Monday"
    
    # Mother's Day (2nd Sunday May)
    may_cal = calendar.monthcalendar(year, 5)
    mothers_day = may_cal[1][calendar.SUNDAY]
    holidays[(5, mothers_day)] = "Mother's Day"
    
    # Father's Day (3rd Sunday June)
    jun_cal = calendar.monthcalendar(year, 6)
    fathers_day = jun_cal[2][calendar.SUNDAY]
    holidays[(6, fathers_day)] = "Father's Day"
    
    return holidays


# Market events calendar
MARKET_EVENTS = {
    "monthly": {
        1: ["NFP (first Friday)", "FOMC minutes"],
        2: ["NFP", "FOMC meeting"],
        3: ["NFP", "FOMC minutes", "Quadruple witching"],
        4: ["NFP", "FOMC meeting", "Tax deadline (US)"],
        5: ["NFP", "FOMC minutes"],
        6: ["NFP", "FOMC meeting", "Quadruple witching"],
        7: ["NFP", "FOMC minutes"],
        8: ["NFP", "FOMC meeting", "Jackson Hole Symposium"],
        9: ["NFP", "FOMC minutes", "Quadruple witching"],
        10: ["NFP", "FOMC meeting"],
        11: ["NFP", "FOMC minutes"],
        12: ["NFP", "FOMC meeting", "Quadruple witching"],
    },
    "recurring": {
        "first_friday": "US Non-Farm Payrolls (NFP)",
        "fomc_meeting": "FOMC Rate Decision (8x/year)",
        "cpi_release": "US CPI (monthly, mid-month)",
        "ppi_release": "US PPI (monthly)",
        "retail_sales": "US Retail Sales (monthly)",
        "unemployment": "Initial Jobless Claims (weekly, Thursday)",
        "fed_speeches": "Fed Speaker Schedule",
        "ecb_meeting": "ECB Rate Decision (6x/year)",
        "boe_meeting": "BoE Rate Decision (8x/year)",
        "boj_meeting": "BoJ Rate Decision (8x/year)",
        "opec_meeting": "OPEC+ Meeting (monthly)",
        "earnings_season": "Earnings Season (Jan, Apr, Jul, Oct)",
    }
}

# Cultural/Sports events
CULTURAL_EVENTS = {
    "annual": {
        (1, 1): "New Year's Day",
        (2, 2): "Super Bowl Sunday (varies)",
        (2, 14): "Valentine's Day",
        (3, 17): "St. Patrick's Day",
        (4, 1): "April Fools'",
        (5, 4): "May the 4th (Star Wars)",
        (5, 5): "Cinco de Mayo",
        (6, 19): "Juneteenth",
        (7, 4): "US Independence Day",
        (8, 15): "Ferragosto",
        (10, 31): "Halloween",
        (11, 11): "Veterans Day",
        (11, 28): "Thanksgiving (varies)",
        (12, 25): "Christmas",
        (12, 31): "New Year's Eve",
    },
    "sports": {
        "super_bowl": "Super Bowl (early Feb)",
        "march_madness": "NCAA March Madness (Mar)",
        "nba_finals": "NBA Finals (Jun)",
        "stanley_cup": "Stanley Cup Finals (Jun)",
        "world_cup": "FIFA World Cup (every 4 years)",
        "olympics": "Olympics (every 4 years)",
        "uefa_cl": "Champions League Final (late May/early Jun)",
        "f1_monaco": "Monaco GP (late May)",
        "f1_british": "British GP (Jul)",
        "f1_italian": "Italian GP (Sep)",
        "f1_abu_dhabi": "Abu Dhabi GP (Nov/Dec)",
    },
    "crypto": {
        "bitcoin_halving": "Bitcoin Halving (every 4 years, next ~2028)",
        "ethereum_upgrade": "Ethereum Upgrades (periodic)",
        "defi_summer_anniversary": "DeFi Summer Anniversary (Jun)",
    }
}

# Personal events (stored per user)
_PERSONAL_EVENTS_FILE = os.path.join(BASE_DIR, "personal_events.json")
_personal_lock = threading.Lock()
_personal_events: dict[str, list[dict]] = {}  # user_id -> [events]

def _load_personal_events() -> None:
    global _personal_events
    if os.path.exists(_PERSONAL_EVENTS_FILE):
        try:
            with open(_PERSONAL_EVENTS_FILE, "r", encoding="utf-8") as f:
                _personal_events = json.load(f)
        except Exception:
            _personal_events = {}

def _save_personal_events() -> None:
    with _personal_lock:
        with open(_PERSONAL_EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_personal_events, f, ensure_ascii=False, indent=2)

_load_personal_events()


# ─── Event Detection ────────────────────────────────────────────────────────

def get_today_events() -> list[dict]:
    """Get all events happening today."""
    today = datetime.now(TZ).date()
    month_day = (today.month, today.day)
    year = today.year
    
    events = []
    
    # Fixed holidays
    if month_day in FIXED_HOLIDAYS:
        events.append({
            "type": "holiday",
            "name": FIXED_HOLIDAYS[month_day],
            "category": "fixed",
        })
    
    # Variable holidays
    var_holidays = get_variable_holidays(year)
    if month_day in var_holidays:
        events.append({
            "type": "holiday",
            "name": var_holidays[month_day],
            "category": "variable",
        })
    
    # Market events (check recurring)
    events.extend(get_market_events_today())
    
    # Cultural events
    events.extend(get_cultural_events_today())
    
    return events


def get_market_events_today() -> list[dict]:
    """Get market events happening today."""
    today = datetime.now(TZ).date()
    events = []
    
    # Check recurring market events
    # NFP - first Friday
    if today.weekday() == 4 and today.day <= 7:  # First Friday
        events.append({
            "type": "market",
            "name": "US Non-Farm Payrolls (NFP)",
            "category": "economic_data",
            "impact": "high",
            "time": "13:30 UTC",
        })
    
    return events


def get_cultural_events_today() -> list[dict]:
    """Get cultural/sports events today."""
    today = datetime.now(TZ).date()
    month_day = (today.month, today.day)
    events = []
    
    if month_day in FIXED_HOLIDAYS:
        events.append({
            "type": "cultural",
            "name": FIXED_HOLIDAYS[month_day],
            "category": "holiday",
        })
    
    return events


def get_upcoming_events(days: int = 7) -> list[dict]:
    """Get events in the next N days."""
    today = datetime.now(TZ).date()
    events = []
    
    for i in range(days + 1):
        check_date = today + timedelta(days=i)
        month_day = (check_date.month, check_date.day)
        
        if month_day in FIXED_HOLIDAYS:
            events.append({
                "date": check_date.isoformat(),
                "name": FIXED_HOLIDAYS[month_day],
                "type": "holiday",
            })
    
    return events


def get_event_context() -> str:
    """Get formatted event context for system prompt."""
    events = get_today_events()
    if not events:
        return ""
    
    parts = ["\n[TODAY'S EVENTS]"]
    for event in events:
        if event["type"] == "holiday":
            parts.append(f"🎉 {event['name']}")
        elif event["type"] == "market":
            parts.append(f"📊 {event['name']} ({event.get('impact', 'medium')} impact)")
        elif event["type"] == "cultural":
            parts.append(f"🎭 {event['name']}")
    
    upcoming = get_upcoming_events(7)
    if upcoming:
        parts.append("\n[UPCOMING]")
        for event in upcoming[:5]:
            parts.append(f"  {event['date']}: {event['name']}")
    
    return "\n".join(parts) + "\n"


def get_market_context() -> str:
    """Get market-specific context for trading conversations."""
    today = datetime.now(TZ)
    parts = []
    
    # Market session
    hour = today.hour
    if 13 <= hour < 22:  # London/NY overlap
        parts.append("🟢 London/NY overlap - HIGH VOLUME")
    elif 8 <= hour < 13:  # London
        parts.append("🟡 London session - MODERATE VOLUME")
    elif 22 <= hour or hour < 1:  # NY
        parts.append("🔴 NY session - MODERATE VOLUME")
    else:
        parts.append("⚫ Asian/Dead hours - LOW VOLUME")
    
    # Day of week
    weekday = today.weekday()
    if weekday == 0:
        parts.append("📅 Monday - Weekly open, watch for gaps")
    elif weekday == 4:
        parts.append("📅 Friday - Weekly close, position management")
    elif weekday >= 5:
        parts.append("📅 Weekend - Markets closed (crypto 24/7)")
    
    # Month context
    month = today.month
    if month in [3, 6, 9, 12]:
        parts.append(f"📅 Q{((month-1)//3)+1} end - Quadruple witching risk")
    
    return "\n".join(["\n[MARKET CONTEXT]"] + parts) + "\n" if parts else ""


# ─── Personal Events ────────────────────────────────────────────────────────

_PERSONAL_EVENTS_FILE = os.path.join(BASE_DIR, "personal_events.json")
_personal_lock = threading.Lock()
_personal_events: dict[str, list[dict]] = {}  # user_id -> [events]

def _load_personal_events() -> None:
    global _personal_events
    if os.path.exists(_PERSONAL_EVENTS_FILE):
        try:
            with open(_PERSONAL_EVENTS_FILE, "r", encoding="utf-8") as f:
                _personal_events = json.load(f)
        except Exception:
            _personal_events = {}

def _save_personal_events() -> None:
    with _personal_lock:
        with open(_PERSONAL_EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_personal_events, f, ensure_ascii=False, indent=2)

_load_personal_events()


def add_personal_event(user_id: str, name: str, event_date: str, category: str = "personal", recurring: bool = False) -> dict:
    """Add personal event for user."""
    with _personal_lock:
        if user_id not in _personal_events:
            _personal_events[user_id] = []
        
        event = {
            "name": name,
            "date": event_date,
            "category": category,
            "recurring": recurring,
            "created_at": datetime.now(TZ).isoformat(),
        }
        _personal_events[user_id].append(event)
        _save_personal_events()
        return event


def get_personal_events(user_id: str, days_ahead: int = 30) -> list[dict]:
    """Get upcoming personal events for user."""
    if user_id not in _personal_events:
        return []
    
    today = datetime.now(TZ).date()
    cutoff = today + timedelta(days=days_ahead)
    
    events = []
    for event in _personal_events[user_id]:
        try:
            event_date = datetime.fromisoformat(event["date"]).date()
            if today <= event_date <= cutoff:
                events.append(event)
        except Exception:
            pass
    
    return sorted(events, key=lambda x: x["date"])


def get_personal_event_context(user_id: str) -> str:
    """Get personal event context for system prompt."""
    events = get_personal_events(user_id, 7)
    if not events:
        return ""
    
    parts = ["\n[PERSONAL EVENTS THIS WEEK]"]
    for event in events:
        parts.append(f"  {event['date']}: {event['name']} ({event['category']})")
    
    return "\n".join(parts) + "\n"


# ─── Quick Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    events = get_today_events()
    print("Today's events:")
    for e in events:
        print(f"  {e['type']}: {e['name']}")
    
    print("\nMarket context:")
    print(get_market_context())
    
    print("\nUpcoming (7 days):")
    for e in get_upcoming_events(7):
        print(f"  {e['date']}: {e['name']}")