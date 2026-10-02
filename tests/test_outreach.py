import json
from pathlib import Path

from leadgen import db, outreach
from leadgen.enrich import enrich, is_entity, owner_fields
from leadgen.models import Lead
from leadgen.web import App, render_template

FIX = Path(__file__).parent / "fixtures"
PARCEL = json.loads((FIX / "parcel_10610001E.json").read_text())["features"][0]["attributes"]


class FakeParcels:
    """Stands in for the county parcel service."""

    def __init__(self, records):
        self.records = {r["PARCEL"]: r for r in records}

    def by_parcels(self, parcels):
        return {p: self.records[p] for p in parcels if p in self.records}

    def by_site_address(self, address):
        for r in self.records.values():
            if r["SITE_ADDRESS"] in address.upper():
                return r
        return None

    def by_owner(self, name):
        return [r for r in self.records.values() if r["ADDRESSEE"].startswith(name.upper())]


def owner(parcel, name, mail, site, use="SFR GRADE 010-3 URBAN SUBDIVIDED"):
    return {**PARCEL, "PARCEL": parcel, "ADDRESSEE": name, "ADDRESS": mail, "SITE_ADDRESS": site,
            "USE_DESC": use}


def seed(conn):
    leads = [
        Lead("tucson_code_cases", "CE-1", "code_violation", "2026-09-30", "1825 W PRICE ST",
             city="Tucson", lat=32.279, lon=-111.005, parcel="10610001E", in_pima=True,
             description="Property Maintenance | Active | PMMULT / trash and debris"),
        Lead("tucson_code_cases", "CE-2", "code_violation", "2026-09-10", "10 E OWNER LN",
             city="Tucson", lat=32.25, lon=-110.95, parcel="P2", in_pima=True,
             description="Property Maintenance | Active | WEEDS / weeds"),
        Lead("tucson_code_cases", "CE-3", "code_violation", "2026-09-29", "20 S RENTAL AVE",
             city="Tucson", lat=32.21, lon=-110.97, parcel="P3", in_pima=True,
             description="Vacant/Nuisance Buildings | Active | open to entry"),
        Lead("pima_jp_calendar", "CV26-000001-EV", "eviction", "2026-09-30", None,
             plaintiff="SAGUARO APARTMENTS LLC", defendant="DOE, JANE", in_pima=True),
    ]
    for l in leads:
        db.upsert(conn, l)
    db.put_settings(conn, {"lead_view": "all"})  # these tests use code cases too
    conn.commit()
    return FakeParcels([
        PARCEL,
        owner("P2", "SMITH JOHN", "10 E OWNER LN", "10 E OWNER LN"),
        owner("P3", "DESERT RENTALS LLC", "PO BOX 1", "20 S RENTAL AVE"),
    ])


def test_owner_fields_absentee_and_entity():
    f = owner_fields(PARCEL)
    assert f["owner_name"] == "FRC HOLDINGS OF TUCSON LLC"
    assert f["owner_absentee"] == 1 and f["owner_entity"] == 1
    assert owner_fields(owner("x", "SMITH JOHN", "10 E OWNER LN", "10 E OWNER LANE"))["owner_absentee"] == 0
    assert is_entity("DOE JANE TR") and not is_entity("DOE JANE")


def test_enrich_fills_owner_columns():
    conn = db.connect(":memory:")
    parcels = seed(conn)
    counts = enrich(conn, parcels)
    assert counts == {"found": 3, "not_found": 1}
    row = conn.execute("SELECT * FROM leads WHERE source_id = 'CE-1'").fetchone()
    assert row["owner_name"] == "FRC HOLDINGS OF TUCSON LLC"
    assert row["owner_zip"] == "85009-5517"
    # Second run skips leads already looked up.
    assert enrich(conn, parcels) == {"found": 0, "not_found": 0}


def test_migration_adds_columns_and_backfills_parcel(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA.split("CREATE TABLE IF NOT EXISTS touches")[0])
    old.execute(
        "INSERT INTO leads (source, source_id, lead_type, first_seen, last_seen, raw_json) "
        "VALUES ('tucson_code_cases', 'X', 'code_violation', 'now', 'now', ?)",
        (json.dumps({"PARCEL": "12345678A"}),),
    )
    old.commit()
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT parcel FROM leads").fetchone()[0] == "12345678A"


def test_scores_rank_absentee_vacant_over_owner_occupied_weeds():
    conn = db.connect(":memory:")
    enrich(conn, seed(conn))
    app = App(":memory:")
    leads = {l["source_id"]: l for l in app.leads(conn, outreach.merged_settings({"lead_view": "all"}))}
    assert leads["CE-3"]["score"] > leads["CE-2"]["score"]
    assert leads["CE-1"]["score"] > leads["CE-2"]["score"]
    assert leads["CV26-000001-EV"]["eligible"] == ["phone", "property_manager"]


def test_assign_splits_evenly_and_respects_eligibility():
    conn = db.connect(":memory:")
    enrich(conn, seed(conn))
    leads = App(":memory:").leads(conn, outreach.merged_settings({"lead_view": "all"}))
    counts = outreach.assign(conn, leads, 4, list(outreach.CHANNELS), seed=1)
    assert sum(counts.values()) == 4
    assert max(counts.values()) - min(counts.values()) <= 1
    ev = conn.execute("SELECT channel FROM leads WHERE lead_type = 'eviction'").fetchone()[0]
    assert ev in ("phone", "property_manager")


def test_app_flow_touch_result_and_results(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    parcels = seed(conn)
    enrich(conn, parcels)
    conn.close()
    app = App(path, parcel_client=parcels)
    state = app.state()
    lead = next(l for l in state["leads"] if l["source_id"] == "CE-1")

    app.update_lead({"id": lead["id"], "fields": {"channel": "door_hanger"}})
    app.add_touches({"lead_id": lead["id"], "kind": "visited"})
    app.update_lead({"id": lead["id"], "fields": {"status": "won", "job_revenue": 450}})

    res = {r["channel"]: r for r in app.state()["results"]}["door_hanger"]
    assert (res["assigned"], res["touched"], res["responded"], res["won"]) == (1, 1, 1, 1)
    assert res["cost"] == 0.35 and res["revenue"] == 450
    assert [p["parcel"] for p in app.owner_properties("FRC HOLDINGS")] == ["10610001E"]


def test_touch_requires_channel(tmp_path):
    path = tmp_path / "leads.db"
    seed(db.connect(path))
    app = App(path)
    lead_id = app.state()["leads"][0]["id"]
    try:
        app.add_touches({"lead_id": lead_id, "kind": "mailed"})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_postcard_channel_is_retired(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed(conn)
    conn.execute("UPDATE leads SET channel = 'postcard', assigned_at = 'x'")
    conn.commit()
    app = App(path)
    state = app.state()
    assert "postcard" not in state["channels"]
    assert all(l["channel"] is None for l in state["leads"])


def test_first_name_from_assessor_order():
    s = outreach.merged_settings({"templates": {"phone": "Hi {owner_first}"}})
    assert render_template(s, "phone", {"owner_name": "SMITH JOHN A", "owner_entity": 0}) == "Hi John"
    assert render_template(s, "phone", {"owner_name": "ACME LLC", "owner_entity": 1}) == "Hi there"


def test_render_template():
    s = outreach.merged_settings({"business_phone": "520-555-0100"})
    text = render_template(s, "phone", {"owner_name": "SMITH JOHN", "owner_entity": 0,
                                           "address": "10 E OWNER LN"})
    assert "10 E Owner Ln" in text and "Steve's Junk Removal" in text


def test_import_calendar_upload(tmp_path):
    path = tmp_path / "leads.db"
    app = App(path, parcel_client=FakeParcels([PARCEL]))
    html = (FIX / "jp_calendar.html").read_bytes()
    counts = app.import_file("pima_jp_calendar", "cal.html", html)
    assert counts["new"] == 2  # the small-claims row is not an eviction


def test_miles_tolerates_text_coordinates():
    assert outreach.miles_between("32.36", "-111.12", 32.22, -110.97) > 0
    assert outreach.miles_between("", -111.0, 32.2, -110.9) is None
