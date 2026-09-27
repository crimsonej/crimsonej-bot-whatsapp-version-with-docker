# Crimsonej — Your WhatsApp AI Companion

A friendly, smart bot that lives in your WhatsApp. Remembers you, searches the web, generates images, downloads media — and if you want, helps you understand markets.

## Quick Links

- 🚀 **[Railway Quick Start](RAILWAY_QUICKSTART.md)** — Deploy in 5 minutes
- 📖 **[Complete Documentation](DOCS.md)** — Full reference
- 🐳 **Local Development** — See below

## Core Identity

**Crimsonej** — built by Crimson (Elijah). Manchester City fan. Hates Liverpool. Speaks human, not bot.

## What It Does

| Feature | Example |
|---------|---------|
| **Chat naturally** | "yo what's up" → "Yo! What's good?" |
| **Remember you** | "I'm from " → recalls it forever |
| **Search the web** | "what's the weather in Tokyo?" → live answer |
| **Generate images** | `/imagine a lion in space` → AI art |
| **Download music/video** | `/song-audio Shape of You` → audio file |
| **Analyze images** | Send a photo → "that's a sunset over mountains" |
| **Read documents** | Send PDF → "summary: ..." |
| **Learn facts** | `/learn I love ugali` → stored permanently |

## Trading Coach (Optional Add-on)

*Only if you ask for it.*

| Feature | Command |
|---------|---------|
| Market analysis | `/analyze BTC 4h` — TA with chart, bias (🟢/🔴/⏸️) |
| Learn concepts | `/teach risk_management` — 13 lessons |
| Trade journal | `/journal log ...` — track your trades |
| Daily briefing | `/brief pre_london` — 9 pairs at 07:30/21:30 EAT |
| Group auto-post | `/briefing_subscribe both BTC ETH` — posts to group |

> Just say: *"what's BTC doing?"* or *"teach me candlesticks"* — the bot detects and responds.

## Architecture

```
WhatsApp → Node.js Bridge → Flask API → LLM + Tools
                                    ├── Web Search (DuckDuckGo)
                                    ├── Image Gen (NVIDIA/HF)
                                    ├── Media (yt-dlp)
                                    ├── RAG (TF-IDF + SQLite + FTS5)
                                    ├── Profiles / Memory / Vaults (SQLite)
                                    ├── GitHub Backup Sync (hourly)
                                    ├── Smart Optimization (CPU/mem/disk aware)
                                    └── Trading Coach (Binance, CoinGecko, yfinance)
```

## Data & Persistence

- **SQLite** (`/data/crimson.db`) — Sessions, profiles, vaults, vectors, cache
- **GitHub Backup** — Hourly push to private repo, auto-restore on boot
- **Volumes** — Persistent across restarts (Railway/Docker)

## Deploy

| Platform | Guide |
|----------|-------|
| **Railway** | [RAILWAY_QUICKSTART.md](RAILWAY_QUICKSTART.md) |
| **Docker** | `docker-compose up -d` |
| **Local** | See below |

## Local Development

```bash
git clone <repo>
cd crimsonej-with-docker

# One-time setup
cd crimson-bot
python3 install.py        # Creates venv, installs deps
cd ..

# Start (two terminals)
# Terminal 1: Bot API
cd crimson-bot && python bot.py server

# Terminal 2: WhatsApp Bridge
cd whatsapp-bridge && npm start
```

### Requirements
- Python 3.11+
- Node.js 20+
- FFmpeg (for media)
- `.env` with API keys (see `crimson-bot/.env.example`)

## CLI Commands (via `crimsonej` wrapper)

| Command | Action |
|---------|--------|
| `crimsonej start` | Start bot + WhatsApp bridge |
| `crimsonej stop` | Stop everything |
| `crimsonej status` | Show PIDs, health, docs count |
| `crimsonej logs` | Tail logs |
| `crimsonej reindex` | Rebuild knowledge from `docs/` |

## In-Chat Commands

| Command | Description |
|---------|-------------|
| `/help` | Full list |
| `/imagine <prompt>` | AI image |
| `/sticker <prompt>` | AI sticker |
| `/song-audio <query>` | YouTube audio |
| `/song-video <query>` | YouTube video |
| `/reg-img` | Analyze attached image |
| `/read` | Summarize document |
| `/learn` | Store in memory |
| `/respond` | Reply to quoted msg |

**Trading (when you ask):**
`/analyze`, `/teach`, `/walkthrough`, `/quiz`, `/journal`, `/brief`, `/price`, `/mtf`, `/patterns`, `/briefing_subscribe`

## Master Control (Creator Only)

`master control chela` → authenticates you. Then:
- `status_posting on/off`
- `scheduler on/off`
- `interval 4`
- `topic add "market update"`
- `wipe cache / memory`

## Built By

**Crimson (Elijah)** — for the group chat that needed a real one.

---

*For complete API reference, configuration options, and troubleshooting, see [DOCS.md](DOCS.md)*