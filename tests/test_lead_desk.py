"""Lead Desk behaviour from the review: case stages and refreshes, imports,
the kill switch, background jobs, input checks and duplicate contacts.
All names and addresses are made up; nothing here touches the network."""

import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from leadgen import daily, db, leadlist, outreach
from leadgen.forms import FieldError
from leadgen.models import Lead
from leadgen.sources.pima_jp_case import case_id, cases_due, parse_case_html
from leadgen.util import az_today
from leadgen.web import App, handle

FIX = Path(__file__).parent / "fixtures"
CASE_HTML = (FIX / "jp_case_eviction.html").read_text()
WRIT_HTML = (FIX / "jp_case_writ.html").read_text()
CASE_URL = "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={}"


class Pages:
    """Stands in for the court's case pages; counts every request."""

    def __init__(self, pages, delay=0.0):
        self.pages, self.delay, self.calls = pages, delay, []

    def fetch(self, url):
        cid = case_id(url)
        self.calls.append(cid)
        if self.delay:
            time.sleep(self.delay)
        if cid not in self.pages:
            raise requests.ConnectionError("offline")
        return parse_case_html(self.pages[cid], url=CASE_URL.format(cid))


class NoNetwork:
    """Any use is a test failure: the kill switch must stop every request."""

    name = "no-network"

    def __getattr__(self, name):
        raise AssertionError(f"made a request ({name}) while paused")


class NoGeocode:
    def geocode(self, *a):
        return None


class NoParcels:
    def by_parcels(self, parcels):
        return {}

    def by_site_address(self, address):
        return None

    def by_owner(self, name, limit=200):
        return []


# ---- #7 judgment and writ ---------------------------------------------------


def test_case_page_records_judgment_and_writ():
    lead = parse_case_html(WRIT_HTML, url=CASE_URL.format("1000099"))
    assert lead.case_stage == "writ"
    assert lead.judgment_date == "2026-09-16" and lead.writ_date == "2026-09-22"
    assert "Writ of restitution issued" in lead.description
    plain = parse_case_html(CASE_HTML)
    assert plain.case_stage == "notice" and plain.judgment_date is None and plain.writ_date is None


def test_writ_case_ranks_above_notice_only_and_shows_its_stage(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(conn, parse_case_html(CASE_HTML, url=CASE_URL.format("1000001")))
    db.upsert(conn, parse_case_html(WRIT_HTML, url=CASE_URL.format("1000099")))
    conn.commit()
    leads = {l["source_id"]: l for l in App(path).state()["leads"]}
    writ, notice = leads["CV26-012399-EA"], leads["CV26-012345-EA"]
    assert writ["score"] > notice["score"]
    assert writ["case_stage"] == "writ" and notice["case_stage"] == "notice"


# ---- #6 refresh open cases ---------------------------------------------------


def test_court_date_passed_case_is_reread_and_closed_case_leaves_the_view(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(conn, parse_case_html(CASE_HTML, url=CASE_URL.format("1000001")))
    yesterday = (az_today() - timedelta(days=1)).isoformat()
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0).isoformat()
    conn.execute("UPDATE leads SET next_court_date = ?, case_checked_at = ?", (yesterday + " 14:00", two_days_ago))
    conn.commit()
    app = App(path)
    assert [l["source_id"] for l in app.state()["leads"]] == ["CV26-012345-EA"]
    assert len(cases_due(conn)) == 1  # the hearing was yesterday: read it again

    closed = Pages(
        {"1000001": CASE_HTML.replace('<span id="lblStatus">Open</span>', '<span id="lblStatus">Closed</span>')}
    )
    daily.run_daily(
        conn,
        code_cases=Empty(),
        calendar=Empty(),
        case_client=closed,
        parcel_client=NoParcels(),
        geocoder=NoGeocode(),
        providers=[],
        log=lambda m: None,
    )
    assert closed.calls == ["1000001"]
    row = conn.execute("SELECT case_status FROM leads").fetchone()
    assert row["case_status"] == "Closed"
    assert app.state()["leads"] == []  # dropped from the default view
    assert cases_due(conn) == []  # and not read again


def test_noticed_cases_are_rechecked_every_few_days():
    conn = db.connect(":memory:")
    db.upsert(conn, parse_case_html(CASE_HTML, url=CASE_URL.format("1000001")))
    conn.execute("UPDATE leads SET next_court_date = '2099-01-01'")
    conn.commit()
    assert cases_due(conn) == []  # read just now
    later = datetime.now(timezone.utc) + timedelta(days=4)
    assert len(cases_due(conn, now=later)) == 1


class Empty:
    def fetch(self, *a, **kw):
        return iter(())


# ---- #30 next court date fallback -------------------------------------------


def test_fallback_court_date_is_the_next_one_not_the_last():
    html = CASE_HTML.replace("Next Court Date:", "Courtroom Note:").replace(
        "<tr><td>10/14/2026</td>",
        "<tr><td>09/01/2026</td><td>09:00 AM</td><td>Hearing</td><td>Eviction Action</td><td></td></tr>"
        "<tr><td>11/20/2026</td><td>09:00 AM</td><td>Hearing</td><td>Status</td><td></td></tr>"
        "<tr><td>10/14/2026</td>",
    )
    lead = parse_case_html(html, today=date(2026, 10, 3))
    assert lead.next_court_date == "2026-10-14"
    assert parse_case_html(html, today=date(2026, 12, 1)).next_court_date is None


# ---- #5 / #23 imports are visible; unchecked count ----------------------------


def test_imported_csv_leads_show_without_changing_the_view(tmp_path):
    app = App(tmp_path / "l.db", parcel_client=NoParcels())
    data = b"Case Number,Address\nCV26-040001-EA,100 N Example Ave\n,77 W Sample Rd\n"
    counts = app.import_file("csv_import", "leads.csv", data)
    assert counts["new"] == 2
    shown = {l["address"] for l in app.state()["leads"]}
    assert shown == {"100 N Example Ave", "77 W Sample Rd"}


def test_imported_calendar_page_counts_cases_waiting_to_be_checked(tmp_path):
    app = App(tmp_path / "l.db", parcel_client=NoParcels())
    counts = app.import_file("pima_jp_calendar", "cal.html", (FIX / "jp_calendar_live_p1.html").read_bytes())
    state = app.state()
    assert counts["new"] >= 1 and len(state["leads"]) == counts["new"]
    assert state["view_counts"]["unchecked"] == counts["waiting_for_case_check"] == counts["new"]


# ---- #13 / #14 / #31 file imports ---------------------------------------------


def test_excel_csv_keeps_accents(tmp_path):
    app = App(tmp_path / "l.db", parcel_client=NoParcels())
    app.import_file("csv_import", "x.csv", "Address\nCalle Ñandú 5\n".encode("cp1252"))
    conn = db.connect(tmp_path / "l.db")
    assert conn.execute("SELECT address FROM leads").fetchone()[0] == "Calle Ñandú 5"


@pytest.mark.parametrize("data", [b"<html>hello</html>", b"", b"name,color\nx,blue\n"])
def test_unrecognised_file_says_so(tmp_path, data):
    app = App(tmp_path / "l.db", parcel_client=NoParcels())
    status, body, _ = handle(app, "POST", "/api/import", "source=pima_jp_calendar&filename=x.html", {"Host": "x"}, data)
    assert status == 400 and "court calendar" in body["error"] and "CSV" in body["error"]


def test_unreadable_csv_date_is_left_empty_and_reported(tmp_path):
    app = App(tmp_path / "l.db", parcel_client=NoParcels())
    counts = app.import_file("csv_import", "x.csv", b"Address,Date Filed\n1 E Sample St,13/45/2026\n")
    assert counts["new"] == 1 and counts["unreadable_dates"] == 1
    row = db.connect(tmp_path / "l.db").execute("SELECT event_date, status FROM leads").fetchone()
    assert row["event_date"] is None and row["status"] == "new"
    db.mark_stale(db.connect(tmp_path / "l.db"), 30)
    assert app.state()["leads"][0]["status"] == "new"


# ---- #9 kill switch ------------------------------------------------------------


def test_pause_stops_the_daily_check_and_lookups(tmp_path, monkeypatch):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-1-EA",
            "eviction",
            "2026-09-30",
            None,
            plaintiff="EXAMPLE HOMES LLC",
            url=CASE_URL.format("1"),
            in_pima=True,
        ),
    )
    conn.commit()
    app = App(
        path,
        case_client=NoNetwork(),
        calendar=NoNetwork(),
        code_cases=NoNetwork(),
        parcel_client=NoNetwork(),
        geocoder=NoNetwork(),
        providers=[NoNetwork()],
    )
    app.save_settings({"paused": True})
    summary = daily.run_daily(
        conn,
        case_client=NoNetwork(),
        calendar=NoNetwork(),
        code_cases=NoNetwork(),
        parcel_client=NoNetwork(),
        geocoder=NoNetwork(),
        providers=[NoNetwork()],
        log=lambda m: None,
    )
    assert summary["paused"] and daily.describe(summary) == "paused: nothing was checked"
    for route in ("/api/find-contacts", "/api/cases/update", "/api/refresh", "/api/cases/add"):
        status, body, _ = handle(app, "POST", route, "", {"Host": "x"}, b'{"text": "1"}')
        assert status == 200 and body.get("paused"), route
        assert "paused" in body["message"]
    assert app.state()["paused"] is True

    # The environment variable does the same, whatever Settings say.
    app.save_settings({"paused": False})
    monkeypatch.setenv("LEADDESK_PAUSED", "1")
    status, body, _ = handle(app, "POST", "/api/find-contacts", "", {"Host": "x"}, b"{}")
    assert body.get("paused")


@pytest.mark.parametrize(
    "argv",
    [
        ["fetch", "--source", "tucson_code_cases", "--days", "3"],
        ["fetch"],
        ["fetch", "--source", "pima_jp_calendar"],
        ["enrich"],
        ["geocode"],
        ["run"],
        ["cases", "update"],
        ["contacts", "find"],
    ],
)
def test_paused_commands_make_no_requests(tmp_path, monkeypatch, capsys, argv):
    from leadgen import cli, sources

    class Refuse:
        def __getattr__(self, name):
            raise AssertionError("contacted a website while paused")

    monkeypatch.setenv("LEADDESK_PAUSED", "1")

    def no_request(*a, **kw):
        raise AssertionError("contacted a website while paused")

    monkeypatch.setattr("requests.Session.request", no_request)
    monkeypatch.setitem(sources.SOURCES, "tucson_code_cases", lambda: Refuse())
    with pytest.raises(SystemExit) as e:
        cli.main(["--db", str(tmp_path / "l.db"), *argv])
    assert "Lead Desk is paused" in str(e.value.code)


def test_paused_fetch_from_saved_files_still_works(tmp_path, monkeypatch, capsys):
    from leadgen import cli

    monkeypatch.setenv("LEADDESK_PAUSED", "1")
    csv = tmp_path / "leads.csv"
    csv.write_text("address,date\n1 W TEST ST,2026-09-30\n")
    cli.main(["--db", str(tmp_path / "l.db"), "fetch", "--source", "csv_import", "--file", str(csv)])
    assert "1 new" in capsys.readouterr().out


def test_paused_lead_desk_skips_owner_and_map_lookups(tmp_path, monkeypatch):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(
        conn, Lead("pima_jp_calendar", "CV26-1-EA", "eviction", "2026-09-30", None, in_pima=True, eviction_notice=True)
    )
    conn.commit()
    app = App(path, parcel_client=NoNetwork(), geocoder=NoNetwork())
    app.save_settings({"paused": True})
    lead_id = app.state()["leads"][0]["id"]
    r = app.update_lead({"id": lead_id, "fields": {"address": "1 W Test St"}})
    assert "paused" in r["message"]
    assert app.run_enrich({})["paused"]
    with pytest.raises(ValueError, match="paused"):
        app.owner_properties("EXAMPLE HOMES")


def test_saving_nothing_is_refused(desk):
    app, lead_id, _ = desk
    status, body, _ = post(app, "/api/lead", {"id": lead_id})
    assert status == 400 and body["error"] == "Nothing to save."
    status, body, _ = post(app, "/api/lead", {"id": lead_id, "fields": {"not_a_field": 1}})
    assert status == 400 and body["error"] == "Nothing to save."


# ---- #8 background jobs ----------------------------------------------------------


def _wait(app, name, timeout=10):
    end = time.monotonic() + timeout
    while app.jobs[name].running():
        assert time.monotonic() < end, "job didn't finish"
        time.sleep(0.02)
    return app.jobs[name]


def test_update_court_cases_runs_in_the_background_with_progress(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    ids = [str(1000001 + i) for i in range(40)]
    for i, cid in enumerate(ids):
        db.upsert(
            conn,
            Lead(
                "pima_jp_calendar", f"CV26-{i:06d}-EA", "eviction", "2026-09-30", url=CASE_URL.format(cid), in_pima=True
            ),
        )
    conn.commit()
    pages = Pages(
        {cid: CASE_HTML.replace("CV26-012345-EA", f"CV26-{i:06d}-EA") for i, cid in enumerate(ids)}, delay=0.02
    )
    app = App(path, case_client=pages, parcel_client=NoParcels())
    started = time.monotonic()
    first = app.update_cases({"force": True})
    assert first["started"] and time.monotonic() - started < 0.5  # returns at once
    second = app.update_cases({"force": True})
    assert not second["started"] and "already running" in second["message"]
    time.sleep(0.15)
    job = app.state()["jobs"]["cases"]
    assert job["running"] and job["total"] == 40 and 0 < job["done"] < 40
    app.cancel_job({"name": "cases"})
    job = _wait(app, "cases")
    assert job.result["cancelled"] and job.result["checked"] < 40


def test_find_phones_runs_in_the_background(tmp_path):
    from leadgen.business import Contact

    class Phones:
        name = "fake"

        def find(self, lead, name):
            return Contact(phone="(520) 555-0101", source="fake") if name else None

    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-1-EA",
            "eviction",
            "2026-09-30",
            plaintiff="EXAMPLE HOMES LLC",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.commit()
    app = App(path, providers=[Phones()])
    assert app.find_contacts({})["started"]
    job = _wait(app, "contacts")
    assert job.result["found"] == 1 and not job.error


def test_jobs_run_inside_the_request_online(tmp_path):
    path = tmp_path / "l.db"
    db.connect(path).close()
    app = App(path, case_client=Pages({}), serverless=True)
    r = app.update_cases({"force": True})
    assert r["started"] is False and r["result"]["checked"] == 0


# ---- #10 / #12 input checks and plain errors ------------------------------------


def post(app, route, payload):
    return handle(app, "POST", route, "", {"Host": "x"}, json.dumps(payload).encode())


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(conn, Lead("t", "1", "code_violation", "2026-09-30", "1 E SAMPLE ST", in_pima=True))
    db.put_settings(conn, {"lead_view": "all"})
    conn.commit()
    app = App(path)
    return app, app.state()["leads"][0]["id"], conn


@pytest.mark.parametrize(
    "route,payload,words",
    [
        ("/api/lead", {"id": "LEAD", "fields": {"job_revenue": -500}}, "can't be negative"),
        ("/api/lead", {"id": "LEAD", "fields": {"quote_amount": "abc"}}, "dollar amount"),
        ("/api/touch", {"lead_id": "LEAD", "channel": "carrier_pigeon", "kind": "visited"}, "outreach method"),
        ("/api/touch", {"lead_id": "LEAD", "channel": "door_hanger", "kind": "smoke_signal"}, "kind of contact"),
        ("/api/settings", {"costs": "free"}, "Cost per contact"),
        ("/api/settings", {"google_daily_limit": -1}, "Google lookups per day"),
        ("/api/settings", {"templates": {"phone": 5}}, "Messages"),
    ],
)
def test_bad_values_are_refused_with_a_plain_message(desk, route, payload, words):
    app, lead_id, conn = desk
    payload = json.loads(json.dumps(payload).replace('"LEAD"', str(lead_id)))
    before = (
        [tuple(r) for r in conn.execute("SELECT * FROM leads")]
        + [tuple(r) for r in conn.execute("SELECT * FROM touches")]
        + [tuple(r) for r in conn.execute("SELECT * FROM settings")]
    )
    status, body, _ = post(app, route, payload)
    assert status == 400 and words in body["error"]
    assert "Error" not in body["error"] and "'" not in body["error"][:1]
    after = (
        [tuple(r) for r in conn.execute("SELECT * FROM leads")]
        + [tuple(r) for r in conn.execute("SELECT * FROM touches")]
        + [tuple(r) for r in conn.execute("SELECT * FROM settings")]
    )
    assert before == after


def test_unknown_lead_is_a_404(desk):
    app, _, conn = desk
    status, body, _ = post(app, "/api/touch", {"lead_id": 999, "channel": "door_hanger", "kind": "visited"})
    assert status == 404 and "no longer exists" in body["error"]
    assert conn.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 0
    status, body, _ = post(app, "/api/lead", {"id": 999, "fields": {"notes": "x"}})
    assert status == 404


def test_unexpected_errors_are_plain(desk, monkeypatch):
    app, lead_id, _ = desk
    monkeypatch.setattr(App, "update_lead", lambda self, body: {}["boom"])
    status, body, _ = post(app, "/api/lead", {"id": lead_id, "fields": {}})
    assert status == 500 and "couldn't save the lead" in body["error"] and "KeyError" not in body["error"]
    status, body, _ = handle(app, "POST", "/api/lead", "", {"Host": "x"}, b"{not json")
    assert status == 400 and "couldn't read that request" in body["error"]


# ---- #11 double clicks and removing a contact ----------------------------------


def test_double_click_logs_one_contact_and_a_contact_can_be_removed(desk):
    app, lead_id, conn = desk
    app.update_lead({"id": lead_id, "fields": {"channel": "door_hanger"}})
    a = app.add_touches({"lead_id": lead_id, "kind": "visited"})
    b = app.add_touches({"lead_id": lead_id, "kind": "visited"})
    assert (a["logged"], b["logged"], b["duplicates"]) == (1, 0, 1)
    res = {r["channel"]: r for r in app.state()["results"]}["door_hanger"]
    assert res["cost"] == 0.35
    touch_id = app.state()["leads"][0]["touches"][0]["id"]
    app.delete_touch({"id": touch_id})
    res = {r["channel"]: r for r in app.state()["results"]}["door_hanger"]
    assert res["cost"] == 0 and res["touched"] == 0
    status, body, _ = post(app, "/api/touch/delete", {"id": touch_id})
    assert status == 404


def test_identical_contacts_logged_at_the_same_moment_store_one(desk):
    """Five identical "log contact" requests at once (a double tap on a slow
    connection, two tabs): one contact is stored, on SQLite and Postgres."""
    app, lead_id, conn = desk
    payload = {"lead_ids": [lead_id], "channel": "phone", "kind": "no_answer"}
    start = threading.Barrier(5)
    answers = []

    def send():
        start.wait()
        answers.append(post(app, "/api/touch", payload))

    threads = [threading.Thread(target=send) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [a[0] for a in answers] == [200] * 5, answers
    assert sum(a[1]["logged"] for a in answers) == 1
    assert sum(a[1]["duplicates"] for a in answers) == 4
    n = conn.execute("SELECT COUNT(*) AS n FROM touches WHERE lead_id = ?", (lead_id,)).fetchone()["n"]
    assert n == 1


# ---- #4 property address entered by hand ----------------------------------------


def test_typed_address_is_located_and_makes_door_hangers_possible(tmp_path):
    from leadgen.geocode import GeocodeResult

    class Geo:
        def geocode(self, address, city=None, zip_=None):
            return GeocodeResult(32.30, -110.98, True, city="Tucson", zip="85705")

    class Parcels(NoParcels):
        def by_site_address(self, address):
            return {
                "PARCEL": "999-01-001A",
                "ADDRESSEE": "EXAMPLE HOMES LLC",
                "ADDRESS": "PO BOX 1",
                "CITY": "PHOENIX",
                "STATE_PROVINCE": "AZ",
                "POSTAL_CODE": "85001",
                "SITE_ADDRESS": "100 W EXAMPLE DR",
                "USE_DESC": "APARTMENTS 25+ UNITS",
            }

    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-1-EA",
            "eviction",
            "2026-09-30",
            None,
            plaintiff="EXAMPLE HOMES LLC",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.commit()
    app = App(path, geocoder=Geo(), parcel_client=Parcels())
    app.save_settings({"base_address": "1 Base St"})
    with app.conn() as c:
        db.put_settings(c, {"base_lat": 32.36, "base_lon": -111.12})
    lead = app.state()["leads"][0]
    assert lead["address"] is None and "door_hanger" not in lead["eligible"]
    r = app.update_lead({"id": lead["id"], "fields": {"address": "100 W Example Dr", "unit": "#12"}})
    assert "found on the map" in r["message"]
    lead = app.state()["leads"][0]
    assert lead["address"] == "100 W EXAMPLE DR" and lead["unit"] == "12"
    assert lead["miles"] is not None and lead["parcel"] == "999-01-001A"
    assert lead["owner_name"] == "EXAMPLE HOMES LLC" and "door_hanger" in lead["eligible"]


def test_typed_address_survives_the_map_service_being_down(tmp_path):
    class Down:
        def geocode(self, *a):
            raise requests.ConnectionError("offline")

        def by_site_address(self, address):
            raise requests.ConnectionError("offline")

    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-1-EA",
            "eviction",
            "2026-09-30",
            None,
            plaintiff="EXAMPLE HOMES LLC",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.commit()
    app = App(path, geocoder=Down(), parcel_client=Down())
    lead_id = app.state()["leads"][0]["id"]
    r = app.update_lead({"id": lead_id, "fields": {"address": "5 S Sample Ave"}})
    assert "try again" in r["message"]
    row = conn.execute("SELECT address, geocode_tried, enriched_at FROM leads").fetchone()
    assert row["address"] == "5 S SAMPLE AVE" and row["geocode_tried"] == 0 and row["enriched_at"] is None


# ---- #3 notes saved with a status change -------------------------------------------


def test_status_change_saves_notes_sent_with_it(desk):
    app, lead_id, conn = desk
    app.update_lead({"id": lead_id, "fields": {"status": "responded", "notes": "Call back Tue", "quote_amount": "300"}})
    row = conn.execute("SELECT status, notes, quote_cents FROM leads").fetchone()
    assert tuple(row) == ("responded", "Call back Tue", 30000)


def test_next_daily_run_wording():
    from leadgen.web import next_daily_run

    morning = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)  # 5:00 in Tucson
    assert next_daily_run({}, now=morning) == "today 6:00 AM"
    noon = datetime(2026, 10, 3, 19, 0, tzinfo=timezone.utc)
    assert next_daily_run({"last_daily_run": "2026-10-03"}, now=noon) == "tomorrow 6:00 AM"
    assert next_daily_run({"paused": True}, now=noon) == "paused"
    # Stopped part way by the pause: still due today, not tomorrow.
    stopped = {"last_daily_run": "2026-10-02", "last_daily_interrupted": "2026-10-03"}
    assert next_daily_run(stopped, now=noon) == "within the next few minutes"
    assert "finish today's" in next_daily_run(stopped, serverless=True, now=noon)


def test_channels_list_has_no_postcards():
    assert "postcard" not in outreach.CHANNELS and "postcard" not in outreach.TOUCH_KINDS


def test_simultaneous_presses_start_one_job(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead("pima_jp_calendar", "CV26-1-EA", "eviction", "2026-09-30", url=CASE_URL.format("1000001"), in_pima=True),
    )
    conn.commit()
    app = App(path, case_client=Pages({"1000001": CASE_HTML}, delay=0.3), parcel_client=NoParcels())
    results = []
    barrier = threading.Barrier(3)

    def press():
        barrier.wait()
        results.append(app.update_cases({"force": True}).get("started"))

    threads = [threading.Thread(target=press) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _wait(app, "cases")
    assert results.count(True) == 1


def test_money_is_whole_cents_with_a_ceiling(desk):
    app, lead_id, conn = desk
    status, body, _ = post(app, "/api/lead", {"id": lead_id, "fields": {"quote_amount": 1e308}})
    assert status == 400 and "at most $100,000" in body["error"]
    status, body, _ = post(app, "/api/lead", {"id": lead_id, "fields": {"job_revenue": "100000.01"}})
    assert status == 400
    app.update_lead({"id": lead_id, "fields": {"channel": "phone", "quote_amount": "0.10", "job_revenue": 0.2}})
    for _ in range(3):  # three contacts at 10 cents: floats would give 0.30000000000000004
        app.add_touches({"lead_id": lead_id, "kind": "talked", "cost": 0.1})
        conn.execute("UPDATE touches SET created_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
    row = conn.execute("SELECT quote_cents, revenue_cents FROM leads").fetchone()
    assert tuple(row) == (10, 20)
    res = {r["channel"]: r for r in app.state()["results"]}["phone"]
    assert res["cost"] == 0.3 and res["revenue"] == 0.2
    lead = app.state({"lead": str(lead_id)})["lead"]
    assert lead["quote_amount"] == 0.1 and [t["cost"] for t in lead["touches"]] == [0.1, 0.1, 0.1]
    status, body, _ = post(app, "/api/touch", {"lead_id": lead_id, "kind": "talked", "cost": 5000})
    assert status == 400 and "at most $1,000" in body["error"]


@pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")), reason="migrates an old SQLite file")
def test_dollar_amounts_from_before_become_cents(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db.connect(path).close()
    old = sqlite3.connect(path)
    old.execute(
        "INSERT INTO leads (source, source_id, lead_type, first_seen, last_seen, quote_amount, job_revenue) "
        "VALUES ('a', '1', 'eviction', 'x', 'x', 249.99, 300.1)"
    )
    old.execute(
        "INSERT INTO touches (lead_id, channel, kind, cost, created_at) VALUES (1, 'phone', 'talked', 0.35, 'x')"
    )
    old.commit()
    old.close()
    db.forget_ready()  # the next start of Lead Desk
    conn = db.connect(path)
    assert tuple(conn.execute("SELECT quote_cents, revenue_cents FROM leads").fetchone()) == (24999, 30010)
    assert conn.execute("SELECT cost_cents FROM touches").fetchone()[0] == 35


# ---- database connections: one per request, closed, schema checked once ----


def test_each_request_uses_one_connection_and_closes_it(tmp_path, monkeypatch):
    app = App(tmp_path / "l.db")
    opened = []
    real = db.connect

    def counting(path):
        conn = real(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "connect", counting)
    status, _, _ = handle(app, "GET", "/api/state", "list=leads", {}, b"")
    assert status == 200 and len(opened) == 1
    status, _, _ = post(app, "/api/settings", {"business_phone": "(520) 555-0100"})
    assert status == 200 and len(opened) == 2
    for conn in opened:  # closed: using it fails
        with pytest.raises(Exception, match="(?i)closed"):
            conn.execute("SELECT 1")


@pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")), reason="counts open SQLite files")
@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_open_database_files_stay_flat_under_load(tmp_path):
    path = tmp_path / "l.db"
    app = App(path)

    def open_files():
        n = 0
        for fd in Path("/proc/self/fd").iterdir():
            try:
                n += str(path) in os.readlink(fd)
            except OSError:
                pass
        return n

    handle(app, "GET", "/api/state", "list=leads", {}, b"")
    before = open_files()
    for _ in range(40):
        assert handle(app, "GET", "/api/state", "list=leads", {}, b"")[0] == 200
    assert open_files() == before <= 1


@pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")), reason="SQLite file upgrades")
def test_schema_and_upgrades_run_once_per_database(tmp_path, monkeypatch):
    path = tmp_path / "l.db"
    db.connect(path).close()
    ran = []
    monkeypatch.setattr(db, "_migrate", lambda conn: ran.append(1))
    db.connect(path).close()
    assert ran == []
    db.forget_ready()
    db.connect(path).close()
    assert ran == [1]


def test_fetching_past_calendar_dates_explains_the_limit(tmp_path, monkeypatch, capsys):
    from leadgen import cli

    def no_request(*a, **kw):
        raise AssertionError("asked the calendar for past dates")

    monkeypatch.setattr("requests.Session.request", no_request)
    argv = ["--db", str(tmp_path / "l.db"), "fetch", "--source", "pima_jp_calendar"]
    cli.main([*argv, "--since", "2025-01-01", "--until", "2025-01-31"])
    out = capsys.readouterr().out
    assert "only lists upcoming hearings" in out
    assert "records request" in out and "Add cases" in out
    assert "0 new" not in out


# ---- review 7: revenue means Won, Undo a removed contact, same landlord ------


def test_job_revenue_marks_the_lead_won_from_any_caller(desk):
    app, lead_id, conn = desk
    app.update_lead({"id": lead_id, "fields": {"job_revenue": "250"}})
    row = conn.execute("SELECT status, revenue_cents, responded_at FROM leads WHERE id = ?", (lead_id,)).fetchone()
    assert row["status"] == "won" and row["revenue_cents"] == 25000 and row["responded_at"]


def test_revenue_on_a_lost_lead_asks_first_and_an_explicit_status_is_kept(desk):
    app, lead_id, conn = desk
    app.update_lead({"id": lead_id, "fields": {"status": "lost"}})
    with pytest.raises(FieldError) as err:
        app.update_lead({"id": lead_id, "fields": {"job_revenue": "90"}})
    assert err.value.field == "job_revenue" and "marked Lost" in str(err.value)
    assert conn.execute("SELECT revenue_cents FROM leads").fetchone()[0] is None
    # "Keep it lost": the page sends the status with the amount.
    app.update_lead({"id": lead_id, "fields": {"job_revenue": "90", "status": "lost"}})
    row = conn.execute("SELECT status, revenue_cents FROM leads").fetchone()
    assert (row["status"], row["revenue_cents"]) == ("lost", 9000)


def test_a_removed_contact_can_be_put_back(desk):
    app, lead_id, conn = desk
    app.update_lead({"id": lead_id, "fields": {"channel": "door_hanger"}})
    app.add_touches({"lead_id": lead_id, "kind": "visited", "notes": "left at the gate"})
    touch_id = app.state()["leads"][0]["touches"][0]["id"]
    removed = app.delete_touch({"id": touch_id})["removed"]
    assert app.state()["leads"][0]["touches"] == []
    status, body, _ = post(app, "/api/touch/restore", {"removed": removed})
    assert status == 200 and body["restored"] is True
    touches = app.state()["leads"][0]["touches"]
    assert [(t["id"], t["notes"]) for t in touches] == [(touch_id, "left at the gate")]
    assert {r["channel"]: r for r in app.state()["results"]}["door_hanger"]["touched"] == 1
    # Undo pressed twice puts it back once.
    assert app.restore_touch({"removed": removed})["restored"] is False
    # A later contact still gets a fresh id.
    assert app.add_touches({"lead_id": lead_id, "kind": "talked", "notes": "again"})["logged"] == 1
    assert len(app.state()["leads"][0]["touches"]) == 2
    bad = dict(removed, channel="carrier_pigeon")
    status, body, _ = post(app, "/api/touch/restore", {"removed": bad})
    assert status == 400 and "can't be put back" in body["error"]


def test_a_number_found_for_a_landlord_fills_its_other_leads(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    for sid, plaintiff in (
        ("CV26-050001-EA", "SAMPLE PROPERTIES LLC"),
        ("CV26-050002-EA", "Sample Properties, LLC"),
        ("CV26-050003-EA", "OTHER EXAMPLE LLC"),
        ("CV26-050004-EA", "SAMPLE PROPERTIES LLC"),
    ):
        db.upsert(
            conn,
            Lead(
                "pima_jp_calendar",
                sid,
                "eviction",
                "2026-09-28",
                None,
                plaintiff=plaintiff,
                defendant="ROE, SAM",
                in_pima=True,
                eviction_notice=True,
            ),
        )
    conn.commit()
    ids = {r["source_id"]: r["id"] for r in conn.execute("SELECT id, source_id FROM leads").fetchall()}
    conn.execute("UPDATE leads SET status = 'lost' WHERE id = ?", (ids["CV26-050004-EA"],))
    conn.commit()
    app = App(path)
    r = app.update_lead({"id": ids["CV26-050001-EA"], "fields": {"owner_phone": "520-555-0123"}, "same_landlord": True})
    assert r["also"] == 1
    phones = {
        r["source_id"]: (r["owner_phone"], r["contact_source"])
        for r in conn.execute("SELECT source_id, owner_phone, contact_source FROM leads").fetchall()
    }
    assert phones["CV26-050002-EA"] == (phones["CV26-050001-EA"][0], "manual")
    assert phones["CV26-050003-EA"][0] is None and phones["CV26-050004-EA"][0] is None  # other landlord; closed
    # Without the box ticked only this lead changes.
    app.update_lead({"id": ids["CV26-050003-EA"], "fields": {"owner_phone": "520-555-0124"}})
    assert conn.execute("SELECT owner_phone FROM leads WHERE id = ?", (ids["CV26-050004-EA"],)).fetchone()[0] is None


def test_records_request_csv_with_judgments_and_writs_shows_in_the_default_view(tmp_path):
    app = App(tmp_path / "l.db", parcel_client=NoParcels())
    data = (
        b"Case Number,Plaintiff,Defendant,Filed,Judgment Date,Writ Date,Disposition\n"
        b'CV26-060001-EA,SAMPLE PROPERTIES LLC,"ROE, SAM",2026-09-01,2026-09-20,,Judgment for plaintiff\n'
        b'CV26-060002-EA,EXAMPLE HOMES LLC,"ROE, JO",2026-09-02,2026-09-21,2026-09-28,\n'
        b'CV26-060003-EA,EXAMPLE HOMES LLC,"ROE, AL",2026-09-03,,,Dismissed\n'
    )
    assert app.import_file("csv_import", "records.csv", data)["new"] == 3
    conn = db.connect(tmp_path / "l.db")
    stages = dict(conn.execute("SELECT source_id, case_stage FROM leads").fetchall())
    assert stages == {"CV26-060001-EA": "judgment", "CV26-060002-EA": "writ", "CV26-060003-EA": "dismissed"}
    assert app.settings(conn).get("lead_view", "eviction_notice") == "eviction_notice"
    shown = [l["source_id"] for l in app.state()["leads"]]
    assert shown[:2] == ["CV26-060002-EA", "CV26-060001-EA"] and "CV26-060003-EA" not in shown


def test_message_previews_come_from_any_view(desk):
    app, lead_id, conn = desk
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-070001-EA",
            "eviction",
            "2026-09-28",
            None,
            plaintiff="SAMPLE PROPERTIES LLC",
            defendant="ROE, SAM",
            in_pima=True,
        ),
    )
    db.put_settings(conn, {"lead_view": "eviction_notice"})
    conn.commit()
    with app.conn() as c:
        found = leadlist.samples(c, app.settings(c))
    assert found["code_violation"]["id"] == lead_id
    assert found["eviction"]["plaintiff"]
