from leadgen import db
from leadgen.contacts import clean_email, clean_phone, import_contacts, skiptrace_csv
from leadgen.lookup import (
    Contact,
    find_contacts,
    lookup_targets,
    names_match,
    pick_osm,
    scan_html,
    split_owner,
)
from leadgen.models import Lead
from leadgen.web import App


def make_db(path=":memory:"):
    conn = db.connect(path)
    leads = [
        # LLC owner of an apartment complex
        Lead("t", "1", "code_violation", "2026-09-30", "500 N SAGUARO AVE", lat=32.25, lon=-110.95,
             parcel="P1", in_pima=True),
        # person owns a house: never looked up
        Lead("t", "2", "code_violation", "2026-09-30", "10 E OWNER LN", lat=32.2, lon=-110.9,
             parcel="P2", in_pima=True),
        # eviction: landlord name, no address
        Lead("jp", "CV26-1", "eviction", "2026-09-30", None, plaintiff="DESERT SKY PROPERTY MGMT LLC",
             in_pima=True),
        # same landlord on a second case: one lookup covers both
        Lead("jp", "CV26-2", "eviction", "2026-09-29", None, plaintiff="DESERT SKY PROPERTY MGMT LLC",
             in_pima=True),
    ]
    for l in leads:
        db.upsert(conn, l)
    conn.execute("UPDATE leads SET owner_name='SAGUARO VISTA APARTMENTS LLC', owner_entity=1, "
                 "property_use='APARTMENTS 25+ UNITS', owner_address='PO BOX 1' WHERE parcel='P1'")
    conn.execute("UPDATE leads SET owner_name='SMITH JOHN', owner_entity=0, "
                 "property_use='SFR GRADE 010-3', owner_address='10 E OWNER LN' WHERE parcel='P2'")
    db.put_settings(conn, {"lead_view": "all"})
    conn.commit()
    return conn


class FakeProvider:
    name = "fake"

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def find(self, lead, business_name):
        self.calls.append(business_name or lead["address"])
        return self.answers.get(business_name or lead["address"])


class FakeScanner:
    def scan(self, url):
        return Contact(phone="(520) 555-0199", email="office@desertskymgmt.com",
                       website=url, source="website")


def test_clean_phone_and_email():
    assert clean_phone("+1 520.555.0100") == "(520) 555-0100"
    assert clean_phone("555-0100") is None
    assert clean_email(" Office@Example.com ") == "Office@Example.com"
    assert clean_email("not an email") is None


def test_names_match():
    assert names_match("Desert Sky Property Management", "DESERT SKY PROPERTY MGMT LLC")
    assert names_match("Saguaro Vista Apartments", "SAGUARO VISTA APARTMENTS LLC")
    assert not names_match("Saguaro Dental", "DESERT SKY PROPERTY MGMT LLC")


def test_lookup_targets_skip_people():
    conn = make_db()
    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM leads")}
    assert lookup_targets(rows["1"]) == (["SAGUARO VISTA APARTMENTS LLC"], True)
    assert lookup_targets(rows["2"]) == ([], False)
    assert lookup_targets(rows["CV26-1"]) == (["DESERT SKY PROPERTY MGMT LLC"], False)


def test_owner_names_attn_and_family_trusts():
    def lead(owner):
        return {"plaintiff": None, "owner_name": owner, "address": "1 MAIN ST",
                "property_use": "SFR"}
    assert split_owner("SUMMIT RIDGE AZ LLC ATTN: DASMEN RESIDENTIAL") == (
        "SUMMIT RIDGE AZ LLC", "DASMEN RESIDENTIAL")
    assert lookup_targets(lead("SUMMIT RIDGE AZ LLC ATTN: DASMEN RESIDENTIAL"))[0] == [
        "DASMEN RESIDENTIAL", "SUMMIT RIDGE AZ LLC"]
    assert lookup_targets(lead("MARTS FAMILY TR ATTN: DANIEL V & BRENDA L MARTS TR")) == ([], False)
    assert lookup_targets(lead("BTFD REVOC TR")) == ([], False)
    assert lookup_targets(lead("ZAZ PROPERTIES ACQ 1"))[0] == ["ZAZ PROPERTIES ACQ 1"]


def test_pick_osm_prefers_name_match_then_housing():
    els = [
        {"tags": {"name": "Joe's Tacos", "phone": "520-555-0001", "amenity": "restaurant"}},
        {"tags": {"name": "Saguaro Vista", "building": "apartments", "phone": "520-555-0002"}},
        {"tags": {"name": "Saguaro Vista Apartments", "phone": "520 555 0003",
                  "email": "leasing@saguarovista.com"}},
    ]
    c = pick_osm(els, "SAGUARO VISTA APARTMENTS LLC")
    assert (c.phone, c.email) == ("(520) 555-0003", "leasing@saguarovista.com")
    assert pick_osm(els[:2], None).phone == "(520) 555-0002"
    assert pick_osm(els[:1], None) is None


def test_scan_html():
    html = """<html><body><a href="tel:+15205550123">Call</a>
      <p>Office: (520) 555-0124 · leasing@desertskymgmt.com</p>
      <a href="/contact-us">Contact</a><a href="https://other.example.com/contact">x</a>
      <a href="mailto:hello@desertskymgmt.com?subject=hi">mail</a></body></html>"""
    phones, emails, pages = scan_html(html, "https://desertskymgmt.com/")
    assert phones == ["(520) 555-0123", "(520) 555-0124"]
    assert set(emails) == {"hello@desertskymgmt.com", "leasing@desertskymgmt.com"}
    assert pages == ["https://desertskymgmt.com/contact-us"]


def test_find_contacts_one_lookup_per_company_and_website_fill():
    conn = make_db()
    prov = FakeProvider({
        "SAGUARO VISTA APARTMENTS LLC": Contact(phone="(520) 555-0100", source="osm",
                                                matched_name="Saguaro Vista Apartments"),
        "DESERT SKY PROPERTY MGMT LLC": Contact(website="desertskymgmt.com", source="google"),
    })
    counts = find_contacts(conn, [prov], scanner=FakeScanner(), log=lambda *_: None)
    assert prov.calls.count("DESERT SKY PROPERTY MGMT LLC") == 1
    assert counts["checked"] == 2 and counts["found"] == 3 and counts["skipped_people"] == 1
    rows = {r["source_id"]: r for r in conn.execute("SELECT * FROM leads")}
    assert rows["1"]["owner_phone"] == "(520) 555-0100" and rows["1"]["contact_source"] == "osm"
    assert rows["CV26-2"]["owner_email"] == "office@desertskymgmt.com"
    assert rows["CV26-2"]["contact_source"] == "google+website"
    assert rows["2"]["owner_phone"] is None
    # Second run does nothing: everything was checked.
    assert find_contacts(conn, [prov], scanner=FakeScanner())["checked"] == 0


def test_manual_contact_never_overwritten(tmp_path):
    path = tmp_path / "l.db"
    make_db(path).close()
    app = App(path)
    lead = next(l for l in app.state()["leads"] if l["source_id"] == "1")
    app.update_lead({"id": lead["id"], "fields": {"owner_phone": "520 555 7777"}})
    conn = db.connect(path)
    prov = FakeProvider({"SAGUARO VISTA APARTMENTS LLC": Contact(phone="(520) 555-0100", source="osm")})
    find_contacts(conn, [prov], scanner=None, refresh=True)
    row = conn.execute("SELECT owner_phone, contact_source FROM leads WHERE id = ?",
                       (lead["id"],)).fetchone()
    assert tuple(row) == ("(520) 555-7777", "manual")


def test_bad_phone_rejected(tmp_path):
    path = tmp_path / "l.db"
    make_db(path).close()
    app = App(path)
    lead_id = app.state()["leads"][0]["id"]
    try:
        app.update_lead({"id": lead_id, "fields": {"owner_phone": "12345"}})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_import_and_skiptrace_export(tmp_path):
    conn = make_db()
    text = ("Owner Name,Property Address,Phone 1,Email 1\n"
            "SMITH JOHN,10 East Owner Lane,520-555-0142,john@smithmail.net\n"
            "NOBODY,1 NOWHERE ST,520-555-0000,\n")
    counts = import_contacts(conn, text)
    assert counts == {"rows": 2, "matched": 1, "updated": 1, "no_match": 1,
                      "skipped_manual": 0, "kept_existing": 0}
    row = conn.execute("SELECT owner_phone, owner_email, contact_source FROM leads "
                       "WHERE parcel = 'P2'").fetchone()
    assert tuple(row) == ("(520) 555-0142", "john@smithmail.net", "import")
    leads = [dict(r) for r in conn.execute("SELECT * FROM leads")]
    out = skiptrace_csv(leads)
    assert "SAGUARO VISTA APARTMENTS LLC" in out and "SMITH JOHN" not in out


def test_state_hides_google_key(tmp_path):
    path = tmp_path / "l.db"
    make_db(path).close()
    app = App(path)
    app.save_settings({"google_places_api_key": "secret-key"})
    st = app.state()["settings"]
    assert "google_places_api_key" not in st and st["google_key_set"] is True
    app.save_settings({"business_name": "X", "google_places_api_key": ""})
    assert app.state()["settings"]["google_key_set"] is True  # blank keeps it
    app.save_settings({"clear_google_key": True})
    assert app.state()["settings"]["google_key_set"] is False


def test_network_failure_is_retried_next_run():
    class Broken:
        name = "broken"

        def find(self, lead, name):
            raise ConnectionError("offline")

    conn = make_db()
    counts = find_contacts(conn, [Broken()], scanner=None, log=lambda *_: None)
    assert counts["errors"] == 2 and counts["not_found"] == 0
    assert conn.execute("SELECT COUNT(*) FROM leads WHERE contact_checked_at IS NOT NULL").fetchone()[0] == 0


def test_osm_stops_after_servers_keep_timing_out():
    import pytest
    import requests

    from leadgen.lookup import OsmProvider, ProviderUnavailable

    class DeadSession:
        headers = {}
        calls = 0

        def post(self, url, **kw):
            DeadSession.calls += 1
            raise requests.ReadTimeout("slow")

    osm = OsmProvider(session=DeadSession(), delay=0)
    lead = {"lat": 32.2, "lon": -110.9}
    servers = len(osm.urls)
    for _ in range(OsmProvider.max_failures):
        with pytest.raises(requests.ReadTimeout):
            osm.find(lead, "X LLC")
    assert DeadSession.calls == servers * OsmProvider.max_failures
    with pytest.raises(ProviderUnavailable):
        osm.find(lead, "X LLC")
    assert DeadSession.calls == servers * OsmProvider.max_failures  # no more waiting


def test_osm_prefers_the_server_that_answered():
    from leadgen.lookup import OsmProvider

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": []}

    class FlakySession:
        headers = {}

        def __init__(self):
            self.urls = []

        def post(self, url, **kw):
            import requests
            self.urls.append(url)
            if url == osm_urls[0]:
                raise requests.ConnectionError("down")
            return Resp()

    session = FlakySession()
    osm = OsmProvider(session=session, delay=0)
    osm_urls = list(osm.urls)
    osm.find({"lat": 1, "lon": 2}, None)
    osm.find({"lat": 1, "lon": 2}, None)
    assert session.urls == [osm_urls[0], osm_urls[1], osm_urls[1]]


class FakePlaces:
    def __init__(self):
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        return type("R", (), {"raise_for_status": lambda s: None,
                              "json": lambda s: {"places": []}})()


def test_google_stops_at_monthly_limit(tmp_path):
    from datetime import datetime, timezone

    import pytest

    from leadgen.lookup import GoogleBudget, GooglePlacesProvider, ProviderUnavailable

    conn = db.connect(str(tmp_path / "g.db"))
    session = FakePlaces()
    google = GooglePlacesProvider("key", session=session,
                                  budget=GoogleBudget(conn, limit=2, daily=0))
    lead = {"lat": None, "lon": None, "address": None, "property_use": None}
    google.find(lead, "SAGUARO VISTA LLC")
    google.find(lead, "SAGUARO VISTA LLC")
    with pytest.raises(ProviderUnavailable):
        google.find(lead, "SAGUARO VISTA LLC")
    assert session.calls == 2
    assert GoogleBudget(conn).used() == 2
    next_month = datetime(2099, 1, 1, tzinfo=timezone.utc)
    assert GoogleBudget(conn, limit=2).take(now=next_month)


def test_google_stops_at_daily_limit(tmp_path):
    from datetime import datetime, timedelta, timezone

    import pytest

    from leadgen.lookup import GoogleBudget, GooglePlacesProvider, ProviderUnavailable

    conn = db.connect(str(tmp_path / "g.db"))
    session = FakePlaces()
    google = GooglePlacesProvider("key", session=session, budget=GoogleBudget(conn))
    lead = {"lat": None, "lon": None, "address": None, "property_use": None}
    for _ in range(30):
        google.find(lead, "SAGUARO VISTA LLC")
    with pytest.raises(ProviderUnavailable, match="daily limit of 30"):
        google.find(lead, "SAGUARO VISTA LLC")
    assert session.calls == 30
    assert GoogleBudget(conn).used_today() == 30
    # The day turns over at midnight Arizona time (07:00 UTC), not UTC midnight.
    late_evening = datetime.now(timezone(timedelta(hours=-7))).replace(hour=23, minute=59)
    assert not GoogleBudget(conn).take(now=late_evening)
    next_day = late_evening + timedelta(minutes=2)
    assert GoogleBudget(conn).take(now=next_day)


def test_google_limit_from_settings(tmp_path):
    from leadgen.lookup import providers_from

    conn = db.connect(str(tmp_path / "g.db"))
    google = providers_from({"google_places_api_key": "k"}, conn=conn)[-1]
    assert google.name == "google" and google.budget.limit == 1000
    google = providers_from({"google_places_api_key": "k", "google_monthly_limit": 0}, conn=conn)[-1]
    assert google.budget.take() and google.budget.limit == 0
    a = App(str(tmp_path / "w.db"))
    a.save_settings({"google_monthly_limit": "250"})
    assert a.state()["settings"]["google_monthly_limit"] == 250
    assert a.state()["settings"]["google_used_this_month"] == 0
    assert a.state()["settings"]["google_daily_limit"] == 30
    a.save_settings({"google_daily_limit": "10"})
    assert a.state()["settings"]["google_daily_limit"] == 10
    assert a.state()["settings"]["google_used_today"] == 0
    with db.connect(str(tmp_path / "w.db")) as c:
        assert providers_from({"google_places_api_key": "k", "google_daily_limit": 10},
                              conn=c)[-1].budget.daily == 10


def test_google_only_for_newest_eviction_notices():
    conn = make_db()
    for case_id, filed, plaintiff in (("CV26-3", "2026-09-20", "OLD NOTICE HOMES LLC"),
                                      ("CV26-4", "2026-09-28", "NEW NOTICE HOMES LLC")):
        db.upsert(conn, Lead("jp", case_id, "eviction", filed, None, plaintiff=plaintiff,
                             in_pima=True, eviction_notice=True))
    conn.commit()

    class FakeGoogle(FakeProvider):
        name = "google"
        only_eviction_notices = True

    free = FakeProvider({})
    google = FakeGoogle({"NEW NOTICE HOMES LLC": Contact(phone="(520) 555-0101", source="google")})
    find_contacts(conn, [free, google], scanner=None)
    # Google sees only the eviction-notice cases, newest filing first.
    assert google.calls == ["NEW NOTICE HOMES LLC", "OLD NOTICE HOMES LLC"]
    # The free provider still tries everything, eviction notices first.
    assert free.calls[:2] == ["NEW NOTICE HOMES LLC", "OLD NOTICE HOMES LLC"]
    assert "DESERT SKY PROPERTY MGMT LLC" in free.calls


def test_lead_over_google_limit_is_retried_tomorrow():
    from leadgen.lookup import ProviderUnavailable

    conn = make_db()
    db.upsert(conn, Lead("jp", "CV26-5", "eviction", "2026-09-28", None,
                         plaintiff="NEW NOTICE HOMES LLC", in_pima=True, eviction_notice=True))
    conn.commit()

    class OverLimit:
        name = "google"
        only_eviction_notices = True

        def find(self, lead, name):
            raise ProviderUnavailable("daily limit of 30 Google lookups reached; more tomorrow")

    site_only = FakeProvider({"NEW NOTICE HOMES LLC": Contact(website="https://example.com",
                                                              source="osm")})
    find_contacts(conn, [site_only, OverLimit()], scanner=None, log=lambda *a: None)
    row = conn.execute("SELECT * FROM leads WHERE source_id = 'CV26-5'").fetchone()
    assert row["contact_checked_at"] is None  # no phone yet: looked up again next run


def test_import_never_overwrites_a_hand_entered_number(tmp_path):
    path = tmp_path / "l.db"
    conn = make_db(path)
    # A second case for the same landlord, with the number typed in by hand on the first.
    app = App(path)
    leads = [l for l in app.state()["leads"] if l["plaintiff"] == "DESERT SKY PROPERTY MGMT LLC"]
    first, second = leads[0]["id"], leads[1]["id"]
    app.update_lead({"id": first, "fields": {"owner_phone": "(520) 555-0100"}})
    text = "Owner Name,Phone\nDESERT SKY PROPERTY MGMT LLC,(602) 555-9999\n"
    counts = import_contacts(conn, text)
    assert counts["skipped_manual"] == 1
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM leads")}
    assert (rows[first]["owner_phone"], rows[first]["contact_source"]) == ("(520) 555-0100", "manual")
    # The other lead had no number, so the import fills it.
    assert rows[second]["owner_phone"] == "(602) 555-9999"
    # A later import with yet another number keeps the one already there.
    counts = import_contacts(conn, "Owner Name,Phone\nDESERT SKY PROPERTY MGMT LLC,(602) 555-1111\n")
    assert counts["kept_existing"] == 1 and counts["updated"] == 0
    assert conn.execute("SELECT owner_phone FROM leads WHERE id = ?", (second,)).fetchone()[0] == \
        "(602) 555-9999"


def test_skiptrace_round_trip_fills_every_lead_of_the_owner():
    conn = make_db()
    for i in (3, 4):
        db.upsert(conn, Lead("t", str(i), "code_violation", "2026-09-30", f"{i}0 N OTHER ST",
                             parcel=f"P{i}", in_pima=True))
    conn.execute("UPDATE leads SET owner_name = 'SAGUARO VISTA APARTMENTS LLC', owner_entity = 1, "
                 "owner_address = 'PO BOX 1' WHERE parcel IN ('P3', 'P4')")
    conn.commit()
    leads = [dict(r) for r in conn.execute("SELECT * FROM leads")]
    exported = skiptrace_csv(leads).splitlines()
    rows = [r for r in exported if "SAGUARO VISTA" in r]
    assert len(rows) == 1  # one row per owner and mailing address
    filled = exported[0] + ",phone\n" + rows[0] + ",520-555-0177\n"
    counts = import_contacts(conn, filled)
    assert counts["updated"] == 3
    phones = [r[0] for r in conn.execute(
        "SELECT owner_phone FROM leads WHERE owner_name = 'SAGUARO VISTA APARTMENTS LLC'")]
    assert phones == ["(520) 555-0177"] * 3


def test_contacts_csv_saved_by_excel(tmp_path):
    path = tmp_path / "l.db"
    make_db(path).close()
    app = App(path)
    data = "Owner Name,Email\nSMITH JOHN,josé@example.com\n".encode("cp1252")
    counts = app.import_file("contacts", "x.csv", data)
    assert counts["updated"] == 1
    conn = db.connect(path)
    assert conn.execute("SELECT owner_email FROM leads WHERE parcel = 'P2'").fetchone()[0] == \
        "josé@example.com"
