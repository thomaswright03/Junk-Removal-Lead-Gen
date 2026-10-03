"""The rules that decide which leads Steve chases: how old a lead is, how it
ranks, where it is and whether a door hanger can go there, and that pausing
stops a run already going. All names and addresses are made up; nothing
here touches the network."""

import time
from datetime import date, timedelta
from pathlib import Path

import requests

from leadgen import daily, db, outreach
from leadgen.enrich import enrich_landlords, fix_inferred_addresses
from leadgen.lookup import Contact, find_contacts
from leadgen.models import Lead
from leadgen.sources.pima_jp_case import case_id, parse_case_html
from leadgen.util import is_multifamily, is_residential
from leadgen.web import App

FIX = Path(__file__).parent / "fixtures"
CASE_HTML = (FIX / "jp_case_eviction.html").read_text()
WRIT_HTML = (FIX / "jp_case_writ.html").read_text()
CASE_URL = "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={}"
TODAY = date(2026, 10, 3)


def eviction(conn, case, filed, **extra):
    """A case-page eviction (filing date read from the case page)."""
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            case,
            "eviction",
            filed,
            plaintiff=extra.pop("plaintiff", "EXAMPLE HOMES LLC"),
            in_pima=True,
            url=CASE_URL.format(case[-4:]),
            eviction_notice=True,
            case_stage=extra.pop("case_stage", "notice"),
            **extra,
        ),
    )
    conn.commit()


def row(conn, case):
    return conn.execute("SELECT * FROM leads WHERE source_id = ?", (case,)).fetchone()


# ---- ageing: the latest court event, not the filing date ---------------------


def test_late_writ_keeps_an_old_filing_fresh(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    filed = (TODAY - timedelta(days=39)).isoformat()
    eviction(conn, "CV26-000001-EA", filed, case_stage="writ", writ_date=(TODAY - timedelta(days=2)).isoformat())
    eviction(conn, "CV26-000002-EA", filed)  # nothing since the filing: old
    assert db.mark_stale(conn, 30, today=TODAY) == 1
    assert row(conn, "CV26-000001-EA")["status"] == "new"
    assert row(conn, "CV26-000002-EA")["status"] == "stale"


def test_won_or_skipped_cases_are_not_reopened_by_a_writ(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    eviction(conn, "CV26-012345-EA", "2026-08-01")
    conn.execute("UPDATE leads SET status = 'skip'")
    lead = parse_case_html(WRIT_HTML, url=CASE_URL.format("2345"))
    lead.source_id = "CV26-012345-EA"
    db.upsert(conn, lead)
    assert row(conn, "CV26-012345-EA")["status"] == "skip"


# ---- priority: no recency points for dates that haven't happened ------------


def lead_dict(**kw):
    base = {
        "id": 1,
        "source": "pima_jp_calendar",
        "lead_type": "eviction",
        "event_date": None,
        "case_checked_at": None,
        "case_stage": None,
        "judgment_date": None,
        "writ_date": None,
        "description": None,
        "owner_absentee": 0,
        "owner_entity": 1,
        "owner_name": "EXAMPLE HOMES LLC",
        "address": None,
        "unit": None,
        "property_use": None,
        "address_source": None,
        "plaintiff": "EXAMPLE HOMES LLC",
    }
    return {**base, **kw}


def test_upcoming_hearing_earns_no_recency_points():
    unread = lead_dict(event_date=(TODAY + timedelta(days=10)).isoformat())
    assert outreach.date_label(unread) == "Hearing"
    assert outreach.latest_event(unread, TODAY) == (None, None)
    assert outreach.score(unread, today=TODAY) == 35 + 10  # eviction + company owner, nothing for the date
    # Even a "filing" date in the future (a typo) earns nothing.
    future = lead_dict(event_date=(TODAY + timedelta(days=3)).isoformat(), case_checked_at="2026-10-01T00:00:00")
    assert outreach.score(future, today=TODAY) == 45


def test_writ_yesterday_outranks_an_unread_case():
    writ = lead_dict(
        event_date=(TODAY - timedelta(days=40)).isoformat(),
        case_checked_at="2026-10-02T13:00:00+00:00",
        case_stage="writ",
        writ_date=(TODAY - timedelta(days=1)).isoformat(),
    )
    unread = lead_dict(event_date=(TODAY + timedelta(days=10)).isoformat())
    assert outreach.latest_event(writ, TODAY) == ("Writ", (TODAY - timedelta(days=1)).isoformat())
    assert outreach.score(writ, today=TODAY) == 35 + 25 + 10 + 15
    assert outreach.score(writ, today=TODAY) > outreach.score(unread, today=TODAY)


def test_dates_say_what_they_are():
    assert outreach.date_label(lead_dict(event_date="2026-10-14")) == "Hearing"
    assert outreach.date_label(lead_dict(case_checked_at="2026-10-01T00:00:00")) == "Filed"
    assert outreach.date_label(lead_dict(lead_type="code_violation", source="tucson_code_cases")) == "Opened"
    assert outreach.date_label(lead_dict(source="csv_import")) == "Filed"


# ---- addresses guessed from the landlord's property ---------------------------


def parcel(pid, site, use):
    return {
        "PARCEL": pid,
        "ADDRESSEE": "EXAMPLE HOMES LLC",
        "ADDRESS": "PO BOX 1",
        "CITY": "PHOENIX",
        "STATE_PROVINCE": "AZ",
        "POSTAL_CODE": "85001",
        "SITE_ADDRESS": site,
        "SITE_ZIP": "85701",
        "USE_DESC": use,
        "LAT": 32.2,
        "LON": -110.9,
    }


class Assessor:
    def __init__(self, rows):
        self.rows = rows

    def by_owner(self, name, limit=200):
        return self.rows

    def by_parcels(self, parcels):
        return {}

    def by_site_address(self, address):
        return None


def test_common_areas_and_land_are_not_homes():
    assert not is_residential("TRUE CONDOMINIUM COMMON AREA")
    assert not is_multifamily("TRUE CONDOMINIUM COMMON AREA")
    assert not is_residential("VACANT RESIDENTIAL LAND")
    assert not is_residential("NON-RESIDENTIAL")
    assert is_residential("CONDOMINIUM") and is_multifamily("APARTMENTS 25+ UNITS")


def test_guessed_address_is_marked_and_common_area_never_used(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    eviction(conn, "CV26-000001-EA", "2026-09-30")
    complex_ = parcel("111", "100 W EXAMPLE APTS", "APARTMENTS 25+ UNITS")
    common = parcel("222", "500 N SAMPLE CT", "TRUE CONDOMINIUM COMMON AREA")
    enrich_landlords(conn, Assessor([complex_, common]))
    r = row(conn, "CV26-000001-EA")
    # The common area doesn't count, so the landlord has one place: the complex.
    assert r["address"] == "100 W EXAMPLE APTS" and r["address_source"] == "landlord"

    conn.execute("UPDATE leads SET address = NULL, parcel = NULL, enriched_at = NULL, address_source = NULL")
    enrich_landlords(conn, Assessor([common]))
    assert row(conn, "CV26-000001-EA")["address"] is None


def test_older_common_area_guesses_are_dropped(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    eviction(conn, "CV26-000001-EA", "2026-09-30")
    conn.execute(
        "UPDATE leads SET address = '500 N SAMPLE CT', parcel = '222', enriched_at = '2026-10-01', "
        "property_use = 'TRUE CONDOMINIUM COMMON AREA'"
    )
    assert fix_inferred_addresses(conn) == 1
    r = row(conn, "CV26-000001-EA")
    assert r["address"] is None and r["parcel"] is None and r["enriched_at"] is None


# ---- the pause stops a run that is already going ------------------------------


class SlowCourt:
    """Case pages that take a moment; Steve pauses Lead Desk during the first."""

    def __init__(self, path, delay=0.05):
        self.path, self.delay, self.calls = path, delay, []

    def fetch(self, url):
        self.calls.append(case_id(url))
        if len(self.calls) == 1:
            App(self.path).save_settings({"paused": True})
        time.sleep(self.delay)
        return parse_case_html(CASE_HTML, url=url)


def seed_cases(conn, n=5):
    for i in range(n):
        db.upsert(
            conn,
            Lead(
                "pima_jp_calendar",
                f"CV26-{i:06d}-EA",
                "eviction",
                "2026-10-10",
                plaintiff="EXAMPLE HOMES LLC",
                url=CASE_URL.format(1000001 + i),
                in_pima=True,
            ),
        )
    conn.commit()


class NoCalls:
    name = "no-calls"

    def __getattr__(self, name):
        raise AssertionError(f"made a request ({name}) after the pause")


class Nothing:
    def fetch(self, *a, **kw):
        return []


def test_pause_during_the_daily_check_stops_the_rest(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    seed_cases(conn)
    court = SlowCourt(path, delay=0)
    summary = daily.run_daily(
        conn,
        calendar=Nothing(),
        code_cases=Nothing(),
        case_client=court,
        parcel_client=NoCalls(),
        geocoder=NoCalls(),
        providers=[NoCalls()],
        log=lambda m: None,
    )
    assert len(court.calls) == 1
    assert summary["paused"] and summary["paused_during"] == "cases"
    assert "owners" not in summary and "contacts" not in summary
    assert "stopped at eviction case pages because Lead Desk was paused" in daily.describe(summary)


def test_pause_stops_the_phone_lookup_loop(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    for i in range(4):
        eviction(conn, f"CV26-00000{i}-EA", "2026-09-30", plaintiff=f"EXAMPLE {i} HOMES LLC")
    calls = []

    class Lookup:
        name = "osm"

        def find(self, lead, name):
            calls.append(name)
            db.put_settings(conn, {"paused": True})
            conn.commit()
            return Contact(phone="(520) 555-0100", source="osm")

    pause = db.PauseWatch(conn)
    counts = find_contacts(conn, [Lookup()], scanner=None, log=lambda *a: None, should_stop=pause)
    assert len(calls) == 1 and counts["stopped_early"] and pause.hit


# ---- failed lookups are reported, not hidden ----------------------------------


def test_failed_lookups_show_in_the_daily_summary(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    for i in range(6):
        eviction(conn, f"CV26-00000{i}-EA", "2026-09-30", plaintiff=f"EXAMPLE {i} HOMES LLC")

    class Down:
        name = "osm"

        def find(self, lead, name):
            raise requests.ConnectionError("lookup service down")

    summary = daily.run_daily(
        conn,
        calendar=Nothing(),
        code_cases=Nothing(),
        case_client=type("C", (), {"fetch": lambda self, url: None})(),
        parcel_client=Assessor([]),
        geocoder=type("G", (), {"geocode": lambda *a: None})(),
        providers=[Down()],
        log=lambda m: None,
    )
    line = daily.describe(summary)
    assert "0 landlord contacts found, 6 lookups failed (will retry tomorrow)" in line


def test_a_refused_google_key_points_to_settings(tmp_path):
    from leadgen.lookup import GooglePlacesProvider

    conn = db.connect(tmp_path / "l.db")
    eviction(conn, "CV26-000001-EA", "2026-09-30")

    class Refused:
        status_code = 403

        def raise_for_status(self):
            raise requests.HTTPError("403 Forbidden", response=self)

    class Session:
        headers: dict = {}

        def post(self, *a, **kw):
            return Refused()

    google = GooglePlacesProvider("bad-key", session=Session())
    counts = find_contacts(conn, [google], scanner=None, log=lambda *a: None)
    assert counts["errors"] == 1 and counts["error_cause"] == "google_key"
    line = daily.describe({"contacts": counts})
    assert "1 lookup failed (will retry tomorrow; check the Google key in Settings)" in line
