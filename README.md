# ENPI Housing Monitor — 24/7 Free Opportunity Alert System (LPL + LPP + Facebook)

Automatically monitors:
1. **ENPI LPL Portal:** [enpi-net.dz/LPL/Inscription.php](https://www.enpi-net.dz/LPL/Inscription.php?lang=fr)
2. **ENPI LPP Portal:** [enpi-net.dz/ENPI/Inscription.php](https://www.enpi-net.dz/ENPI/Inscription.php?lang=fr)
3. **Official ENPI Facebook Page:** [facebook.com/ENPI.dz](https://www.facebook.com/ENPI.dz/)

Watches for new projects, quotas, and typologies in **Alger (16), Tipaza (42), Blida (09), and Boumerdes (35)**.
Sends instant alerts via **Telegram Bot** (with clickable buttons), **ntfy** push, **WhatsApp**, and **Email**.

**100% Free. No VPS. Runs on GitHub Actions ~every 5 minutes.**

---

## Architecture & Detection Pipeline

```
Every ~5 minutes (GitHub Actions)
  ↓
1. Scan LPL Portal:
   GET  https://www.enpi-net.dz/LPL/Inscription.php?lang=fr
     → extract CSRF token + PHP session cookie
     → probe target wilayas: POST /LPL/api/projet-by-wilaya.php
     → probe project typologies: POST /LPL/api/Typologie-by-projet.php
  ↓
2. Scan LPP Portal:
   GET  https://www.enpi-net.dz/ENPI/Inscription.php?lang=fr
     → extract CSRF token + session
     → probe target wilayas & typologies (F3, F4, F5...)
  ↓
3. Scan Official Facebook Page / Press Releases:
   Fetch latest ENPI announcements (Facebook RSS / Google News Algeria)
     → filter for target wilayas (Alger, Tipaza, Blida, Boumerdes, Sidi Abdellah, etc.)
     → filter for housing keywords (LPL, LPP, souscription, inscription, quota, etc.)
     → deduplicate against seen posts
  ↓
4. Compare with trusted state (state.json):
     → Detect OPPORTUNITY, NEW_PROJECT, NEW_TYPOLOGY, or NEW_WILAYA
  ↓
5. Dispatch instant alerts via Telegram, ntfy, WhatsApp, and Email
```

**False-Positive & Glitch Protection:**
- HTTP errors / empty responses → retain previous trusted state, never infer a change.
- Mass disappearance (>50% projects) → wait 6 consecutive scans before confirming.
- Single project removed → wait 2 consecutive scans before confirming.
- Auto-recovery notification once connection is restored after a failure.

---

## Multi-Channel Alert Configuration

### 1. Telegram Bot (Recommended — Instant & 100% Free)
- Create a bot via [@BotFather](https://t.me/BotFather) and get `TELEGRAM_BOT_TOKEN`.
- Send `/start` to your bot.
- Get your `chat_id` via `@userinfobot`.
- Add to GitHub Secrets:
  - `TELEGRAM_BOT_TOKEN`: `8965800028:AAHK...`
  - `TELEGRAM_CHAT_IDS`: `8794217005` (or comma-separated for multiple users).

### 2. ntfy Push Notifications
- Install **ntfy** app on Android or iOS.
- Subscribe to your private topic.
- Add `NTFY_TOPIC` to GitHub Secrets.

### 3. WhatsApp (CallMeBot)
- Add `WHATSAPP_TARGETS` formatted as `+213555123456:apikey` in GitHub Secrets.

### 4. Optional Facebook RSS
- Set `FACEBOOK_RSS_URL` in GitHub Secrets if using a custom RSS.app / FetchRSS bridge.

---

## GitHub Secrets Checklist

| Secret Name          | Description                                    | Status   |
|----------------------|------------------------------------------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot API token from @BotFather                  | ✅ Recommended |
| `TELEGRAM_CHAT_IDS`  | Comma-separated Telegram Chat IDs              | ✅ Recommended |
| `NTFY_TOPIC`         | Private ntfy channel string                    | Optional |
| `WHATSAPP_TARGETS`   | Multi-recipient `phone:apikey` for CallMeBot    | Optional |
| `FACEBOOK_RSS_URL`   | Dedicated Facebook page RSS feed               | Optional |
| `HEALTHCHECK_URL`    | Ping URL (healthchecks.io)                     | Optional |

---

## Alert Examples

**New opportunity detected:**
```
🚨🚨 ENPI LPL — OPPORTUNITE DETECTEE

Wilaya:    Tipaza
Projet:    120 LOGTS LPL KHEMISTI
Typologie: F3, F4
Note:      Selectionnable sur le formulaire officiel. Verifiez le site.

Detecte:   23 September 2026 — 22:10 (Alger)
Lien:      https://www.enpi-net.dz/LPL/Inscription.php?lang=fr
```

**Project removed (not confirmed sold):**
```
⚠️ ENPI LPL — PROJET NON DETECTE

Wilaya:    Blida
Projet:    12 Villas Mouzaia
Note:      Non confirme comme vendu. Peut etre: maintenance ou changement temporaire.
```

**Monitoring failure:**
```
⚠️ ENPI MONITORING FAILURE
Derniere analyse: 2026-09-23T22:10:00
Raison: Connection timeout
Donnees precedentes conservees.
```

---

## Customise Targets

Edit `TARGET_WILAYAS` in the workflow file (or GitHub Secret):

```yaml
TARGET_WILAYAS: "Alger,Tipaza,Blida,Boumerdes"
```

Or monitor everything:
```yaml
MONITOR_ALL_WILAYAS: "true"
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "CSRF token not found" | Site layout changed | Open `python monitor.py --discover` |
| "wilaya select not found" | Same | Same |
| "Only N wilayas found" | Site downtime | Wait and retry |
| ntfy not received | Wrong topic name | Check `NTFY_TOPIC` secret |
| Email fails | Wrong app password | Gmail → Manage Account → Security → App Passwords |
| Actions not running | Repo inactive 60+ days | Push a commit or use workflow_dispatch |
| Actions delayed | GitHub load | Normal — expect ~5-10 min, not exactly 5 |

---

## Facebook Monitoring

Facebook scraping from GitHub Actions IP ranges reliably triggers login walls
and bot detection. This system correctly uses the **official ENPI registration
portal as the primary source**. Facebook is NOT scraped.

WhatsApp and SMS require paid APIs for reliable automation. ntfy + email is
the recommended free solution.

---

## Architecture Decision Log

| Question | Decision | Reason |
|----------|----------|--------|
| Playwright? | ❌ Not used | Site uses simple POST/GET AJAX — no JS rendering needed |
| Database? | ❌ Not used | JSON state file is sufficient |
| VPS? | ❌ Not used | GitHub Actions handles scheduling and execution |
| Session handling | ✅ Fresh per run | CSRF tokens + PHP session are per-request; obtained before each scan |
| State commit strategy | Only on change | Avoids 288 pointless commits/day |
| Facebook | ❌ Not built | Unreliable from datacenter IPs; primary source is sufficient |
