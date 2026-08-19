# Crimsonej Bot (AI Engine) 🤖

The Flask + LLM core powering Crimsonej — a friendly WhatsApp companion with optional trading coach capabilities.

## Quick Start

```bash
cd crimson-bot
python3 install.py    # creates venv, installs deps
crimsonej start       # from parent dir, or: python bot.py server
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/reply` | POST | Main message handler (WhatsApp bridge → bot) |
| `/health` | GET | Health check (chunks, uptime) |
| `/sent_ids` | POST | Bridge reports sent message IDs |
| `/post_status` | POST | Post WhatsApp status (story) |

## Slash Commands

### Core Features
| Command | Description |
|---------|-------------|
| `/help` | Full command list |
| `/imagine <prompt>` | AI image generation (HF/Pollinations) |
| `/sticker <prompt>` | AI sticker generation |
| `/song-audio <query>` | YouTube audio search/download |
| `/song-video <query>` | YouTube video search/download |
| `/reg-img [prompt]` | Image analysis (NVIDIA VLM) |
| `/read [prompt]` | Summarize attached document |
| `/learn [text]` | Store in permanent memory |
| `/respond <prompt>` | Direct reply to quoted message |

### Trading Coach (Add-on — activated by user request)
| Command | Description |
|---------|-------------|
| `/analyze <symbol> [interval]` | Full TA with chart, bias (🟢/🔴/⏸️), levels, patterns |
| `/mtf <symbol>` | Multi-timeframe alignment (Daily/4H/1H) |
| `/patterns <symbol> [interval]` | Detect chart patterns |
| `/walkthrough <symbol> [interval]` | Step-by-step chart breakdown |
| `/teach <topic>` | 13 lessons: candlesticks, structure, risk, RSI, MACD, EMAs, volume, MTF, liquidity, journaling, psychology, sessions |
| `/lessons` | List all lesson topics |
| `/quiz [topic]` | Interactive quiz |
| `/quiz_answer <topic> <0-3>` | Submit quiz answer |
| `/journal log <trade>` | Log trade: symbol side entry sl tp size result [pnl] [r] [setup] [notes] |
| `/journal stats` | Win rate, expectancy, avg R, setup breakdown |
| `/price <symbols...>` | Quick multi-symbol price check |
| `/brief [pre_london\|eod]` | Daily briefing (9 pairs) |
| `/briefing_subscribe [pre_london\|eod\|both] [topics...]` | Group subscription (07:30/21:30 EAT) |
| `/briefing_unsubscribe` | Stop group briefings |
| `/briefing_list` | List active subscriptions |

### Master Control (Creator Only)
| Command | Description |
|---------|-------------|
| `master control chela` | Authenticate as creator |
| `master control status_posting [on/off]` | Toggle status posting |
| `master control status_reply [on/off]` | Toggle status replies |
| `master control scheduler [on/off]` | Toggle background scheduler |
| `master control interval [hours]` | Set posting interval |
| `master control topic add/remove/clear/list` | Manage status topics |
| `master control status_now` | Trigger immediate status post |
| `master control config` | View config |
| `master control wipe cache\|memory` | Reset bot state |

## LLM Tools (31 Total)

### Core (16)
`web_search`, `analyze_image`, `generate_image`, `generate_sticker`, `download_audio`, `download_video`, `post_status`, `update_user_profile`, `update_preferences`, `self_aware`, `run_self_heal`, `schedule_task`, `list_tasks`, `cancel_task`, `run_task`, `manage_watchlist`

### Trading Coach (15) — loaded but only used when user asks
`analyze_market`, `teach_concept`, `list_lessons`, `daily_briefing`, `quick_price`, `trading_quiz`, `quiz_answer`, `live_walkthrough`, `multi_timeframe_analysis`, `detect_patterns`, `journal_trade`, `journal_stats`, `subscribe_briefing`, `unsubscribe_briefing`, `list_briefings`

## Configuration

Edit `config.json` or use `master control config`:

```json
{
  "model": "llama-3.3-70b-versatile",
  "relevance_threshold": 0.08,
  "session_ttl": 1800,
  "session_max_turns": 8,
  "allow_status_posting": true,
  "allow_status_reply": true,
  "status_scheduler_enabled": false,
  "status_scheduler_interval_hours": 4,
  "trading_briefing_enabled": true,
  "trading_briefing_pre_london": "07:30",
  "trading_briefing_eod": "21:30",
  "owner_jid": "256789@s.whatsapp.net"
}
```

Environment (`.env`):
```
GROQ_API_KEY=...
NVIDIA_API_KEY=...      # vision
HF_API_KEY=...          # image gen
BOT_PORT=5000
```

## Project Structure

```
crimson-bot/
├── bot.py                    # Flask app, commands, LLM orchestration
├── core/
│   ├── config.py             # Config loader, logging, constants
│   ├── llm.py                # LLM client (Groq/NVIDIA), tool calling
│   ├── eventlog.py           # Structured event logging
│   ├── market_data.py        # Binance, CoinGecko, yfinance clients
│   ├── trading_ta.py         # Pure-Python TA: RSI, MACD, EMA, patterns, MTF
│   └── chart_render.py       # Matplotlib candlestick charts
├── services/
│   ├── tools.py              # 31 LLM tool definitions + executors
│   ├── trading.py            # Analysis, lessons, journal, briefings, subs
│   ├── trading_scheduler.py  # Daily briefing cron (07:30/21:30 EAT)
│   ├── memory.py             # User profiles, sessions, vaults
│   ├── dispatcher.py         # Background task executor
│   ├── scheduler.py          # Status posting scheduler
│   └── ... (health, reporter, autofix, etc.)
├── data/
│   ├── trading_lessons.json
│   ├── user_watchlists.json
│   ├── trade_journal.json
│   └── briefing_subscriptions.json
├── trading/cache/charts/     # Generated chart PNGs
└── sessions.json / vectors.json / user_profiles.json
```

## Data Sources

| Source | Markets | Auth |
|--------|---------|------|
| Binance Public REST | Crypto (spot + futures) | None |
| CoinGecko | Crypto prices, market cap | None |
| yfinance (Yahoo Finance) | Stocks, forex, indices, commodities, bonds | None |

## Development

```bash
# Run server directly
python bot.py server

# Rebuild RAG index
crimsonej reindex

# View logs
crimsonej logs bot
```