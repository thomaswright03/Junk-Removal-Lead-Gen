"""Whether eviction leads can be reached (a phone or email, or a known
property address), the work lists putting reachable leads first, and the
setup the Leads tab walks through: a Google key and the court records
request. Names and addresses are made up."""

from datetime import timedelta

import pytest

from leadgen import db, leadlist
from leadgen.models import Lead
from leadgen.util import az_today
from leadgen.web import App


class NoParcels:
    def by_parcels(self, parcels):
        return {}

    def by_site_address(self, address):
        return None

    def by_owner(self, name, limit=200):
        return []


def eviction(n, **fields):
    lead = Lead(
        "pima_jp_calendar",
        f"CV26-00000{n}-EA",
        "eviction",
        "2026-09-29",
        None,
        plaintiff=f"EXAMPLE RENTALS {n} LLC",
        defendant="DOE, PAT",
        in_pima=True,
        eviction_notice=True,
    )
    return lead, fields


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    leads = [
        eviction(1),  # nothing yet
        eviction(2, owner_phone="(520) 555-0102"),
        eviction(3, owner_email="office@example.com"),
        eviction(4, address="4 W TYPED ST", address_source="manual"),
        eviction(5, address="5 W GUESS ST", address_source="landlord"),  # a guess isn't known
        eviction(6, owner_phone="(520) 555-0106", address="6 W TYPED ST", address_source="manual"),
    ]
    for lead, fields in leads:
        db.upsert(conn, lead)
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE leads SET {sets} WHERE source_id = ?", [*fields.values(), lead.source_id])
    conn.commit()
    return App(path, parcel_client=NoParcels(), providers=[]), conn


def test_each_eviction_says_how_it_can_be_reached(desk):
    app, _ = desk
    reach = {l["source_id"][-4]: l["reach"] for l in app.state()["leads"]}
    assert reach == {"1": "none", "2": "contact", "3": "contact", "4": "address", "5": "none", "6": "both"}


def test_counts_say_how_many_evictions_can_be_reached(desk):
    app, _ = desk
    c = app.state({"list": "leads"})["counts"]
    assert c["evictions_open"] == 6
    assert c["evictions_with_contact"] == 3
    assert c["evictions_with_address"] == 2
    assert c["evictions_reachable"] == 4  # 1 and 5 have nothing (5 only a guess)


def test_reachable_and_unreachable_filters(desk):
    app, _ = desk
    unreachable = app.state({"list": "leads", "type": "unreachable"})["list"]["leads"]
    assert sorted(l["source_id"][-4] for l in unreachable) == ["1", "5"]
    reachable = app.state({"list": "leads", "type": "reachable"})["list"]["leads"]
    assert len(reachable) == 4


def test_work_lists_put_leads_with_a_phone_first(desk):
    app, conn = desk
    conn.execute("UPDATE leads SET channel = 'phone'")
    # The best-scored lead has no phone: it still comes after the ones with a number.
    conn.execute("UPDATE leads SET case_stage = 'writ' WHERE source_id LIKE 'CV26-000001%'")
    conn.commit()
    q = app.state({"list": "queue", "channel": "phone", "status": "new", "sort": "contact"})["list"]["leads"]
    order = [l["source_id"][-4] for l in q]
    assert set(order[:2]) == {"2", "6"}  # phone numbers
    assert order[2] == "3"  # then email
    assert order[3] == "1"  # then the rest by priority (the writ first)
    # The plain priority order still puts the writ first.
    q = app.state({"list": "queue", "channel": "phone", "status": "new", "sort": "score"})["list"]["leads"]
    assert q[0]["source_id"][-4] == "1"


def test_records_request_sent_is_tracked_with_the_next_due_date(desk):
    app, _ = desk
    today = az_today()
    rec = app.state({"list": "leads"})["addresses"]["records"]
    assert rec["last_request"] is None and not rec["waiting"] and rec["due"]
    assert app.save_settings({"records_requested": True})["ok"]
    rec = app.state({"list": "leads"})["addresses"]["records"]
    assert rec["last_request"] == today.isoformat() and rec["waiting"] and not rec["due"]
    assert rec["due_on"] == (today + timedelta(days=14)).isoformat()
    assert rec["request_from"] == today.isoformat()
    # The file came: no longer waiting; the next one is two weeks from the import.
    csv = "Case Number,Property Address\nCV26-000001-EA,1 N FOUND AVE\n"
    assert app.import_file("csv_import", "records.csv", csv.encode())["addresses_filled"] == 1
    rec = app.state({"list": "leads"})["addresses"]["records"]
    assert not rec["waiting"] and rec["due_on"] == (today + timedelta(days=14)).isoformat()
    with pytest.raises(ValueError):
        app.save_settings({"records_requested": "yes"})


def test_setup_guide_can_be_hidden(desk):
    app, _ = desk
    assert not app.state({"list": "leads"})["settings"]["setup_guide_hidden"]
    app.save_settings({"setup_guide_hidden": True})
    assert app.state({"list": "leads"})["settings"]["setup_guide_hidden"] is True


def test_reach_of_a_lead_row():
    row = {"owner_phone": None, "owner_email": None, "address": None, "address_source": None}
    assert leadlist.reach(row) == "none"
    assert leadlist.reach({**row, "address": "1 W A ST", "address_source": "import"}) == "address"
    assert leadlist.reach({**row, "address": "1 W A ST", "address_source": "landlord"}) == "none"
    assert leadlist.reach({**row, "owner_email": "a@example.com"}) == "contact"
