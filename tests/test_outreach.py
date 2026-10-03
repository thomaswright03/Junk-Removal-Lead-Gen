import json
import os
from pathlib import Path

import pytest

from leadgen import db, outreach
from leadgen.enrich import enrich, is_entity, owner_fields
from leadgen.models import Lead
from leadgen.web import App

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

    def by_owner(self, name, limit=200):
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
    assert counts == {"found": 3, "not_found": 1, "landlord_property": 0}
    row = conn.execute("SELECT * FROM leads WHERE source_id = 'CE-1'").fetchone()
    assert row["owner_name"] == "FRC HOLDINGS OF TUCSON LLC"
    assert row["owner_zip"] == "85009-5517"
    # Second run skips leads already looked up.
    assert enrich(conn, parcels) == {"found": 0, "not_found": 0, "landlord_property": 0}


@pytest.mark.skipif(bool(os.environ.get("TEST_DATABASE_URL")), reason="migrates an old SQLite file")
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


def seed_evictions(conn, with_address=30, without=30):
    """Eviction leads with made-up landlords. Some landlords have several
    cases; scores vary with filing date, absentee and company owners."""
    n = 0
    for has_address, how_many in ((True, with_address), (False, without)):
        for i in range(how_many):
            n += 1
            landlord = f"MESA {'ADDR' if has_address else 'NOADDR'} {i // 3 if i < 9 else i} LLC"
            db.upsert(conn, Lead(
                "pima_jp_calendar", f"CV26-{n:06d}-EA", "eviction",
                f"2026-09-{1 + (i * 7) % 29:02d}",
                f"{100 + n} W TEST ST" if has_address else None,
                plaintiff=landlord, defendant="DOE, PAT", in_pima=True, eviction_notice=True))
            conn.execute("UPDATE leads SET owner_name = ?, owner_absentee = ?, owner_entity = ? "
                         "WHERE source_id = ?", (landlord, i % 2, int(i % 3 == 0), f"CV26-{n:06d}-EA"))
    conn.commit()


def assigned_rows(conn, round_only=True):
    sql = "SELECT * FROM leads WHERE channel IS NOT NULL" + (" AND assign_round IS NOT NULL"
                                                              if round_only else "")
    return conn.execute(sql).fetchall()


def test_assign_round_is_like_for_like(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_evictions(conn)
    app = App(path)
    out = app.assign({"count": 60, "channels": list(outreach.CHANNELS)})
    # Door hangers need an address, so only the 30 leads every channel can work go out.
    assert sum(out["assigned"].values()) == 30
    assert out["left_out"]["needs_address"] == 30
    assert sorted(out["assigned"].values()) == [10, 10, 10]

    rows = assigned_rows(conn)
    by_channel = {}
    for r in rows:
        by_channel.setdefault(r["channel"], []).append(r)
    scores = {l["id"]: l["score"] for l in app.state()["leads"]}
    for ch, rs in by_channel.items():
        assert all(r["address"] for r in rs), ch  # same address share: 100% each
    means = [sum(scores[r["id"]] for r in rs) / len(rs) for rs in by_channel.values()]
    assert max(means) - min(means) <= 8

    # No landlord is reached through two channels.
    channels_per_landlord = {}
    for r in rows:
        channels_per_landlord.setdefault(r["plaintiff"], set()).add(r["channel"])
    assert all(len(c) == 1 for c in channels_per_landlord.values())

    cmp = app.state()["comparison"]
    assert cmp["fair"] and not cmp["ready"]  # fair mix, but nothing logged yet


def test_assign_without_door_hangers_balances_address_share(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_evictions(conn)
    app = App(path)
    out = app.assign({"count": 60, "channels": ["phone", "property_manager"]})
    assert sum(out["assigned"].values()) == 60
    shares = {}
    for r in assigned_rows(conn):
        shares.setdefault(r["channel"], []).append(bool(r["address"]))
    for flags in shares.values():
        assert abs(sum(flags) / len(flags) - 0.5) <= 0.1
    res = {r["channel"]: r for r in app.state()["results"]}
    assert res["phone"]["mix"]["with_address"] is not None


def test_landlord_keeps_its_channel_in_later_rounds(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_evictions(conn, with_address=6, without=0)
    app = App(path)
    app.assign({"count": 6, "channels": list(outreach.CHANNELS)})
    first = {r["plaintiff"]: r["channel"] for r in assigned_rows(conn)}
    db.upsert(conn, Lead("pima_jp_calendar", "CV26-NEW-EA", "eviction", "2026-09-30", "9 W NEW ST",
                         plaintiff="MESA ADDR 0 LLC", in_pima=True, eviction_notice=True))
    conn.commit()
    out = app.assign({"count": 6, "channels": list(outreach.CHANNELS)})
    new = conn.execute("SELECT channel FROM leads WHERE source_id = 'CV26-NEW-EA'").fetchone()[0]
    assert new == first["MESA ADDR 0 LLC"] and sum(out["followed"].values()) == 1


def test_results_refuse_to_rank_an_unequal_mix(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_evictions(conn, with_address=10, without=10)
    conn.execute("UPDATE leads SET channel = 'door_hanger' WHERE address IS NOT NULL")
    conn.execute("UPDATE leads SET channel = 'phone' WHERE address IS NULL")
    conn.commit()
    cmp = App(path).state()["comparison"]
    assert not cmp["fair"]
    assert any("property address" in r for r in cmp["reasons"])


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


def test_import_calendar_upload(tmp_path):
    path = tmp_path / "leads.db"
    app = App(path, parcel_client=FakeParcels([PARCEL]))
    html = (FIX / "jp_calendar.html").read_bytes()
    counts = app.import_file("pima_jp_calendar", "cal.html", html)
    assert counts["new"] == 2  # the small-claims row is not an eviction


def test_miles_tolerates_text_coordinates():
    assert outreach.miles_between("32.36", "-111.12", 32.22, -110.97) > 0
    assert outreach.miles_between("", -111.0, 32.2, -110.9) is None
