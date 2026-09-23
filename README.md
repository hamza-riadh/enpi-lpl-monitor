# ENPI LPL Monitor — 24/7 Free Housing Opportunity Alert System

Automatically monitors [ENPI LPL](https://www.enpi-net.dz/LPL/Inscription.php?lang=fr)
for new housing projects in Algeria. Sends instant phone notifications (ntfy) and
email alerts when new wilayas, projects, or typologies become selectable.

**Free. No VPS. No paid API. Runs on GitHub Actions ~every 5 minutes.**

---

## How It Works

```
Every ~5 minutes (GitHub Actions)
  ↓
GET  https://www.enpi-net.dz/LPL/Inscription.php?lang=fr
  → extract CSRF token + PHP session cookie
  → parse wilaya dropdown
  ↓
For each target wilaya:
  POST /LPL/api/projet-by-wilaya.php {country_id, csrf_token}
    → get project list
  POST /LPL/api/Typologie-by-projet.php {state_id, csrf_token}
    → get typologies (F3, F4, F5...)
  ↓
Compare with trusted previous state (state.json)
  ↓
Change? → ntfy phone alert + optional email
No change? → silent exit
```

**False-positive protection:**
- HTTP errors / empty responses → keep previous trusted state, never infer a change
- Mass disappearance (>50% projects) → wait 6 consecutive scans before believing it
- Single project removed → wait 2 consecutive scans to confirm

---

## Quick Setup (Ubuntu)

### 1 — Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/enpi-lpl-monitor.git
cd enpi-lpl-monitor
pip install -r requirements.txt   # needs httpx, beautifulsoup4
```

### 2 — Configure ntfy

1. Install the **ntfy** app on your phone (Google Play / App Store / F-Droid)
2. Subscribe to a **private topic** — use a long random string, e.g.:
   `enpi_lpl_alerts_7f39b2a64c0e819dff3a`
3. Keep this topic name **secret** (anyone who knows it can read your alerts)

### 3 — Create `.env` for local testing

```bash
cp .env.example .env
# Edit .env and fill in at minimum NTFY_TOPIC
```

### 4 — Test notifications

```bash
NTFY_TOPIC=your-secret-topic python monitor.py --test
# Check your phone — you should receive a test alert instantly
```

### 5 — Run one live scan

```bash
python monitor.py
# First run: creates baseline in state.json (no alerts sent)
# Second run: compares and alerts on any change
```

### 6 — Discover live site structure (diagnostic)

```bash
python monitor.py --discover
# Prints actual API responses to verify everything works
```

---

## GitHub Actions Setup (24/7 free monitoring)

### Step 1 — Create a PUBLIC GitHub repository

> Public repos get **unlimited** free Actions minutes.  
> Private repos: 2,000 min/month → exhausted ~day 7 at 5-min intervals.

### Step 2 — Push your code

```bash
git remote add origin git@github.com:YOUR_USERNAME/enpi-lpl-monitor.git
git branch -M main
git add .
git commit -m "feat: initial ENPI LPL monitor"
git push -u origin main
```

### Step 3 — Enable workflow permissions

GitHub → Your Repo → **Settings → Actions → General**  
Scroll to "Workflow permissions" → select **"Read and write permissions"** → Save

### Step 4 — Add GitHub Secrets

GitHub → Your Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name    | Example Value                          | Required |
|----------------|----------------------------------------|----------|
| `NTFY_TOPIC`   | `enpi_lpl_alerts_7f39b2a64c0e819d`    | ✅ Yes   |
| `EMAIL_TO`     | `you@gmail.com`                        | Optional |
| `SMTP_HOST`    | `smtp.gmail.com`                       | Optional |
| `SMTP_PORT`    | `465`                                  | Optional |
| `SMTP_USER`    | `your-sender@gmail.com`                | Optional |
| `SMTP_PASSWORD`| `xxxx xxxx xxxx xxxx` (App Password)   | Optional |
| `HEALTHCHECK_URL` | `https://hc-ping.com/your-uuid`     | Optional |

### Step 5 — Trigger first run

GitHub → **Actions → ENPI LPL 24/7 Monitor → Run workflow**

After the first run:
- `state.json` is committed with the baseline snapshot
- You receive a "Monitor started" ntfy notification
- All subsequent runs compare against this baseline

### Step 6 — Verify scheduling

The cron `3-58/5 * * * *` runs at minutes :03, :08, :13, ... :58 of every hour.
GitHub may delay by up to ~10 minutes under load.

> **60-day inactivity rule:** GitHub disables scheduled workflows if no commits
> are pushed for 60 consecutive days. State updates reset this counter.
> Use **workflow_dispatch** to manually trigger if needed.

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
