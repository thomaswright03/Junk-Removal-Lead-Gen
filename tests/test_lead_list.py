"""What Lead Desk sends the page: one page of leads filtered and sorted on
the server, a small status poll, labelled dates, and plain messages for bad
input (counts, long notes, odd dates in an imported file). Names and
addresses are made up."""

import json
import time
from datetime import timedelta

import pytest

# Shared made-up leads and stand-ins for the court and the assessor.
from test_lead_rules import CASE_HTML, CASE_URL, Assessor, SlowCourt, eviction, parcel, row, seed_cases

from leadgen import db
from leadgen.enrich import enrich_landlords
from leadgen.forms import NOTES_LIMIT
from leadgen.models import Lead
from leadgen.sources.pima_jp_case import parse_case_html
from leadgen.util import az_today
from leadgen.web import App, handle


def post(app, route, payload):
    return handle(app, "POST", route, "", {"Host": "x"}, json.dumps(payload).encode())


def get(app, route, query=""):
    return handle(app, "GET", route, query, {"Host": "x"}, b"")


def many_leads(conn, n):
    """``n`` code-case leads in one statement (fast on SQLite and Postgres)."""
    conn.execute(
        "WITH RECURSIVE k(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM k WHERE i < ?) "
        "INSERT INTO leads (source, source_id, lead_type, event_date, address, address_norm, in_pima, "
        "description, owner_name, owner_absentee, status, first_seen, last_seen) "
        "SELECT 'tucson_code_cases', 'CE-' || i, 'code_violation', '2026-09-01', "
        "i || ' W SAMPLE ST', i || ' W SAMPLE ST', 1, 'Property Maintenance | Active | REFS / trash', "
        "'OWNER ' || (i % 50), i % 2, 'new', '2026-09-01T00:00:00', '2026-09-01T00:00:00' FROM k",
        (n,),
    )
    conn.commit()


class NoParcels:
    def by_parcels(self, parcels):
        return {}

    def by_site_address(self, address):
        return None

    def by_owner(self, name, limit=200):
        return []


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.put_settings(conn, {"lead_view": "all"})
    conn.commit()
    return App(path, parcel_client=NoParcels()), conn


def test_ten_thousand_leads_open_quickly_and_the_poll_is_small(desk):
    app, conn = desk
    many_leads(conn, 10000)
    started = time.monotonic()
    status, body, _ = get(app, "/api/state", "list=leads&status=open&sort=score&offset=0")
    took = time.monotonic() - started
    print(f"10,000 leads: first page in {took:.2f}s")
    size = len(json.dumps(body))
    assert status == 200 and body["list"]["total"] == 10000 and len(body["list"]["leads"]) == 100
    assert "leads" not in body  # never the whole table
    assert size < 400_000, size
    assert took < 5, took  # well under a second on SQLite; Postgres in CI is slower
    status, poll, _ = get(app, "/api/status")
    assert status == 200 and len(json.dumps(poll)) < 10_000
    assert set(poll) == {"daily", "jobs", "paused", "paused_by_env"}


def test_list_is_filtered_sorted_and_paged_on_the_server(desk):
    app, conn = desk
    many_leads(conn, 250)
    conn.execute("UPDATE leads SET status = 'won' WHERE source_id = 'CE-1'")
    conn.execute("UPDATE leads SET owner_phone = '(520) 555-0100' WHERE source_id IN ('CE-7', 'CE-8')")
    conn.commit()
    page = app.state({"list": "leads", "status": "open", "offset": "200"})["list"]
    assert page["total"] == 249 and page["offset"] == 200 and len(page["leads"]) == 49
    assert app.state({"list": "leads", "status": ""})["list"]["total"] == 250
    phones = app.state({"list": "leads", "type": "has_phone"})["list"]["leads"]
    assert sorted(l["source_id"] for l in phones) == ["CE-7", "CE-8"]
    found = app.state({"list": "leads", "q": "123 w sample"})["list"]["leads"]
    assert [l["source_id"] for l in found] == ["CE-123"]
    scores = [l["score"] for l in app.state({"list": "leads"})["list"]["leads"]]
    assert scores == sorted(scores, reverse=True)
    # The open lead comes along, with its contact history, wherever it is in the list.
    lead_id = conn.execute("SELECT id FROM leads WHERE source_id = 'CE-250'").fetchone()[0]
    assert app.state({"lead": str(lead_id)})["lead"]["source_id"] == "CE-250"


def test_newest_first_uses_the_latest_real_event(desk):
    app, conn = desk
    today = az_today()
    leads = [
        # Filed long ago but a writ yesterday: newest.
        ("CV26-000001-EA", (today - timedelta(days=40)).isoformat(), (today - timedelta(days=1)).isoformat(), True),
        # Filed a week ago.
        ("CV26-000002-EA", (today - timedelta(days=7)).isoformat(), None, True),
        # Not read yet: only an upcoming hearing, which isn't an event yet, so last.
        ("CV26-000003-EA", (today + timedelta(days=10)).isoformat(), None, False),
    ]
    for case, date_, writ, read in leads:
        db.upsert(
            conn,
            Lead(
                "pima_jp_calendar",
                case,
                "eviction",
                date_,
                plaintiff="EXAMPLE HOMES LLC",
                in_pima=True,
                eviction_notice=True if read else None,
                case_stage="writ" if writ else None,
                writ_date=writ,
            ),
        )
    conn.commit()
    rows = app.state({"list": "leads", "sort": "date"})["list"]["leads"]
    assert [l["source_id"] for l in rows] == ["CV26-000001-EA", "CV26-000002-EA", "CV26-000003-EA"]
    labels = [(l["latest_label"], l["date_label"]) for l in rows]
    assert labels == [("Writ", "Filed"), ("Filed", "Filed"), (None, "Hearing")]


def test_assign_with_a_count_that_is_not_a_number(desk):
    app, _ = desk
    status, body, _ = post(app, "/api/assign", {"count": "abc"})
    assert status == 400 and body["error"] == "Leads this round must be a whole number, like 40."


def test_notes_over_the_limit_are_refused_with_the_limit(desk):
    app, conn = desk
    many_leads(conn, 1)
    lead_id = conn.execute("SELECT id FROM leads").fetchone()[0]
    long = "x" * (NOTES_LIMIT + 1)
    status, body, _ = post(app, "/api/lead", {"id": lead_id, "fields": {"notes": long}})
    assert status == 400 and "2,000 characters" in body["error"]
    touch = {"lead_id": lead_id, "kind": "visited", "channel": "door_hanger", "notes": long}
    status, body, _ = post(app, "/api/touch", touch)
    assert status == 400 and "2,000 characters" in body["error"]
    status, _, _ = post(app, "/api/lead", {"id": lead_id, "fields": {"notes": "x" * NOTES_LIMIT}})
    assert status == 200


def test_import_reports_dates_that_look_wrong(desk):
    app, _ = desk
    recent = (az_today() - timedelta(days=3)).isoformat()
    data = (
        "Case Number,Address,Date\n"
        "CV26-040001-EA,100 N Example Ave,1900-01-01\n"
        f"CV26-040002-EA,5 E Sample Rd,{recent}\n"
    ).encode()
    out = app.import_file("csv_import", "filings.csv", data)
    assert out["imported"] == 2 and out["odd_dates"] == 1


def test_page_files_are_served_and_others_are_not(desk):
    app, _ = desk
    status, body, ctype = get(app, "/static/core.js")
    assert status == 200 and ctype.startswith("text/javascript") and b"function load" in body
    assert get(app, "/static/app.css")[2].startswith("text/css")
    assert get(app, "/static/../web.py")[0] == 404
    assert get(app, "/static/index.html")[0] == 404


# ---- the rules as the page sees them ----------------------------------------


def test_stale_case_that_gains_a_writ_is_new_again_and_open(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    filed = (az_today() - timedelta(days=45)).isoformat()
    eviction(conn, "CV26-012345-EA", filed)
    db.mark_stale(conn, 30)
    assert row(conn, "CV26-012345-EA")["status"] == "stale"
    # The next read of the case page shows a writ issued two days ago.
    writ = (az_today() - timedelta(days=2)).isoformat()
    lead = parse_case_html(CASE_HTML, url=CASE_URL.format("2345"))
    lead.event_date, lead.case_stage, lead.writ_date = filed, "writ", writ
    db.upsert(conn, lead)
    conn.commit()
    assert row(conn, "CV26-012345-EA")["status"] == "new"
    db.mark_stale(conn, 30)
    conn.commit()
    assert row(conn, "CV26-012345-EA")["status"] == "new"
    app = App(path)
    shown = app.state({"list": "leads", "status": "open"})["list"]["leads"]
    assert [l["source_id"] for l in shown] == ["CV26-012345-EA"]
    assert (shown[0]["latest_label"], shown[0]["latest_date"]) == ("Writ", writ)


def test_apartment_lead_needs_a_unit_or_confirmation_for_a_door_hanger(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    eviction(conn, "CV26-000001-EA", "2026-09-30")
    enrich_landlords(conn, Assessor([parcel("111", "100 W EXAMPLE APTS", "APARTMENTS 25+ UNITS")]))
    app = App(path, geocoder=type("G", (), {"geocode": lambda *a: None})(), parcel_client=Assessor([]))
    lead = app.state()["leads"][0]
    assert lead["door_hanger_problem"] == "needs_unit" and "door_hanger" not in lead["eligible"]

    out = app.assign({"count": 10, "channels": ["door_hanger", "phone"]})
    assert out["left_out"]["needs_unit"] == 1 and sum(out["assigned"].values()) == 0

    # Typing the unit makes it a door you can knock on.
    app.update_lead({"id": lead["id"], "fields": {"address": "100 W EXAMPLE APTS", "unit": "12"}})
    conn.execute("UPDATE leads SET property_use = 'APARTMENTS 25+ UNITS'")  # as the assessor says
    conn.commit()
    lead = app.state()["leads"][0]
    assert lead["door_hanger_problem"] is None and lead["address_source"] == "manual"
    out = app.assign({"count": 10, "channels": ["door_hanger", "phone"]})
    assert sum(out["assigned"].values()) == 1


def test_confirming_an_address_allows_door_hangers(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    eviction(conn, "CV26-000001-EA", "2026-09-30")
    enrich_landlords(conn, Assessor([parcel("111", "100 W EXAMPLE APTS", "APARTMENTS 25+ UNITS")]))
    app = App(path)
    lead_id = app.state()["leads"][0]["id"]
    app.update_lead({"id": lead_id, "fields": {"confirm_address": True}})
    lead = app.state()["leads"][0]
    assert lead["address_source"] == "confirmed" and "door_hanger" in lead["eligible"]


def test_pause_stops_update_court_cases_within_one_case(tmp_path):
    path = tmp_path / "l.db"
    seed_cases(db.connect(path))
    court = SlowCourt(path)
    app = App(path, case_client=court)
    assert app.update_cases({"force": True})["started"]
    end = time.monotonic() + 10
    while app.jobs["cases"].running():
        assert time.monotonic() < end
        time.sleep(0.02)
    job = app.jobs["cases"]
    assert len(court.calls) == 1, "no case page is read after the pause"
    assert job.result["paused"] and job.result["stopped_early"]
