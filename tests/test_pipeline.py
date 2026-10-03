import json
from datetime import date
from pathlib import Path

from leadgen import cli, db, export
from leadgen.geocode import parse_census_response
from leadgen.models import Lead
from leadgen.normalize import normalize_address
from leadgen.sources.csv_import import read_csv
from leadgen.sources.pima_jp_calendar import parse_calendar_html
from leadgen.sources.tucson_code_cases import TucsonCodeCases, matches_keywords, violation_code

FIX = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse(self.payload)


def test_normalize_address_matches_variants():
    a = normalize_address("123 North Main Street, Apt 4, Tucson, AZ 85701")
    b = normalize_address("123 N MAIN ST #4")
    assert a == b == "123 N MAIN ST UNIT 4"
    assert normalize_address("") is None


def test_tucson_code_cases_keeps_cleanout_cases():
    session = FakeSession(json.loads((FIX / "tucson_code_cases.json").read_text()))
    leads = list(TucsonCodeCases(session).fetch("2026-09-01", "2026-12-31"))
    assert [l.source_id for l in leads] == ["T26CE01001", "T26CE01003"]
    first = leads[0]
    assert first.address == "1234 E FAKE ST"
    assert first.event_date == "2026-09-24"
    assert first.lat == 32.2501
    # Missing LAT/LON falls back to the geometry.
    assert leads[1].lat == 32.21
    assert "OPENEDDATE >= DATE '2026-09-01'" in session.calls[0][1]["where"]


def test_tucson_code_cases_all_cases_flag():
    session = FakeSession(json.loads((FIX / "tucson_code_cases.json").read_text()))
    leads = list(TucsonCodeCases(session).fetch("2026-09-01", "2026-12-31", all_cases=True))
    assert len(leads) == 4


def test_violation_codes():
    assert violation_code("WEEDS / Overgrown weeds") == "WEEDS"
    assert violation_code("JMV: Older pickup") == "JMV"
    assert violation_code("RMIN/ roaches") == "RMIN"
    assert matches_keywords({"DESCRIPTION": "DUMP/ items piled in alley"})
    assert not matches_keywords({"DESCRIPTION": "RMIN/ roaches", "CaseType": "Minimum Housing"})


def test_parse_calendar_vs_column():
    leads = parse_calendar_html((FIX / "jp_calendar.html").read_text())
    assert [l.source_id for l in leads] == ["CV26-012345-EV", "CV26-012346-EV"]
    assert leads[0].plaintiff == "SAGUARO APARTMENTS LLC"
    assert leads[0].defendant == "DOE, JANE"
    assert leads[0].event_date == "2026-09-29"
    assert leads[1].plaintiff == "DESERT PROPERTY MGMT"
    assert leads[0].address is None


def test_parse_calendar_split_columns_with_address():
    leads = parse_calendar_html((FIX / "jp_calendar_split.html").read_text(), assume_eviction=True)
    assert len(leads) == 1
    l = leads[0]
    assert (l.plaintiff, l.defendant) == ("CASA GRANDE HOMES", "DOE, JOHN")
    assert l.address.startswith("4321 N Example Street")
    assert l.event_date == "2026-10-01"


def test_csv_import():
    leads = list(read_csv(FIX / "leads.csv"))
    assert len(leads) == 2
    assert leads[0].source_id == "CV26-040001-EV"
    assert leads[0].event_date == "2026-09-20"
    assert leads[0].plaintiff == "ACME RENTALS"
    assert leads[1].source_id  # hashed id for rows without a case number


def test_census_parse():
    r = parse_census_response(json.loads((FIX / "census_match.json").read_text()))
    assert r.in_pima and r.zip == "85704"
    assert parse_census_response({"result": {"addressMatches": []}}) is None


def test_upsert_is_idempotent_and_keeps_steves_edits():
    conn = db.connect(":memory:")
    lead = Lead(source="s", source_id="1", lead_type="eviction", event_date="2026-09-30", address="1 N Main St")
    assert db.upsert(conn, lead) == "new"
    row_id = conn.execute("SELECT id FROM leads").fetchone()["id"]
    db.set_status(conn, row_id, "contacted", "left voicemail")
    lead.description = "updated upstream"
    assert db.upsert(conn, lead) == "updated"
    row = conn.execute("SELECT * FROM leads").fetchone()
    assert (row["status"], row["notes"]) == ("contacted", "left voicemail")
    assert row["description"] == "updated upstream"
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1


def test_same_address_from_two_sources_is_one_lead():
    conn = db.connect(":memory:")
    db.upsert(
        conn,
        Lead(
            source="a",
            source_id="1",
            lead_type="code_violation",
            event_date="2026-09-20",
            address="100 North Main Street",
        ),
    )
    db.upsert(
        conn, Lead(source="b", source_id="X", lead_type="eviction", event_date="2026-09-25", address="100 N MAIN ST")
    )
    assert len(db.query(conn)) == 1
    assert len(db.query(conn, include_duplicates=True)) == 2


def test_mark_stale():
    conn = db.connect(":memory:")
    db.upsert(conn, Lead(source="a", source_id="old", lead_type="eviction", event_date="2026-08-01"))
    db.upsert(conn, Lead(source="a", source_id="new", lead_type="eviction", event_date="2026-09-30"))
    assert db.mark_stale(conn, 30, today=date(2026, 10, 2)) == 1


def test_exports(tmp_path):
    conn = db.connect(":memory:")
    db.upsert(
        conn,
        Lead(
            source="a",
            source_id="1",
            lead_type="eviction",
            event_date="2026-09-30",
            address="1 Main",
            plaintiff="</script><b>x</b>",
        ),
    )
    rows = db.query(conn)
    export.write_csv(rows, tmp_path / "l.csv")
    export.write_html(rows, tmp_path / "l.html")
    assert "1 Main" in (tmp_path / "l.csv").read_text()
    page = (tmp_path / "l.html").read_text()
    assert "</script><b>" not in page


def test_cli_fetch_csv_and_export(tmp_path, capsys):
    dbfile = tmp_path / "leads.db"
    cli.main(
        [
            "--db",
            str(dbfile),
            "fetch",
            "--source",
            "csv_import",
            "--file",
            str(FIX / "leads.csv"),
            "--lead-type",
            "eviction",
        ]
    )
    out = tmp_path / "out.csv"
    cli.main(["--db", str(dbfile), "--stale-days", "3650", "export", "--out", str(out)])
    text = out.read_text()
    assert "ACME RENTALS" in text and "77 W Test Rd" in text
