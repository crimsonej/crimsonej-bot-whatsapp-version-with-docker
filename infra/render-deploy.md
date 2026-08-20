# Deploying Crimsonej to Render

This runbook assumes you've accepted that **Render free tier is not a great host for a WhatsApp bot**. The bot will work for short demos, then sleep after 15 minutes, and the bridge will lose its Baileys session every time it spins down. Persistent Disk is also unavailable on free.

If you want reliability, you need at minimum:

| Service | Free | Starter ($7/mo) | Notes |
|---|---|---|---|
| crimson-bot | works, but cold-starts every 15 min idle | fine | Set BOT_PORT=10000 |
| whatsapp-bridge | sleeps, Baileys drops | fine | Persistent Disk **required** |

This guide targets the **free tier** so you can ship today and upgrade later.

---

## 0. What you'll need before clicking anything

- GitHub repo with this code pushed
- API keys: NVIDIA_API_KEY (recommended), GROQ_API_KEY (fallback), HF_API_KEY (optional)
- A second WhatsApp number you can afford to lose (the bridge will likely get your primary banned within days — cloud IP bans are aggressive)
- Patience: first deploy will take 5–10 minutes per service

## 1. Push the repo

```bash
cd "/home/joa/Desktop/edit room/crimsonej-with-docker"
git init            # if not already
git add .
git commit -m "render deploy files"
git branch -M main
git remote add origin git@github.com:YOUR_USER/crimsonej-render.git
git push -u origin main
```

## 2. Create a Render Blueprint

1. Go to https://dashboard.render.com → **New +** → **Blueprint**.
2. Connect your GitHub account if not already.
3. Pick the `crimsonej-render` repo.
4. Render detects `infra/render.yaml` and shows two services.
5. Click **Apply**.

> ⚠️ On free plan, **Render will reject the Persistent Disk blocks** when you apply. The render.yaml above deliberately does NOT declare disks — see "Disk workaround" below.

## 3. Fill in secret env vars

In the Render Dashboard → crimson-bot → Environment:

- `NVIDIA_API_KEY` = your key
- `GROQ_API_KEY`   = your key
- `HF_API_KEY`     = your key (optional)
- `CREATOR_PHONE`  = your WhatsApp number in international format, e.g. `2567XXXXXXXX`

For each secret, click the small "secret" toggle so it's not logged.

## 4. Wire the bridge to the bot

After crimson-bot finishes deploying, copy its public URL (something like `https://crimson-bot-xxxx.onrender.com`). Then in Render Dashboard → whatsapp-bridge → Environment:

- Edit `AI_SERVER` to `https://crimson-bot-xxxx.onrender.com/reply`
- Trigger a manual deploy so the bridge picks up the new env var

> Internal hostnames like `http://crimson-bot:10000/reply` in `render.yaml` are placeholders. **They do not resolve between Render services.** Render services are fully isolated; cross-service calls must use the public URL.

## 5. Scan the QR (one-time, painful)

The bridge logs its Baileys QR code to stdout via `qrcode-terminal`. To see it:

1. Render Dashboard → whatsapp-bridge → Logs
2. Click the live tail / refresh
3. On first boot (and every cold-start after sleep), you'll see a QR.
4. Open WhatsApp on your phone → Linked Devices → Link a Device → scan it.

**Important:** the QR string is printed as ASCII art to the logs. It will be visible to anyone with read-only access to your Render logs. Render keeps the QR visible only briefly (it refreshes every few seconds), so be ready to scan within ~30 seconds.

If you miss it: trigger a manual redeploy. Restart = fresh QR.

## 6. Verify

Send a message to the WhatsApp number you linked.

- Render Dashboard → whatsapp-bridge → Logs: should show `[UPTIME] 🟢 HTTP Server running on port 7860`
- Render Dashboard → crimson-bot → Logs: should show `* Running on http://0.0.0.0:10000`

If the bot replies, you have a working deploy.

## Disk workaround (free tier)

Render free plan does NOT allow Persistent Disks. This means every redeploy and every sleep/wake cycle wipes:

- All Baileys auth files → QR has to be rescanned
- All bot state: `vectors.json`, `sessions.json`, `user_profiles.json`, `cache.json`, vaults, RAG index, group state

Workaround options:

### Option A: Upgrade to Starter on at least the bridge

`whatsapp-bridge` → Settings → Plan → Starter ($7/mo). Then add this block to `infra/render.yaml` and re-apply:

```yaml
disk:
  name: bridge-data
  mountPath: /data
  sizeGB: 1
```

The bridge already auto-detects `/data` for `auth_info_baileys` (see `bridge.js:34`). The bot's Dockerfile already WORKDIRs into `/data`, so its `BASE_DIR` ends up on whatever you mount there.

### Option B: Stay free, accept the wipe

- Bot state rebuilds on each cold start (~30s of "building RAG index..." messages)
- Bridge QR has to be re-scanned after every sleep
- Effectively: the bot is only usable while you're actively sending messages

### Option C: Don't use Render

If you want reliability for free, Oracle Cloud's "Always Free" tier gives you a 1 GB RAM ARM VM that can host both services 24/7 with no sleeping. Then deploy via Docker Compose (the `Dockerfile`s are already written).

---

## What I did NOT change in the bot code

- `bot.py`, `bridge.js`, `core/*`, `services/*` — all unmodified
- The Persistent Disk trick works because the bot's `BASE_DIR` is computed at import time as the directory of `core/config.py`. Setting `WORKDIR /data` and copying the source there makes `BASE_DIR == /data` automatically.

## Known issues you'll hit on free tier

1. **Bridge QR rescans every sleep.** Plan around it. Don't expect "set and forget".
2. **Cold starts add 30+ seconds** to the first reply after idle. Tell your users.
3. **Cloud IP WhatsApp ban.** This is the most likely failure. Have a backup number.
4. **Puppeteer image size.** The bridge Dockerfile pulls Chromium via Puppeteer, which adds ~300 MB. Render's free image cap is 2 GB — we're under, but close. If the build fails on size, see "Strip Puppeteer" in runbook.
5. **Render free build minutes.** Each service gets 500 build-minutes/month. Two services rebuilding on every push will burn through that fast.

## Where to look when things break

- crimson-bot logs: search for `[ERROR]` or `Traceback`
- whatsapp-bridge logs: search for `EPROTO` (TLS hiccup), `loggedOut` (session expired), `DisconnectReason`
- Both: `/health` endpoint should return 200; if not, the service is crashed

---

## Strip Puppeteer (only if build fails on size)

The bridge uses **two** WhatsApp libraries (`whatsapp-web.js` AND `@whiskeysockets/baileys`). Only Baileys is actually wired up to `sock`; the whatsapp-web.js / venom-bot / Puppeteer imports are dead weight.

If you must reduce the image size, **you can edit `whatsapp-bridge/package.json` to remove `puppeteer`, `venom-bot`, and `whatsapp-web.js` from `dependencies`** before deploying. This is technically modifying the bridge's dependency list (not source), but it's reversible. The Dockerfile already installs Chromium via apt for Puppeteer, so removing the Puppeteer npm package also lets you strip the apt deps.

If you do that, also remove from the Dockerfile:
- `RUN apt-get install ... chromium deps ...`
- The whole Chromium block

That gets the bridge image down to ~200 MB. Faster builds, more monthly budget left.
