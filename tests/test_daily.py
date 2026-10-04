from datetime import date, datetime
from pathlib import Path

from leadgen import daily, db, schedule
from leadgen.enrich import enrich_landlords, landlord_name, landlord_property
from leadgen.lookup import Contact
from leadgen.sources.pima_jp_calendar import (
    CalendarClient,
    PimaJpCalendar,
    has_page_link,
    parse_calendar_html,
)
from leadgen.sources.pima_jp_case import case_id, parse_case_html

FIX = Path(__file__).parent / "fixtures"
P1 = (FIX / "jp_calendar_live_p1.html").read_text()
P2 = (FIX / "jp_calendar_live_p2.html").read_text()
CASE_HTML = (FIX / "jp_case_eviction.html").read_text()


class Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeCourt:
    """Stands in for jp.pima.gov: the filter page, then two results pages."""

    def __init__(self):
        self.headers = {}
        self.posts = []

    def get(self, url, **kw):
        return Resp(
            '<form><input type="hidden" name="__VIEWSTATE" value="start"/>'
            '<input type="submit" name="ctl00$MainContent$submitFilter" value="submit"/></form>'
        )

    def post(self, url, data=None, **kw):
        self.posts.append(dict(data))
        return Resp(P2 if data.get("__EVENTARGUMENT") == "Page$2" else P1)


def test_parse_live_calendar_rows():
    leads = {l.source_id: l for l in parse_calendar_html(P1, assume_eviction=True)}
    lead = leads["CV26-012345-EA"]
    assert lead.plaintiff == "SAGUARO VISTA APARTMENTS LLC"
    assert lead.defendant == "DOE, JANE A"
    assert lead.event_date == "2026-10-05"
    assert lead.url == "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID=1000001"


def test_page_links_with_either_quote():
    assert has_page_link("__doPostBack('grid','Page$2')", 2)
    assert has_page_link("__doPostBack(&#39;grid&#39;,&#39;Page$11&#39;)", 11)
    assert not has_page_link("__doPostBack(&#39;grid&#39;,&#39;Page$12&#39;)", 1)


def test_calendar_search_reads_every_page():
    court = FakeCourt()
    client = CalendarClient(session=court, delay=0)
    leads = list(PimaJpCalendar().fetch("2026-10-03", "2026-11-02", client=client))
    assert [l.source_id for l in leads] == ["CV26-012345-EA", "CV26-012346-EA", "CV26-012347-EA"]
    first, second = court.posts
    assert first["drpDnCaseType"] == "Eviction Actions"
    assert first["ctl00$MainContent$drpDnEventType"] == "Eviction Action"
    assert first["startDate"] == "10-03-2026" and first["endDate"] == "11-02-2026"
    assert second["__EVENTARGUMENT"] == "Page$2" and second["__VIEWSTATE"] == "vs1"
    assert client.pages == 2


def parcel(pid, owner, site, use="APARTMENTS 25+ UNITS"):
    return {
        "PARCEL": pid,
        "ADDRESSEE": owner,
        "ADDRESS": "PO BOX 1",
        "CITY": "PHOENIX",
        "STATE_PROVINCE": "AZ",
        "POSTAL_CODE": "85001",
        "SITE_ADDRESS": site,
        "SITE_ZIP": "85705",
        "USE_DESC": use,
        "YearBuilt": "1985",
        "LAT": 32.3,
        "LON": -110.98,
    }


class FakeParcels:
    def __init__(self, rows):
        self.rows = rows

    def by_owner(self, name, limit=200):
        return [r for r in self.rows if r["ADDRESSEE"].startswith(name)]

    def by_parcels(self, parcels):
        return {}

    def by_site_address(self, address):
        return None


def test_landlord_property_only_when_unambiguous():
    rows = [
        parcel("P1", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA"),
        parcel("P2", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA"),
        parcel("P3", "DESERT SKY PROPERTY MGMT LLC", "1 A ST"),
        parcel("P4", "DESERT SKY PROPERTY MGMT LLC", "2 B ST"),
    ]
    client = FakeParcels(rows)
    owner, site = landlord_property(client, "SAGUARO VISTA APARTMENTS LLC")
    assert owner["PARCEL"] == "P1" and site["SITE_ADDRESS"] == "100 W SAGUARO VISTA"
    owner, site = landlord_property(client, "DESERT SKY PROPERTY MGMT LLC")
    assert owner and site is None
    assert landlord_name("DOE, JANE; ROE, RICHARD") is None  # people aren't searched
    assert landlord_name("MESQUITE GARDENS, LP; OTHER LLC") == "MESQUITE GARDENS LP"


def test_enrich_landlords_fills_owner_and_property():
    conn = db.connect(":memory:")
    for l in parse_calendar_html(P1, assume_eviction=True):
        db.upsert(conn, l)
    rows = [parcel("P1", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA")]
    counts = enrich_landlords(conn, FakeParcels(rows))
    assert counts == {"found": 1, "with_property": 1, "not_found": 1, "errors": 0}
    r = conn.execute("SELECT * FROM leads WHERE source_id = 'CV26-012345-EA'").fetchone()
    assert r["address"] == "100 W SAGUARO VISTA" and r["lat"] == 32.3
    assert r["owner_name"] == "SAGUARO VISTA APARTMENTS LLC" and r["enriched_at"]


class NoCodeCases:
    def fetch(self, since, until, **kw):
        return iter(())


class FakeCases:
    def __init__(self):
        self.fetched = []

    def fetch(self, url):
        cid = case_id(url)
        self.fetched.append(cid)
        html = CASE_HTML.replace(
            "CV26-012345-EA",
            {"1000001": "CV26-012345-EA", "1000002": "CV26-012346-EA", "1000003": "CV26-012347-EA"}[cid],
        )
        return parse_case_html(html, url=url)


class FakePhones:
    name = "fake"

    def find(self, lead, business_name):
        if business_name and "SAGUARO" in business_name:
            return Contact(phone="(520) 555-0101", source="fake", matched_name=business_name)
        return None


class NoGeocode:
    def geocode(self, *a):
        return None


def test_run_daily_end_to_end():
    conn = db.connect(":memory:")
    heard = []
    cases = FakeCases()
    # The calendar needs a client; give it the fake court.
    cal = PimaJpCalendar()
    cal_fetch = cal.fetch
    cal.fetch = lambda since, until, **kw: cal_fetch(since, until, client=CalendarClient(FakeCourt(), delay=0))
    summary = daily.run_daily(
        conn,
        today=date(2026, 10, 3),
        code_cases=NoCodeCases(),
        calendar=cal,
        case_client=cases,
        parcel_client=FakeParcels([parcel("P1", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA")]),
        geocoder=NoGeocode(),
        providers=[FakePhones()],
        log=lambda m: None,
        progress=heard.append,
    )
    # The header counts the case pages as they're read.
    assert [m for m in heard if m.startswith("reading court cases")] == [
        "reading court cases: 1 of 3",
        "reading court cases: 2 of 3",
        "reading court cases: 3 of 3",
    ]
    assert summary["evictions"] == {"new": 3, "updated": 0}
    assert summary["cases"]["checked"] == 3 and summary["cases"]["with_notice"] == 3
    assert summary["contacts"]["found"] >= 1
    r = conn.execute("SELECT * FROM leads WHERE source_id = 'CV26-012345-EA'").fetchone()
    assert r["eviction_notice"] == 1 and r["owner_phone"] == "(520) 555-0101"
    assert "3 new evictions" in daily.describe(summary)
    assert db.get_settings(conn)["last_daily_run"] == "2026-10-03"
    # The day's address share is kept for the Leads tab's "a week ago".
    assert "2026-10-03" in db.get_settings(conn)["address_history"]
    # Next day: the cases already confirmed aren't read again.
    cases.fetched.clear()
    daily.run_daily(
        conn,
        today=date(2026, 10, 4),
        code_cases=NoCodeCases(),
        calendar=cal,
        case_client=cases,
        parcel_client=FakeParcels([]),
        geocoder=NoGeocode(),
        providers=[],
        log=lambda m: None,
    )
    assert cases.fetched == []


def test_one_failing_step_does_not_stop_the_rest():
    class Broken:
        def fetch(self, *a, **kw):
            raise ConnectionError("court site down")

    conn = db.connect(":memory:")
    summary = daily.run_daily(
        conn,
        today=date(2026, 10, 3),
        code_cases=NoCodeCases(),
        calendar=Broken(),
        case_client=FakeCases(),
        parcel_client=FakeParcels([]),
        geocoder=NoGeocode(),
        providers=[],
        log=lambda m: None,
    )
    assert "error" in summary["evictions"]
    assert "stale" in summary and "failed: Justice Court calendar" in daily.describe(summary)


def test_due_once_a_day_after_six():
    conn = db.connect(":memory:")
    assert not daily.due(conn, now=datetime(2026, 10, 3, 5, 0))
    assert daily.due(conn, now=datetime(2026, 10, 3, 6, 5))
    db.put_settings(conn, {"last_daily_run": "2026-10-03"})
    assert not daily.due(conn, now=datetime(2026, 10, 3, 9, 0))
    assert daily.due(conn, now=datetime(2026, 10, 4, 6, 0))


def test_launch_agent_plist(tmp_path):
    xml = schedule.plist(tmp_path / "leads & co.db", 6, 30, tmp_path, tmp_path / "daily.log")
    assert "<string>daily</string>" in xml and "leads &amp; co.db" in xml
    assert "<integer>6</integer>" in xml and "<integer>30</integer>" in xml


class Down:
    """A source whose website can't be reached."""

    def fetch(self, *a, **kw):
        raise ConnectionError("network unreachable")


def run(conn, now, calendar=None, code_cases=None):
    return daily.run_daily(
        conn,
        now=now,
        code_cases=code_cases or NoCodeCases(),
        calendar=calendar or Down(),
        case_client=FakeCases(),
        parcel_client=FakeParcels([]),
        geocoder=NoGeocode(),
        providers=[],
        log=lambda m: None,
    )


def test_a_failed_source_is_retried_later_the_same_day():
    from leadgen.jobs import next_daily_run

    conn = db.connect(":memory:")
    morning = datetime(2026, 10, 3, 6, 5)
    summary = run(conn, morning, code_cases=Down())
    assert summary["retry_at"] == "2026-10-03T06:35"
    assert "trying again at 6:35 AM" in daily.describe(summary)
    settings = db.get_settings(conn)
    # Not today's check: the header says a retry is coming, and when.
    assert settings.get("last_daily_run") is None
    assert settings["daily_retry"]["failed"] == ["tucson_code_cases", "evictions"]
    assert daily.retry_status(settings, morning.date()) == {
        "time": "6:35 AM",
        "failed": ["Tucson code cases", "Justice Court calendar"],
        "attempt": 1,
    }
    assert next_daily_run(settings, now=datetime(2026, 10, 3, 13, 10)).startswith("today at 6:35 AM (retrying:")
    assert next_daily_run(settings, serverless=True, now=datetime(2026, 10, 3, 13, 10)).startswith(
        "today after 6:35 AM"
    )
    # Due again at the retry time (a run on the hour a few minutes early counts), not before.
    assert not daily.due(conn, now=datetime(2026, 10, 3, 6, 20))
    assert daily.due(conn, now=datetime(2026, 10, 3, 6, 26))
    # Still down: backing off, 1 hour then 2.
    assert run(conn, datetime(2026, 10, 3, 6, 40))["retry_at"] == "2026-10-03T07:40"
    assert run(conn, datetime(2026, 10, 3, 7, 45))["retry_at"] == "2026-10-03T09:45"
    # After the last retry the day counts as checked; tomorrow starts over.
    summary = run(conn, datetime(2026, 10, 3, 9, 50))
    assert "retry_at" not in summary
    settings = db.get_settings(conn)
    assert settings["last_daily_run"] == "2026-10-03" and settings["daily_retry"]["gave_up"]
    assert daily.retry_status(settings, date(2026, 10, 3)) is None
    assert not daily.due(conn, now=datetime(2026, 10, 3, 12, 0))
    assert daily.due(conn, now=datetime(2026, 10, 4, 6, 0))


def test_a_retry_that_gets_through_makes_the_day_checked():
    conn = db.connect(":memory:")
    run(conn, datetime(2026, 10, 3, 6, 0))
    assert db.get_settings(conn)["daily_retry"]["attempt"] == 1
    summary = run(conn, datetime(2026, 10, 3, 6, 30), calendar=NoCodeCases())  # the court is back (no hearings)
    assert "retry_at" not in summary and "error" not in summary["evictions"]
    settings = db.get_settings(conn)
    assert settings["last_daily_run"] == "2026-10-03" and settings["daily_retry"] is None
    assert not daily.due(conn, now=datetime(2026, 10, 3, 7, 0))


def test_a_retry_is_never_set_past_midnight():
    conn = db.connect(":memory:")
    summary = run(conn, datetime(2026, 10, 3, 23, 50))
    assert "retry_at" not in summary
    assert db.get_settings(conn)["last_daily_run"] == "2026-10-03"


def test_scheduled_job_runs_again_for_retries(tmp_path):
    xml = schedule.plist(tmp_path / "leads.db", 6, 0, tmp_path, tmp_path / "daily.log")
    assert "<string>--if-due</string>" in xml
    assert xml.count("<key>Hour</key>") == 5 and "<integer>12</integer>" in xml
    assert schedule.run_hours(20) == [20, 21, 22]


def test_status_has_no_progress_message_after_the_check(tmp_path):
    from leadgen.web import App

    app = App(
        tmp_path / "l.db",
        calendar=NoCodeCases(),
        code_cases=NoCodeCases(),
        case_client=FakeCases(),
        parcel_client=FakeParcels([]),
        geocoder=NoGeocode(),
        providers=[],
    )
    app.refresh({})
    status = app.status()["daily"]
    assert status["running"] is False and status["message"] is None and status["retry"] is None


def test_the_check_says_how_long_it_takes_and_the_readme_agrees(tmp_path):
    """Starting the check says it takes about 15 minutes (it reads up to 400
    case pages at a polite pace), and so does the README."""
    from leadgen.web import App

    from leadgen.jobs import STARTED_MESSAGE

    app = App(
        tmp_path / "l.db",
        calendar=NoCodeCases(),
        code_cases=NoCodeCases(),
        case_client=FakeCases(),
        parcel_client=FakeParcels([]),
        geocoder=NoGeocode(),
        providers=[],
    )
    started = app.start_daily()
    assert started["started"] is True and started["message"] == STARTED_MESSAGE
    assert daily.CHECK_MINUTES in STARTED_MESSAGE and "header counts" in STARTED_MESSAGE
    import threading

    for t in threading.enumerate():  # let the check finish
        if t.name == "daily":
            t.join(10)
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert "a minute later" not in readme.lower() and daily.CHECK_MINUTES in readme


def test_scheduled_daily_does_nothing_once_today_has_run(tmp_path, capsys):
    from leadgen import cli
    from leadgen.util import az_today

    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.put_settings(conn, {"last_daily_run": az_today().isoformat()})
    conn.commit()
    cli.main(["--db", str(path), "daily", "--if-due"])
    assert "nothing to do" in capsys.readouterr().out


def test_daily_command_fails_plainly_when_a_whole_source_is_down(tmp_path, monkeypatch, capsys):
    import pytest
    import requests

    from leadgen import cli

    url = "https://www.jp.pima.gov/CaseSearch/Calendar.aspx"

    class Unreachable:
        def fetch(self, *a, **kw):
            err = requests.exceptions.ProxyError(f"HTTPSConnectionPool(host='www.jp.pima.gov'): {url}")
            err.request = requests.Request("GET", url)
            raise err

    real = daily.run_daily

    def offline(conn, **kw):
        return real(
            conn,
            code_cases=Unreachable(),
            calendar=Unreachable(),
            case_client=FakeCases(),
            parcel_client=FakeParcels([]),
            geocoder=NoGeocode(),
            providers=[],
            log=kw["log"],
        )

    monkeypatch.setattr(daily, "run_daily", offline)
    with pytest.raises(SystemExit) as stop:
        cli.main(["--db", str(tmp_path / "l.db"), "daily"])
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "the Pima County Justice Court website couldn't be reached" in out
    assert "trying again at" in out
    for technical in ("ProxyError", "Error:", "http", "HTTPSConnectionPool", "{"):
        assert technical not in out, technical
    # The full details are there for --debug.
    with pytest.raises(SystemExit):
        cli.main(["--debug", "--db", str(tmp_path / "l2.db"), "daily"])
    assert "ProxyError" in capsys.readouterr().out


def test_daily_line_is_in_arizona_time(capsys):
    from datetime import timezone

    # 21:57 UTC is 2:57 PM in Tucson.
    daily.main_log({"retry_at": "2026-10-03T15:27"}, now=datetime(2026, 10, 3, 21, 57, tzinfo=timezone.utc))
    line = capsys.readouterr().out.strip()
    assert line.startswith("Oct 3, 2026 2:57 PM ") and line.endswith("trying again at 3:27 PM")
    assert "\n" not in line  # one line, no JSON
