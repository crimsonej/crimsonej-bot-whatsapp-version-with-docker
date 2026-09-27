# Crimsonej Bot — Complete Documentation

## Overview

Crimsonej is a WhatsApp AI companion with trading coach capabilities, built as a two-service architecture:
- **crimson-bot** — Flask + LLM core (Python 3.11)
- **whatsapp-bridge** — Baileys WhatsApp Web client (Node.js 20)

## Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   WhatsApp      │────▶│  whatsapp-bridge │────▶│   crimson-bot   │
│   (User)        │     │   (Node.js)      │     │   (Python)      │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                              │                       │
                              ▼                       ▼
                        ┌─────────────────┐     ┌─────────────────┐
                        │   Persistent    │     │   SQLite +      │
                        │   Volumes       │     │   GitHub Sync   │
                        └─────────────────┘     └─────────────────┘
```

## Quick Start (Railway)

### 1. Prerequisites
- Railway account
- GitHub repo with this code
- NVIDIA API key (for vision/LLM)
- Groq API key (fallback LLM)
- GitHub Personal Access Token with `repo` scope (for backup sync)

### 2. Deploy to Railway

**Option A: One-click (recommended)**
1. Fork this repo
2. In Railway: New Project → Deploy from GitHub → Select fork
3. Railway auto-detects `railway.toml` and creates two services

**Option B: Manual**
1. Create two services in Railway:
   - `crimson-bot` → Dockerfile: `crimson-bot/Dockerfile`
   - `whatsapp-bridge` → Dockerfile: `whatsapp-bridge/Dockerfile`
2. Add volumes: `bot_data`→`/data`, `bridge_data`→`/data`, `media_data`→`/media`
3. Link services (bridge depends on bot)

### 3. Required Environment Variables

#### crimson-bot service
```bash
# LLM Providers (at least one required)
NVIDIA_API_KEY=nvapi-xxxxx              # Primary: vision + LLM
GROQ_API_KEY=gsk_xxxxx                  # Fallback LLM
HF_API_KEY=hf_xxxxx                     # Optional: image generation

# GitHub Backup Sync
GITHUB_TOKEN=ghp_xxxxx                  # PAT with 'repo' scope
GITHUB_BACKUP_REPO=crimsonej/crimsonej-data  # Private repo for backups

# Security
CRIMSON_API_TOKEN=shared-secret-here    # Must match bridge
CREATOR_PHONE=256742184690              # Your WhatsApp (digits only)

# Optional
BOT_PORT=5000                           # Railway overrides with $PORT
```

#### whatsapp-bridge service
```bash
PORT=7860                                 # Railway overrides with $PORT
AI_SERVER=http://crimson-bot:5000/reply  # Internal DNS
AUTH_DIR=/data/auth_info_baileys
MEDIA_ROOT=/media
CRIMSON_API_TOKEN=shared-secret-here     # Must match bot
```

### 4. First Run
1. Deploy both services
2. Check `whatsapp-bridge` logs for QR code
3. Scan with WhatsApp → bot is live
4. Send `/help` to see all commands

## Features

### Core AI
- **Natural conversation** — No bot-like narration, adaptive personality
- **Persistent memory** — SQLite + GitHub backup survives restarts
- **Smart optimization** — Adapts to CPU/memory/disk constraints
- **Web search** — DuckDuckGo with caching
- **Image analysis** — NVIDIA VLM
- **Image/sticker generation** — Flux/NVIDIA

### Media
- **YouTube audio/video** — Search, preview, download
- **Document processing** — PDF, DOCX, XLSX, PPTX parsing
- **URL auto-extraction** — Reads linked pages automatically

### Trading Coach (opt-in)
| Command | Description |
|---------|-------------|
| `/analyze BTC [1h]` | Full TA with chart, bias, levels |
| `/mtf BTC` | Multi-timeframe (D/4H/1H) |
| `/patterns BTC [4h]` | Chart pattern detection |
| `/walkthrough BTC [4h]` | Step-by-step chart breakdown |
| `/teach <topic>` | 13 lessons: candlesticks, risk, RSI, MACD, etc. |
| `/quiz [topic]` | Interactive trading quiz |
| `/journal log <trade>` | Log trades with R-multiples |
| `/journal stats` | Win rate, expectancy, setup breakdown |
| `/price BTC ETH` | Quick multi-symbol price check |
| `/brief [pre_london\|eod]` | Daily briefing (9 pairs) |
| `/briefing_subscribe` | Group briefings at 07:30/21:30 EAT |

### Group Features
- `/group_fact <text>` — Store fact in group memory
- `/group_facts` — View group memory vault
- `/group_forget` — Clear vault (creator only)
- Mention-aware replies
- Multi-bot conflict avoidance
- Rate limiting (15/min default)

### Creator Commands
```
master control chela                    # Authenticate
master control status_posting on/off    # Toggle status posts
master control status_reply on/off      # Toggle status replies
master control scheduler on/off         # Background scheduler
master control interval 4               # Post interval (hours)
master control topic add "BTC"          # Add status topic
master control status_now               # Post immediately
master control config                   # View config
master control wipe cache/memory        # Reset state
```

## API Endpoints

### crimson-bot
| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/reply` | POST | Bearer | Main message handler |
| `/health` | GET | — | Health check (chunks, uptime) |
| `/sent_ids` | POST | Bearer | Bridge reports sent message IDs |
| `/post_status` | POST | Bearer | Post WhatsApp status (story) |
| `/sync_status` | GET | — | GitHub backup sync status |
| `/sync_now` | POST | — | Trigger manual GitHub sync |

### Request Format (bridge → bot)
```json
{
  "message": "user text",
  "sender": "256742184690@s.whatsapp.net",
  "user_phone": "256742184690",
  "push_name": "Elijah",
  "group_name": "12345@g.us",          // optional
  "group_admins": ["admin@s.whatsapp.net"],  // optional
  "quoted_message": "quoted text",    // optional
  "quoted_author": "name",            // optional
  "quoted_author_jid": "jid@s.whatsapp.net", // optional
  "edited": false,                    // message edit
  "deleted": false,                   // message delete
  "message_id": "msg_id",             // for edit/delete
  "document_data": "base64...",       // optional
  "document_name": "file.pdf",        // optional
  "document_mimetype": "application/pdf", // optional
  "read_command": false,              // /read trigger
  "learn_command": false,             // /learn trigger
  "image_base64": "base64...",        // optional
  "is_status": false                  // status reply
}
```

### Response Format
```json
{
  "reply": "text response",
  "image": "/path/to/image.png",       // optional
  "sticker": "base64...",              // optional
  "audio": "/path/to/audio.mp3",       // optional
  "video": "/path/to/video.mp4",       // optional
  "filename": "file.pdf",              // optional
  "file_path": "/path/to/file",        // optional
  "file_format": "pdf"                 // optional
}
```

## Configuration

### config.json (persisted in /data)
```json
{
  "providers": {
    "nvidia": "nvapi-xxx",
    "groq": "gsk-xxx",
    "huggingface": "hf-xxx"
  },
  "models": [
    {"id": "nvidia/nemotron-3-super-120b-a12b", "provider": "nvidia", "tier": "primary"},
    {"id": "nvidia/nemotron-3.5-lightning-30b-a3b", "provider": "nvidia", "tier": "scout"},
    {"id": "llama-3.3-70b-versatile", "provider": "groq", "tier": "fallback"}
  ],
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

### Override via Environment
Any config key can be overridden via environment variable (uppercase):
```bash
SESSION_TTL=3600
EMOJI_MAX_PER_REPLY=5
```

## Data Persistence

### SQLite Database (`/data/crimson.db`)
| Table | Description |
|-------|-------------|
| `sessions` | Conversation history per user/group |
| `profiles` | User profiles: name, facts, interests, relationship |
| `vaults` | Personal (`user:<phone>`) + global knowledge |
| `vectors` | RAG chunks with owner/group metadata |
| `vectors_fts` | FTS5 full-text search index |
| `cache` | Key-value with TTL |
| `sent_messages` | Last sent message per chat (for edits/deletes) |
| `schema_version` | Migration tracking |

### GitHub Backup (`GITHUB_BACKUP_REPO`)
```
backup/
├── profiles.json     # All user profiles
├── sessions.json     # Active sessions
├── vaults.json       # Personal + global vaults
├── vectors.json      # Recent 5000 RAG chunks
└── meta.json         # Sync timestamps
```
- **Pull**: On startup (restores state)
- **Push**: Every hour (configurable via `github_sync_interval_sec`)

## Smart Optimization

The bot detects environment constraints at startup and optimizes automatically:

| Constraint | Optimization Applied |
|------------|---------------------|
| Low disk (<0.5GB) | Stream media, 10MB cap, WebP images, compress GitHub |
| Low memory (<512MB) | 256x256 images, 10 steps, external API, smaller vectors |
| Low CPU (<1 core) | 1 background worker, longer intervals |
| No internet | Cache + local knowledge, queue GitHub, describe images |
| No DNS | Same as no internet |

Check status: `GET /health` shows constraints.

## Development

### Local Run
```bash
cd crimson-bot
python3 install.py          # Creates venv, installs deps
crimsonej start             # From parent dir
# OR
python bot.py server
```

### Rebuild RAG Index
```bash
crimsonej reindex
```

### View Logs
```bash
crimsonej logs bot
crimsonej logs bridge
```

### Project Structure
```
crimson-bot/
├── bot.py                 # Flask app, commands, LLM orchestration
├── config.json            # Runtime config (persisted)
├── crimson.db             # SQLite database
├── requirements.txt       # Python deps
├── .env                   # Secrets (not committed)
├── core/
│   ├── config.py          # Config loader, logging
│   ├── llm.py             # Multi-provider LLM client
│   ├── eventlog.py        # Structured event logging
│   ├── market_data.py     # Binance/CoinGecko/yfinance
│   ├── trading_ta.py      # Pure-Python TA (RSI, MACD, patterns)
│   └── chart_render.py    # Matplotlib candlestick charts
├── services/
│   ├── tools.py           # 31 LLM tool definitions + executors
│   ├── memory.py          # Profiles, sessions, vaults (→ storage)
│   ├── storage.py         # SQLite persistence layer
│   ├── rag.py             # RAG chunking + indexing
│   ├── github_search.py   # GitHub search + write tools
│   ├── github_sync.py     # Backup pull/push scheduler
│   ├── environment.py     # Constraint detection + optimization
│   ├── trading.py         # Analysis, lessons, journal, briefings
│   ├── trading_scheduler.py # Daily briefing cron
│   ├── dispatcher.py      # Background task engine
│   ├── scheduler.py       # Status posting scheduler
│   ├── health.py          # Health checks + heartbeat
│   ├── media.py           # YouTube search/download
│   ├── vision.py          # NVIDIA VLM + image generation
│   ├── bridge_api.py      # HTTP client for WhatsApp bridge
│   └── ... (25+ services)
└── data/                  # Docs, charts, trading cache
```

## Troubleshooting

### Bridge won't connect
- Check `CRIMSON_API_TOKEN` matches on both services
- Verify `AI_SERVER=http://crimson-bot:5000/reply` (internal DNS)
- Check bridge logs for QR code

### Bot not replying
- Check `/health` endpoint
- Verify `NVIDIA_API_KEY` or `GROQ_API_KEY` set
- Check logs for LLM errors

### GitHub sync failing
- Verify `GITHUB_TOKEN` has `repo` scope
- Check `GITHUB_BACKUP_REPO` format: `owner/repo`
- Repo must exist and be accessible

### Out of memory/disk
- Check `/health` for constraints
- Bot auto-optimizes — see Smart Optimization table
- Increase Railway volume size

## License
Proprietary — Crimsonej by Crimson (Elijah)