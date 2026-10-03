"""The Lead Desk page in a real (headless) browser: opening a lead from the
keyboard, keeping typed notes through other actions and refreshes, importing
a CSV, splitting leads, logging a contact once, the address bar keeping
the view, labelled dates and guessed addresses, and the page on a phone.
Skipped when Playwright or its browser isn't installed:

    pip install -e ".[dev]" && python -m playwright install chromium
"""

import threading
from http.server import ThreadingHTTPServer

import pytest

from leadgen import db
from leadgen.models import Lead
from leadgen.web import App, Handler

sync_api = pytest.importorskip("playwright.sync_api")


class NoGeocode:
    def geocode(self, *a, **kw):
        return None


class NoParcels:
    def by_parcels(self, parcels):
        return {}

    def by_site_address(self, address):
        return None

    def by_owner(self, name, limit=200):
        return []


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "leads.db"
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "tucson_code_cases",
            "CE-1",
            "code_violation",
            "2026-09-30",
            "10 E SAMPLE ST",
            lat=32.25,
            lon=-110.95,
            in_pima=True,
            description="Property Maintenance | Active | REFS / trash in yard",
        ),
    )
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-000001-EA",
            "eviction",
            "2026-09-29",
            None,
            plaintiff="EXAMPLE HOMES LLC",
            defendant="DOE, PAT",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.execute("UPDATE leads SET owner_name = 'ROE RIVER', owner_entity = 0 WHERE source_id = 'CE-1'")
    conn.execute("UPDATE leads SET owner_phone = '(520) 555-0101' WHERE source_id = 'CV26-000001-EA'")
    db.put_settings(conn, {"lead_view": "all", "base_lat": 32.36, "base_lon": -111.12})
    conn.commit()
    app = App(path, geocoder=NoGeocode(), parcel_client=NoParcels())
    handler = type("H", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/", app, path
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def page(server):
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as e:  # browser not downloaded
        pytest.skip(f"no browser for Playwright: {e}")
    page = browser.new_page()
    page.set_default_timeout(5000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    yield page
    browser.close()
    pw.stop()
    assert not errors, errors


def lead_row(page, text):
    return page.locator("#tab-leads tbody tr", has_text=text)


def test_open_a_lead_with_the_keyboard_and_close_it(server, page):
    url, app, _ = server
    page.goto(url)
    row = lead_row(page, "Example Homes")
    row.focus()
    page.keyboard.press("Enter")
    assert page.locator("#drawer.open").is_visible()
    assert page.evaluate("document.activeElement.id") == "dTitle"
    page.keyboard.press("Escape")
    assert not page.locator("#drawer.open").is_visible()
    assert page.evaluate("document.activeElement.dataset.id") == str(row.get_attribute("data-id"))


def test_notes_survive_a_status_change_and_a_refresh(server, page):
    url, app, path = server
    page.goto(url)
    lead_row(page, "10 E Sample St").click()
    page.fill("#dNotes", "Owner wants a quote Friday")
    page.fill("#dQuote", "350")
    page.click("[data-status=responded]")
    page.wait_for_selector("text=Marked responded and saved your notes")
    page.reload()
    lead_row(page, "10 E Sample St").click()
    assert page.input_value("#dNotes") == "Owner wants a quote Friday"
    assert page.input_value("#dQuote") == "350"
    row = db.connect(path).execute("SELECT status, notes, quote_amount FROM leads WHERE source_id = 'CE-1'").fetchone()
    assert tuple(row) == ("responded", "Owner wants a quote Friday", 350)

    # Unsaved typing survives another action (logging a contact) and the background refresh.
    page.select_option("#dChannel", "door_hanger")
    page.click("#dSaveCh")
    page.wait_for_selector("[data-touch=visited]")
    page.fill("#dNotes", "Typed but not saved")
    page.click("[data-touch=visited]")
    page.wait_for_selector("text=Logged: Hanger left")
    page.click("h1")  # click elsewhere, then the page redraws as the 3-second poll does
    page.evaluate("load()")
    assert page.input_value("#dNotes") == "Typed but not saved"
    assert page.locator("text=unsaved changes").is_visible()
    saved = db.connect(path).execute("SELECT notes FROM leads WHERE source_id = 'CE-1'").fetchone()[0]
    assert saved == "Owner wants a quote Friday"  # still only typed, not saved


def test_double_click_logs_one_contact_and_it_can_be_removed(server, page):
    url, app, path = server
    page.goto(url)
    lead_row(page, "10 E Sample St").click()
    page.select_option("#dChannel", "door_hanger")
    page.click("#dSaveCh")
    page.wait_for_selector("[data-touch=visited]")
    page.dblclick("[data-touch=visited]")
    page.wait_for_selector("[data-untouch]")
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 1
    page.click("[data-untouch]")
    page.wait_for_selector("text=Removed from the history")
    assert conn.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 0


def test_import_a_csv_and_see_the_leads(server, page, tmp_path):
    url, app, _ = server
    app.save_settings({"lead_view": "eviction_notice"})
    csv = tmp_path / "filings.csv"
    csv.write_text("Case Number,Address\nCV26-040001-EA,100 N Example Ave\n,77 W Sample Rd\n")
    page.goto(url)
    page.set_input_files("#importFile", str(csv))
    page.wait_for_selector("text=Imported 2 leads")
    assert lead_row(page, "100 N Example Ave").is_visible()
    assert lead_row(page, "77 W Sample Rd").is_visible()


def test_split_leads_asks_first(server, page):
    url, app, path = server
    page.goto(url)
    page.click("#nav [data-tab=outreach]")
    page.uncheck(".aCh[value=door_hanger]")
    page.once("dialog", lambda d: d.accept())
    page.click("#aGo")
    page.wait_for_selector("text=Assigned:")
    assert db.connect(path).execute("SELECT COUNT(*) FROM leads WHERE channel IS NOT NULL").fetchone()[0] >= 1


def test_calls_queue_shows_a_script_for_each_kind_of_lead(server, page):
    url, app, path = server
    conn = db.connect(path)
    conn.execute("UPDATE leads SET channel = 'phone'")
    conn.commit()
    page.goto(url + "#tab=outreach&method=phone")
    eviction = page.inner_text("[data-script=eviction]")
    code = page.inner_text("[data-script=code]")
    assert "eviction" in eviction and "[address]" not in eviction and "City has opened a case" in code


def test_reload_keeps_the_filter_and_the_open_lead(server, page):
    url, app, _ = server
    page.goto(url)
    page.select_option("#fType", "has_phone")
    lead_row(page, "Example Homes").click()
    page.reload()
    assert page.input_value("#fType") == "has_phone"
    assert page.locator("#drawer.open").is_visible()
    assert "Example Homes" in page.inner_text("#dTitle")
    page.go_back()  # back closes the lead
    page.wait_for_selector("#drawer.open", state="hidden")


def _luminance(rgb):
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(page, selector):
    fg, bg = page.evaluate(f"""() => {{ const s = getComputedStyle(document.querySelector({selector!r}));
        return [s.color, s.backgroundColor].map(c => c.match(/\\d+/g).slice(0, 3).map(Number)); }}""")
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_primary_buttons_are_readable(server, page, scheme):
    url, _, _ = server
    page.emulate_media(color_scheme=scheme)
    page.goto(url)
    assert _contrast(page, "#refreshBtn") >= 4.5


def test_theme_choice_is_remembered(server, page):
    url, _, _ = server
    page.goto(url + "#tab=settings")
    page.select_option("#sTheme", "dark")
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"


def test_dates_and_guessed_addresses_are_labelled(server, page):
    url, app, path = server
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-000002-EA",
            "eviction",
            "2099-10-14",  # not read yet: the date is an upcoming hearing
            None,
            plaintiff="SAMPLE RIVER LLC",
            in_pima=True,
            url="https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID=1000002",
        ),
    )
    conn.execute(
        "UPDATE leads SET address = '100 W EXAMPLE APTS', address_source = 'landlord', "
        "property_use = 'APARTMENTS 25+ UNITS' WHERE source_id = 'CV26-000001-EA'"
    )
    conn.commit()
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    cells = page.locator("#leadTable tbody td.datecell").all_inner_texts()
    assert len(cells) == 3
    for cell in cells:
        assert cell.split()[0] in ("Filed", "Opened", "Hearing", "Writ", "Judgment"), cell
    assert any(c.startswith("Hearing") and "2099" in c for c in cells)
    row = lead_row(page, "100 W Example Apts")
    assert "landlord's only complex — confirm" in row.inner_text()
    row.click()
    page.wait_for_selector("#dConfirm")
    assert page.locator("#dChannel option[value=door_hanger]").is_disabled()
    page.click("#dConfirm")
    page.wait_for_selector("text=Address confirmed")
    page.wait_for_selector("#drawer.open:not(:has(#dConfirm))")
    assert not page.locator("#dChannel option[value=door_hanger]").is_disabled()


def test_notes_counter_and_limit(server, page):
    url, app, path = server
    page.goto(url)
    lead_row(page, "10 E Sample St").click()
    page.fill("#dNotes", "x" * 2001)
    page.wait_for_selector("#dNotesCount.over")
    assert "2,001 / 2,000" in page.inner_text("#dNotesCount")
    page.click("#dSave")
    page.wait_for_selector("text=Notes can be up to 2,000 characters")


def test_works_on_a_phone(server, page):
    url, app, path = server
    conn = db.connect(path)
    for i in range(30):
        db.upsert(
            conn,
            Lead(
                "tucson_code_cases",
                f"CE-{100 + i}",
                "code_violation",
                "2026-09-20",
                f"{200 + i} N SAMPLE AVENUE WITH A LONG NAME",
                in_pima=True,
                description="Property Maintenance | Active | REFS / trash in yard",
            ),
        )
    conn.commit()
    page.set_viewport_size({"width": 375, "height": 740})
    for tab in ("leads", "outreach", "results", "settings"):
        page.goto(f"{url}#tab={tab}")
        page.wait_for_selector(f"#tab-{tab}:not([hidden]) > *")
        assert page.evaluate("document.documentElement.scrollWidth") == 375, tab
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    page.mouse.wheel(0, 1500)
    page.wait_for_function("window.scrollY > 500")
    assert page.evaluate("Math.max(0, document.querySelector('header').getBoundingClientRect().bottom)") <= 110

    # Each lead is a card: property, landlord, stage and phone readable without sideways scrolling.
    card = lead_row(page, "Example Homes")
    card.scroll_into_view_if_needed()
    box = card.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 375
    text = card.inner_text()
    assert "Example Homes" in text and "notice filed" in text and "(520) 555-0101" in text
    phone = card.locator("a[href^='tel:']")
    assert phone.get_attribute("href") == "tel:5205550101" and phone.bounding_box()["height"] >= 44

    # Open it, set the door-hanger method and log a contact: every target is big enough to tap.
    lead_row(page, "10 E Sample St").click()
    page.wait_for_selector("#drawer.open")
    page.select_option("#dChannel", "door_hanger")
    page.click("#dSaveCh")
    page.wait_for_selector("[data-touch=visited]")
    small = page.evaluate(
        """[...document.querySelectorAll('#drawer button, #drawer select, #drawer input:not([type=checkbox])')]
        .filter(e => e.offsetParent && e.getBoundingClientRect().height < 44).map(e => e.id || e.textContent)"""
    )
    assert small == []
    page.click("[data-touch=visited]")
    page.wait_for_selector("text=Logged: Hanger left")
    assert db.connect(path).execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 1
