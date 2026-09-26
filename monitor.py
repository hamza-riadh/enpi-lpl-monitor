#!/usr/bin/env python3
"""
ENPI 24/7 Housing Opportunity Monitor (LPL + LPP + Facebook)
============================================================
Monitors:
  1. ENPI LPL Portal: https://www.enpi-net.dz/LPL/Inscription.php?lang=fr
  2. ENPI LPP Portal: https://www.enpi-net.dz/ENPI/Inscription.php?lang=fr
  3. Official ENPI Facebook: https://www.facebook.com/ENPI.dz/

Target Wilayas Monitored by Default:
  Alger (16), Tipaza (42), Blida (09), Boumerdes (35)

Dispatches instant alerts via:
  - Telegram Bot (Official, instant, multi-recipient)
  - ntfy Push (Instant mobile push with alarm)
  - WhatsApp CallMeBot (Multi-recipient)
  - Email (SMTP backup)

Usage:
  python monitor.py              # Full scan cycle (LPL + LPP + Facebook)
  python monitor.py --test       # Send simulated OPPORTUNITY alert to all channels
  python monitor.py --dry-run    # Scan + print events; nothing saved/sent
  python monitor.py --discover   # Probe live LPL & LPP structures
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
import urllib.parse
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("enpi")
ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Africa/Algiers")

# ─────────────────────────────────────────────────────────────────
# PROGRAM CONFIGURATIONS (LPL & LPP)
# ─────────────────────────────────────────────────────────────────
PROGRAMS = {
    "LPL": {
        "name": "LPL",
        "title": "Logement Promotionnel Libre (LPL)",
        "base_url": "https://www.enpi-net.dz/LPL/",
        "inscription_url": "https://www.enpi-net.dz/LPL/Inscription.php?lang=fr",
        "projects_api": "https://www.enpi-net.dz/LPL/api/projet-by-wilaya.php",
        "typology_api": "https://www.enpi-net.dz/LPL/api/Typologie-by-projet.php",
        "min_wilayas": 5,
    },
    "LPP": {
        "name": "LPP",
        "title": "Logement Promotionnel Public (LPP)",
        "base_url": "https://www.enpi-net.dz/ENPI/",
        "inscription_url": "https://www.enpi-net.dz/ENPI/Inscription.php?lang=fr",
        "projects_api": "https://www.enpi-net.dz/ENPI/api/projet-by-wilaya.php",
        "typology_api": "https://www.enpi-net.dz/ENPI/api/Typologie-by-projet.php",
        "min_wilayas": 3,
    },
}

# ─────────────────────────────────────────────────────────────────
# FACEBOOK CONFIGURATION & KEYWORDS
# ─────────────────────────────────────────────────────────────────
FACEBOOK_PAGE_URL = "https://www.facebook.com/ENPI.dz/"
TARGET_WILAYA_KEYWORDS = [
    "alger", "tipaza", "blida", "boumerdes", "boumerdès",
    "الجزائر", "تيبازة", "البليدة", "بومرداس",
    "khemis", "larbaa", "ouled yaich", "mouzaia", "bouinan", "sidi abdellah", "zeralda",
    "زرالدة", "بوينان", "سيدي عبد الله", "خميس الخشنة", "الأربعاء"
]
HOUSING_KEYWORDS = [
    "lpl", "lpp", "logement", "logts", "projet", "souscription", "inscription",
    "ouverture", "vente", "quota", "tranche",
    "سكن", "سكنات", "مشروع", "افتتاح", "تسجيل", "مكتتب", "مكتتبين", "اقتناء", "ترقوي", "حصة"
]

DEFAULT_TARGETS = ["Alger", "Tipaza", "Blida", "Boumerdes"]

REMOVAL_CONFIRMATIONS = 2
MASS_REMOVAL_RATIO    = 0.5
MASS_CONFIRMATIONS    = 6
FAILURE_ALERT_AFTER   = 3
HEARTBEAT_HOURS       = 12

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/133.0.0.0 Safari/537.36"
TIMEOUT     = 25
RETRIES     = 3
REQ_DELAY   = 0.4
RETRY_DELAY = 2

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


# ─────────────────────────────────────────────────────────────────
# PORTAL FETCHER (LPL / LPP)
# ─────────────────────────────────────────────────────────────────
class EnpiFetcher:
    def __init__(self, program="LPL"):
        self.program = program.upper()
        self.cfg = PROGRAMS[self.program]

    def _open_session(self):
        client = httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, verify=False,
            headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7"}
        )
        resp = self._get_retry(client, self.cfg["inscription_url"])
        m = re.search(r"csrfToken:\s*['\"]([a-f0-9]{64})['\"]", resp.text)
        if not m:
            m = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([a-f0-9]{64})["\']', resp.text)
        csrf = m.group(1) if m else ""
        if not csrf:
            raise ExtractionError(f"[{self.program}] Could not extract CSRF token")
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
            if attempt < RETRIES - 1 and RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY ** (attempt + 1))
        raise ExtractionError(f"GET failed after {RETRIES} attempts: {last_err}")

    def _post_retry(self, client, url, data, csrf):
        headers = {
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": self.cfg["inscription_url"],
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
            if attempt < RETRIES - 1 and RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY ** (attempt + 1))
        raise ExtractionError(f"POST failed after {RETRIES} attempts: {last_err}")

    def scan(self):
        client, csrf = self._open_session()
        page_resp = self._get_retry(client, self.cfg["inscription_url"])
        wilayas = parse_wilaya_select(page_resp.text)
        log.info("[INFO] [%s] %d wilayas in dropdown", self.program, len(wilayas))
        min_expected = self.cfg.get("min_wilayas", 3)
        if len(wilayas) < min_expected:
            raise ExtractionError(f"[{self.program}] Only {len(wilayas)} wilayas found (expected >= {min_expected})")

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
                    client, self.cfg["projects_api"],
                    {"country_id": w["id"], "csrf_token": csrf}, csrf
                )
                projects = _parse_options(proj_resp.text)
                log.info("[INFO]   [%s] Wilaya %-20s -> %d project(s)", self.program, w["label"], len(projects))

                for p in projects:
                    time.sleep(REQ_DELAY)
                    try:
                        typo_resp = self._post_retry(
                            client, self.cfg["typology_api"],
                            {"state_id": p["id"], "csrf_token": csrf}, csrf
                        )
                        typologies = _parse_options(typo_resp.text)
                        status = typo_resp.headers.get("X-Projet-Encours", "")
                    except ExtractionError as e:
                        log.warning("[WARNING] [%s] Typology fail %s/%s: %s", self.program, w["label"], p["label"], e)
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
                log.warning("[WARNING] [%s] Failed to probe %s: %s", self.program, w["label"], e)

        if scanned == 0:
            log.info("[INFO] [%s] None of target wilayas currently open in dropdown (%d other wilayas open)", self.program, len(wilayas))
        elif failed and len(failed) == scanned:
            raise ExtractionError(f"[{self.program}] Every target probe failed: {'; '.join(warnings)[:300]}")

        return snap, failed, warnings


# ─────────────────────────────────────────────────────────────────
# FACEBOOK FETCHER (PAGE MONITOR)
# ─────────────────────────────────────────────────────────────────
class FacebookFetcher:
    def __init__(self):
        self.rss_url = os.getenv("FACEBOOK_RSS_URL", "").strip()
        self._client = httpx.Client(timeout=15, follow_redirects=True, headers={
            "User-Agent": UA,
            "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8,en;q=0.7"
        })

    def fetch_posts(self) -> list:
        posts = []
        # Priority 1: User-configured RSS feed (e.g. from RSS.app, FetchRSS, or custom bridge)
        if self.rss_url:
            try:
                r = self._client.get(self.rss_url)
                if r.status_code == 200:
                    import xml.etree.ElementTree as ET
                    try:
                        root = ET.fromstring(r.content)
                        for item in root.findall(".//item"):
                            title = (item.findtext("title") or "").strip()
                            desc = (item.findtext("description") or "").strip()
                            link = (item.findtext("link") or "").strip() or FACEBOOK_PAGE_URL
                            guid = (item.findtext("guid") or "").strip() or link
                            pub = (item.findtext("pubDate") or "").strip()
                            posts.append({
                                "id": guid or link,
                                "text": f"{title}\n{desc}".strip(),
                                "url": link,
                                "date": pub
                            })
                    except Exception:
                        soup = BeautifulSoup(r.text, "html.parser")
                        for item in soup.find_all("item"):
                            title = item.find("title").get_text().strip() if item.find("title") else ""
                            desc = item.find("description").get_text().strip() if item.find("description") else ""
                            link_m = re.search(r"<link>(.*?)</link>", str(item), re.I)
                            link = link_m.group(1).strip() if link_m else FACEBOOK_PAGE_URL
                            guid = item.find("guid").get_text().strip() if item.find("guid") else link
                            pub = item.find("pubdate").get_text().strip() if item.find("pubdate") else ""
                            posts.append({
                                "id": guid or link,
                                "text": f"{title}\n{desc}".strip(),
                                "url": link,
                                "date": pub
                            })
                    if posts:
                        log.info("[INFO] Fetched %d posts from Facebook RSS feed", len(posts))
                        return posts
            except Exception as e:
                log.warning("[WARNING] Facebook RSS fetch failed: %s", e)

        # Priority 2: Google News Algeria search for ENPI / LPL / LPP official housing announcements
        try:
            q = 'ENPI OR LPL OR LPP OR "Entreprise Nationale de Promotion Immobilière"'
            url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=fr&gl=DZ&ceid=DZ:fr"
            r = self._client.get(url)
            if r.status_code == 200:
                import xml.etree.ElementTree as ET
                try:
                    root = ET.fromstring(r.content)
                    for item in root.findall(".//item")[:20]:
                        title = (item.findtext("title") or "").strip()
                        desc = (item.findtext("description") or "").strip()
                        link = (item.findtext("link") or "").strip() or FACEBOOK_PAGE_URL
                        pub = (item.findtext("pubDate") or "").strip()
                        posts.append({
                            "id": f"gn_{abs(hash(title))}",
                            "text": f"{title}\n{desc}".strip(),
                            "url": link,
                            "date": pub
                        })
                except Exception:
                    pass
                if posts:
                    log.info("[INFO] Fetched %d recent ENPI announcement items from news feed", len(posts))
        except Exception as e:
            log.warning("[WARNING] Google News ENPI search mirror failed: %s", e)

        # Priority 3: Fallback search query snippet extraction
        if not posts:
            try:
                r = self._client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": "site:facebook.com/ENPI.dz/"}
                )
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    for res in soup.find_all("div", class_="result"):
                        snip = res.find("a", class_="result__snippet")
                        link = res.find("a", class_="result__url")
                        if snip:
                            text = snip.get_text().strip()
                            url = link.get("href", "").strip() if link else FACEBOOK_PAGE_URL
                            if text:
                                posts.append({
                                    "id": norm(text[:70]),
                                    "text": text,
                                    "url": url if "facebook.com" in url else FACEBOOK_PAGE_URL,
                                    "date": ""
                                })
            except Exception as e:
                log.warning("[WARNING] Facebook search mirror query failed: %s", e)

        return posts

    def filter_relevant(self, posts: list, seen_ids: list) -> list:
        relevant = []
        seen_set = set(seen_ids or [])
        for post in posts:
            pid = post.get("id") or norm(post.get("text", "")[:80])
            if pid in seen_set:
                continue
            low_text = norm(post.get("text", ""))
            matched_wilayas = [w for w in TARGET_WILAYA_KEYWORDS if w in low_text]
            has_housing = any(k in low_text for k in HOUSING_KEYWORDS)
            if matched_wilayas and has_housing:
                relevant.append({
                    **post,
                    "id": pid,
                    "wilaya_match": ", ".join(matched_wilayas).title()
                })
        return relevant


# ─────────────────────────────────────────────────────────────────
# RECONCILIATION & DIFF ENGINE
# ─────────────────────────────────────────────────────────────────
def _event(kind, wilaya, project=None, typologies=None, new_typologies=None, program="LPL"):
    return {
        "type": kind,
        "program": program,
        "wilaya": wilaya,
        "project": project,
        "typologies": sorted(typologies or []),
        "new_typologies": sorted(new_typologies or [])
    }


def reconcile(old, new, failed, counters, program="LPL"):
    trusted = copy.deepcopy(new)
    if old is None:
        if failed:
            raise ExtractionError(f"[{program}] First scan incomplete; refusing partial baseline.")
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
        log.warning("[WARNING] [%s] %d/%d projects vanished at once — suspicious, need %d scans to confirm",
                    program, missing_projects, old_projects_total, need)

    events = []
    new_counters = {}

    for kind, wk, pk, tk in missing:
        key = "|".join(x for x in (kind, wk, pk, tk) if x)
        n = counters.get(key, 0) + 1
        ow = old["wilayas"][wk]
        if n >= need:
            if kind == "w":
                events.append(_event("REMOVED_WILAYA", ow["label"], program=program))
            elif kind == "p":
                events.append(_event("REMOVED", ow["label"], ow["projects"][pk]["label"], program=program))
            else:
                op = ow["projects"][pk]
                events.append(_event("REMOVED_TYPOLOGY", ow["label"], op["label"], [op["typologies"][tk]], program=program))
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
            events.append(_event("NEW_WILAYA", nw["label"], program=program))
        if nw.get("projects") is None or (ow is not None and ow.get("projects") is None):
            continue
        old_p = (ow or {}).get("projects") or {}
        for pk, np_ in nw["projects"].items():
            op = old_p.get(pk)
            all_t  = list(np_["typologies"].values())
            added  = [l for k, l in np_["typologies"].items() if op is None or k not in op["typologies"]]
            if op is None:
                kind = "OPPORTUNITY" if all_t else "NEW_PROJECT"
                events.append(_event(kind, nw["label"], np_["label"], all_t, added, program=program))
            elif added:
                kind = "OPPORTUNITY" if not op["typologies"] else "NEW_TYPOLOGY"
                events.append(_event(kind, nw["label"], np_["label"], all_t, added, program=program))

    return trusted, events, new_counters, False


# ─────────────────────────────────────────────────────────────────
# STATE PERSISTENCE (LPL + LPP + FACEBOOK)
# ─────────────────────────────────────────────────────────────────
EPHEMERAL_KEYS = ("last_success", "last_attempt", "saved_at", "last_success_by_program")


def _empty_state():
    return {
        "version": 3,
        "snapshots": {"LPL": None, "LPP": None},
        "snapshot": None,
        "missing": {"LPL": {}, "LPP": {}},
        "seen_fb_posts": [],
        "consecutive_failures": 0,
        "failures_by_program": {"LPL": 0, "LPP": 0},
        "failure_alerted": False,
        "failure_alerted_by_program": {"LPL": False, "LPP": False},
        "last_success": None,
        "last_success_by_program": {"LPL": None, "LPP": None},
        "last_attempt": None,
        "pending": [],
        "saved_at": None,
    }


def load_state(path: Path) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text())
        state = {**_empty_state(), **data}
        # Backward-compatibility migration from version 2
        if "snapshots" not in data or data.get("snapshots") is None:
            old_snap = data.get("snapshot")
            state["snapshots"] = {"LPL": old_snap, "LPP": None}
            state["missing"] = {"LPL": data.get("missing", {}) if isinstance(data.get("missing"), dict) else {}, "LPP": {}}
            cf = data.get("consecutive_failures", 0)
            if isinstance(cf, int):
                state["failures_by_program"] = {"LPL": cf, "LPP": 0}
            elif isinstance(cf, dict):
                state["failures_by_program"] = cf
            fa = data.get("failure_alerted", False)
            if isinstance(fa, bool):
                state["failure_alerted_by_program"] = {"LPL": fa, "LPP": False}
            elif isinstance(fa, dict):
                state["failure_alerted_by_program"] = fa
            ls = data.get("last_success")
            if isinstance(ls, str):
                state["last_success_by_program"] = {"LPL": ls, "LPP": None}
            elif isinstance(ls, dict):
                state["last_success_by_program"] = ls

        if not isinstance(state.get("missing"), dict) or "LPL" not in state["missing"]:
            state["missing"] = {"LPL": state.get("missing", {}) if isinstance(state.get("missing"), dict) else {}, "LPP": {}}

        state["snapshot"] = state["snapshots"].get("LPL")
        state["consecutive_failures"] = max(state.get("failures_by_program", {}).values()) if state.get("failures_by_program") else 0
        state["failure_alerted"] = any(state.get("failure_alerted_by_program", {}).values()) if state.get("failure_alerted_by_program") else False
        return state
    except json.JSONDecodeError as e:
        log.error("[ERROR] Corrupt state file (%s). Resetting.", e)
        return _empty_state()


def save_state(path: Path, state: dict, force=False) -> bool:
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


# ─────────────────────────────────────────────────────────────────
# ALERT FORMATTING & DISPATCH
# ─────────────────────────────────────────────────────────────────
EVENT_META = {
    "OPPORTUNITY":       ("🚨🚨 ENPI — OPPORTUNITE DETECTEE",         5, ["rotating_light", "house"]),
    "NEW_PROJECT":       ("🚨 ENPI — NOUVEAU PROJET",                  4, ["rotating_light"]),
    "NEW_TYPOLOGY":      ("🚨 ENPI — NOUVELLE TYPOLOGIE",              4, ["rotating_light"]),
    "NEW_WILAYA":        ("🚨 ENPI — NOUVELLE WILAYA",                 4, ["rotating_light"]),
    "REMOVED":           ("⚠️ ENPI — PROJET NON DETECTE",              2, ["warning"]),
    "REMOVED_TYPOLOGY":  ("⚠️ ENPI — TYPOLOGIE NON DETECTEE",         2, ["warning"]),
    "REMOVED_WILAYA":    ("⚠️ ENPI — WILAYA NON DETECTEE",            2, ["warning"]),
}

REMOVAL_NOTES = {
    "REMOVED":           "Non confirme comme vendu. Peut etre: epuisement, maintenance, ou probleme temporaire.",
    "REMOVED_TYPOLOGY":  "Peut etre epuisement de stock ou changement de donnees.",
    "REMOVED_WILAYA":    "Peut etre un changement temporaire du site.",
}


def _make_alert_item(events, official_url, now):
    events = sorted(events, key=lambda e: -EVENT_META[e["type"]][1])
    prog = events[0].get("program", "LPL")
    blocks = []
    for ev in events:
        p_name = ev.get("program", prog)
        title_prefix, _, _ = EVENT_META[ev["type"]]
        ev_title = title_prefix.replace("ENPI", f"ENPI {p_name}")
        lines = [ev_title] if len(events) > 1 else []
        lines.append(f"Programme: {p_name}")
        lines.append(f"Wilaya:    {ev['wilaya']}")
        if ev["project"]:
            lines.append(f"Projet:    {ev['project']}")
        if ev["typologies"]:
            lines.append(f"Typologie: {', '.join(ev['typologies'])}")
        if ev["new_typologies"] and ev["type"] == "NEW_TYPOLOGY":
            lines.append(f"Nouvelles: {', '.join(ev['new_typologies'])}")
        note = REMOVAL_NOTES.get(ev["type"])
        if ev["type"] == "OPPORTUNITY":
            note = f"Selectionnable sur le formulaire officiel {p_name}. Verifiez le site pour confirmer."
        if note:
            lines.append(f"Note: {note}")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    body += (f"\n\nDetecte: {now.strftime('%d %B %Y — %H:%M')} (Alger)\n"
             f"Source: Portail inscription ENPI {prog}\nLien: {official_url}")
    top = events[0]["type"]
    title_str = EVENT_META[top][0].replace("ENPI", f"ENPI {prog}")
    if len(events) > 1:
        title_str += f" (+{len(events) - 1})"
    return {"title": title_str, "body": body, "priority": EVENT_META[top][1],
            "tags": EVENT_META[top][2], "click": official_url}


def _facebook_alert_item(post, now):
    w_match = post.get("wilaya_match", "Alger / Tipaza / Blida / Boumerdes")
    title = f"📢 ENPI FACEBOOK — ANNONCE DETECTEE ({w_match})"
    body = (
        f"Publication Facebook detectee:\n\n"
        f"{post['text'][:350]}...\n\n"
        f"Detecte: {now.strftime('%d %B %Y — %H:%M')} (Alger)\n"
        f"Page: {FACEBOOK_PAGE_URL}\n"
        f"Lien: {post['url']}"
    )
    return {
        "title": title,
        "body": body,
        "priority": 4,
        "tags": ["loudspeaker", "information_source"],
        "click": post["url"]
    }


def _simple_item(title, body, priority=3, tags=("warning",), click=""):
    return {"title": title, "body": body, "priority": priority, "tags": list(tags), "click": click}


# ─────────────────────────────────────────────────────────────────
# MULTI-CHANNEL NOTIFIER (Telegram + ntfy + WhatsApp + Email)
# ─────────────────────────────────────────────────────────────────
class Notifier:
    def __init__(self):
        self.ntfy_topic  = os.getenv("NTFY_TOPIC", "")
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.ntfy_token  = os.getenv("NTFY_TOKEN", "")

        # Telegram Bot configuration (token + comma-separated chat IDs)
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_tg_chats        = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
        self.telegram_chats = [c.strip() for c in re.split(r"[,\n]+", raw_tg_chats) if c.strip()]

        # WhatsApp CallMeBot recipients: comma or newline separated list of phone:apikey
        raw_wa = os.getenv("WHATSAPP_TARGETS", "").strip()
        self.whatsapp_targets = []
        if raw_wa:
            for entry in re.split(r"[,\n]+", raw_wa):
                entry = entry.strip()
                if ":" in entry:
                    phone, key = entry.split(":", 1)
                    phone = phone.strip().replace(" ", "").replace("-", "")
                    key = key.strip()
                    if phone and key:
                        self.whatsapp_targets.append((phone, key))

        # Email backup
        self.email_to    = os.getenv("EMAIL_TO", "")
        self.smtp_host   = os.getenv("SMTP_HOST", "")
        port_val         = os.getenv("SMTP_PORT", "").strip()
        self.smtp_port   = int(port_val) if port_val.isdigit() else 465
        self.smtp_user   = os.getenv("SMTP_USER", "")
        self.smtp_pass   = os.getenv("SMTP_PASSWORD", "")
        self.email_from  = os.getenv("EMAIL_FROM", self.smtp_user)

        self._client     = httpx.Client(timeout=10, verify=False)

    def configured(self):
        return bool(
            self.ntfy_topic or
            (self.telegram_token and self.telegram_chats) or
            self.whatsapp_targets or
            (self.email_to and self.smtp_host)
        )

    def send(self, item):
        return any([
            self._telegram(item),
            self._ntfy(item),
            self._whatsapp(item),
            self._email(item)
        ])

    def _telegram(self, item):
        if not (self.telegram_token and self.telegram_chats):
            return False
        title_html = (item.get("title") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body_html  = (item.get("body") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = f"<b>{title_html}</b>\n\n{body_html}"
        payload = {
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if item.get("click"):
            label = "🔗 Ouvrir Inscription ENPI" if "enpi-net.dz" in item["click"] else "🔗 Ouvrir le Post Facebook"
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "url": item["click"]}]]
            }

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        success = False
        for chat_id in self.telegram_chats:
            masked = str(chat_id)[:min(4, len(str(chat_id)))] + "***"
            chat_payload = {**payload, "chat_id": chat_id}
            for attempt in range(3):
                try:
                    r = self._client.post(url, json=chat_payload)
                    if r.status_code == 200 and r.json().get("ok"):
                        log.info("[ALERT] Telegram sent OK to chat %s", masked)
                        success = True
                        break
                    log.error("[ALERT] Telegram HTTP %s to %s: %s", r.status_code, masked, r.text[:100])
                except httpx.HTTPError as e:
                    log.error("[ALERT] Telegram network error to %s: %r", masked, e)
                time.sleep(1 + attempt)
        return success

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
            label = "Ouvrir ENPI" if "enpi-net.dz" in item["click"] else "Ouvrir Facebook"
            payload["actions"] = [{"action": "view", "label": label, "url": item["click"]}]
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

    def _whatsapp(self, item):
        if not self.whatsapp_targets:
            return False
        text = f"*{item['title']}*\n\n{item['body']}"
        success = False
        for phone, apikey in self.whatsapp_targets:
            masked = phone[:min(6, len(phone))] + "***"
            for attempt in range(3):
                try:
                    r = self._client.get(
                        "https://api.callmebot.com/whatsapp.php",
                        params={"phone": phone, "text": text, "apikey": apikey},
                    )
                    low = r.text.lower()
                    if r.status_code == 200 and ("error" not in low or "no error" in low):
                        log.info("[ALERT] WhatsApp sent OK to %s", masked)
                        success = True
                        break
                    log.error("[ALERT] WhatsApp HTTP %s to %s: %s", r.status_code, masked, r.text[:80])
                except httpx.HTTPError as e:
                    log.error("[ALERT] WhatsApp network error to %s: %r", masked, e)
                time.sleep(1 + attempt)
        return success

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


# ─────────────────────────────────────────────────────────────────
# MULTI-ENGINE EXECUTION (LPL + LPP + FACEBOOK)
# ─────────────────────────────────────────────────────────────────
def run_once(state, fetchers, fb_fetcher=None, notifier=None, dry_run=False):
    # Support flexible signature: run_once(state, fetchers, notifier) OR run_once(state, fetchers, fb_fetcher, notifier)
    if isinstance(fb_fetcher, Notifier) or (fb_fetcher is not None and not isinstance(fb_fetcher, FacebookFetcher) and hasattr(fb_fetcher, "send")):
        dry_run = notifier if isinstance(notifier, bool) else False
        notifier = fb_fetcher
        fb_fetcher = None

    if not isinstance(fetchers, dict):
        prog = getattr(fetchers, "program", "LPL")
        fetchers = {prog: fetchers}

    now = datetime.now(TZ)
    state["last_attempt"] = now.isoformat(timespec="seconds")
    log.info("[INFO] === ENPI Multi-Monitor started (LPL + LPP + Facebook) | Targets: %s ===",
             "ALL" if monitor_all() else ", ".join(targets()))

    state.setdefault("snapshots", {"LPL": None, "LPP": None})
    state.setdefault("missing", {"LPL": {}, "LPP": {}})
    state.setdefault("failures_by_program", {"LPL": 0, "LPP": 0})
    state.setdefault("failure_alerted_by_program", {"LPL": False, "LPP": False})
    state.setdefault("last_success_by_program", {"LPL": None, "LPP": None})
    state.setdefault("seen_fb_posts", [])
    state.setdefault("pending", [])

    all_events = []

    # 1. Scan Inscription Portals (LPL and LPP)
    for prog_name in ["LPL", "LPP"]:
        fetcher = fetchers.get(prog_name)
        if not fetcher:
            continue
        cfg = PROGRAMS.get(prog_name, {
            "name": prog_name,
            "title": f"Logement {prog_name}",
            "inscription_url": f"https://www.enpi-net.dz/{prog_name}/Inscription.php?lang=fr"
        })
        try:
            log.info("[INFO] Scanning %s portal (%s)", prog_name, cfg["inscription_url"])
            snap, failed, warnings = fetcher.scan()
            for w in warnings:
                log.warning("[WARNING] [%s] Partial failure (trusted data retained): %s", prog_name, w)

            old_snap = state["snapshots"].get(prog_name)
            trusted, events, counters, baseline = reconcile(
                old_snap, snap, failed,
                state["missing"].get(prog_name, {}),
                program=prog_name
            )
            state["snapshots"][prog_name] = trusted
            if prog_name == "LPL":
                state["snapshot"] = trusted
            state["missing"][prog_name] = counters
            state["failures_by_program"][prog_name] = 0
            state["last_success_by_program"][prog_name] = now.isoformat(timespec="seconds")
            state["last_success"] = now.isoformat(timespec="seconds")

            if state["failure_alerted_by_program"].get(prog_name):
                state["pending"].append(_simple_item(
                    f"ENPI {prog_name} Monitoring recovered",
                    f"Analyses {prog_name} fonctionnelles.",
                    3, ["white_check_mark"], cfg["inscription_url"]
                ))
                state["failure_alerted_by_program"][prog_name] = False

            if baseline:
                log.info("[INFO] [%s] Baseline stored — no alerts sent", prog_name)
                state["pending"].append(_simple_item(
                    f"ENPI {prog_name} Monitor demarre",
                    f"Reference {prog_name}: {len(trusted['wilayas'])} wilayas detectees.\nVous serez alerte des nouvelles opportunites.",
                    2, ["white_check_mark"], cfg["inscription_url"]
                ))
            elif events:
                for ev in events:
                    log.info("[ALERT] [%s] %s | Wilaya: %s | Projet: %s | Typologies: %s",
                             prog_name, ev["type"], ev["wilaya"], ev["project"], ev["typologies"])
                state["pending"].append(_make_alert_item(events, cfg["inscription_url"], now))
                all_events.extend(events)
            else:
                log.info("[INFO] [%s] No changes detected", prog_name)

        except ExtractionError as e:
            cf = state["failures_by_program"].get(prog_name, 0) + 1
            state["failures_by_program"][prog_name] = cf
            blocked = isinstance(e, BlockedError)
            log.error("[WARNING] [%s] Extraction FAILED (%d in a row): %s", prog_name, cf, e)
            if (blocked or cf >= FAILURE_ALERT_AFTER) and not state["failure_alerted_by_program"].get(prog_name, False):
                title = f"ENPI {prog_name} MONITORING FAILURE"
                body = (f"Derniere analyse {prog_name} reussie: {state['last_success_by_program'].get(prog_name) or 'jamais'}\n"
                        f"Raison: {e}\nDonnees precedentes conservees.")
                state["pending"].append(_simple_item(title, body, priority=4, click=cfg["inscription_url"]))
                state["failure_alerted_by_program"][prog_name] = True

    state["consecutive_failures"] = max(state["failures_by_program"].values()) if state.get("failures_by_program") else 0
    state["failure_alerted"] = any(state["failure_alerted_by_program"].values()) if state.get("failure_alerted_by_program") else False

    # 2. Scan Official Facebook Page
    if fb_fetcher:
        try:
            log.info("[INFO] Checking ENPI Facebook page for new announcements...")
            fb_posts = fb_fetcher.fetch_posts()
            relevant = fb_fetcher.filter_relevant(fb_posts, state.get("seen_fb_posts", []))
            if relevant:
                log.info("[ALERT] Found %d relevant new Facebook post(s)!", len(relevant))
                for post in relevant:
                    state.setdefault("seen_fb_posts", []).append(post["id"])
                    state["pending"].append(_facebook_alert_item(post, now))
                    all_events.append({"type": "FACEBOOK_POST", "post": post})
            else:
                log.info("[INFO] Facebook check OK — no new target announcements")
            state["seen_fb_posts"] = state.get("seen_fb_posts", [])[-200:]
        except Exception as e:
            log.warning("[WARNING] Facebook check skipped: %s", e)

    log.info("[INFO] Multi-Monitor cycle finished")

    if dry_run:
        log.info("[INFO] dry-run: nothing sent or saved")
        state["pending"] = []
    else:
        if notifier:
            notifier.ping_healthcheck()
            flush_pending(state, notifier)

    return all_events


def discover():
    print("=== ENPI MULTI-SITE DISCOVERY (LPL & LPP) ===\n")
    client = httpx.Client(timeout=TIMEOUT, follow_redirects=True, verify=False,
                          headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})

    for prog_name, cfg in PROGRAMS.items():
        print(f"\n{'='*20} {prog_name}: {cfg['title']} {'='*20}")
        print(f"GET {cfg['inscription_url']}")
        r = client.get(cfg["inscription_url"])
        print(f"Status: {r.status_code}  Bytes: {len(r.text)}")
        m = re.search(r"csrfToken:\s*['\"]([a-f0-9]{64})['\"]", r.text)
        csrf = m.group(1) if m else ""
        print(f"CSRF token: {csrf[:20]}..." if csrf else "CSRF token: NOT FOUND")
        soup = BeautifulSoup(r.text, "html.parser")
        sel = soup.find("select", attrs={"name": re.compile(r"wilaya", re.I)})
        wilayas = _parse_options(str(sel)) if sel else []
        print(f"Wilaya dropdown: {len(wilayas)} options")
        for w in wilayas[:6]:
            print(f"  {w}")

        if wilayas and csrf:
            print(f"\n--- Probing projects for {prog_name} first wilaya: {wilayas[0]['label']} ---")
            pr = client.post(cfg["projects_api"], data={"country_id": wilayas[0]["id"], "csrf_token": csrf},
                             headers={"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": cfg["inscription_url"]})
            print(f"Status: {pr.status_code}  Response: {pr.text[:300]}")
            projects = _parse_options(pr.text)
            print(f"Parsed projects: {projects[:2]}")
            if projects:
                print(f"--- Probing typologies for project: {projects[0]['label']} ---")
                tr = client.post(cfg["typology_api"], data={"state_id": projects[0]["id"], "csrf_token": csrf},
                                 headers={"X-CSRF-Token": csrf, "X-Requested-With": "XMLHttpRequest", "Referer": cfg["inscription_url"]})
                print(f"Status: {tr.status_code}  Typologies: {_parse_options(tr.text)}")


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
        discover()
        return 0

    notifier = Notifier()

    if a.test:
        now = datetime.now(TZ)
        ev  = _event("OPPORTUNITY", "Boumerdes (TEST)", "TEST 150 LOGTS BOUMERDES VILLE", ["F3", "F4"], ["F3", "F4"], program="LPL")
        item = _make_alert_item([ev], PROGRAMS["LPL"]["inscription_url"], now)
        item["title"] = "TEST -- " + item["title"]
        results = {
            "telegram": notifier._telegram(item),
            "ntfy": notifier._ntfy(item),
            "whatsapp": notifier._whatsapp(item),
            "email": notifier._email(item),
        }
        print("Notification results:", results)
        return 0 if any(results.values()) else 1

    state_path = Path(a.state)
    state      = load_state(state_path)
    fetchers   = {
        "LPL": EnpiFetcher("LPL"),
        "LPP": EnpiFetcher("LPP"),
    }
    fb_fetcher = FacebookFetcher()
    run_once(state, fetchers, fb_fetcher, notifier, dry_run=a.dry_run)

    if not a.dry_run:
        written = save_state(state_path, state)
        if written:
            log.info("[INFO] state.json updated")
        gh_out = os.getenv("GITHUB_OUTPUT", "")
        if gh_out and state.get("snapshots"):
            with open(gh_out, "a") as f:
                f.write("state_changed=true\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
