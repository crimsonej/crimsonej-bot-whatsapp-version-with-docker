# Crimsonej Bot (AI Engine) 🤖

The Flask + LLM core powering Crimsonej — a friendly WhatsApp companion with optional trading coach capabilities.

## Quick Start (Local)

```bash
cd crimson-bot
python3 install.py    # creates venv, installs deps
crimsonej start       # from parent dir, or: python bot.py server
```

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/reply` | POST | Bearer | Main message handler (WhatsApp bridge → bot) |
| `/health` | GET | — | Health check (chunks, uptime, constraints) |
| `/sent_ids` | POST | Bearer | Bridge reports sent message IDs |
| `/post_status` | POST | Bearer | Post WhatsApp status (story) |
| `/sync_status` | GET | — | GitHub backup sync status |
| `/sync_now` | POST | — | Trigger manual GitHub sync |

## Request Format

```json
{
  "message": "user text",
  "sender": "256742184690@s.whatsapp.net",
  "user_phone": "256742184690",
  "push_name": "Elijah",
  "group_name": "12345@g.us",
  "quoted_message": "quoted text",
  "quoted_author": "name",
  "quoted_author_jid": "jid@s.whatsapp.net",
  "edited": false,
  "deleted": false,
  "message_id": "msg_id",
  "document_data": "base64...",
  "document_name": "file.pdf",
  "document_mimetype": "application/pdf",
  "read_command": false,
  "learn_command": false,
  "image_base64": "base64...",
  "is_status": false
}
```

## Response Format

```json
{
  "reply": "text response",
  "image": "/path/to/image.png",
  "sticker": "base64...",
  "audio": "/path/to/audio.mp3",
  "video": "/path/to/video.mp4",
  "filename": "file.pdf",
  "file_path": "/path/to/file",
  "file_format": "pdf"
}
```

## Slash Commands

### Core Features
| Command | Description |
|---------|-------------|
| `/help` | Full command list |
| `/imagine <prompt>` | AI image generation (NVIDIA Flux/HF) |
| `/sticker <prompt>` | AI sticker generation |
| `/song-audio <query>` | YouTube audio search/download |
| `/song-video <query>` | YouTube video search/download |
| `/reg-img [prompt]` | Image analysis (NVIDIA VLM) |
| `/read [prompt]` | Summarize attached document |
| `/learn [text]` | Store in permanent memory |
| `/respond <prompt>` | Direct reply to quoted message |

### Trading Coach (Add-on)
| Command | Description |
|---------|-------------|
| `/analyze <symbol> [interval]` | Full TA with chart, bias, levels |
| `/mtf <symbol>` | Multi-timeframe (Daily/4H/1H) |
| `/patterns <symbol> [interval]` | Detect chart patterns |
| `/walkthrough <symbol> [interval]` | Step-by-step chart breakdown |
| `/teach <topic>` | 13 lessons: candlesticks, structure, risk, RSI, MACD, etc. |
| `/lessons` | List all lesson topics |
| `/quiz [topic]` | Interactive quiz |
| `/quiz_answer <topic> <0-3>` | Submit quiz answer |
| `/journal log <trade>` | Log trade with R-multiples |
| `/journal stats` | Win rate, expectancy, setup breakdown |
| `/price <symbols...>` | Quick multi-symbol price check |
| `/brief [pre_london\|eod]` | Daily briefing (9 pairs) |
| `/briefing_subscribe [session] [pairs]` | Group subscription (07:30/21:30 EAT) |
| `/briefing_unsubscribe` | Stop group briefings |
| `/briefing_list` | List active subscriptions |

### Group Commands
| Command | Description |
|---------|-------------|
| `/group_fact <text>` | Store fact in group memory |
| `/group_facts` | View group memory vault |
| `/group_forget` | Clear group memory (creator only) |

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

### GitHub (6)
`search_github`, `github_create_file`, `github_update_file`, `github_get_file`, `github_upsert_file`, `github_list_files`

### Trading Coach (15)
`analyze_market`, `teach_concept`, `list_lessons`, `daily_briefing`, `quick_price`, `trading_quiz`, `quiz_answer`, `live_walkthrough`, `multi_timeframe_analysis`, `detect_patterns`, `journal_trade`, `journal_stats`, `subscribe_briefing`, `unsubscribe_briefing`, `list_briefings`

### Research & Media (4)
`deep_research`, `fetch_url_content`, `get_youtube_transcript`, `search_reddit`

## Configuration

Edit `config.json` or use `master control config`:

```json
{
  "providers": {
    "nvidia": "nvapi-xxx",
    "groq": "gsk-xxx",
    "huggingface": "hf-xxx"
  },
  "active_model": "nvidia/nemotron-3-super-120b-a12b",
  "port": 5000,
  "owner_jid": "256742184690@s.whatsapp.net",
  "relevance_threshold": 0.03,
  "session_ttl": 7200,
  "session_max_turns": 20,
  "emoji_max_per_reply": 3,
  "emoji_allow_in_roast": 5,
  "thinking_variability_ms": 500,
  "github_sync_interval_sec": 3600,
  "github_backup_repo": "crimsonej/crimsonej-data",
  "trading_briefing_enabled": true,
  "trading_briefing_pre_london": "07:30",
  "trading_briefing_eod": "21:30",
  "group_rate_limit_per_min": 15
}
```

Environment overrides (uppercase): `SESSION_TTL=3600`, `EMOJI_MAX_PER_REPLY=5`

## Data Persistence

### SQLite (`/data/crimson.db`)
| Table | Description |
|-------|-------------|
| `sessions` | Conversation history per user/group |
| `profiles` | User profiles: name, facts, interests, relationship |
| `vaults` | Personal (`user:<phone>`) + global knowledge |
| `vectors` | RAG chunks with owner/group metadata |
| `vectors_fts` | FTS5 full-text search index |
| `cache` | Key-value with TTL |
| `sent_messages` | Last sent message per chat (for edits/deletes) |

### GitHub Backup (`github_backup_repo`)
```
backup/
├── profiles.json     # All user profiles
├── sessions.json     # Active sessions
├── vaults.json       # Personal + global vaults
├── vectors.json      # Recent 5000 RAG chunks
└── meta.json         # Sync timestamps
```
- **Pull**: On startup (restores state)
- **Push**: Every hour (configurable)

## Smart Optimization

Bot detects environment constraints and adapts:

| Constraint | Optimization |
|------------|--------------|
| Low disk | Stream media, 10MB cap, WebP, compress GitHub |
| Low memory | 256x256 images, 10 steps, external API |
| Low CPU | 1 background worker, longer intervals |
| No internet | Cache + local knowledge, queue GitHub |
| No DNS | Same as no internet |

Check: `GET /health` → shows constraints

## Project Structure

```
crimson-bot/
├── bot.py                 # Flask app, commands, LLM orchestration
├── config.json            # Runtime config
├── crimson.db             # SQLite database
├── requirements.txt       # Python deps
├── .env                   # Secrets
├── core/
│   ├── config.py          # Config loader, logging
│   ├── llm.py             # Multi-provider LLM client
│   ├── eventlog.py        # Structured event logging
│   ├── market_data.py     # Binance/CoinGecko/yfinance
│   ├── trading_ta.py      # Pure-Python TA
│   └── chart_render.py    # Matplotlib charts
├── services/
│   ├── tools.py           # 31 LLM tool definitions
│   ├── memory.py          # Profiles, sessions (→ storage)
│   ├── storage.py         # SQLite persistence
│   ├── rag.py             # RAG chunking + indexing
│   ├── github_search.py   # GitHub search + write
│   ├── github_sync.py     # Backup pull/push
│   ├── environment.py     # Constraint detection
│   ├── trading.py         # Analysis, lessons, journal
│   ├── dispatcher.py      # Background task engine
│   ├── health.py          # Health checks + heartbeat
│   ├── media.py           # YouTube search/download
│   ├── vision.py          # NVIDIA VLM + image gen
│   └── ... (25+ services)
└── data/                  # Docs, charts, trading cache
```

## Health Check

```bash
curl http://localhost:5000/health
```

Response includes: dispatcher status, task stats, bridge connectivity, environment constraints.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NVIDIA_API_KEY` | Yes | Vision + primary LLM |
| `GROQ_API_KEY` | Yes | Fallback LLM |
| `HF_API_KEY` | Optional | Image generation fallback |
| `GITHUB_TOKEN` | For backup | PAT with `repo` scope |
| `GITHUB_BACKUP_REPO` | For backup | `owner/repo` |
| `CRIMSON_API_TOKEN` | Yes | Shared secret for bridge |
| `CREATOR_PHONE` | Yes | Your WhatsApp (digits) |
| `BOT_PORT` | Auto | Railway/Render injects $PORT |

## Development

```bash
# Rebuild RAG index
python -c "from services.rag import build_index_from_docs; build_index_from_docs(force=True)"

# Migrate JSON → SQLite
python -c "from services.storage import migrate_from_json; migrate_from_json()"

# Test LLM
python -c "from core.llm import call_llm; print(call_llm([{'role':'user','content':'hi'}]))"
```