"""A new install's blank business details (and an existing one keeping its
own), what the page may send (response times, money, settings names, HTTP
methods), the command line refusing a mistyped database path, the daily
check's plain wording and its report on the top leads, and the lookup
working down the list from the top."""

import http.client
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest
from test_lead_rules import eviction

from leadgen import daily, db, leadlist, outreach
from leadgen.business import Contact
from leadgen.lookup import find_contacts
from leadgen.routes import handle
from leadgen.web import App, Handler


def post(app, route, payload):
    return handle(app, "POST", route, "", {"Host": "x"}, json.dumps(payload).encode())


class NoMap:
    def geocode(self, *a, **kw):
        return None


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    eviction(conn, "CV26-000001-EA", "2026-09-30", defendant="DOE, PAT")
    return App(path, geocoder=NoMap())


def lead_id(app):
    with app.conn() as conn:
        return conn.execute("SELECT id FROM leads").fetchone()["id"]


# ---- business details ---------------------------------------------------------


def test_a_new_database_starts_with_no_business_name_or_base_and_no_miles(tmp_path):
    path = tmp_path / "new.db"
    db.connect(path)
    app = App(path, geocoder=NoMap())
    settings = app.state()["settings"]
    assert settings["business_name"] == "" and settings["base_address"] == ""
    app._ensure_base()  # nothing to look up: no base address
    assert app.state()["settings"]["base_lat"] is None


def test_a_database_in_use_keeps_the_details_it_ran_with(tmp_path):
    """Made before the defaults went blank (leads, no saved name): the old
    values are saved into it once, so a live install doesn't lose them."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    eviction(conn, "CV26-000001-EA", "2026-09-30")
    # As a database from before: no marker, nothing typed in Settings.
    conn.execute("DELETE FROM settings WHERE key IN ('old_defaults_kept', 'business_name', 'base_address')")
    conn.commit()
    db.forget_ready()
    db.connect(path)
    saved = db.get_settings(db.connect(path))
    assert saved["business_name"] == outreach.LEGACY_DEFAULTS["business_name"]
    assert saved["base_address"] == outreach.LEGACY_DEFAULTS["base_address"]

    # One with its own name keeps it.
    other = tmp_path / "own.db"
    conn = db.connect(other)
    eviction(conn, "CV26-000002-EA", "2026-09-30")
    conn.execute("DELETE FROM settings WHERE key = 'old_defaults_kept'")
    db.put_settings(conn, {"business_name": "Desert Haul"})
    conn.commit()
    db.forget_ready()
    db.connect(other)
    saved = db.get_settings(db.connect(other))
    assert saved["business_name"] == "Desert Haul" and saved["base_address"] == outreach.LEGACY_DEFAULTS["base_address"]


def test_clearing_the_base_address_hides_the_miles(app):
    with app.conn() as conn:
        db.put_settings(conn, {"base_address": "1 Base St", "base_lat": 32.3, "base_lon": -111.0})
    assert app.save_settings({"base_address": ""})["ok"]
    settings = app.state()["settings"]
    assert settings["base_lat"] is None and settings["base_lon"] is None


# ---- what the page sends ------------------------------------------------------


@pytest.mark.parametrize(
    "value, words",
    [
        ("1900-01-01", "date and time"),
        ("not a date", "date and time"),
        ((datetime.now(timezone.utc) + timedelta(days=3)).isoformat(), "future"),
        ("2026-09-01T10:00", "before the lead was filed"),
        (12, "date and time"),
    ],
)
def test_a_response_time_must_be_a_real_time_after_the_filing(app, value, words):
    status, body, _ = post(app, "/api/lead", {"id": lead_id(app), "fields": {"responded_at": value}})
    assert status == 400 and body["field"] == "responded_at" and words in body["error"]


def test_a_valid_response_time_is_saved_in_utc(app):
    status, _, _ = post(app, "/api/lead", {"id": lead_id(app), "fields": {"responded_at": "2026-10-01T09:30"}})
    assert status == 200
    with app.conn() as conn:
        saved = conn.execute("SELECT responded_at FROM leads").fetchone()["responded_at"]
    assert saved == "2026-10-01T16:30:00+00:00"  # 9:30 AM in Tucson


def test_amounts_are_whole_cents_by_decimal_arithmetic(app):
    lid = lead_id(app)
    for amount in ("12.345", "12.355", 12.345):
        status, body, _ = post(app, "/api/lead", {"id": lid, "fields": {"quote_amount": amount}})
        assert status == 400 and body["field"] == "quote_amount" and "2 decimal places" in body["error"]
    for amount, cents in (("12.35", 1235), ("$1,234.50", 123450), (0.1, 10), ("19.99", 1999), (250, 25000)):
        status, _, _ = post(app, "/api/lead", {"id": lid, "fields": {"quote_amount": amount}})
        assert status == 200
        with app.conn() as conn:
            assert conn.execute("SELECT quote_cents FROM leads").fetchone()["quote_cents"] == cents


def test_an_unknown_setting_is_refused_by_name(app):
    status, body, _ = post(app, "/api/settings", {"business_address": "", "lead_view": "all"})
    assert status == 400 and "business_address" in body["error"] and body["field"] == "business_address"
    assert app.state()["settings"]["lead_view"] != "all"  # nothing saved


def test_other_http_methods_get_the_json_405(app):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"app": app}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for method in ("DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"):
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            c.request(method, "/api/lead")
            r = c.getresponse()
            body = r.read()
            assert r.status == 405 and r.getheader("Content-Type") == "application/json", method
            if method != "HEAD":
                assert json.loads(body) == {"error": "That request isn't allowed."}
            c.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---- the command line ---------------------------------------------------------


@pytest.mark.parametrize("argv", [["list"], ["export"], ["status", "1", "won"], ["age"]])
def test_a_mistyped_database_path_is_an_error_not_an_empty_list(tmp_path, argv, capsys):
    from leadgen import cli

    missing = tmp_path / "nonexistent" / "x.db"
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(missing), *argv])
    assert e.value.code and "No lead database at" in str(e.value.code) and str(missing) in str(e.value.code)
    assert not missing.exists() and not missing.parent.exists()
    assert "0 leads" not in capsys.readouterr().out


# ---- the daily check ----------------------------------------------------------


class DownMap:
    def geocode(self, *a, **kw):
        import requests

        raise requests.exceptions.ProxyError("proxy refused")


def test_daily_output_says_in_words_what_couldnt_be_reached(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    eviction(conn, "CV26-000001-EA", "2026-09-30")
    conn.execute("UPDATE leads SET address = '1 W SAMPLE ST', lat = NULL, lon = NULL, geocode_tried = 0")
    conn.commit()
    lines = []
    counts = daily.geocode_new(conn, DownMap(), log=lines.append)
    assert counts["errors"] == 1
    assert lines == ["the map service couldn't be reached for 1 lead; it will be tried again on the next check"]
    assert "ProxyError" not in " ".join(lines)


def test_the_top_leads_are_reported_after_a_check(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    for i in range(3):
        eviction(conn, f"CV26-00000{i}-EA", "2026-09-30", plaintiff=f"SAMPLE {i} LLC")
    conn.execute(
        "UPDATE leads SET owner_phone = '(520) 555-0100', contact_source = 'osm', "
        "contact_checked_at = '2026-10-03T13:00:00+00:00' WHERE source_id = 'CV26-000000-EA'"
    )
    conn.execute("UPDATE leads SET contact_checked_at = '2026-10-03T13:00:00+00:00' WHERE source_id = 'CV26-000001-EA'")
    conn.commit()
    settings = outreach.merged_settings(db.get_settings(conn))
    leadlist.refresh_ranking(conn, settings)
    from leadgen import phonepass

    top = phonepass.top_reach(conn, settings)
    assert top == {"leads": 3, "reached": 1, "lookable": 3, "looked_up": 2, "found": 1}
    line = daily.top_line(top)
    assert line == "top 3 leads: 1 can be reached now; the free lookup found numbers for 1 of the 2 it looked up"
    assert line in daily.describe({"top": top})


class OneAnswer:
    name = "fake"

    def __init__(self):
        self.asked = []

    def find(self, lead, name):
        self.asked.append(lead["source_id"])
        return Contact(phone="(520) 555-0199", source="osm", matched_name=name)


def test_the_lookup_starts_at_the_top_of_the_list(tmp_path):
    """A run that stops at its limit has looked up the best lead, not the newest."""
    conn = db.connect(tmp_path / "l.db")
    eviction(conn, "CV26-000001-EA", "2026-09-30", plaintiff="NEWEST LLC")
    eviction(conn, "CV26-000002-EA", "2026-09-20", plaintiff="LOCKOUT LLC", case_stage="writ", writ_date="2026-09-29")
    prov = OneAnswer()
    find_contacts(conn, [prov], scanner=False, limit=1, log=lambda m: None)
    assert prov.asked == ["CV26-000002-EA"]


def test_a_new_google_key_looks_up_again_what_the_free_lookup_missed(app):
    with app.conn() as conn:
        conn.execute("UPDATE leads SET contact_checked_at = '2026-10-03T13:00:00+00:00'")
    app.save_settings({"google_places_api_key": "test-key"})
    with app.conn() as conn:
        assert conn.execute("SELECT contact_checked_at FROM leads").fetchone()["contact_checked_at"] is None


def test_the_priority_reason_is_plain_words():
    lead = {"lead_type": "eviction", "eviction_notice": 1, "case_stage": "notice"}
    parts = [("Eviction", 35), ("company owner", 10), ("repeat owner", 10), ("filed 11 days ago", 8)]
    assert outreach.priority_reason(lead, parts) == (
        "Eviction notice, filed 11 days ago; company landlord; several cases on the list for this landlord"
    )
    writ = outreach.priority_reason({"lead_type": "eviction"}, [("Eviction", 35), ("writ issued", 25)])
    assert writ.startswith("Lockout ordered (writ)")
    code = outreach.priority_reason(
        {"lead_type": "code_violation"}, [("Code case: trash", 30), ("owner lives elsewhere", 20)]
    )
    assert code == "City code case for trash; owner lives elsewhere (a landlord)"
