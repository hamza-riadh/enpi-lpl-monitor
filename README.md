# ENPI Housing Monitor — 24/7 Free Opportunity Alert System (LPL + LPP Portals)

Automatically monitors:
1. **ENPI LPL Portal:** [enpi-net.dz/LPL/Inscription.php](https://www.enpi-net.dz/LPL/Inscription.php?lang=fr)
2. **ENPI LPP Portal:** [enpi-net.dz/ENPI/Inscription.php](https://www.enpi-net.dz/ENPI/Inscription.php?lang=fr)

Watches for new apartment opportunities (F3, F4, F5, F6) in **Alger (16), Tipaza (42), Blida (09), and Boumerdes (35)**.
Strictly filters out and rejects villas (e.g. Larbaâ, Mouzaia).
Sends instant alerts via **Telegram Bot** (with clickable direct buttons), **ntfy** push, **WhatsApp**, and **Email**.

**100% Free. No VPS. Runs on GitHub Actions ~every 5 minutes.**

---

## Architecture & Detection Pipeline

```
Every ~5 minutes (GitHub Actions / cron-job.org)
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
     → probe target wilayas & typologies (F3, F4, F5, F6)
  ↓
3. Reconcile against trusted state (state.json):
     → Detect OPPORTUNITY, NEW_PROJECT, NEW_TYPOLOGY, or NEW_WILAYA
     → Filter out all villas (e.g. Larbaâ, Mouzaia)
     → Require target apartment typologies (F3, F4, F5, F6)
  ↓
4. Dispatch instant priority alerts via Telegram, ntfy, WhatsApp, and Email
```

**False-Positive & Glitch Protection:**
- HTTP errors / empty responses → retain previous trusted state, never infer a change.
- Mass disappearance (>50% projects) → wait 6 consecutive scans before confirming.
- Single project removed → wait 2 consecutive scans before confirming.
- Auto-recovery notification once connection is restored after a failure.

---

## Multi-Channel Alert Configuration

### 1. Telegram Bot (Instant & 100% Free)
- Create a bot via [@BotFather](https://t.me/BotFather) and get `TELEGRAM_BOT_TOKEN`.
- Send `/start` to your bot.
- Get your `chat_id` via `@userinfobot`.
- Add to GitHub Secrets:
  - `TELEGRAM_BOT_TOKEN`: `8965800028:AAHK...`
  - `TELEGRAM_CHAT_IDS`: `8794217005`

### 2. ntfy Push Notifications
- Install **ntfy** app on Android or iOS.
- Subscribe to your private topic.
- Add `NTFY_TOPIC` to GitHub Secrets.

### 3. WhatsApp (CallMeBot)
- Add `WHATSAPP_TARGETS` formatted as `+213555123456:apikey` in GitHub Secrets.

---

## GitHub Secrets Checklist

| Secret Name          | Description                                    | Status   |
|----------------------|------------------------------------------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot API token from @BotFather                  | ✅ Recommended |
| `TELEGRAM_CHAT_IDS`  | Telegram Chat ID (Hamza Riadh: 8794217005)      | ✅ Recommended |
| `NTFY_TOPIC`         | Private ntfy channel string                    | Optional |
| `WHATSAPP_TARGETS`   | Multi-recipient `phone:apikey` for CallMeBot    | Optional |
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

## Portal-Only Strategy & Apartment Targeting

ENPI posts new apartment quotas directly to the official registration portals (LPL & LPP) before any social media announcements. Facebook announcements often lag by 20 to 60+ minutes and heavily feature slow-moving villa projects (e.g. Larbaâ, Mouzaia) that were already listed.

By querying the official portal APIs directly every 5 minutes:
1. **Zero Social Media Latency:** Instant detection the second a quota or apartment project opens.
2. **Strict Typology Filtering:** Only apartments (**F3, F4, F5, F6**) in the 4 target wilayas (Alger, Tipaza, Blida, Boumerdes) trigger alerts.
3. **No Noise / No Villas:** Villa projects are automatically filtered out.

---

## Architecture Decision Log

| Question | Decision | Reason |
|----------|----------|--------|
| Playwright / Selenium? | ❌ Not used | Portals use clean AJAX endpoints — lightweight HTTP requests are 50x faster and never get blocked |
| Database? | ❌ Not used | `state.json` Git-backed atomic state is 100% reliable and zero-cost |
| VPS? | ❌ Not used | GitHub Actions + cron-job.org handles 24/7 execution |
| Session handling | ✅ Fresh per run | CSRF tokens + PHP session cookies obtained dynamically per scan |
| State commit strategy | Only on change | Avoids pointless commits; preserves GitHub API quota |
| Filtering | Apartments F3-F6 | Villas and commercial premises are strictly excluded |

