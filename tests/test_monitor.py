"""
ENPI LPL Monitor — Unit Tests
Uses httpx.MockTransport. No real network calls.
"""
import copy, json, sys
from pathlib import Path
from urllib.parse import unquote_plus
import httpx, pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m
m.REQ_DELAY = 0    # zero network delay during unit tests
m.RETRY_DELAY = 0  # zero backoff delay during unit tests

# ── shared constants ───────────────────────────────────────────────────────
CSRF = "a" * 64
WILAYAS = [
    "1 - Adrar", "2 - Chlef", "9 - Blida", "16 - Alger",
    "35 - Boumerdes", "42 - Tipaza", "19 - Setif", "25 - Constantine",
    "23 - Annaba", "15 - Tizi Ouzou", "14 - Tiaret",
]


# ── Fake ENPI site ─────────────────────────────────────────────────────────
class FakeSite:
    """data = {norm(wilaya): {project_label: [typologies]}}"""

    def __init__(self, data=None):
        self.data  = data if data is not None else {
            "9 - blida":     {"12 Villas Mouzaia": ["F5", "F6"]},
            "35 - boumerdes":{"86 LOGTS KHEMIS":   ["F3", "F4"]},
            "42 - tipaza":   {},   # known target wilaya, no projects yet
            "16 - alger":    {},   # known target wilaya, no projects yet
        }
        self.mode  = "ok"
        self.calls = 0

    def _body(self, req):
        d = {}
        if req.content:
            for pair in req.content.decode().split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    d[k] = unquote_plus(v)   # decode + → space, %20 → space etc.
        return d

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        path = req.url.path

        # ── main page ──
        if "Inscription.php" in path:
            if self.mode == "blocked":
                return httpx.Response(403, text="Forbidden")
            if self.mode == "garbage":
                return httpx.Response(200, text="<html><body><h1>Maintenance</h1></body></html>")
            if self.mode == "down":
                raise httpx.ConnectError("boom")
            opts = "".join(
                f'<option value="{i+1}">{w}</option>'
                for i, w in enumerate(WILAYAS)
            )
            html = (
                f'<html><script>window.CONFIG = {{csrfToken: "{CSRF}", lang: "fr"}};</script>'
                f'<form><select name="wilaya">'
                f'<option value="">choisir...</option>{opts}'
                f'</select></form></html>'
            )
            return httpx.Response(200, text=html)

        # ── projects API ──
        if "projet-by-wilaya" in path:
            if self.mode == "down":
                raise httpx.ConnectError("boom")
            if self.mode in ("error_json", "empty"):
                return httpx.Response(200, text='{"error":"SQL failure"}' if self.mode == "error_json" else "")
            body = self._body(req)
            cid  = body.get("country_id", "0")
            try:
                w_label = WILAYAS[int(cid) - 1]
            except (ValueError, IndexError):
                return httpx.Response(200, text='<option value="">choisir Projet:</option>')
            w_norm = m.norm(w_label)
            projs  = self.data.get(w_norm, {})
            opts   = "".join(f'<option value="proj_{p}">{p}</option>' for p in projs)
            return httpx.Response(200, text=f'<option value="">choisir Projet:</option>{opts}')

        # ── typologies API ──
        if "Typologie" in path:
            if self.mode == "down":
                raise httpx.ConnectError("boom")
            if self.mode in ("error_json", "empty"):
                return httpx.Response(200, text='{"error":"x"}' if self.mode == "error_json" else "")
            body    = self._body(req)
            sid     = body.get("state_id", "")
            p_label = sid.replace("proj_", "") if sid.startswith("proj_") else ""
            typos   = []
            for _, projs in self.data.items():
                if p_label in projs:
                    typos = projs[p_label]
                    break
            opts = "".join(f'<option value="{t}">{t}</option>' for t in typos)
            return httpx.Response(200, text=f'<option value="">choisir...</option>{opts}')

        return httpx.Response(404, text="Not Found")


# ── Fake notifier ──────────────────────────────────────────────────────────
class FakeNotifier:
    def __init__(self, ok=True):
        self.ok   = ok
        self.sent = []

    def configured(self):
        return True

    def send(self, item):
        if self.ok:
            self.sent.append(item)
        return self.ok

    def ping_healthcheck(self, suffix=""):
        pass


# ── Test harness ───────────────────────────────────────────────────────────
class Env:
    def __init__(self, site=None, notifier=None):
        self.site     = site or FakeSite()
        self.notifier = notifier or FakeNotifier()
        transport     = httpx.MockTransport(self.site.handler)
        self._client  = httpx.Client(transport=transport, follow_redirects=True)
        self.fetcher  = m.EnpiFetcher()
        self.fetcher._open_session = lambda: (self._client, CSRF)
        self.state    = m._empty_state()

    def run(self):
        return m.run_once(self.state, self.fetcher, self.notifier)

    @property
    def sent(self):
        return self.notifier.sent


def types(events):
    return [e["type"] for e in events]


# ═══════════════════════════════════════════════════════════════════
# TEST CASES
# ═══════════════════════════════════════════════════════════════════

def test_baseline_no_alert():
    """First run stores baseline, sends 'started' notice, no change events."""
    e = Env()
    evs = e.run()
    assert evs == []
    assert len(e.sent) == 1
    title = e.sent[0]["title"].lower()
    assert "demarre" in title or "started" in title or "monitor" in title


def test_unchanged_no_alert():
    """Second run with identical data → no events, no alerts."""
    e = Env()
    e.run(); e.sent.clear()
    assert e.run() == []
    assert e.sent == []


def test_new_project_no_typology():
    """New project with 0 typologies → NEW_PROJECT."""
    e = Env()
    e.run(); e.sent.clear()
    e.site.data["42 - tipaza"] = {"Nouveau Projet Vide": []}
    ev = e.run()
    assert types(ev) == ["NEW_PROJECT"]


def test_new_project_with_typologies_is_opportunity():
    """New project with typologies → OPPORTUNITY (priority 5)."""
    e = Env()
    e.run(); e.sent.clear()
    e.site.data["42 - tipaza"] = {"XYZ 120 LOGTS KHEMISTI": ["F3", "F4"]}
    ev = e.run()
    assert types(ev) == ["OPPORTUNITY"]
    body = e.sent[0]["body"]
    assert "XYZ" in body
    assert e.sent[0]["priority"] == 5


def test_project_gaining_first_typology_is_opportunity():
    """Project goes from 0 typologies to 1+ → OPPORTUNITY."""
    e = Env()
    e.site.data["42 - tipaza"] = {"Later": []}
    e.run(); e.sent.clear()
    e.site.data["42 - tipaza"]["Later"] = ["F4"]
    ev = e.run()
    assert types(ev) == ["OPPORTUNITY"]


def test_new_typology_on_existing_project():
    """Existing project gains extra typology → NEW_TYPOLOGY."""
    e = Env()
    e.run(); e.sent.clear()
    # Replace the whole list (not append) to avoid mutating shared reference in snapshot
    e.site.data["9 - blida"]["12 Villas Mouzaia"] = ["F5", "F6", "F3"]
    ev = e.run()
    assert types(ev) == ["NEW_TYPOLOGY"]
    assert "F3" in ev[0]["new_typologies"]


def test_no_repeat_alert():
    """Once alerted, subsequent identical scans are silent."""
    e = Env()
    e.run()
    e.site.data["42 - tipaza"] = {"XYZ": ["F3"]}
    assert len(e.run()) == 1
    n = len(e.sent)
    for _ in range(5):
        assert e.run() == []
    assert len(e.sent) == n


def test_removal_needs_two_scans():
    """Removed project is only confirmed after 2 consecutive misses."""
    e = Env()
    e.site.data["9 - blida"]["P2"] = ["F3"]
    e.run(); e.sent.clear()
    del e.site.data["9 - blida"]["P2"]
    assert e.run() == []                   # 1st miss: retain trusted data
    ev = e.run()                           # 2nd miss: confirmed
    assert types(ev) == ["REMOVED"]
    txt = (e.sent[-1]["title"] + e.sent[-1]["body"]).lower()
    assert "non detecte" in txt or "removed" in txt
    # Alert must flag uncertainty — "non confirme" means it's not declared sold out
    assert "non confirme" in txt or "non detecte" in txt


def test_glitch_then_recovery_no_alert():
    """Transient 1-scan removal then reappearance → no alert."""
    e = Env()
    e.site.data["9 - blida"]["P2"] = ["F3"]
    e.run(); e.sent.clear()
    saved = e.site.data["9 - blida"].pop("P2")
    e.run()   # 1 miss
    e.site.data["9 - blida"]["P2"] = saved
    assert e.run() == []
    assert e.sent == []


def test_mass_disappearance_not_believed():
    """All projects vanishing at once → suspicious, hold off alerts."""
    e = Env()
    e.site.data["9 - blida"]["A"] = ["F3"]
    e.site.data["9 - blida"]["B"] = ["F4"]
    e.run(); e.sent.clear()
    e.site.data["9 - blida"]     = {}
    e.site.data["35 - boumerdes"] = {}
    for _ in range(5):
        assert e.run() == []
    assert e.state["snapshot"] is not None   # trusted data retained


def test_website_down_keeps_snapshot():
    """Connection failure retains trusted snapshot, alerts after threshold."""
    e = Env()
    e.run(); e.sent.clear()
    snap_before = json.dumps(e.state["snapshot"], sort_keys=True)
    e.site.mode = "down"
    for _ in range(2):
        assert e.run() == []
    assert e.sent == []              # not yet alerted
    e.run()                          # 3rd failure → MONITORING FAILURE
    assert len(e.sent) == 1
    assert "FAILURE" in e.sent[0]["title"].upper() or "MONITOR" in e.sent[0]["title"].upper()
    e.run(); e.run()
    assert len(e.sent) == 1         # no repeated alerts
    assert json.dumps(e.state["snapshot"], sort_keys=True) == snap_before
    e.site.mode = "ok"
    e.run()
    assert "recover" in e.sent[-1]["title"].lower()
    assert e.state["consecutive_failures"] == 0


def test_garbage_html_is_failure_not_change():
    """Maintenance/garbage page → ExtractionError, snapshot unchanged."""
    e = Env()
    e.run(); e.sent.clear()
    e.site.mode = "garbage"
    assert e.run() == []
    assert e.state["consecutive_failures"] == 1
    assert e.state["snapshot"] is not None   # retained


def test_blocked_alerts_immediately():
    """HTTP 403 (bot-block) → immediate alert, does not retry around it."""
    e = Env()
    e.run(); e.sent.clear()
    e.site.mode = "blocked"
    e.run()
    titles = " ".join(x["title"].upper() for x in e.sent)
    assert "BLOCK" in titles or "FAILURE" in titles or "MONITOR" in titles


def test_notification_failure_queues_alert():
    """Failed send is queued and retried on next run."""
    n = FakeNotifier(ok=False)
    e = Env(notifier=n)
    e.run(); e.state["pending"].clear()
    e.site.data["42 - tipaza"] = {"XYZ": ["F3"]}
    e.run()
    assert len(e.state["pending"]) == 1
    n.ok = True
    e.run()
    assert e.state["pending"] == []
    assert "OPPORTUNIT" in n.sent[-1]["title"].upper()


def test_state_not_written_when_unchanged(tmp_path):
    """save_state returns False when nothing material changed."""
    p = tmp_path / "state.json"
    e = Env()
    e.run()
    assert m.save_state(p, e.state) is True
    e.run()
    assert m.save_state(p, e.state) is False
    e.site.data["42 - tipaza"] = {"NEW": ["F3"]}
    e.run()
    assert m.save_state(p, e.state) is True


def test_norm_accent_fold():
    assert m.norm("  TIPAZA  ") == "tipaza"
    assert m.norm("Boumerdès")  == m.norm("BOUMERDES")
    assert m.norm("16 - Alger") == "16 - alger"


def test_parse_options_filters_placeholders():
    html = '<option value="">Choisir...</option><option value="1">Projet X</option>'
    result = m._parse_options(html)
    assert len(result) == 1
    assert result[0]["label"] == "Projet X"


def test_parse_options_json_error_raises():
    with pytest.raises(m.ExtractionError):
        m._parse_options('{"error": "SQL failure"}')


def test_first_scan_partial_failure_rejects_baseline():
    """If first scan has failing wilayas, refuse to store incomplete baseline."""
    e = Env()
    e.site.mode = "error_json"
    e.run()
    assert e.state["snapshot"] is None


def test_new_wilaya_appears():
    """A new wilaya appearing in the dropdown → NEW_WILAYA event."""
    e = Env()
    e.run(); e.sent.clear()
    WILAYAS.append("58 - Nouvelle Wilaya")
    e.site.data["58 - nouvelle wilaya"] = {}
    try:
        ev = e.run()
        assert any(ev_["type"] == "NEW_WILAYA" for ev_ in ev)
    finally:
        WILAYAS.remove("58 - Nouvelle Wilaya")
        e.site.data.pop("58 - nouvelle wilaya", None)


def test_whatsapp_notifier_multi_recipient(monkeypatch):
    """WhatsApp alerts are dispatched to all configured recipients."""
    monkeypatch.setenv("WHATSAPP_TARGETS", "+213555123456:key1, +213770987654:key2")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("EMAIL_TO", raising=False)

    notifier = m.Notifier()
    assert notifier.configured() is True
    assert len(notifier.whatsapp_targets) == 2
    assert notifier.whatsapp_targets[0] == ("+213555123456", "key1")
    assert notifier.whatsapp_targets[1] == ("+213770987654", "key2")

    calls = []
    def fake_handler(req: httpx.Request) -> httpx.Response:
        calls.append(dict(req.url.params))
        return httpx.Response(200, text="Message queued")

    notifier._client = httpx.Client(transport=httpx.MockTransport(fake_handler))
    item = {"title": "🚨 OPPORTUNITE", "body": "Wilaya: Alger\nProjet: X"}
    assert notifier._whatsapp(item) is True
    assert len(calls) == 2
    assert calls[0]["phone"] == "+213555123456"
    assert calls[0]["apikey"] == "key1"
    assert calls[1]["phone"] == "+213770987654"
    assert calls[1]["apikey"] == "key2"
    assert "OPPORTUNITE" in calls[0]["text"]


def test_whatsapp_invalid_target_ignored(monkeypatch):
    """Malformed targets without colon are safely ignored."""
    monkeypatch.setenv("WHATSAPP_TARGETS", "bad_entry, +213111222333:validkey, :missingphone")
    notifier = m.Notifier()
    assert len(notifier.whatsapp_targets) == 1
    assert notifier.whatsapp_targets[0] == ("+213111222333", "validkey")


def test_telegram_notifier_multi_recipient(monkeypatch):
    """Telegram alerts are dispatched to all configured chat IDs with buttons."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "111222, 333444")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("EMAIL_TO", raising=False)
    monkeypatch.delenv("WHATSAPP_TARGETS", raising=False)

    notifier = m.Notifier()
    assert notifier.configured() is True
    assert len(notifier.telegram_chats) == 2
    assert notifier.telegram_chats[0] == "111222"
    assert notifier.telegram_chats[1] == "333444"

    calls = []
    def fake_handler(req: httpx.Request) -> httpx.Response:
        data = json.loads(req.content.decode())
        calls.append(data)
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier._client = httpx.Client(transport=httpx.MockTransport(fake_handler))
    item = {"title": "🚨 OPPORTUNITE", "body": "Wilaya: Alger", "click": "https://example.com"}
    assert notifier._telegram(item) is True
    assert len(calls) == 2
    assert calls[0]["chat_id"] == "111222"
    assert calls[1]["chat_id"] == "333444"
    assert "OPPORTUNITE" in calls[0]["text"]
    assert calls[0]["parse_mode"] == "HTML"
    assert "reply_markup" in calls[0]


def test_multi_engine_lpl_and_lpp():
    """Both LPL and LPP scan concurrently and report opportunities with their program label."""
    site_lpl = FakeSite()
    site_lpp = FakeSite()
    client_lpl = httpx.Client(transport=httpx.MockTransport(site_lpl.handler))
    client_lpp = httpx.Client(transport=httpx.MockTransport(site_lpp.handler))

    fetcher_lpl = m.EnpiFetcher("LPL")
    fetcher_lpl._open_session = lambda: (client_lpl, CSRF)
    fetcher_lpp = m.EnpiFetcher("LPP")
    fetcher_lpp._open_session = lambda: (client_lpp, CSRF)

    notifier = FakeNotifier()
    state = m._empty_state()

    # Run 1: Baselines stored for both
    ev1 = m.run_once(state, {"LPL": fetcher_lpl, "LPP": fetcher_lpp}, notifier)
    assert ev1 == []
    assert state["snapshots"]["LPL"] is not None
    assert state["snapshots"]["LPP"] is not None
    notifier.sent.clear()

    # Run 2: LPL adds project in Boumerdes, LPP adds project in Tipaza
    site_lpl.data["35 - boumerdes"]["Nouveau LPL Boumerdes"] = ["F3", "F4"]
    site_lpp.data["42 - tipaza"]["Nouveau LPP Tipaza"] = ["F4", "F5"]

    ev2 = m.run_once(state, {"LPL": fetcher_lpl, "LPP": fetcher_lpp}, notifier)
    assert len(ev2) == 2
    progs = {e["program"] for e in ev2}
    assert progs == {"LPL", "LPP"}
    assert any("LPL" in alert["title"] for alert in notifier.sent)
    assert any("LPP" in alert["title"] for alert in notifier.sent)


def test_resilience_lpl_down_lpp_continues():
    """If LPL fails, LPP continues scanning and alerting normally."""
    site_lpl = FakeSite()
    site_lpp = FakeSite()
    site_lpl.mode = "down"

    client_lpl = httpx.Client(transport=httpx.MockTransport(site_lpl.handler))
    client_lpp = httpx.Client(transport=httpx.MockTransport(site_lpp.handler))

    fetcher_lpl = m.EnpiFetcher("LPL")
    fetcher_lpl._open_session = lambda: (client_lpl, CSRF)
    fetcher_lpp = m.EnpiFetcher("LPP")
    fetcher_lpp._open_session = lambda: (client_lpp, CSRF)

    notifier = FakeNotifier()
    state = m._empty_state()

    # Scan should not crash, LPP gets baseline
    m.run_once(state, {"LPL": fetcher_lpl, "LPP": fetcher_lpp}, notifier)
    assert state["snapshots"]["LPL"] is None
    assert state["snapshots"]["LPP"] is not None
    assert state["failures_by_program"]["LPL"] == 1
    assert state["failures_by_program"]["LPP"] == 0


def test_facebook_keyword_matching_french_and_arabic():
    """Facebook post filtering detects target wilayas in French and Arabic with housing keywords."""
    fb = m.FacebookFetcher()
    posts = [
        {"id": "1", "text": "Ouverture des souscriptions pour 120 logements LPL à Boumerdes", "url": "https://fb.com/1"},
        {"id": "2", "text": "المؤسسة الوطنية للترقية العقارية تعلن عن افتتاح تسجيلات لاقتناء سكنات ترقوي حر بتيبازة", "url": "https://fb.com/2"},
        {"id": "3", "text": "Disponibilité de quotas LPP à Sidi Abdellah Alger", "url": "https://fb.com/3"},
        {"id": "4", "text": "Projet de 50 logements à Oran Es-Senia", "url": "https://fb.com/4"},  # Non-target wilaya
        {"id": "5", "text": "عيد فطر مبارك لكافة المكتتبين والعمال", "url": "https://fb.com/5"},      # No housing/target project
    ]
    relevant = fb.filter_relevant(posts, seen_ids=[])
    assert len(relevant) == 3
    rel_ids = [p["id"] for p in relevant]
    assert rel_ids == ["1", "2", "3"]


def test_facebook_post_deduplication():
    """Already seen Facebook posts are ignored on next cycle."""
    fb = m.FacebookFetcher()
    posts = [
        {"id": "post_100", "text": "Projet 80 logts LPL Blida Bouinan", "url": "https://fb.com/100"},
    ]
    # First time: relevant
    r1 = fb.filter_relevant(posts, seen_ids=[])
    assert len(r1) == 1

    # Second time with post_100 in seen_ids: ignored
    r2 = fb.filter_relevant(posts, seen_ids=["post_100"])
    assert len(r2) == 0


def test_facebook_rss_parsing():
    """FacebookFetcher parses RSS XML format correctly."""
    xml_data = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>ENPI Official</title>
        <item>
          <title>Nouveau projet Boumerdes</title>
          <description>Vente de logements LPL disponibles</description>
          <link>https://facebook.com/ENPI.dz/posts/999</link>
          <guid>guid_999</guid>
          <pubDate>Mon, 26 Sep 2026 12:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>"""
    fb = m.FacebookFetcher()
    fb.rss_url = "https://mock-rss.local/feed"
    fb._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=xml_data)))
    posts = fb.fetch_posts()
    assert len(posts) == 1
    assert posts[0]["id"] == "guid_999"
    assert "Boumerdes" in posts[0]["text"]
    assert "https://facebook.com/ENPI.dz/posts/999" in posts[0]["url"]

