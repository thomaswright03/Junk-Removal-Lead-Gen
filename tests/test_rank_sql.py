"""The lead list is filtered, ranked and paged in the database from a rank
kept in columns (leadlist.refresh_ranking). These tests check the stored
rank against the Python rules (outreach.score_parts, sort_leads, reach) on
a mixed set of made-up leads, and that it follows every change. Names and
addresses are made up."""

import itertools
import json
import time
from datetime import date, timedelta

import pytest
from conftest import PG_URL

from leadgen import db, leadlist, outreach
from leadgen.models import Lead
from leadgen.web import App, handle

TODAY = date(2026, 10, 3)


def iso(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


def mixed_leads(conn):
    """About 60 leads covering every rule that moves a lead's priority or
    decides a filter: stages, hearings not read yet, future papers, code
    case codes, absentee and company owners, repeat owners, recency,
    addresses that need a unit or are guesses, phones and emails."""
    owners = ["SAMPLE HOLDINGS LLC", "DOE JANE", None, "EXAMPLE APARTMENTS LP", ""]
    uses = [None, "APARTMENTS 5+ UNITS", "SINGLE FAMILY RESIDENCE", "CONDO COMMON AREA"]
    n = 0
    for stage, days, owner, use in itertools.product(
        ("writ", "judgment", "notice", None, "dismissed"), (0, 5, 9, 20, -3), owners[:3], uses[:2]
    ):
        n += 1
        if n % 3:
            continue
        lead = Lead(
            source="pima_jp_calendar" if n % 2 else "pima_jp_case",
            source_id=f"CV26-{n:06d}-EA",
            lead_type="eviction",
            event_date=iso(days),
            plaintiff=owner or "SAMPLE PROPERTIES LLC",
            defendant="DOE, JOHN",
            url=f"https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={1000 + n}",
        )
        db.upsert(conn, lead)
        conn.execute(
            "UPDATE leads SET case_stage = ?, judgment_date = ?, writ_date = ?, owner_name = ?, property_use = ?, "
            "eviction_notice = ?, case_checked_at = ?, owner_entity = ?, address = ?, unit = ?, address_source = ?, "
            "owner_phone = ? WHERE source_id = ?",
            (
                stage,
                iso(days - 1) if stage in ("judgment", "writ") else None,
                iso(days - 2) if stage == "writ" else None,
                owner,
                use,
                1 if stage else None,
                None if n % 4 == 1 else "2026-10-01T00:00:00",
                n % 2,
                "1 W SAMPLE ST" if n % 5 == 0 else None,
                "4" if n % 7 == 0 else None,
                ["landlord", "confirmed", "import", None][n % 4],
                "(520) 555-0100" if n % 11 == 0 else None,
                lead.source_id,
            ),
        )
    for i, (desc, days, owner, absentee) in enumerate(
        itertools.product(
            (
                "Property Maintenance | Active | REFS / trash",
                "Property Maintenance | Active | WEEDS / weeds",
                "Vacant | Active | VACANT/NUISANCE building",
                "Zoning | Active | nothing known",
                "Property Maintenance | Active | DUMP: alley",
            ),
            (1, 12, 40),
            owners,
            (0, 1),
        )
    ):
        if i % 2:
            continue
        db.upsert(
            conn,
            Lead(
                source="tucson_code_cases",
                source_id=f"CE-{i}",
                lead_type="code_violation",
                event_date=iso(days) if i % 9 else "10/01/2026",
                address=f"{100 + i} E EXAMPLE RD",
                description=desc,
            ),
        )
        conn.execute(
            "UPDATE leads SET owner_name = ?, owner_absentee = ?, owner_email = ?, property_use = ? "
            "WHERE source_id = ?",
            (owner, absentee, "a@example.com" if i % 6 == 0 else None, uses[i % 4], f"CE-{i}"),
        )
    conn.commit()


def python_order(conn, settings, key="score"):
    """The list as the Python rules order it (the reference)."""
    leads = [l for l in leadlist.lead_dicts(conn, settings, today=TODAY) if l["status"] not in leadlist.CLOSED]
    return [l["id"] for l in leadlist.sort_leads(leads, key)], {l["id"]: l for l in leads}


def sql_page(conn, settings, **params):
    params.setdefault("limit", 500)
    return leadlist.page(conn, settings, params, today=TODAY)


def check_matches(conn, settings):
    expected, by_id = python_order(conn, settings)
    got = sql_page(conn, settings)
    assert [l["id"] for l in got["leads"]] == expected
    stored = {r["id"]: r["rank_score"] for r in conn.execute("SELECT id, rank_score FROM leads").fetchall()}
    for lead in got["leads"]:
        assert stored[lead["id"]] == lead["score"] == by_id[lead["id"]]["score"], lead["id"]
    expected_dates, _ = python_order(conn, settings, "date")
    assert [l["id"] for l in sql_page(conn, settings, sort="date")["leads"]] == expected_dates
    # Work lists: leads with a phone first, then an email, each by priority.
    expected_contact, _ = python_order(conn, settings, "contact")
    assert [l["id"] for l in sql_page(conn, settings, sort="contact")["leads"]] == expected_contact
    return by_id


def test_stored_rank_matches_the_python_rules_in_every_view(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    mixed_leads(conn)
    for view in leadlist.LEAD_VIEWS:
        settings = outreach.merged_settings({"lead_view": view})
        check_matches(conn, settings)


def test_filters_match_the_python_rules(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    mixed_leads(conn)
    settings = outreach.merged_settings({"lead_view": "all"})
    _, by_id = python_order(conn, settings)
    rules = {
        "eviction": lambda l: l["lead_type"] == "eviction",
        "code_violation": lambda l: l["lead_type"] == "code_violation",
        "absentee": lambda l: bool(l["owner_absentee"]),
        "entity": lambda l: bool(l["owner_entity"]),
        "has_phone": lambda l: bool(l["owner_phone"]),
        "no_phone": lambda l: not l["owner_phone"],
        "no_address": lambda l: not l["address"],
        "guessed_address": lambda l: l["address_source"] == "landlord",
        "address_work": lambda l: l["lead_type"] == "eviction" and l["door_hanger_problem"] is not None,
        "reachable": lambda l: l["reach"] != "none",
        "unreachable": lambda l: l["reach"] == "none",
    }
    for kind, rule in rules.items():
        got = {l["id"] for l in sql_page(conn, settings, type=kind)["leads"]}
        assert got == {i for i, l in by_id.items() if rule(l)}, kind
    # Search: any of the searched columns, any case; % and _ are plain characters.
    assert {l["id"] for l in sql_page(conn, settings, q="sample holdings")["leads"]} == {
        i for i, l in by_id.items() if "SAMPLE HOLDINGS" in (l["owner_name"] or "") + (l["plaintiff"] or "")
    }
    assert sql_page(conn, settings, q="100%")["total"] == 0
    assert sql_page(conn, settings, q="_")["total"] == 0


def test_the_rank_follows_every_change(tmp_path):
    conn = db.connect(tmp_path / "l.db")
    mixed_leads(conn)
    settings = outreach.merged_settings({"lead_view": "all"})
    check_matches(conn, settings)
    # A writ arrives, an owner is found (a repeat owner now), a code case's
    # description changes, a lead leaves the view: each by plain SQL, as the
    # daily check and the assessor lookup write them.
    first = conn.execute("SELECT id FROM leads WHERE lead_type = 'eviction' ORDER BY id LIMIT 1").fetchone()["id"]
    conn.execute(
        "UPDATE leads SET case_stage = 'writ', writ_date = ?, judgment_date = ? WHERE id = ?", (iso(0), iso(1), first)
    )
    conn.execute(
        "UPDATE leads SET owner_name = 'DOE JANE' WHERE id = (SELECT MAX(id) FROM leads WHERE owner_name IS NULL)"
    )
    conn.execute(
        "UPDATE leads SET description = 'Property Maintenance | Active | VACANT/NUISANCE' "
        "WHERE id = (SELECT MIN(id) FROM leads WHERE lead_type = 'code_violation')"
    )
    conn.execute("UPDATE leads SET in_pima = 0 WHERE id = (SELECT MAX(id) FROM leads WHERE owner_name = 'DOE JANE')")
    conn.commit()
    by_id = check_matches(conn, settings)
    assert by_id[first]["score_parts"][1] == ("writ issued", 25)
    # A new lead.
    db.upsert(conn, Lead(source="csv_import", source_id="X-1", lead_type="eviction", event_date=iso(2)))
    conn.commit()
    check_matches(conn, settings)
    # Days pass: recency points fall away, and a paper dated ahead counts once its day comes.
    later = TODAY + timedelta(days=10)
    leads = [l for l in leadlist.lead_dicts(conn, settings, today=later) if l["status"] not in leadlist.CLOSED]
    expected = [l["id"] for l in leadlist.sort_leads(leads)]
    got = leadlist.page(conn, settings, {"limit": 500}, today=later)
    assert [l["id"] for l in got["leads"]] == expected


def test_an_older_database_gets_its_rank_on_first_open(tmp_path):
    """A database from before the rank columns: they are added, and the
    first list request works every rank out."""
    path = tmp_path / "old.db"
    conn = db.connect(path)
    mixed_leads(conn)
    conn.execute("UPDATE leads SET derived_src = NULL, rank_score = NULL, stage_rank = NULL, rank_latest = NULL")
    conn.commit()
    check_matches(conn, outreach.merged_settings({"lead_view": "evictions"}))


def test_counts_are_remembered_until_a_lead_changes(tmp_path):
    path = tmp_path / "l.db"
    conn = db.connect(path)
    mixed_leads(conn)
    db.put_settings(conn, {"lead_view": "all"})
    conn.commit()
    app = App(path)

    def state():
        return handle(app, "GET", "/api/state", "list=leads&status=open", {"Host": "x"}, b"")[1]

    before = state()
    assert state()["counts"] == before["counts"]
    conn.execute("UPDATE leads SET owner_phone = '(520) 555-0199' WHERE owner_phone IS NULL AND lead_type = 'eviction'")
    conn.commit()
    after = state()
    assert after["counts"]["with_phone"] > before["counts"]["with_phone"]
    assert after["counts"]["evictions_with_contact"] == after["counts"]["evictions_open"]
    # A logged contact is a change too.
    lead = after["list"]["leads"][0]["id"]
    conn.execute(
        "INSERT INTO touches (lead_id, channel, kind, cost_cents, created_at) VALUES (?, 'phone', 'called', 0, ?)",
        (lead, "2026-10-03T12:00:00"),
    )
    conn.execute("UPDATE leads SET channel = 'phone' WHERE id = ?", (lead,))
    conn.commit()
    assert sum(r["touched"] for r in state()["results"]) == 1


def many(conn, n, offset=0):
    conn.execute(
        "WITH RECURSIVE k(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM k WHERE i < ?) "
        "INSERT INTO leads (source, source_id, lead_type, event_date, address, address_norm, in_pima, "
        "description, owner_name, owner_absentee, status, first_seen, last_seen) "
        "SELECT 'tucson_code_cases', 'CE-' || (i + ?), 'code_violation', '2026-09-01', "
        "i || ' W SAMPLE ST', i || ' W SAMPLE ST', 1, 'Property Maintenance | Active | REFS / trash', "
        "'OWNER ' || (i % 50), i % 2, 'new', '2026-09-01T00:00:00', '2026-09-01T00:00:00' FROM k",
        (n, offset),
    )
    conn.commit()


def test_a_page_costs_about_the_same_however_many_leads(tmp_path):
    """With 10,000 leads and then 20,000, one page of the list (after the
    first request has worked the rank out) takes about as long: the
    database walks its index to the page instead of ranking every lead."""
    path = tmp_path / "l.db"
    conn = db.connect(path)
    db.put_settings(conn, {"lead_view": "all"})
    conn.commit()
    app = App(path)

    def timed():
        handle(app, "GET", "/api/state", "list=leads&status=open&sort=score", {"Host": "x"}, b"")
        took = []
        for _ in range(5):
            started = time.perf_counter()
            status, body, _ = handle(app, "GET", "/api/state", "list=leads&status=open&sort=score", {"Host": "x"}, b"")
            took.append(time.perf_counter() - started)
            assert status == 200 and len(body["list"]["leads"]) == 100
        return min(took), len(json.dumps(body))

    many(conn, 10000)
    small, size = timed()
    many(conn, 10000, offset=10000)
    big, size2 = timed()
    print(f"one page: {small * 1000:.0f} ms at 10,000 leads, {big * 1000:.0f} ms at 20,000")
    assert size2 < size * 1.2
    if not PG_URL:  # timings on a shared CI Postgres are too noisy to compare
        assert big < small * 1.8 + 0.02, (small, big)


class _Err(Exception):
    def __init__(self, sqlstate, text="error"):
        super().__init__(text)
        self.sqlstate = sqlstate


class _Raises:
    def __init__(self, err):
        self.err = err

    def execute(self, *a):
        raise self.err


def test_two_instances_starting_at_once_both_start():
    # The loser of a race to make a trigger finds it made; anything else is raised.
    for state, text in (("42710", "exists"), ("23505", "dup"), ("XX000", "tuple concurrently updated")):
        db._pg_create(_Raises(_Err(state, text)), "CREATE TRIGGER x")
    for err in (_Err("42601", "syntax"), _Err("XX000", "something else"), ValueError("x")):
        with pytest.raises(type(err)):
            db._pg_create(_Raises(err), "CREATE TRIGGER x")


def test_a_busy_database_keeps_the_stored_rank():
    import sqlite3

    assert leadlist._busy(sqlite3.OperationalError("database is locked"))
    assert leadlist._busy(_Err("40P01")) and not leadlist._busy(_Err("42601"))
    assert not leadlist._busy(ValueError("locked"))
