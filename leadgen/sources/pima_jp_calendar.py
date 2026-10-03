"""Pima County Consolidated Justice Court eviction calendar.

Every residential eviction in Pima County is filed in the Consolidated
Justice Court, and each one gets an "Eviction Action" hearing on the court's
public calendar: https://www.jp.pima.gov/NewCalendar2018/

``CalendarClient`` runs that search live (Case Type "Eviction Actions", Event
Type "Eviction Action", a date range) and reads every results page. Each row
links to the case page, which ``pima_jp_case`` reads for the eviction notice.
Saved results pages still work: ``leadgen fetch --source pima_jp_calendar
--file data/inbox/*.html``.

The parser does not depend on exact column positions: it looks for rows that
carry a justice-court case number and pulls the parties, the hearing date
and, when the page has one, an address column. Rows that don't mention an
eviction are skipped unless ``assume_eviction`` is set (useful when the page
was already filtered to eviction hearings).

The calendar usually lists parties, not the property address. Those leads are
stored with the landlord (plaintiff) name and no address; the landlord is the
person who pays for the clean-out, so they are still worth a call. See
docs/DATA_SOURCES.md for how to fill in addresses.
"""

import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..config import USER_AGENT

from ..models import Lead
from .base import Source

CALENDAR_URL = "https://www.jp.pima.gov/NewCalendar2018/"
CASE_PAGE_BASE = "https://www.jp.pima.gov/CaseSearch/"

# Calendar rows and case pages describe the same cases, keyed by case number,
# so both are stored under one source name and update the same row.
JP_SOURCE = "pima_jp_calendar"

CASE_RE = re.compile(r"\b([A-Z]{2}\d{2}-\d{4,7}(?:-[A-Z]{1,3})?)\b")
DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
ROLE_RE = re.compile(r"^(.*?)\s*\((PLAINTIFF|PETITIONER|DEFENDANT|RESPONDENT)[^)]*\)\s*$", re.IGNORECASE)
VS_RE = re.compile(r"^(.*?)\s+(?:vs\.?|v\.)\s+(.*)$", re.IGNORECASE)
EVICTION_RE = re.compile(r"EVICT|DETAINER|\bFED\b|SPECIAL DETAINER", re.IGNORECASE)

_HEADER_KEYS = {
    "plaintiff": ("PLAINTIFF", "PETITIONER", "LANDLORD"),
    "defendant": ("DEFENDANT", "RESPONDENT", "TENANT"),
    "parties": ("PARTIES", "PARTY", "CASE NAME", "CASE TITLE", "STYLE"),
    "address": ("ADDRESS", "PROPERTY", "PREMISES"),
    "date": ("DATE",),
    "event": ("EVENT", "HEARING", "TYPE"),
}


def _iso(m):
    month, day, year = (int(x) for x in m.groups())
    return datetime(year, month, day).date().isoformat()


def _header_map(cells):
    mapping = {}
    for i, text in enumerate(cells):
        up = text.upper()
        for key, needles in _HEADER_KEYS.items():
            if key not in mapping and any(n in up for n in needles):
                mapping[key] = i
                break
    return mapping


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_calendar_html(html, assume_eviction=False):
    """Return Leads for the eviction rows in a saved calendar results page."""
    soup = BeautifulSoup(html, "html.parser")
    leads = {}
    for table in soup.find_all("table"):
        header = {}
        for tr in table.find_all("tr"):
            if tr.find_parent("table") is not table:
                continue
            link = tr.find("a", href=re.compile(r"jcDisplayCase", re.IGNORECASE))
            tds = tr.find_all(["th", "td"])
            cells = [_clean(c.get_text(" ")) for c in tds]
            if not cells:
                continue
            row_text = " ".join(cells)
            case_m = CASE_RE.search(row_text)
            if not case_m:
                hm = _header_map(cells)
                if len(hm) >= 2:
                    header = hm
                continue
            if not assume_eviction and not EVICTION_RE.search(row_text):
                continue

            def col(key):
                i = header.get(key)
                return cells[i] if i is not None and i < len(cells) else None

            plaintiff, defendant = col("plaintiff"), col("defendant")
            if not (plaintiff and defendant) and header.get("parties") is not None \
                    and header["parties"] < len(tds):
                # Live calendar: one party per line, "NAME (Plaintiff)".
                roles = {"P": [], "D": []}
                for line in tds[header["parties"]].stripped_strings:
                    m = ROLE_RE.match(_clean(line))
                    if m:
                        roles["P" if m.group(2).upper() in ("PLAINTIFF", "PETITIONER") else "D"].append(
                            _clean(m.group(1)))
                plaintiff = plaintiff or "; ".join(roles["P"]) or None
                defendant = defendant or "; ".join(roles["D"]) or None
            if not (plaintiff and defendant):
                for text in [col("parties")] + cells:
                    m = VS_RE.match(text or "")
                    if m:
                        plaintiff = plaintiff or _clean(m.group(1))
                        defendant = defendant or _clean(m.group(2))
                        break

            date_text = col("date") or row_text
            date_m = DATE_RE.search(date_text) or DATE_RE.search(row_text)
            case = case_m.group(1)
            if not case.startswith("CV"):
                continue
            leads[case] = Lead(
                source=JP_SOURCE,
                source_id=case,
                lead_type="eviction",
                event_date=_iso(date_m) if date_m else None,
                address=col("address") or None,
                in_pima=True,
                plaintiff=plaintiff,
                defendant=defendant,
                description=_clean(col("event") or "Eviction Action hearing"),
                url=urljoin(CASE_PAGE_BASE, link["href"]) if link else CALENDAR_URL,
                raw={"cells": cells},
            )
    return list(leads.values())


RESULT_URL = CALENDAR_URL + "SearchResult.aspx"
GRID = "ctl00$MainContent$searchResultDView"


def _form_fields(html):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError("calendar page has no form; the court site may have changed")
    return {i["name"]: i.get("value") or "" for i in form.find_all("input")
            if i.get("name") and i.get("type") not in ("submit", "button", "image")}


class CalendarClient:
    """Runs the calendar search the way the court's page does: Case Type
    "Eviction Actions", Event Type "Eviction Action", a date range, then every
    results page (50 rows each)."""

    def __init__(self, session=None, delay=1.5, max_pages=60):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self.max_pages = max_pages

    def _post(self, data):
        time.sleep(self.delay)
        resp = self.session.post(RESULT_URL, data=data, timeout=90)
        resp.raise_for_status()
        return resp.text

    def search(self, start, end):
        """Yield the HTML of each results page for hearings from ``start`` to ``end`` (dates)."""
        page = self.session.get(CALENDAR_URL, timeout=30)
        page.raise_for_status()
        data = _form_fields(page.text)
        data.update({
            "startDate": start.strftime("%m-%d-%Y"), "endDate": end.strftime("%m-%d-%Y"),
            "Party": "All", "Attorney": "All", "ARSCode": "All", "drpDnJudge": "All",
            "drpDnCaseType": "Eviction Actions",
            "ctl00$MainContent$drpDnEventType": "Eviction Action",
            "ctl00$MainContent$submitFilter": "submit",
        })
        html = self._post(data)
        yield html
        n = 2
        while n <= self.max_pages and f"Page${n}'" in html:
            data = _form_fields(html)
            data.update({"__EVENTTARGET": GRID, "__EVENTARGUMENT": f"Page${n}"})
            html = self._post(data)
            yield html
            n += 1


class PimaJpCalendar(Source):
    name = "pima_jp_calendar"
    description = "Pima County Justice Court eviction hearings (live calendar search, or saved pages with --file)"

    def fetch(self, since, until, paths=None, assume_eviction=False, client=None, **options):
        """Saved pages when ``paths`` is given; otherwise the live calendar
        from ``since`` to ``until`` (hearing dates, ISO strings)."""
        if not paths:
            client = client or CalendarClient()
            start = datetime.strptime(since, "%Y-%m-%d").date()
            end = datetime.strptime(until, "%Y-%m-%d").date()
            seen = set()
            for html in client.search(start, end):
                for lead in parse_calendar_html(html, assume_eviction=True):
                    if lead.source_id not in seen:
                        seen.add(lead.source_id)
                        yield lead
            return
        for p in paths:
            html = Path(p).read_text(encoding="utf-8", errors="replace")
            for lead in parse_calendar_html(html, assume_eviction=assume_eviction):
                # Hearing dates run ahead of filing dates, so upcoming hearings
                # past ``until`` are kept; only old ones are dropped.
                if lead.event_date and lead.event_date < since:
                    continue
                yield lead
