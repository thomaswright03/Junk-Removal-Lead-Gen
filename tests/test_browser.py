"""The Lead Desk page in a real (headless) browser: opening a lead from the
keyboard, keeping typed notes through other actions and refreshes, importing
a CSV, splitting leads, logging a contact once, the address bar keeping
the view, labelled dates and guessed addresses, and the page on a phone.
Skipped when Playwright or its browser isn't installed (in CI, where CI=1,
a missing browser fails instead):

    pip install -e ".[dev]" && python -m playwright install chromium
"""

import os
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


def _launch():
    """Start Playwright and Chromium. If the browser can't start, Playwright
    is stopped again (otherwise every later test fails with a misleading
    "Sync API inside the asyncio loop" error) and the real reason is given:
    a skip on a computer without the browser, a failure in CI, where a
    missing browser must not pass quietly."""
    pw = sync_api.sync_playwright().start()
    try:
        return pw, pw.chromium.launch()
    except Exception as e:
        pw.stop()
        reason = f"no browser for Playwright: {str(e).strip().splitlines()[0]}"
        if os.environ.get("CI"):
            pytest.fail(reason + " (CI must run `python -m playwright install chromium`)")
        pytest.skip(reason)


@pytest.fixture
def page(server):
    pw, browser = _launch()
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
    # A dialog for assistive tech: Tab stays inside it, the page behind doesn't scroll.
    assert page.get_attribute("#drawer", "role") == "dialog" and page.get_attribute("#drawer", "aria-modal") == "true"
    assert page.evaluate("getComputedStyle(document.body).overflow") == "hidden"
    for _ in range(60):
        page.keyboard.press("Tab")
        assert page.evaluate("document.getElementById('drawer').contains(document.activeElement)")
    page.keyboard.press("Escape")
    page.wait_for_selector("#drawer.open", state="hidden")
    page.wait_for_function(f"document.activeElement.dataset.id === '{row.get_attribute('data-id')}'")
    assert page.evaluate("getComputedStyle(document.body).overflow") != "hidden"


def test_leaving_unsaved_typing_asks_in_the_page(server, page):
    url, app, _ = server
    page.goto(url)
    lead_row(page, "10 E Sample St").click()
    page.fill("#dNotes", "call back Tuesday")
    page.keyboard.press("Escape")
    page.wait_for_selector("#confirmBox[open]")
    assert page.inner_text("#confirmOk") == "Leave without saving"
    page.keyboard.press("Escape")  # cancels the question, keeps the lead open
    page.wait_for_selector("#confirmBox:not([open])", state="attached")
    assert page.locator("#drawer.open").is_visible()
    page.click("#dClose")
    page.click("#confirmOk")
    page.wait_for_selector("#drawer.open", state="hidden")


def test_details_priority_and_header_are_readable(server, page):
    url, app, path = server
    conn = db.connect(path)
    conn.execute(
        "UPDATE leads SET description = 'Case open | Eviction notice filed | Eviction Action Oct 5, 2026 9:00 AM', "
        "case_status = 'Open', next_court_date = '2026-10-05 09:00' WHERE source_id = 'CV26-000001-EA'"
    )
    db.put_settings(
        conn,
        {
            "last_daily_run": "2026-10-03",
            "last_daily_summary": {"finished_at": "2026-10-03T13:05:00+00:00", "owners": {"found": 1, "errors": 2}},
        },
    )
    conn.commit()
    page.goto(url)
    # One short status line with a warning sign; the full summary under Details.
    page.wait_for_selector("#sub .warn-sign")
    assert "open leads" in page.inner_text("#sub") and "Last check" in page.inner_text("#sub")
    assert page.is_hidden("#subDetails")
    page.click("#subMore")
    page.wait_for_selector("#subDetails >> text=2 owner lookups failed")
    # Hovering the priority shows what it is made of.
    chip = lead_row(page, "Example Homes").locator(".score")
    assert chip.get_attribute("title").startswith("Priority ") and "Eviction 35" in chip.get_attribute("title")
    # Details: readable lines, nothing the drawer already shows.
    lead_row(page, "Example Homes").click()
    page.wait_for_selector("#drawer.open")
    assert "Eviction 35" in page.inner_text("#dWhy")
    text = page.inner_text("#drawer dl")
    assert " | " not in text and "Eviction Action" not in text
    # An eviction with no address says how to find it.
    assert "Find the address" in page.inner_text("#drawer") and "search “Doe, Pat”" in page.inner_text("#drawer")


def test_empty_search_says_what_was_searched(server, page):
    url, app, path = server
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    page.fill("#q", "zzzzqqq")
    page.wait_for_selector("#leadTable >> text=No leads match “zzzzqqq”")
    page.click("#fClear")
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    assert page.input_value("#q") == ""


def test_code_case_coverage_and_counts_are_shown(server, page):
    url, app, path = server
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    assert "1 of them code cases" in page.inner_text("#fView")
    # The explanations are one click away ("How this works"), not above the list.
    assert page.locator("#leadHelp").count() == 0
    page.click("#helpToggle")
    assert "City of Tucson only" in page.inner_text("#leadHelp")
    assert "0 of 1 open eviction lead has an address a door hanger can go to" in page.inner_text("#addrShare")


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
    row = db.connect(path).execute("SELECT status, notes, quote_cents FROM leads WHERE source_id = 'CE-1'").fetchone()
    assert tuple(row) == ("responded", "Owner wants a quote Friday", 35000)

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
    before = tuple(conn.execute("SELECT id, created_at, cost_cents, kind FROM touches").fetchone())
    page.click("[data-untouch]")
    page.wait_for_selector("text=Removed from the history")
    assert conn.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 0
    # A mis-tap: Undo puts the same entry back, with its time and cost.
    page.click("#toastUndo")
    page.wait_for_selector("text=Put back in the history")
    page.wait_for_selector("[data-untouch]")
    assert tuple(conn.execute("SELECT id, created_at, cost_cents, kind FROM touches").fetchone()) == before


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


def test_split_leads_says_what_it_can_hand_out_and_asks_first(server, page):
    url, app, path = server
    with db.connect(path) as conn:  # the code case owner's number was found too
        conn.execute("UPDATE leads SET owner_phone = '(520) 555-0102' WHERE source_id = 'CE-1'")
    page.goto(url)
    page.click("#nav [data-tab=outreach]")
    # The first round offered is evictions only, with the methods they can all be worked by.
    page.wait_for_selector("#aSplit >> text=1 eviction")
    assert page.input_value("#aKind") == "eviction"
    assert page.is_checked(".aCh[value=phone]") and page.is_checked(".aCh[value=property_manager]")
    assert not page.is_checked(".aCh[value=door_hanger]")
    # Ticking a method no lead fits says so before anything is pressed, and offers the fix.
    page.check(".aCh[value=door_hanger]")
    page.wait_for_selector("#aSplit >> text=No unassigned evictions")
    assert page.is_disabled("#aGo")
    page.click("#aDrop")
    page.wait_for_selector("#aSplit >> text=1 eviction")
    # Code cases are a round of their own.
    page.select_option("#aKind", "code_violation")
    page.wait_for_selector("#aSplit >> text=1 code case")
    assert page.is_checked(".aCh[value=door_hanger]") and page.is_checked(".aCh[value=phone]")

    # Both kinds, with door hangers: the eviction (no address) is left out, and the
    # summary of the round stays, naming it, until dismissed.
    page.select_option("#aKind", "")
    page.wait_for_selector("#aSplit >> text=(1 eviction, 0 code cases)")
    page.uncheck(".aCh[value=property_manager]")
    page.check(".aCh[value=door_hanger]")
    page.wait_for_selector("#aSplit >> text=(0 evictions, 1 code case)")
    page.click("#aGo")
    page.wait_for_selector("#confirmBox[open]")
    assert "This round: 0 evictions and 1 City code case" in page.inner_text("#confirmBody")
    assert page.inner_text("#confirmOk") == "Assign 1 lead"
    # Escape cancels: nothing is assigned.
    page.keyboard.press("Escape")
    page.wait_for_selector("#confirmBox:not([open])", state="attached")
    assert db.connect(path).execute("SELECT COUNT(*) FROM leads WHERE channel IS NOT NULL").fetchone()[0] == 0
    page.click("#aGo")
    page.wait_for_selector("#confirmBox[open]")
    page.click("#confirmOk")
    summary = page.locator("#roundSummary")
    summary.wait_for()
    assert "0 evictions and 1 City code case assigned" in summary.inner_text()
    assert "1 left out: 1 with no property address" in summary.inner_text()
    rows = db.connect(path).execute("SELECT lead_type FROM leads WHERE channel IS NOT NULL").fetchall()
    assert [r[0] for r in rows] == ["code_violation"]  # what the question said
    # It outlasts the toast and a redraw, and links the lead it left out.
    page.evaluate("load()")
    summary.wait_for()
    page.click("#roundSummary [data-lead]")
    page.wait_for_selector("#drawer.open >> text=Example Homes Llc")
    page.keyboard.press("Escape")
    page.wait_for_selector("#drawer.open", state="hidden")
    page.click("#roundDismiss")
    page.wait_for_selector("#roundSummary", state="detached")

    # An eviction-only round in one step: the default.
    page.wait_for_selector("#aSplit >> text=1 eviction")
    page.click("#aGo")
    page.wait_for_selector("#confirmBox[open]")
    assert "This round: 1 eviction and 0 City code cases" in page.inner_text("#confirmBody")
    page.click("#confirmOk")
    page.wait_for_selector("#roundSummary >> text=1 eviction and 0 City code cases assigned")
    assert db.connect(path).execute("SELECT COUNT(*) FROM leads WHERE channel IS NOT NULL").fetchone()[0] == 2


def test_a_round_leaves_out_leads_nobody_can_call_unless_steve_includes_them(server, page):
    url, app, path = server
    with db.connect(path) as conn:  # day one: no phone or email for anyone
        conn.execute("UPDATE leads SET owner_phone = NULL")
    page.goto(url)
    page.click("#nav [data-tab=outreach]")
    # The methods that could call the landlord are ticked, and the line says why nothing can go yet.
    page.wait_for_selector("#aSplit >> text=1 more eviction has no phone or email yet")
    assert page.is_checked(".aCh[value=phone]") and page.is_checked(".aCh[value=property_manager]")
    assert page.is_disabled("#aGo")
    page.check("#aAll")
    page.wait_for_selector("#aSplit >> text=1 of them have no phone or email yet")
    page.wait_for_selector("#aGo:not([disabled])")
    page.click("#aGo")
    page.wait_for_selector("#confirmBox[open]")
    page.wait_for_selector("#confirmBody >> text=1 of them have no phone or email yet")
    page.click("#confirmOk")
    page.wait_for_selector("#roundSummary >> text=1 eviction and 0 City code cases assigned")
    assert db.connect(path).execute("SELECT COUNT(*) FROM leads WHERE channel IS NOT NULL").fetchone()[0] == 1


def test_calls_queue_shows_a_script_for_each_kind_of_lead(server, page):
    url, app, path = server
    conn = db.connect(path)
    conn.execute("UPDATE leads SET channel = 'phone'")
    conn.commit()
    page.goto(url + "#tab=outreach&method=phone")
    eviction = page.inner_text("[data-script=eviction]")
    code = page.inner_text("[data-script=code]")
    assert "eviction" in eviction and "[address]" not in eviction and "City has opened a case" in code
    # The landlord is a company (EXAMPLE HOMES LLC): no made-up first name.
    assert "Hi there, this is Steve" in eviction and "Homes," not in eviction


def test_methods_on_an_eviction_say_who_they_reach_and_what_they_offer(server, page):
    url, app, path = server
    page.goto(url)
    lead_row(page, "Example Homes").click()
    page.wait_for_selector("#drawer.open")
    page.select_option("#dChannel", "phone")
    page.click("#dSaveCh")
    page.wait_for_selector("[data-pitch=phone]")
    phone = page.inner_text("[data-pitch=phone]")
    page.select_option("#dChannel", "property_manager")
    page.click("#dSaveCh")
    page.wait_for_selector("[data-pitch=property_manager]")
    pitch = page.inner_text("[data-pitch=property_manager]")
    assert "landlord" in phone and "one-time clean-out of this unit" in phone
    assert "standing clean-out rate" in pitch and phone != pitch
    # Results say what they compare: eviction leads only, by default here.
    page.keyboard.press("Escape")
    page.click("#nav [data-tab=results]")
    page.wait_for_selector("#rBasis >> text=eviction leads only")
    assert page.input_value("#rKind") == "eviction"
    page.select_option("#rKind", "code_violation")
    page.wait_for_selector("#rBasis >> text=City code cases only")


def test_address_work_queue_confirms_a_guess_in_one_click(server, page):
    url, app, path = server
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-000002-EA",
            "eviction",
            "2026-09-28",
            None,
            plaintiff="MESA RENTALS LLC",
            defendant="ROE, SAM",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.execute(
        "UPDATE leads SET address = '5 W GUESS ST', address_source = 'landlord' WHERE source_id = 'CV26-000002-EA'"
    )
    conn.commit()
    page.goto(url)
    page.click("#helpToggle")
    page.click("#fNeedAddr")
    page.wait_for_selector("#recordsCard")
    # What to ask the court for, with the dates, and where: the first request
    # reaches back a month, for the judgments and writs the calendar never shows.
    assert "special detainer" in page.inner_text("#recAsk")
    assert "judgment or writ of restitution" in page.inner_text("#recAsk")
    assert "First request: the last month" in page.inner_text("#backfillNote")
    assert page.get_attribute("#recordsCard a", "href").startswith("https://www.jp.pima.gov/OnlineRecordsRequest")
    rows = page.locator("#addrTable tbody tr.click")
    assert rows.count() == 2
    guess = page.locator("#addrTable tr.click", has_text="CV26-000002-EA")
    assert "tenant Roe, Sam" in guess.inner_text() and "Mesa Rentals LLC" in guess.inner_text()
    # The landlord's properties open under the row.
    page.locator("#addrTable tr.click", has_text="CV26-000001-EA").locator("[data-aprops]").click()
    page.wait_for_selector("tr.aprops:not([hidden]) >> text=No properties found")
    guess.locator("[data-aconfirm]").click()
    page.wait_for_selector("text=Address confirmed")
    page.wait_for_function("document.querySelectorAll('#addrTable tbody tr.click').length === 1")
    row = db.connect(path).execute("SELECT address_source FROM leads WHERE source_id = 'CV26-000002-EA'").fetchone()
    assert row[0] == "confirmed"


def test_message_fields_insert_from_buttons_with_a_live_preview(server, page):
    url, app, path = server
    page.goto(url + "#tab=settings")
    page.wait_for_selector("#pv-phone_eviction")
    # The preview is filled in for a real lead: the eviction's landlord is a company.
    page.wait_for_function("document.getElementById('pv-phone_eviction').textContent.startsWith('Hi there,')")
    box = page.locator("#tpl-door_hanger")
    box.fill("Hello ")
    box.evaluate("b => b.setSelectionRange(6, 6)")
    page.click("[data-for=tpl-door_hanger][data-field='{business}']")
    assert box.input_value() == "Hello {business}"
    page.wait_for_function("document.getElementById('pv-door_hanger').textContent === \"Hello Steve's Junk Removal\"")
    # The code-case call previews with the code case's address.
    assert "10 E Sample St" in page.inner_text("#pv-phone")


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
    # The first lead is on screen without scrolling.
    first = page.locator("#leadTable tbody tr[data-id]").first.bounding_box()
    assert first["y"] + 40 < 740, first
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


def test_header_is_one_row_on_a_wide_screen(server, page):
    url, app, path = server
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    title, button = page.locator("header h1").bounding_box(), page.locator("#refreshBtn").bounding_box()
    # Same row, and the main action at the right edge.
    assert abs((title["y"] + title["height"] / 2) - (button["y"] + button["height"] / 2)) < 12
    assert button["x"] + button["width"] > 1440 - 40


def test_phone_text_and_targets_are_big_enough(server, page):
    url, app, path = server
    page.set_viewport_size({"width": 375, "height": 740})
    for tab in ("leads", "outreach", "results", "settings"):
        page.goto(f"{url}#tab={tab}")
        page.wait_for_selector(f"#tab-{tab}:not([hidden]) > *")
        small_text = page.evaluate(
            """[...document.querySelectorAll('body *')].filter(e => e.offsetParent
              && [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())
              && parseFloat(getComputedStyle(e).fontSize) < 13)
              .map(e => e.tagName + ' ' + e.textContent.trim().slice(0, 30))"""
        )
        assert small_text == [], (tab, small_text)
        small_targets = page.evaluate(
            """[...document.querySelectorAll('button, .btn, select, summary, nav button')].filter(e => e.offsetParent
              && e.getBoundingClientRect().height < 44).map(e => e.id || e.textContent.trim().slice(0, 30))"""
        )
        assert small_targets == [], (tab, small_targets)


def add_unreachable_eviction(path):
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-000002-EA",
            "eviction",
            "2026-09-28",
            None,
            plaintiff="SAMPLE PROPERTIES LLC",
            defendant="ROE, SAM",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.commit()


def test_first_run_guide_explains_how_to_reach_eviction_leads(server, page):
    url, app, path = server
    add_unreachable_eviction(path)
    app.providers = []  # "find phones" looks nothing up here
    page.goto(url)
    # One status line above the list says how many can be reached and how many
    # steps are left; the guide opens from it (shut, so the list starts on screen).
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    assert "1 of 2 open eviction leads can be reached now" in page.inner_text("#reachLine")
    assert "2 steps to do" in page.inner_text("#reachLine")
    assert page.locator("#setupGuide").count() == 0
    page.click("#setupShow")
    guide = page.locator("#setupGuide")
    guide.wait_for()
    assert "2 steps to do" in guide.inner_text()
    assert "1,000 of these lookups a month free" in guide.inner_text() and "$0 a month" in guide.inner_text()
    # The key, click by click, with links straight to each Google page.
    assert "places.googleapis.com" in page.get_attribute("#keySteps a >> nth=2", "href")
    assert "Restrict key" in page.inner_text("#keySteps")
    # What each step would reach, and the one to start with (it reaches more).
    assert "up to 2 of 2 open eviction leads" in page.inner_text("#yieldRecords")
    assert "up to 1 of 2 open eviction leads" in page.inner_text("#yieldGoogle")
    assert "start here" in page.inner_text("#setupRecords") and "start here" not in page.inner_text("#setupGoogle")
    # The lead with nothing says so in the list.
    assert "can't reach yet" in lead_row(page, "Sample Properties").inner_text()
    assert "can't reach yet" not in lead_row(page, "Example Homes").inner_text()

    # A blank key is refused at the box, before anything is sent.
    page.click("#setupKeySave")
    page.wait_for_selector("#setupKey-err:not([hidden])")
    assert page.get_attribute("#setupKey", "aria-invalid") == "true"
    assert page.evaluate("document.activeElement.id") == "setupKey"
    page.fill("#setupKey", "test-key-123")
    page.wait_for_selector("#setupKey-err", state="hidden")
    page.click("#setupKeySave")
    page.wait_for_selector("#setupGuide >> text=key saved")
    assert db.get_settings(db.connect(path))["google_places_api_key"] == "test-key-123"
    page.wait_for_selector("#setupGuide >> text=1 step to do")

    # The records request: sent today, so the guide is done and says when the next is due.
    page.click("#setupGuide [data-recsent]")
    page.wait_for_selector("#setupGuide >> text=waiting for the file")
    assert db.get_settings(db.connect(path))["records_requested"]["date"]
    page.click("#setupHide")
    page.wait_for_selector("#setupGuide", state="detached")
    # The reach numbers stay above the list, with the way back to the guide.
    assert "1 of 2 open eviction leads can be reached now" in page.inner_text("#reachLine")
    assert "to do" not in page.inner_text("#reachLine")
    page.click("#fUnreach")
    page.wait_for_function("document.querySelectorAll('#leadTable tbody tr[data-id]').length === 1")
    assert "Sample Properties" in page.inner_text("#leadTable")


def test_call_list_puts_numbers_first_and_says_why_others_have_none(server, page):
    url, app, path = server
    add_unreachable_eviction(path)
    conn = db.connect(path)
    conn.execute("UPDATE leads SET channel = 'phone' WHERE lead_type = 'eviction'")
    # The lead without a number has the higher priority; the one with a number still comes first.
    conn.execute("UPDATE leads SET case_stage = 'writ' WHERE source_id = 'CV26-000002-EA'")
    conn.commit()
    page.goto(url + "#tab=outreach&method=phone")
    note = page.locator("#noPhoneNote")
    note.wait_for()
    assert "No phone number yet for 1 of 2 calls" in note.inner_text()
    assert "without a Google Places key" in note.inner_text()
    rows = page.locator("#oBody tbody tr")
    assert "(520) 555-0101" in rows.nth(0).inner_text()
    assert "Search" in rows.nth(1).inner_text()
    # "Set up phone lookups" opens the guide on the Leads tab.
    page.click("#noPhoneNote [data-setup]")
    page.wait_for_selector("#setupGuide")


def test_form_mistakes_show_under_the_field_and_nothing_is_sent(server, page):
    url, app, path = server
    sent = []
    page.on("request", lambda r: sent.append(r.post_data) if r.method == "POST" else None)
    page.goto(url)
    lead_row(page, "10 E Sample St").click()
    page.wait_for_selector("#drawer.open")

    page.fill("#dQuote", "-50")
    page.click("#dSave")
    page.wait_for_selector("#dQuote-err:not([hidden])")
    assert page.inner_text("#dQuote-err") == "Quote can't be negative."
    assert page.get_attribute("#dQuote", "aria-invalid") == "true"
    assert "dQuote-err" in page.get_attribute("#dQuote", "aria-describedby")
    assert page.evaluate("document.activeElement.id") == "dQuote"
    page.fill("#dRev", "250000")
    page.click("#dSave")
    page.wait_for_selector("#dRev-err >> text=Job revenue can be at most $100,000")
    assert sent == []
    # It stays through a redraw, and goes once the value is corrected.
    page.evaluate("load()")
    page.wait_for_selector("#dQuote-err:not([hidden])")
    page.fill("#dQuote", "50")
    page.wait_for_selector("#dQuote-err", state="hidden")
    assert page.get_attribute("#dQuote", "aria-invalid") is None
    page.fill("#dRev", "")

    page.fill("#dPhone", "12")
    page.click("#dSaveContact")
    page.wait_for_selector("#dPhone-err >> text=The phone number needs 10 digits")
    assert page.evaluate("document.activeElement.id") == "dPhone"
    page.fill("#dPhone", "")
    page.fill("#dEmail", "not-an-email")
    page.click("#dSaveContact")
    page.wait_for_selector("#dEmail-err >> text=That email address doesn't look right")
    page.fill("#dAddress", "")
    page.fill("#dUnit", "4")
    page.click("#dSaveAddress")
    page.wait_for_selector("#dAddress-err >> text=Type the street address as well as the unit")
    assert sent == []

    # The server's own refusal lands under the field too.
    lead_id = lead_row(page, "10 E Sample St").get_attribute("data-id")
    send = f"api('/api/lead', {{ id: {lead_id}, fields: {{ owner_phone: '55' }} }})"
    page.evaluate(f"act(() => {send}, 'x', null, undefined, drawerError)")
    page.wait_for_selector("#dPhone-err >> text=The phone number needs 10 digits")


def test_settings_mistakes_show_under_the_field(server, page):
    url, app, path = server
    sent = []
    page.on("request", lambda r: sent.append(r.url) if r.method == "POST" else None)
    page.goto(url + "#tab=settings")
    page.wait_for_selector("#sSave")
    page.fill("#sName", "")
    page.fill("#cost-door_hanger", "-1")
    page.click("#sSave")
    page.wait_for_selector("#sName-err >> text=Type your business name")
    page.wait_for_selector("#cost-door_hanger-err >> text=can't be negative")
    assert page.evaluate("document.activeElement.id") == "sName"
    assert sent == []
    page.fill("#sName", "Desert Haul")
    page.wait_for_selector("#sName-err", state="hidden")
    page.fill("#cost-door_hanger", "0.5")
    page.click("#sSave")
    page.wait_for_selector("text=Settings saved")
    assert db.get_settings(db.connect(path))["business_name"] == "Desert Haul"


def test_header_says_when_a_failed_check_is_retried(server, page):
    from leadgen.util import az_today

    url, app, path = server
    conn = db.connect(path)
    today = az_today().isoformat()
    db.put_settings(
        conn,
        {
            "daily_retry": {"date": today, "attempt": 1, "at": f"{today}T23:58", "failed": ["evictions"]},
            "last_daily_summary": {"evictions": {"error": "ConnectionError"}, "finished_at": f"{today}T13:05:00"},
        },
    )
    conn.commit()
    page.goto(url)
    page.wait_for_selector("#sub >> text=Today's check failed (Justice Court calendar)")
    assert "trying again today at 11:58 PM" in page.inner_text("#sub")


def test_a_mistyped_address_shows_a_page_with_a_way_back(server, page):
    url, app, path = server
    resp = page.goto(url + "nope")
    assert resp.status == 404
    assert page.inner_text("h2") == "Page not found"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != "rgba(0, 0, 0, 0)"
    page.click("text=Back to the leads")
    page.wait_for_selector("#leadTable tbody tr[data-id]")


def test_leads_tab_controls_have_distinct_labels_and_the_hint_fits_the_view(server, page):
    url, app, path = server
    page.goto(url)  # the fixture shows All leads
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    labels = page.evaluate(
        """[...document.querySelectorAll('#tab-leads select')]
        .map(s => [...s.options].map(o => o.text.split(/[:(]/)[0].trim()))"""
    )
    seen = [t for opts in labels for t in opts]
    assert len(seen) == len(set(seen)), seen
    assert page.inner_text("#fType option[value='']") == "Any kind of lead"
    # Show is already All leads: no instruction to pick it.
    page.click("#helpToggle")
    assert "Pick “All leads”" not in page.inner_text("#coverage")
    assert "City of Tucson only" in page.inner_text("#coverage")
    # What is and isn't collected, one click away.
    page.click("#coverageMore summary")
    page.wait_for_selector("#coverageMore >> text=unincorporated Pima County")
    assert "foreclosures" in page.inner_text("#coverageMore")
    page.select_option("#fView", "evictions")
    page.wait_for_selector("#coverage >> text=Pick “All leads” under Show to see them")


def test_revenue_on_a_lost_lead_asks_before_marking_it_won(server, page):
    url, app, path = server
    conn = db.connect(path)
    conn.execute("UPDATE leads SET status = 'lost' WHERE source_id = 'CE-1'")
    conn.commit()
    status = lambda: db.connect(path).execute("SELECT status, revenue_cents FROM leads WHERE source_id = 'CE-1'")
    page.goto(url + "#status=")
    lead_row(page, "10 E Sample St").click()
    page.wait_for_selector("#drawer.open")
    page.fill("#dRev", "400")
    page.click("#dSave")
    page.wait_for_selector("#confirmBox[open]")
    assert page.inner_text("#confirmTitle") == "Mark this lost lead as won?"
    assert page.inner_text("#confirmCancel") == "Keep it lost"
    page.click("#confirmCancel")
    page.wait_for_selector("text=Saved. The lead stays Lost.")
    assert tuple(status().fetchone()) == ("lost", 40000)
    # Asked again and confirmed: Won.
    page.fill("#dRev", "450")
    page.click("#dSave")
    page.wait_for_selector("#confirmBox[open]")
    page.click("#confirmOk")
    page.wait_for_selector("text=Saved and marked won")
    assert tuple(status().fetchone()) == ("won", 45000)


def test_a_quote_on_a_skipped_lead_asks_before_marking_it_quoted(server, page):
    url, app, path = server
    with db.connect(path) as conn:
        conn.execute("UPDATE leads SET status = 'skip' WHERE source_id = 'CE-1'")
    status = lambda: tuple(
        db.connect(path).execute("SELECT status, quote_cents FROM leads WHERE source_id = 'CE-1'").fetchone()
    )
    page.goto(url + "#status=")
    lead_row(page, "10 E Sample St").click()
    page.wait_for_selector("#drawer.open")
    page.fill("#dQuote", "300")
    page.click("#dSave")
    page.wait_for_selector("#confirmBox[open]")
    assert page.inner_text("#confirmTitle") == "Mark this skip lead as quoted?"
    assert page.inner_text("#confirmCancel") == "Keep it skip"
    page.click("#confirmCancel")
    page.wait_for_selector("text=Saved. The lead stays Skip.")
    assert status() == ("skip", 30000)
    page.fill("#dQuote", "350")
    page.click("#dSave")
    page.wait_for_selector("#confirmBox[open]")
    page.click("#confirmOk")
    page.wait_for_selector("text=Saved and marked quoted")
    assert status() == ("quoted", 35000)


def test_a_blank_business_phone_is_flagged_before_hangers_go_out(server, page):
    url, app, path = server
    with db.connect(path) as conn:
        conn.execute("UPDATE leads SET channel = 'door_hanger' WHERE source_id = 'CE-1'")
    page.goto(url + "#tab=outreach&method=door_hanger")
    warning = page.locator("#oBody .phone-missing")
    warning.wait_for()
    assert "phone number isn't set" in warning.inner_text()
    # Printing asks first.
    page.click("#rPrint")
    page.wait_for_selector("#confirmBox[open]")
    assert "[phone]" in page.inner_text("#confirmBody")
    page.click("#confirmCancel")
    page.wait_for_selector("#confirmBox:not([open])", state="attached")
    # The link goes to the phone box in Settings; once it's set the warning is gone.
    page.click("#oBody [data-goto-settings]")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'sPhone'")
    app.save_settings({"business_phone": "(520) 555-0100"})
    page.goto(url + "#tab=outreach&method=door_hanger")
    page.wait_for_selector("#oBody h2 >> text=Door hangers")
    assert page.locator(".phone-missing").count() == 0


def test_purpose_line_is_whole_on_a_phone_and_a_wide_screen(server, page):
    url, _, _ = server
    for width in (375, 1440):
        page.set_viewport_size({"width": width, "height": 800})
        page.goto(url)
        page.wait_for_selector("#purpose >> text=eviction and clean-out job leads")
        assert page.is_visible("#purpose")
        cut = page.evaluate(
            "(() => { const p = document.getElementById('purpose');"
            " return p.scrollWidth > p.clientWidth + 1 || getComputedStyle(p).textOverflow === 'ellipsis'; })()"
        )
        assert not cut, width


def test_dark_theme_is_the_same_from_the_computer_and_from_settings(server, page):
    url, _, _ = server
    colors = "['--bg', '--panel', '--accent'].map(v => getComputedStyle(document.documentElement).getPropertyValue(v))"
    page.emulate_media(color_scheme="dark")
    page.goto(url)
    page.wait_for_selector("#leadTable")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    from_computer = page.evaluate(colors)
    # Picked in Settings, with the computer set to light: the same palette.
    page.emulate_media(color_scheme="light")
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    page.goto(url + "#tab=settings")
    page.select_option("#sTheme", "dark")
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    assert page.evaluate(colors) == from_computer
    # Back to "Same as this computer": light again.
    page.select_option("#sTheme", "system")
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")


def test_find_phone_on_the_row_saves_a_number_without_leaving_the_list(server, page):
    """No Google key: each lead with no number has Find phone on its row,
    with searches for the landlord and a box to paste the number into."""
    url, app, path = server
    add_unreachable_eviction(path)
    conn = db.connect(path)
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-000003-EA",
            "eviction",
            "2026-09-27",
            None,
            plaintiff="SAMPLE PROPERTIES LLC",
            defendant="ROE, JO",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    db.upsert(
        conn,
        Lead(
            "pima_jp_calendar",
            "CV26-000004-EA",
            "eviction",
            "2026-09-26",
            None,
            plaintiff="MHC DIAMOND II LLC",
            defendant="ROE, AL",
            in_pima=True,
            eviction_notice=True,
        ),
    )
    conn.commit()
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    assert "1 of 4 open eviction leads can be reached now" in page.inner_text("#reachLine")
    # Company names keep their acronyms.
    assert "MHC Diamond II LLC" in page.inner_text("#leadTable")
    first = page.locator("#leadTable [data-findphone]").first
    first.click()
    panel = page.locator("tr.findrow:not([hidden])")
    panel.wait_for()
    assert page.evaluate("document.activeElement.id").startswith("fp-")
    links = [panel.locator("a").nth(i).get_attribute("href") for i in range(3)]
    assert (
        links[0].startswith("https://www.google.com/search?q=") and "maps" in links[1] and "ecorp.azcc.gov" in links[2]
    )
    # A number that isn't one is refused at the box.
    page.keyboard.type("555-01")
    page.keyboard.press("Enter")
    page.wait_for_selector(".findphone .field-error:not([hidden])")
    lid = int(page.evaluate("document.activeElement.id").split("-")[1])
    page.fill(f"#fp-{lid}", "(520) 555-0177")
    page.click(f"[data-savephone='{lid}']")
    page.wait_for_selector("text=Number saved")
    rows = {r[0]: r[1] for r in db.connect(path).execute("SELECT source_id, owner_phone FROM leads").fetchall()}
    saved = [k for k, v in rows.items() if v == "(520) 555-0177"]
    assert saved and all(k.startswith("CV26") for k in saved)
    # The same landlord's other lead got it too; the reach count went up; on to the next.
    if "CV26-000002-EA" in saved:
        assert "CV26-000003-EA" in saved
    page.wait_for_function("document.activeElement && document.activeElement.dataset.findphone")
    assert f"{1 + len(saved)} of 4 open eviction leads can be reached now" in page.inner_text("#reachLine")


@pytest.mark.parametrize("size", [(1440, 900), (390, 844)])
def test_the_first_lead_is_on_screen_without_scrolling(server, page, size):
    url, app, path = server
    add_unreachable_eviction(path)
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.goto(url)
    first = page.locator("#leadTable tbody tr[data-id]").first
    first.wait_for()
    assert page.evaluate("window.scrollY") == 0
    box = first.bounding_box()
    assert box["y"] + min(box["height"], 60) <= size[1], box


def test_outreach_and_results_say_what_unlocks_them(server, page):
    url, app, path = server
    conn = db.connect(path)
    conn.execute("UPDATE leads SET owner_phone = NULL, address = NULL")
    conn.commit()
    page.goto(url + "#tab=outreach")
    locked = page.locator("#outreachLocked")
    locked.wait_for()
    assert page.locator("#aGo").count() == 0  # no form that can't do anything
    assert "opens once a lead can be contacted" in locked.inner_text()
    page.click("#nav [data-tab=results]")
    page.wait_for_selector("#resultsLocked")
    assert page.locator("#tab-results table").count() == 0  # no tables of zeros
    page.click("#resultsLocked [data-unlock]")
    page.wait_for_selector("#leadTable [data-findphone]")
    assert page.input_value("#fType") == "unreachable"


def test_message_previews_use_a_lead_of_the_right_kind(server, page):
    url, app, path = server
    conn = db.connect(path)
    conn.execute("DELETE FROM leads WHERE lead_type = 'code_violation'")
    db.put_settings(conn, {"lead_view": "eviction_notice"})
    conn.commit()
    page.goto(url + "#tab=settings")
    page.wait_for_selector("#pvl-phone")
    assert "No code case to preview yet" in page.inner_text("#pvl-phone")
    assert "Example Homes LLC" in page.inner_text("#pvl-phone_eviction")


def test_theme_colour_and_column_names_stay_in_view(server, page):
    url, app, path = server
    page.set_viewport_size({"width": 1440, "height": 900})
    page.emulate_media(color_scheme="dark")
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    colours = page.evaluate("[...document.querySelectorAll('meta[name=theme-color]')].map(m => m.content)")
    assert "#1b1b1e" in colours
    assert page.evaluate("getComputedStyle(document.querySelector('#leadTable thead th')).position") == "sticky"
    top = page.evaluate("getComputedStyle(document.querySelector('#leadTable thead th')).top")
    header = page.evaluate("document.querySelector('header').offsetHeight")
    assert top == f"{header}px"


@pytest.mark.parametrize("width", [721, 768, 1024, 1280, 1440])
def test_the_page_never_scrolls_sideways_on_a_tablet_or_laptop(server, page, width):
    """From 721 px (the card layout ends) up, the page is never wider than
    the window: a table too wide for it scrolls in its own box, with its
    column names in view, and the Phone column can be scrolled to."""
    url, app, path = server
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    page.wait_for_function("document.documentElement.scrollWidth === window.innerWidth")
    phone = page.locator("#leadTable thead th", has_text="Phone")
    phone.scroll_into_view_if_needed()
    box = phone.bounding_box()
    assert 0 <= box["x"] and box["x"] + box["width"] <= width, box
    assert page.evaluate("document.documentElement.scrollWidth") == width
    assert page.evaluate("getComputedStyle(document.querySelector('#leadTable thead th')).position") == "sticky"
    # Back to another tab and back: still measured right.
    page.click("#nav [data-tab=settings]")
    page.click("#nav [data-tab=leads]")
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    assert page.evaluate("document.documentElement.scrollWidth") == width


@pytest.mark.parametrize("width", [1280, 1440])
def test_box_hints_are_whole_on_a_computer(server, page, width):
    """The case-link and search boxes' hints fit their boxes."""
    url, app, path = server
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(url)
    page.wait_for_selector("#leadTable tbody tr[data-id]")
    cut = page.evaluate(
        """['#cLinks', '#q'].filter(sel => {
          const el = document.querySelector(sel), cs = getComputedStyle(el);
          const ctx = document.createElement('canvas').getContext('2d');
          ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
          const room = el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
          return ctx.measureText(el.placeholder).width > room - (el.type === 'search' ? 20 : 0);
        })"""
    )
    assert cut == []
