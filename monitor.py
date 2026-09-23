#!/usr/bin/env python3
"""
ENPI LPL 24/7 Housing Opportunity Monitor
==========================================
Monitors https://www.enpi-net.dz/LPL/Inscription.php?lang=fr
for new Wilaya / Project / Typology changes.

CONFIRMED API FLOW (reverse-engineered 2026-09-23):
  1. GET  /LPL/Inscription.php?lang=fr
         → sets PHP session cookie + embeds CSRF token in page
         → <select name="wilaya"> contains all open wilaya IDs & names
  2. POST /LPL/api/projet-by-wilaya.php
         Body: country_id=<wilaya_id>&csrf_token=<token>
         Headers: X-CSRF-Token, X-Requested-With: XMLHttpRequest
         → returns raw <option> HTML fragments (project list)
           Project IDs are base64-encoded session-bound tokens
  3. POST /LPL/api/Typologie-by-projet.php
         Body: state_id=<project_token>&csrf_token=<token>
         Headers: X-CSRF-Token, X-Requested-With: XMLHttpRequest
         → returns raw <option> HTML fragments (typology list, e.g. F3, F4, F5)
         → response header X-Projet-Encours: 10 or 11 indicates project status

Usage:
  python monitor.py              # one scan
  python monitor.py --test       # send simulated OPPORTUNITY alert
  python monitor.py --dry-run    # scan + print events; nothing saved/sent
  python monitor.py --discover   # print live site structure
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("enpi")
ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Africa/Algiers")

# Configuration
BASE_URL        = "https://www.enpi-net.dz/LPL/"
INSCRIPTION_URL = "https://www.enpi-net.dz/LPL/Inscription.php?lang=fr"
PROJECTS_API    = "https://www.enpi-net.dz/LPL/api/projet-by-wilaya.php"
TYPOLOGY_API    = "https://www.enpi-net.dz/LPL/api/Typologie-by-projet.php"

DEFAULT_TARGETS = ["Alger", "Tipaza", "Blida", "Boumerdes"]

MIN_WILAYAS_EXPECTED  = 10
REMOVAL_CONFIRMATIONS = 2
MASS_REMOVAL_RATIO    = 0.5
MASS_CONFIRMATIONS    = 6
FAILURE_ALERT_AFTER   = 3
HEARTBEAT_HOURS       = 12

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/133.0.0.0 Safari/537.36"
TIMEOUT   = 25
RETRIES   = 3
REQ_DELAY = 0.4

PLACEHOLDER = ("choisir", "choisissez", "select", "veuillez", "--", "...", "choisir projet",
               "choisir wilaya", "choisir typ", "session invalide")
BLOCK_MARKERS = ("just a moment...", "access denied", "cf-chl", "attention required",
                 "verify you are human", "unusual traffic", "403 forbidden", "cloudflare")
ERROR_WORDS   = ("erreur", "error", "exception", "fatal", "mysql", "sql syntax", "stack trace")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().casefold()


def targets() -> list:
    env = os.getenv("TARGET_WILAYAS")
    if env:
        return [x.strip() for x in env.split(",") if x.strip()]
    return DEFAULT_TARGETS


def monitor_all() -> bool:
    return os.getenv("MONITOR_ALL_WILAYAS", "false").lower() in ("1", "true", "yes")


class ExtractionError(Exception):
    pass


class BlockedError(ExtractionError):
    pass


def _parse_options(html: str) -> list:
    html = (html or "").strip()
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    low = norm(soup.get_text(" "))
    if any(m in low for m in BLOCK_MARKERS):
        raise BlockedError(f"blocked/anti-bot content in API response: {low[:80]!r}")
    if any(w in low for w in ERROR_WORDS) and len(html) < 400:
        raise ExtractionError(f"error fragment in API response: {low[:80]!r}")
    out = []
    for opt in soup.find_all("option"):
        if opt.has_attr("disabled"):
            continue
        val   = (opt.get("value") or "").strip()
        label = re.sub(r"\s+", " ", opt.get_text(" ")).strip()
        n_lab = norm(label)
        if not val or not label:
            continue
        if any(n_lab.startswith(p) for p in PLACEHOLDER):
            continue
        out.append({"id": val, "label": label})
    return out


def parse_wilaya_select(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    sel = (soup.find("select", attrs={"name": re.compile(r"wilaya", re.I)}) or
           soup.find("select", attrs={"id":   re.compile(r"wilaya", re.I)}))
    if not sel:
        low = norm(soup.get_text(" "))
        if any(m in low for m in BLOCK_MARKERS):
            raise BlockedError("CAPTCHA / anti-bot wall on main page")
        raise ExtractionError("wilaya <select> not found")
    return _parse_options(str(sel))


class EnpiFetcher:
    def _open_session(self):
        client = httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, verify=False,
            headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7"}
        )
        resp = self._get_retry(client, INSCRIPTION_URL)
        m = re.search(r"csrfToken:\s*['\"]([a-f0-9]{64})['\"]", resp.text)
        if not m:
            m = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([a-f0-9]{64})["\']', resp.text)
        csrf = m.group(1) if m else ""
        if not csrf:
            raise ExtractionError("Could not extract CSRF token")
        return client, csrf

    def _get_retry(self, client, url):
        last_err = None
        for attempt in range(RETRIES):
            try:
                r = client.get(url)
                if r.status_code == 403:
                    raise BlockedError(f"HTTP 403 on GET {url}")
                if r.status_code >= 500 or r.status_code == 429:
                    last_err = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    raise ExtractionError(f"HTTP {r.status_code}")
                elif r.status_code == 200 and len(r.text) > 200:
                    return r
                else:
                    last_err = f"HTTP {r.status_code} short body"
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_err = str(e)
            if attempt < RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
        raise ExtractionError(f"GET failed after {RETRIES} attempts: {last_err}")

    def _post_retry(self, client, url, data, csrf):
        headers = {
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": INSCRIPTION_URL,
        }
        last_err = None
        for attempt in range(RETRIES):
            try:
                r = client.post(url, data=data, headers=headers)
                if r.status_code == 403:
                    raise BlockedError(f"HTTP 403 on POST {url}")
                if r.status_code >= 500 or r.status_code == 429:
                    last_err = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    raise ExtractionError(f"HTTP {r.status_code}")
                else:
                    return r
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_err = str(e)
            if attempt < RETRIES - 1:
                time.sleep(2 ** (attempt + 1))
        raise ExtractionError(f"POST failed after {RETRIES} attempts: {last_err}")

    def scan(self):
        client, csrf = self._open_session()
        page_resp = self._get_retry(client, INSCRIPTION_URL)
        wilayas = parse_wilaya_select(page_resp.text)
        log.info("[INFO] %d wilayas in dropdown", len(wilayas))
        if len(wilayas) < MIN_WILAYAS_EXPECTED:
            raise ExtractionError(f"Only {len(wilayas)} wilayas found (expected >= {MIN_WILAYAS_EXPECTED})")

        t_norms = [norm(t) for t in targets()]
        snap = {"wilayas": {}}
        failed = set()
        warnings = []
        scanned = 0

        for w in wilayas:
            wk = norm(w["label"])
            if wk in snap["wilayas"]:
                continue
            is_target = monitor_all() or any(t in wk for t in t_norms)
            entry = {"label": w["label"], "id": w["id"], "projects": None}
            snap["wilayas"][wk] = entry
            if not is_target:
                continue

            scanned += 1
            entry["projects"] = {}
            try:
                time.sleep(REQ_DELAY)
                proj_resp = self._post_retry(
                    client, PROJECTS_API,
                    {"country_id": w["id"], "csrf_token": csrf}, csrf
                )
                projects = _parse_options(proj_resp.text)
                log.info("[INFO]   Wilaya %-20s -> %d project(s)", w["label"], len(projects))

                for p in projects:
                    time.sleep(REQ_DELAY)
                    try:
                        typo_resp = self._post_retry(
                            client, TYPOLOGY_API,
                            {"state_id": p["id"], "csrf_token": csrf}, csrf
                        )
                        typologies = _parse_options(typo_resp.text)
                        status = typo_resp.headers.get("X-Projet-Encours", "")
                    except ExtractionError as e:
                        log.warning("[WARNING] Typology fail %s/%s: %s", w["label"], p["label"], e)
                        typologies = []
                        status = ""

                    entry["projects"][norm(p["label"])] = {
                        "label": p["label"],
                        "typologies": {norm(t["label"]): t["label"] for t in typologies},
                        "status": status,
                    }
            except BlockedError:
                raise
            except ExtractionError as e:
                failed.add(wk)
                warnings.append(f"{w['label']}: {e}")
                log.warning("[WARNING] Failed to probe %s: %s", w["label"], e)

        if scanned == 0:
            raise ExtractionError(
                f"None of the target wilayas matched. Targets: {targets()}. "
                f"Available (norm): {[norm(w['label']) for w in wilayas[:8]]}"
            )
        if failed and len(failed) == scanned:
            raise ExtractionError(f"Every target probe failed: {'; '.join(warnings)[:300]}")

        return snap, failed, warnings


def _event(kind, wilaya, project=None, typologies=None, new_typologies=None):
    return {"type": kind, "wilaya": wilaya, "project": project,
            "typologies": sorted(typologies or []), "new_typologies": sorted(new_typologies or [])}


def reconcile(old, new, failed, counters):
    trusted = copy.deepcopy(new)
    if old is None:
        if failed:
            raise ExtractionError("First scan incomplete; refusing partial baseline.")
        return trusted, [], {}, True

    for wk in failed:
        if wk in old["wilayas"]:
            trusted["wilayas"][wk] = copy.deepcopy(old["wilayas"][wk])
        else:
            trusted["wilayas"].pop(wk, None)

    missing = []
    old_projects_total = 0
    missing_projects   = 0

    for wk, ow in old["wilayas"].items():
        if wk in failed:
            continue
        nw = trusted["wilayas"].get(wk)
        n_old = len(ow.get("projects") or {})
        old_projects_total += n_old
        if nw is None:
            missing.append(("w", wk, None, None))
            missing_projects += n_old
            continue
        if ow.get("projects") is None or nw.get("projects") is None:
            continue
        for pk, op in ow["projects"].items():
            np_ = nw["projects"].get(pk)
            if np_ is None:
                missing.append(("p", wk, pk, None))
                missing_projects += 1
                continue
            for tk in op["typologies"]:
                if tk not in np_["typologies"]:
                    missing.append(("t", wk, pk, tk))

    mass = (old_projects_total >= 1 and (
        missing_projects == old_projects_total or
        (old_projects_total >= 3 and missing_projects >= math.ceil(MASS_REMOVAL_RATIO * old_projects_total))
    ))
    need = MASS_CONFIRMATIONS if mass else REMOVAL_CONFIRMATIONS
    if mass:
        log.warning("[WARNING] %d/%d projects vanished at once — suspicious, need %d scans to confirm",
                    missing_projects, old_projects_total, need)

    events = []
    new_counters = {}

    for kind, wk, pk, tk in missing:
        key = "|".join(x for x in (kind, wk, pk, tk) if x)
        n = counters.get(key, 0) + 1
        ow = old["wilayas"][wk]
        if n >= need:
            if kind == "w":
                events.append(_event("REMOVED_WILAYA", ow["label"]))
            elif kind == "p":
                events.append(_event("REMOVED", ow["label"], ow["projects"][pk]["label"]))
            else:
                op = ow["projects"][pk]
                events.append(_event("REMOVED_TYPOLOGY", ow["label"], op["label"], [op["typologies"][tk]]))
            continue
        new_counters[key] = n
        if kind == "w":
            trusted["wilayas"][wk] = copy.deepcopy(ow)
        elif kind == "p":
            trusted["wilayas"][wk]["projects"][pk] = copy.deepcopy(ow["projects"][pk])
        else:
            trusted["wilayas"][wk]["projects"][pk]["typologies"][tk] = ow["projects"][pk]["typologies"][tk]

    for wk, nw in trusted["wilayas"].items():
        ow = old["wilayas"].get(wk)
        if ow is None:
            events.append(_event("NEW_WILAYA", nw["label"]))
        if nw.get("projects") is None or (ow is not None and ow.get("projects") is None):
            continue
        old_p = (ow or {}).get("projects") or {}
        for pk, np_ in nw["projects"].items():
            op = old_p.get(pk)
            all_t  = list(np_["typologies"].values())
            added  = [l for k, l in np_["typologies"].items() if op is None or k not in op["typologies"]]
            if op is None:
                kind = "OPPORTUNITY" if all_t else "NEW_PROJECT"
                events.append(_event(kind, nw["label"], np_["label"], all_t, added))
            elif added:
                kind = "OPPORTUNITY" if not op["typologies"] else "NEW_TYPOLOGY"
                events.append(_event(kind, nw["label"], np_["label"], all_t, added))

    return trusted, events, new_counters, False


EPHEMERAL_KEYS = ("last_success", "last_attempt", "saved_at")


def _empty_state():
    return {
        "version": 2, "snapshot": None, "last_success": None, "last_attempt": None,
        "consecutive_failures": 0, "failure_alerted": False, "missing": {}, "pending": [], "saved_at": None,
    }


def load_state(path):
    if not path.exists():
        return _empty_state()
    try:
        return {**_empty_state(), **json.loads(path.read_text())}
    except json.JSONDecodeError as e:
        log.error("[ERROR] Corrupt state file (%s). Resetting.", e)
        return _empty_state()


def save_state(path, state, force=False):
    material = lambda s: {k: v for k, v in s.items() if k not in EPHEMERAL_KEYS}
    if path.exists():
        on_disk = load_state(path)
        stale = False
        if on_disk.get("saved_at"):
            age = datetime.now(TZ) - datetime.fromisoformat(on_disk["saved_at"])
            stale = age.total_seconds() > HEARTBEAT_HOURS * 3600
        if not (force or material(on_disk) != material(state) or stale):
            return False
    state["saved_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return True


EVENT_META = {
    "OPPORTUNITY":       ("🚨🚨 ENPI LPL — OPPORTUNITE DETECTEE",         5, ["rotating_light", "house"]),
    "NEW_PROJECT":       ("🚨 ENPI LPL — NOUVEAU PROJET",                  4, ["rotating_light"]),
    "NEW_TYPOLOGY":      ("🚨 ENPI LPL — NOUVELLE TYPOLOGIE",              4, ["rotating_light"]),
    "NEW_WILAYA":        ("🚨 ENPI LPL — NOUVELLE WILAYA",                 4, ["rotating_light"]),
    "REMOVED":           ("⚠️ ENPI LPL — PROJET NON DETECTE",              2, ["warning"]),
    "REMOVED_TYPOLOGY":  ("⚠️ ENPI LPL — TYPOLOGIE NON DETECTEE",         2, ["warning"]),
    "REMOVED_WILAYA":    ("⚠️ ENPI LPL — WILAYA NON DETECTEE",            2, ["warning"]),
}

REMOVAL_NOTES = {
    "REMOVED":           "Non confirme comme vendu. Peut etre: epuisement, maintenance, ou probleme temporaire.",
    "REMOVED_TYPOLOGY":  "Peut etre epuisement de stock ou changement de donnees.",
    "REMOVED_WILAYA":    "Peut etre un changement temporaire du site.",
}


def _make_alert_item(events, official_url, now):
    events = sorted(events, key=lambda e: -EVENT_META[e["type"]][1])
    blocks = []
    for ev in events:
        title, _, _ = EVENT_META[ev["type"]]
        lines = [title] if len(events) > 1 else []
        lines.append(f"Wilaya:    {ev['wilaya']}")
        if ev["project"]:
            lines.append(f"Projet:    {ev['project']}")
        if ev["typologies"]:
            lines.append(f"Typologie: {', '.join(ev['typologies'])}")
        if ev["new_typologies"] and ev["type"] == "NEW_TYPOLOGY":
            lines.append(f"Nouvelles: {', '.join(ev['new_typologies'])}")
        note = REMOVAL_NOTES.get(ev["type"])
        if ev["type"] == "OPPORTUNITY":
            note = "Selectionnable sur le formulaire officiel. Verifiez le site pour confirmer."
        if note:
            lines.append(f"Note: {note}")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    body += (f"\n\nDetecte: {now.strftime('%d %B %Y — %H:%M')} (Alger)\n"
             f"Source: Systeme inscription ENPI officiel\nLien: {official_url}")
    top = events[0]["type"]
    title_str = EVENT_META[top][0]
    if len(events) > 1:
        title_str += f" (+{len(events) - 1})"
    return {"title": title_str, "body": body, "priority": EVENT_META[top][1],
            "tags": EVENT_META[top][2], "click": official_url}


def _simple_item(title, body, priority=3, tags=("warning",), click=""):
    return {"title": title, "body": body, "priority": priority, "tags": list(tags), "click": click}


class Notifier:
    def __init__(self):
        self.ntfy_topic  = os.getenv("NTFY_TOPIC", "")
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.ntfy_token  = os.getenv("NTFY_TOKEN", "")
        self.email_to    = os.getenv("EMAIL_TO", "")
        self.smtp_host   = os.getenv("SMTP_HOST", "")
        port_val         = os.getenv("SMTP_PORT", "").strip()
        self.smtp_port   = int(port_val) if port_val.isdigit() else 465
        self.smtp_user   = os.getenv("SMTP_USER", "")
        self.smtp_pass   = os.getenv("SMTP_PASSWORD", "")
        self.email_from  = os.getenv("EMAIL_FROM", self.smtp_user)
        self._client     = httpx.Client(timeout=10, verify=False)

    def configured(self):
        return bool(self.ntfy_topic or (self.email_to and self.smtp_host))

    def send(self, item):
        return any([self._ntfy(item), self._email(item)])

    def _ntfy(self, item):
        if not self.ntfy_topic:
            return False
        headers = {}
        if self.ntfy_token:
            headers["Authorization"] = f"Bearer {self.ntfy_token}"
        payload = {"topic": self.ntfy_topic, "title": item["title"], "message": item["body"],
                   "priority": item["priority"], "tags": item["tags"]}
        if item.get("click"):
            payload["click"] = item["click"]
            payload["actions"] = [{"action": "view", "label": "Ouvrir ENPI", "url": item["click"]}]
        for attempt in range(3):
            try:
                r = self._client.post(self.ntfy_server + "/", json=payload, headers=headers)
                if r.status_code == 200:
                    log.info("[ALERT] ntfy sent OK")
                    return True
                log.error("[ALERT] ntfy HTTP %s: %s", r.status_code, r.text[:100])
            except httpx.HTTPError as e:
                log.error("[ALERT] ntfy error: %r", e)
            time.sleep(2 ** attempt)
        return False

    def _email(self, item):
        if not (self.email_to and self.smtp_host):
            return False
        msg = EmailMessage()
        msg["Subject"] = item["title"]
        msg["From"]    = self.email_from
        msg["To"]      = self.email_to
        msg.set_content(item["body"])
        try:
            cls = smtplib.SMTP_SSL if self.smtp_port == 465 else smtplib.SMTP
            with cls(self.smtp_host, self.smtp_port, timeout=20) as s:
                if self.smtp_port != 465:
                    s.starttls()
                if self.smtp_user:
                    s.login(self.smtp_user, self.smtp_pass)
                s.send_message(msg)
            log.info("[ALERT] Email sent to %s OK", self.email_to)
            return True
        except (smtplib.SMTPException, OSError) as e:
            log.error("[ALERT] Email failed: %r", e)
            return False

    def ping_healthcheck(self, suffix=""):
        url = os.getenv("HEALTHCHECK_URL", "")
        if url:
            try:
                self._client.get(url.rstrip("/") + suffix)
            except httpx.HTTPError:
                pass


def flush_pending(state, notifier):
    if not state["pending"]:
        return
    if not notifier.configured():
        log.warning("[WARNING] No notification channel configured — %d alert(s) queued", len(state["pending"]))
        return
    keep = []
    for item in state["pending"]:
        if not notifier.send(item):
            log.error("[ALERT] Notification FAILED — will retry next run: %s", item["title"])
            keep.append(item)
    state["pending"] = keep[-50:]


def run_once(state, fetcher, notifier, dry_run=False):
    now     = datetime.now(TZ)
    official = INSCRIPTION_URL
    state["last_attempt"] = now.isoformat(timespec="seconds")
    log.info("[INFO] === ENPI LPL Monitor started | Targets: %s ===",
             "ALL" if monitor_all() else ", ".join(targets()))

    events = []
    try:
        log.info("[INFO] Fetching ENPI data")
        snap, failed, warnings = fetcher.scan()
        for w in warnings:
            log.warning("[WARNING] partial failure (trusted data retained): %s", w)
        trusted, events, counters, baseline = reconcile(state["snapshot"], snap, failed, state["missing"])
    except ExtractionError as e:
        state["consecutive_failures"] += 1
        blocked = isinstance(e, BlockedError)
        log.error("[WARNING] ENPI extraction FAILED (%d in a row): %s", state["consecutive_failures"], e)
        log.error("[WARNING] Trusted snapshot retained. No changes inferred.")
        if (blocked or state["consecutive_failures"] >= FAILURE_ALERT_AFTER) and not state["failure_alerted"]:
            title = "ENPI MONITORING BLOCKED" if blocked else "ENPI MONITORING FAILURE"
            body = (f"Derniere analyse reussie: {state['last_success'] or 'jamais'}\n"
                    f"Raison: {e}\nDonnees precedentes conservees.\n"
                    "ATTENTION: L'absence d'alerte ne signifie PAS l'absence de changement.")
            state["pending"].append(_simple_item(title, body, priority=4, click=official))
            state["failure_alerted"] = True
        if not dry_run:
            notifier.ping_healthcheck("/fail")
            flush_pending(state, notifier)
        return []

    n_w = len(trusted["wilayas"])
    n_p = sum(len(w.get("projects") or {}) for w in trusted["wilayas"].values())
    log.info("[INFO] Extraction OK — %d wilayas, %d projects in monitored wilayas", n_w, n_p)
    state["last_success"] = now.isoformat(timespec="seconds")
    if state["failure_alerted"]:
        state["pending"].append(_simple_item("ENPI Monitoring recovered", "Analyses fonctionnelles.", 3, ["white_check_mark"], official))
    state["consecutive_failures"] = 0
    state["failure_alerted"]      = False
    state["snapshot"]             = trusted
    state["missing"]              = counters

    if baseline:
        log.info("[INFO] First run: baseline stored — no alerts sent")
        state["pending"].append(_simple_item(
            "ENPI LPL Monitor demarre",
            f"Reference: {n_w} wilayas, {n_p} projets dans les wilayas cibles.\nVous serez alerte des changements.",
            2, ["white_check_mark"], official
        ))
    elif events:
        for ev in events:
            log.info("[ALERT] %s | Wilaya: %s | Projet: %s | Typologies: %s",
                     ev["type"], ev["wilaya"], ev["project"], ev["typologies"])
        state["pending"].append(_make_alert_item(events, official, now))
    else:
        log.info("[INFO] No changes detected")

    log.info("[INFO] Monitor finished")

    if dry_run:
        log.info("[INFO] dry-run: nothing sent or saved")
        state["pending"] = []
    else:
        notifier.ping_healthcheck()
        flush_pending(state, notifier)

    return events


def discover():
    print("=== ENPI LPL SITE DISCOVERY ===\n")
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True, verify=False,
                          headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    print(f"GET {INSCRIPTION_URL}")
    r = client.get(INSCRIPTION_URL)
    print(f"Status: {r.status_code}  Bytes: {len(r.text)}")
    m = re.search(r"csrfToken:\s*['\"]([a-f0-9]{64})['\"]", r.text)
    csrf = m.group(1) if m else ""
    print(f"CSRF token: {csrf[:20]}..." if csrf else "CSRF token: NOT FOUND")
    soup = BeautifulSoup(r.text, "html.parser")
    sel = soup.find("select", attrs={"name": re.compile(r"wilaya", re.I)})
    wilayas = _parse_options(str(sel)) if sel else []
    print(f"\nWilaya dropdown: {len(wilayas)} options")
    for w in wilayas[:10]:
        print(f"  {w}")
    if not wilayas or not csrf:
        print("\nCannot probe further."); return
    print(f"\n--- Probing projects for wilaya: {wilayas[0]} ---")
    pr = client.post(PROJECTS_API, data={"country_id": wilayas[0]["id"], "csrf_token": csrf},
                     headers={"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": INSCRIPTION_URL})
    print(f"Status: {pr.status_code}  Response: {pr.text[:500]}")
    projects = _parse_options(pr.text)
    print(f"Parsed: {projects[:3]}")
    if projects:
        print(f"\n--- Probing typologies for project: {projects[0]['label']} ---")
        tr = client.post(TYPOLOGY_API, data={"state_id": projects[0]["id"], "csrf_token": csrf},
                         headers={"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": INSCRIPTION_URL})
        print(f"Status: {tr.status_code}  X-Projet-Encours: {tr.headers.get('X-Projet-Encours','(not set)')}")
        print(f"Response: {tr.text[:500]}")
        print(f"Parsed typologies: {_parse_options(tr.text)}")


def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state",   default=str(ROOT / "state.json"))
    ap.add_argument("--test",    action="store_true", help="Send simulated OPPORTUNITY alert")
    ap.add_argument("--discover",action="store_true", help="Probe live site (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="Scan but do not save/send")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _load_dotenv()

    if a.discover:
        discover(); return 0

    notifier = Notifier()

    if a.test:
        now = datetime.now(TZ)
        ev  = _event("OPPORTUNITY", "Tipaza (TEST)", "TEST PROJET 120 LOGTS", ["F3", "F4"], ["F3", "F4"])
        item = _make_alert_item([ev], INSCRIPTION_URL, now)
        item["title"] = "TEST -- " + item["title"]
        results = {"ntfy": notifier._ntfy(item), "email": notifier._email(item)}
        print("Notification results:", results)
        return 0 if any(results.values()) else 1

    state_path = Path(a.state)
    state      = load_state(state_path)
    fetcher    = EnpiFetcher()
    run_once(state, fetcher, notifier, dry_run=a.dry_run)

    if not a.dry_run:
        written = save_state(state_path, state)
        if written:
            log.info("[INFO] state.json updated")
        gh_out = os.getenv("GITHUB_OUTPUT", "")
        if gh_out and state.get("snapshot"):
            with open(gh_out, "a") as f:
                f.write("state_changed=true\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
