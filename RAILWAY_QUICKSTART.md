# Crimsonej Bot — Railway Quick Start

## 1. Fork & Deploy
```bash
# Fork this repo on GitHub
# In Railway: New Project → Deploy from GitHub → Select your fork
# Railway reads railway.toml and creates 2 services automatically
```

## 2. Add Environment Variables

### crimson-bot service
| Variable | Required | Example |
|----------|----------|---------|
| `NVIDIA_API_KEY` | Yes | `nvapi-xxxxx` |
| `GROQ_API_KEY` | Yes | `gsk-xxxxx` |
| `GITHUB_TOKEN` | For backup | `ghp-xxxxx` (repo scope) |
| `GITHUB_BACKUP_REPO` | For backup | `crimsonej/crimsonej-data` |
| `CRIMSON_API_TOKEN` | Yes | `shared-secret-123` |
| `CREATOR_PHONE` | Yes | `256742184690` |
| `HF_API_KEY` | Optional | `hf-xxxxx` |

### whatsapp-bridge service
| Variable | Required | Example |
|----------|----------|---------|
| `CRIMSON_API_TOKEN` | Yes | `shared-secret-123` (must match bot) |

## 3. Deploy & Connect
1. Railway builds both services
2. Check **whatsapp-bridge logs** for QR code
3. Scan with WhatsApp → **bot is live**
4. Send `/help` to verify

## 4. Verify
```bash
# Health check
curl https://your-bot.up.railway.app/health

# GitHub sync status
curl https://your-bot.up.railway.app/sync_status

# Force sync
curl -X POST https://your-bot.up.railway.app/sync_now
```

## 5. Key Commands
| Command | Description |
|---------|-------------|
| `/help` | Full command list |
| `/analyze BTC` | Trading analysis |
| `/teach candlesticks` | Learn trading |
| `/imagine cat in space` | Generate image |
| `/song-audio Never Gonna Give You Up` | Download audio |
| `/group_fact We love BTC` | Group memory |
| `master control chela` | Creator auth |

## Volumes (Auto-created)
- `bot_data` → `/data` (SQLite, config, backups)
- `bridge_data` → `/data` (WhatsApp auth)
- `media_data` → `/media` (downloaded files)

## Troubleshooting
| Issue | Fix |
|-------|-----|
| No QR code | Check bridge logs, restart bridge service |
| Bot not replying | Verify API keys, check `/health` |
| Sync failing | Verify GITHUB_TOKEN has repo scope |
| OOM kills | Railway: increase memory limit; bot auto-optimizes |

## Full Docs
See [DOCS.md](DOCS.md) for complete documentation.