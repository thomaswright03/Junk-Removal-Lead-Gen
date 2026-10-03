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
        return Resp('<form><input type="hidden" name="__VIEWSTATE" value="start"/>'
                    '<input type="submit" name="ctl00$MainContent$submitFilter" value="submit"/></form>')

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
    return {"PARCEL": pid, "ADDRESSEE": owner, "ADDRESS": "PO BOX 1", "CITY": "PHOENIX",
            "STATE_PROVINCE": "AZ", "POSTAL_CODE": "85001", "SITE_ADDRESS": site,
            "SITE_ZIP": "85705", "USE_DESC": use, "YearBuilt": "1985", "LAT": 32.3, "LON": -110.98}


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
    rows = [parcel("P1", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA"),
            parcel("P2", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA"),
            parcel("P3", "DESERT SKY PROPERTY MGMT LLC", "1 A ST"),
            parcel("P4", "DESERT SKY PROPERTY MGMT LLC", "2 B ST")]
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
    assert counts == {"found": 1, "with_property": 1, "not_found": 1}
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
        html = CASE_HTML.replace("CV26-012345-EA", {"1000001": "CV26-012345-EA",
                                                   "1000002": "CV26-012346-EA",
                                                   "1000003": "CV26-012347-EA"}[cid])
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
    cases = FakeCases()
    # The calendar needs a client; give it the fake court.
    cal = PimaJpCalendar()
    cal_fetch = cal.fetch
    cal.fetch = lambda since, until, **kw: cal_fetch(since, until, client=CalendarClient(FakeCourt(), delay=0))
    summary = daily.run_daily(
        conn, today=date(2026, 10, 3), code_cases=NoCodeCases(), calendar=cal, case_client=cases,
        parcel_client=FakeParcels([parcel("P1", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA")]),
        geocoder=NoGeocode(), providers=[FakePhones()], log=lambda m: None,
    )
    assert summary["evictions"] == {"new": 3, "updated": 0}
    assert summary["cases"]["checked"] == 3 and summary["cases"]["with_notice"] == 3
    assert summary["contacts"]["found"] >= 1
    r = conn.execute("SELECT * FROM leads WHERE source_id = 'CV26-012345-EA'").fetchone()
    assert r["eviction_notice"] == 1 and r["owner_phone"] == "(520) 555-0101"
    assert "3 new evictions" in daily.describe(summary)
    assert db.get_settings(conn)["last_daily_run"] == "2026-10-03"
    # Next day: the cases already confirmed aren't read again.
    cases.fetched.clear()
    daily.run_daily(conn, today=date(2026, 10, 4), code_cases=NoCodeCases(), calendar=cal,
                    case_client=cases, parcel_client=FakeParcels([]), geocoder=NoGeocode(),
                    providers=[], log=lambda m: None)
    assert cases.fetched == []


def test_one_failing_step_does_not_stop_the_rest():
    class Broken:
        def fetch(self, *a, **kw):
            raise ConnectionError("court site down")

    conn = db.connect(":memory:")
    summary = daily.run_daily(conn, today=date(2026, 10, 3), code_cases=NoCodeCases(),
                              calendar=Broken(), case_client=FakeCases(),
                              parcel_client=FakeParcels([]), geocoder=NoGeocode(), providers=[],
                              log=lambda m: None)
    assert "error" in summary["evictions"]
    assert "stale" in summary and "failed: evictions" in daily.describe(summary)


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
