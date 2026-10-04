from pathlib import Path

import requests

from leadgen import db
from leadgen.sources.pima_jp_calendar import parse_calendar_html
from leadgen.sources.pima_jp_case import add_cases, case_id, parse_case_html, split_case_inputs, update_cases
from leadgen.web import App

FIX = Path(__file__).parent / "fixtures"
CASE_HTML = (FIX / "jp_case_eviction.html").read_text()
NO_NOTICE_HTML = CASE_HTML.replace("EVICTION NOTICE", "ANSWER")
CASE_URL = "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID=1000001"


class FakeCases:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, id_or_url):
        cid = case_id(id_or_url)
        self.calls.append(cid)
        if cid not in self.pages:
            raise requests.ConnectionError("offline")
        return parse_case_html(self.pages[cid], url=f"https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={cid}")


def test_parse_case_page():
    lead = parse_case_html(CASE_HTML)
    assert lead.source_id == "CV26-012345-EA"
    assert lead.lead_type == "eviction"
    assert lead.eviction_notice is True
    assert lead.event_date == "2026-10-02"
    assert lead.case_status == "Open"
    assert lead.next_court_date == "2026-10-14 14:00"
    assert lead.plaintiff == "SAGUARO VISTA APARTMENTS LLC"
    assert lead.defendant == "DOE, JANE A; DOE, JOHN"
    assert lead.url == CASE_URL  # from the saved page's form action


def test_a_party_listed_twice_is_named_once():
    """The court lists a party again for a second address or attorney: each
    name appears once per role, in the order first listed."""
    jane = "<tr><td>Defendant</td><td>DOE, JANE A</td>" + "<td>&nbsp;</td>" * 7 + "</tr>"
    landlord = "<tr><td>Plaintiff</td><td>SAGUARO VISTA APARTMENTS LLC</td>" + "<td>&nbsp;</td>" * 7 + "</tr>"
    head, rest = CASE_HTML.split('<table id="gvParty">', 1)
    html = head + '<table id="gvParty">' + rest.replace("</table>", jane + landlord + "</table>", 1)
    assert html.count("DOE, JANE A") == 2
    lead = parse_case_html(html)
    assert lead.defendant == "DOE, JANE A; DOE, JOHN"
    assert lead.plaintiff == "SAGUARO VISTA APARTMENTS LLC"


def test_case_without_notice():
    lead = parse_case_html(NO_NOTICE_HTML)
    assert lead.lead_type == "eviction"
    assert lead.eviction_notice is False


def test_case_links_and_ids():
    assert case_id(CASE_URL) == "1000001"
    assert case_id("1000001") == "1000001"
    assert case_id("https://example.com/?ID=5") is None
    ids, unknown = split_case_inputs(f"{CASE_URL}\n1000002, CV26-000001-EA {CASE_URL}")
    assert ids == ["1000001", "1000002"]
    assert unknown == ["CV26-000001-EA"]


def test_calendar_row_and_case_page_share_one_row():
    conn = db.connect(":memory:")
    html = CASE_HTML  # reuse parties from the case page in a calendar row
    cal = (
        "<table><tr><th>Date</th><th>Case Number</th><th>Case Name</th><th>Event</th></tr>"
        '<tr><td>10/14/2026</td><td><a href="jcDisplayCase.aspx?ID=1000001">CV26-012345-EA</a></td>'
        "<td>SAGUARO VISTA APARTMENTS LLC vs. DOE, JANE A</td><td>Eviction Action</td></tr></table>"
    )
    cal_lead = parse_calendar_html(cal)[0]
    assert cal_lead.url == CASE_URL
    assert db.upsert(conn, cal_lead) == "new"
    assert db.upsert(conn, parse_case_html(html)) == "updated"
    # A later calendar import keeps the case page's details.
    db.upsert(conn, cal_lead)
    rows = conn.execute("SELECT * FROM leads").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["eviction_notice"] == 1 and r["case_checked_at"]
    assert r["event_date"] == "2026-10-02" and "Eviction notice filed" in r["description"]


def test_lead_desk_shows_only_evictions_with_notice(tmp_path):
    path = tmp_path / "l.db"
    app = App(
        path,
        case_client=FakeCases(
            {"1000001": CASE_HTML, "1000002": NO_NOTICE_HTML.replace("CV26-012345-EA", "CV26-012346-EA")}
        ),
    )
    with app.conn() as conn:
        from leadgen.models import Lead

        db.upsert(
            conn,
            Lead(
                source="tucson_code_cases",
                source_id="T1",
                lead_type="code_violation",
                address="1 MAIN ST",
                in_pima=True,
            ),
        )
    counts = app.add_cases({"text": f"{CASE_URL} 1000002 9999999"})
    assert counts["new"] == 2 and counts["with_notice"] == 1 and counts["failed"] == 1

    state = app.state()
    assert [l["source_id"] for l in state["leads"]] == ["CV26-012345-EA"]
    assert state["view_counts"] == {"all": 3, "evictions": 2, "eviction_notice": 1, "unchecked": 0, "code_cases": 1}

    app.save_settings({"lead_view": "evictions"})
    assert len(app.state()["leads"]) == 2
    app.save_settings({"lead_view": "all"})
    assert len(app.state()["leads"]) == 3


def test_view_counts_follow_the_status_filter(tmp_path):
    from leadgen.models import Lead

    path = tmp_path / "l.db"
    app = App(path)
    with app.conn() as conn:
        for i in range(3):
            db.upsert(conn, Lead("tucson_code_cases", f"T{i}", "code_violation", address=f"{i} MAIN ST", in_pima=True))
        conn.execute("UPDATE leads SET status = 'won' WHERE source_id = 'T0'")
        conn.commit()
    app.save_settings({"lead_view": "all"})
    state = app.state({"list": "leads", "status": "open"})
    assert state["view_counts"]["all"] == state["view_counts"]["code_cases"] == state["list"]["total"] == 2
    state = app.state({"list": "leads", "status": ""})
    assert state["view_counts"]["all"] == state["list"]["total"] == 3


def test_update_cases_rereads_open_evictions(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    db.upsert(conn, parse_case_html(NO_NOTICE_HTML, url=CASE_URL))
    conn.execute("UPDATE leads SET case_checked_at = '2000-01-01T00:00:00+00:00'")
    fake = FakeCases({"1000001": CASE_HTML})
    counts = update_cases(conn, fake, log=lambda m: None)
    assert counts == {"checked": 1, "with_notice": 1, "failed": 0, "total": 1}
    assert conn.execute("SELECT eviction_notice FROM leads").fetchone()[0] == 1
    # Just checked, so a second run skips it.
    assert update_cases(conn, fake, log=lambda m: None)["checked"] == 0


def test_import_saved_case_page(tmp_path):
    app = App(tmp_path / "l.db")
    counts = app.import_file("pima_jp_calendar", "case.html", CASE_HTML.encode())
    assert (counts["new"], counts["updated"], counts["with_notice"]) == (1, 0, 1)
    assert app.state()["leads"][0]["url"] == CASE_URL


def test_add_cases_reports_network_failure():
    conn = db.connect(":memory:")
    log = []
    counts = add_cases(conn, "1000001", FakeCases({}), log=log.append)
    assert counts["failed"] == 1 and counts["new"] == 0
    assert log == ["case 1000001: the court's case page couldn't be read"]
