from leadgen import db
from leadgen.contacts import clean_email, clean_phone, import_contacts, skiptrace_csv
from leadgen.lookup import (Contact, find_contacts, lookup_targets, names_match, pick_osm,
                            scan_html, split_owner)
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
    assert counts == {"rows": 2, "matched": 1, "updated": 1, "no_match": 1}
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
    for _ in range(OsmProvider.max_failures):
        with pytest.raises(requests.ReadTimeout):
            osm.find(lead, "X LLC")
    tried = DeadSession.calls
    with pytest.raises(ProviderUnavailable):
        osm.find(lead, "X LLC")
    assert DeadSession.calls == tried  # no more waiting on dead servers


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
