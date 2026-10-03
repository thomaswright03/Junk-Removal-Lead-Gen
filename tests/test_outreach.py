import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from leadgen import db, leadlist, outreach
from leadgen.enrich import enrich, is_entity, owner_fields
from leadgen.models import Lead
from leadgen.util import az_today
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
    return {**PARCEL, "PARCEL": parcel, "ADDRESSEE": name, "ADDRESS": mail, "SITE_ADDRESS": site, "USE_DESC": use}


def seed(conn):
    leads = [
        Lead(
            "tucson_code_cases",
            "CE-1",
            "code_violation",
            "2026-09-30",
            "1825 W PRICE ST",
            city="Tucson",
            lat=32.279,
            lon=-111.005,
            parcel="10610001E",
            in_pima=True,
            description="Property Maintenance | Active | PMMULT / trash and debris",
        ),
        Lead(
            "tucson_code_cases",
            "CE-2",
            "code_violation",
            "2026-09-10",
            "10 E OWNER LN",
            city="Tucson",
            lat=32.25,
            lon=-110.95,
            parcel="P2",
            in_pima=True,
            description="Property Maintenance | Active | WEEDS / weeds",
        ),
        Lead(
            "tucson_code_cases",
            "CE-3",
            "code_violation",
            "2026-09-29",
            "20 S RENTAL AVE",
            city="Tucson",
            lat=32.21,
            lon=-110.97,
            parcel="P3",
            in_pima=True,
            description="Vacant/Nuisance Buildings | Active | open to entry",
        ),
        Lead(
            "pima_jp_calendar",
            "CV26-000001-EV",
            "eviction",
            "2026-09-30",
            None,
            plaintiff="SAGUARO APARTMENTS LLC",
            defendant="DOE, JANE",
            in_pima=True,
        ),
    ]
    for l in leads:
        db.upsert(conn, l)
    db.put_settings(conn, {"lead_view": "all"})  # these tests use code cases too
    conn.commit()
    return FakeParcels(
        [
            PARCEL,
            owner("P2", "SMITH JOHN", "10 E OWNER LN", "10 E OWNER LN"),
            owner("P3", "DESERT RENTALS LLC", "PO BOX 1", "20 S RENTAL AVE"),
        ]
    )


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
            db.upsert(
                conn,
                Lead(
                    "pima_jp_calendar",
                    f"CV26-{n:06d}-EA",
                    "eviction",
                    f"2026-09-{1 + (i * 7) % 29:02d}",
                    f"{100 + n} W TEST ST" if has_address else None,
                    plaintiff=landlord,
                    defendant="DOE, PAT",
                    in_pima=True,
                    eviction_notice=True,
                ),
            )
            conn.execute(
                "UPDATE leads SET owner_name = ?, owner_absentee = ?, owner_entity = ? WHERE source_id = ?",
                (landlord, i % 2, int(i % 3 == 0), f"CV26-{n:06d}-EA"),
            )
    conn.commit()


def assigned_rows(conn, round_only=True):
    sql = "SELECT * FROM leads WHERE channel IS NOT NULL" + (" AND assign_round IS NOT NULL" if round_only else "")
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
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-NEW-EA",
            "eviction",
            "2026-09-30",
            "9 W NEW ST",
            plaintiff="MESA ADDR 0 LLC",
            in_pima=True,
            eviction_notice=True,
        ),
    )
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


def seed_real_mix(conn):
    """What a real daily run gives: eviction notices with no address, one at
    an apartment complex with no unit, and City code cases with addresses."""
    for i in range(6):
        db.upsert(
            conn,
            Lead(
                "pima_jp_case",
                f"CV26-{i:06d}-EA",
                "eviction",
                "2026-09-28",
                None,
                plaintiff=f"CACTUS {i} LLC",
                in_pima=True,
                eviction_notice=True,
            ),
        )
    db.upsert(
        conn,
        Lead(
            "pima_jp_case",
            "CV26-000099-EA",
            "eviction",
            "2026-09-28",
            "500 N BIG COMPLEX DR",
            plaintiff="BIG COMPLEX LLC",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.execute(
        "UPDATE leads SET property_use = 'APARTMENTS 25+ UNITS', owner_name = 'BIG COMPLEX LLC', owner_entity = 1 "
        "WHERE source_id = 'CV26-000099-EA'"
    )
    for i in range(8):
        db.upsert(
            conn,
            Lead(
                "tucson_code_cases",
                f"CE-{i}",
                "code_violation",
                "2026-09-25",
                f"{10 + i} E JUNK ST",
                in_pima=True,
                description="Property Maintenance | Active | DUMP / items in yard",
            ),
        )
        conn.execute(
            "UPDATE leads SET owner_name = ?, owner_entity = ? WHERE source_id = ?",
            (f"OWNER {i} LLC" if i % 2 else f"PERSON {i} PAT", i % 2, f"CE-{i}"),
        )
    conn.commit()


def test_default_settings_assign_more_than_zero_on_a_real_mix(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_real_mix(conn)
    app = App(path)  # default settings: evictions with a notice
    preview = app.state({"list": "queue", "channel": "door_hanger"})["split"]
    combos = preview["combos"]
    # No eviction has a door a hanger can go to (no address, or a complex with no unit).
    assert combos["door_hanger+phone+property_manager"] == 0 and combos["door_hanger"] == 0
    assert combos["phone+property_manager"] == 7
    # So the methods ticked at first are the ones the pool can work.
    assert preview["suggested"] == ["phone", "property_manager"]
    out = app.assign({"count": 40, "channels": preview["suggested"]})
    assert sum(out["assigned"].values()) == 7

    # All leads (code cases too): door hangers become possible.
    conn.execute("UPDATE leads SET channel = NULL, assigned_by = NULL, assign_round = NULL")
    conn.commit()
    app.save_settings({"lead_view": "all"})
    preview = app.state({"list": "queue", "channel": "door_hanger"})["split"]
    assert preview["combos"]["door_hanger+phone+property_manager"] == 4  # company-owned code cases
    assert preview["combos"]["door_hanger+phone"] == 8
    assert preview["suggested"] == ["door_hanger", "phone", "property_manager"]
    out = app.assign({"count": 40, "channels": preview["suggested"]})
    assert sum(out["assigned"].values()) == 4


def test_followed_leads_are_counted_apart_from_hand_set_ones(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_evictions(conn, with_address=6, without=0)
    app = App(path)
    app.assign({"count": 6, "channels": ["door_hanger", "phone"]})
    # More cases for the same landlords, and new landlords.
    for i in range(4):
        db.upsert(
            conn,
            Lead(
                "pima_jp_calendar",
                f"CV26-MORE{i}-EA",
                "eviction",
                "2026-09-30",
                f"{i} W MORE ST",
                plaintiff="MESA ADDR 0 LLC" if i < 3 else "NEW LANDLORD LLC",
                in_pima=True,
                eviction_notice=True,
            ),
        )
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-OTHER-EA",
            "eviction",
            "2026-09-30",
            "7 W OTHER ST",
            plaintiff="OTHER LANDLORD LLC",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.commit()
    out = app.assign({"count": 3, "channels": ["door_hanger", "phone"]})
    # The 3 followed leads don't use up the round's 3.
    assert sum(out["followed"].values()) == 3 and sum(out["assigned"].values()) == 2
    state = app.state()
    mix = {r["channel"]: r["mix"] for r in state["results"]}
    assert sum(m["followed"] for m in mix.values()) == 3
    assert sum(m["set_by_hand"] for m in mix.values()) == 0
    assert not any("by hand" in r for r in state["comparison"]["reasons"])
    assert any("already working their landlord" in n for n in state["comparison"]["notes"])

    # Only a method changed on the lead counts as set by hand.
    lead = next(l for l in state["leads"] if l["source_id"] == "CV26-OTHER-EA")
    other = "phone" if lead["channel"] == "door_hanger" else "door_hanger"
    app.update_lead({"id": lead["id"], "fields": {"channel": other}})
    mix = {r["channel"]: r["mix"] for r in app.state()["results"]}
    assert sum(m["set_by_hand"] for m in mix.values()) == 1


def test_older_followed_leads_are_recognised(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_evictions(conn, with_address=3, without=0)
    ids = [r["id"] for r in conn.execute("SELECT id FROM leads ORDER BY id").fetchall()]
    # As an older version stored them: a round, a lead that followed its
    # landlord in that round (no round id), and one set by hand later.
    conn.execute("UPDATE leads SET channel = 'phone', assigned_at = 'T1', assign_round = 'R1' WHERE id = ?", (ids[0],))
    conn.execute("UPDATE leads SET channel = 'phone', assigned_at = 'T1' WHERE id = ?", (ids[1],))
    conn.execute("UPDATE leads SET channel = 'phone', assigned_at = 'T2' WHERE id = ?", (ids[2],))
    conn.commit()
    mix = {r["channel"]: r["mix"] for r in App(path).state()["results"]}["phone"]
    assert (mix["followed"], mix["set_by_hand"]) == (1, 1)


class DownParcels(FakeParcels):
    """The county parcel service answering with errors."""

    def by_parcels(self, parcels):
        raise ConnectionError("county service down")

    def by_site_address(self, address):
        raise ConnectionError("county service down")

    def by_owner(self, name, limit=200):
        raise ConnectionError("county service down")


def test_assessor_outage_leaves_leads_for_the_next_run(caplog):
    conn = db.connect(":memory:")
    parcels = seed(conn)
    with caplog.at_level("WARNING"):
        counts = enrich(conn, DownParcels([]))
    # Three code cases (by parcel) and the eviction's landlord: all failed, none marked as looked up.
    assert counts["errors"] == 4 and counts["found"] == counts["not_found"] == 0
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE enriched_at IS NOT NULL").fetchone()[0] == 0
    assert "owner lookup by parcel failed" in caplog.text and "ConnectionError" in caplog.text
    assert "landlord lookup failed for lead" in caplog.text
    # The next run, with the county back, finds them.
    assert enrich(conn, parcels)["found"] == 3

    # A lead with only an address: the address match fails the same way.
    conn.execute("UPDATE leads SET parcel = NULL, enriched_at = NULL WHERE source_id = 'CE-2'")
    with caplog.at_level("WARNING"):
        counts = enrich(conn, DownParcels([]))
    assert counts["errors"] == 1
    assert "owner lookup failed for lead" in caplog.text
    assert conn.execute("SELECT enriched_at FROM leads WHERE source_id = 'CE-2'").fetchone()[0] is None


def test_daily_summary_names_failed_lookups():
    from leadgen import daily

    summary = {"owners": {"found": 2, "not_found": 0, "errors": 3}, "geocode": {"geocoded": 0, "errors": 1}}
    line = daily.describe(summary)
    assert "3 owner lookups failed" in line and "1 map lookup failed" in line
    assert daily.problems(summary) == ["3 owner lookups failed", "1 map lookup failed"]


def test_records_request_csv_fills_addresses_of_known_cases(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_real_mix(conn)
    conn.execute(
        "UPDATE leads SET address = '1 W GUESS ST', address_source = 'landlord' WHERE source_id = 'CV26-000001-EA'"
    )
    conn.execute(
        "UPDATE leads SET address = '2 W TYPED ST', address_source = 'manual' WHERE source_id = 'CV26-000002-EA'"
    )
    conn.commit()
    app = App(path, parcel_client=FakeParcels([]))
    state = app.state({"list": "leads", "type": "address_work"})
    before = state["counts"]
    assert (before["evictions_open"], before["evictions_with_address"]) == (7, 2)
    # The work queue: open evictions with no address or only a guess.
    queue = {l["source_id"] for l in state["list"]["leads"]}
    assert "CV26-000001-EA" in queue and "CV26-000002-EA" not in queue
    assert all(l["lead_type"] == "eviction" for l in state["list"]["leads"])
    records = state["addresses"]["records"]
    assert records["last_import"] is None and records["due"]
    csv = (
        "Case Number,Property Address,Plaintiff\n"
        "CV26-000000-EA,10 N FOUND AVE,CACTUS 0 LLC\n"
        "CV26-000001-EA,11 N FOUND AVE,CACTUS 1 LLC\n"
        "CV26-000002-EA,12 N FOUND AVE,CACTUS 2 LLC\n"
        "CV26-777777-EA,13 N NEW AVE,SOMEONE NEW LLC\n"
    )
    counts = app.import_file("csv_import", "records.csv", csv.encode(), lead_type="eviction")
    assert counts["addresses_filled"] == 3 and counts["new"] == 1
    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM leads").fetchall()}
    assert rows["CV26-000000-EA"]["address"] == "10 N FOUND AVE"
    assert rows["CV26-000000-EA"]["address_source"] == "import"
    assert rows["CV26-000001-EA"]["address"] == "11 N FOUND AVE"  # a guess is replaced
    assert rows["CV26-000002-EA"]["address"] == "2 W TYPED ST"  # what Steve typed is kept
    after = app.state({"list": "leads"})["counts"]
    # Three filled or kept, plus the new case from the file.
    assert (after["evictions_open"], after["evictions_with_address"]) == (8, 5)
    # The next request asks from today on, in two weeks.
    records = app.state({"list": "leads"})["addresses"]["records"]
    today = az_today()
    assert records["last_import"] == today.isoformat() and records["last_filled"] == 3
    assert records["request_from"] == today.isoformat() and not records["due"]
    assert records["due_on"] == (today + timedelta(days=14)).isoformat()


def test_address_share_is_kept_daily_and_compared_with_a_week_ago(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed_real_mix(conn)
    today = az_today()
    week_ago = (today - timedelta(days=8)).isoformat()
    db.put_settings(conn, {"address_history": {week_ago: [1, 10], "2020-01-01": [0, 1]}})
    conn.commit()
    leadlist.record_address_share(conn, db.get_settings(conn), today)
    conn.commit()
    history = db.get_settings(conn)["address_history"]
    assert "2020-01-01" not in history  # only the last two months are kept
    assert history[today.isoformat()] == [1, 7] and history[week_ago] == [1, 10]
    progress = App(path).state({"list": "leads"})["addresses"]
    assert progress["week_ago"] == {"date": week_ago, "with_address": 1, "open": 10}


@pytest.mark.parametrize(
    "owner_name, owner_entity, plaintiff, first",
    [
        # Eviction with no assessor match: the landlord on the case.
        (None, 0, "SEDONA POINTE LLC", ""),
        (None, 0, "ROMERO, RAYNALDO M", "Raynaldo"),
        (None, 0, "ROMERO, RAYNALDO M; ROMERO, ANA", "Raynaldo"),
        (None, 0, "DESERT VISTA APTS", ""),
        (None, 0, "PALO VERDE TRUST", ""),
        (None, 0, "SAGUARO GROUP LTD", ""),
        (None, 0, "MARIA LOPEZ", ""),  # no comma: which word is the first name isn't known
        # Assessor owners are "LAST FIRST MIDDLE".
        ("SMITH JOHN A & MARY", 0, None, "John"),
        ("O'BRIEN MARY-KATE", 0, None, "Mary-Kate"),
        ("DESERT RENTALS LLC", 1, None, ""),
        ("CANYON HOLDINGS", 1, None, ""),
        ("SMITH J", 0, None, ""),  # only an initial
    ],
)
def test_call_script_greets_people_by_first_name_and_companies_with_there(owner_name, owner_entity, plaintiff, first):
    lead = {"owner_name": owner_name, "owner_entity": owner_entity, "plaintiff": plaintiff}
    assert outreach.owner_first_name(lead) == first


def test_lead_carries_the_greeting_name(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    for case, plaintiff in (("CV26-000001-EA", "SEDONA POINTE LLC"), ("CV26-000002-EA", "ROMERO, RAYNALDO M")):
        db.upsert(conn, Lead("pima_jp_calendar", case, "eviction", "2026-09-30", plaintiff=plaintiff, in_pima=True))
    db.put_settings(conn, {"lead_view": "all"})
    conn.commit()
    leads = {l["source_id"]: l for l in App(path).state({"list": "leads"})["list"]["leads"]}
    assert leads["CV26-000001-EA"]["owner_first"] == ""
    assert leads["CV26-000002-EA"]["owner_first"] == "Raynaldo"


def test_each_method_reaches_someone_else_or_offers_something_else_on_an_eviction():
    pitches = {ch: outreach.PITCHES[outreach.template_key(ch, "eviction")] for ch in outreach.CHANNELS}
    assert len({p["who"] for p in pitches.values()}) >= 2
    # The two methods that reach the landlord make different offers.
    assert len({(p["who"], p["offer"]) for p in pitches.values()}) == len(outreach.CHANNELS)
    assert "one-time" in pitches["phone"]["offer"] and "standing" in pitches["property_manager"]["offer"]


def test_results_compare_methods_within_one_kind_of_lead(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    seed(conn)
    seed_evictions(conn, with_address=4, without=0)
    conn.execute("UPDATE leads SET channel = 'phone' WHERE lead_type = 'eviction'")
    conn.execute("UPDATE leads SET channel = 'door_hanger' WHERE lead_type = 'code_violation'")
    conn.commit()
    state = App(path).state()
    by_kind = state["results_by_kind"]
    evictions = {r["channel"]: r["assigned"] for r in by_kind["eviction"]["results"]}
    codes = {r["channel"]: r["assigned"] for r in by_kind["code_violation"]["results"]}
    assert evictions == {"door_hanger": 0, "phone": 5, "property_manager": 0}
    assert codes == {"door_hanger": 3, "phone": 0, "property_manager": 0}
    # All together the two methods look comparable; within each kind there's nothing to compare yet.
    assert not by_kind["eviction"]["comparison"]["fair"]
    assert "eviction leads only" in state["comparison_basis"]["eviction"]
