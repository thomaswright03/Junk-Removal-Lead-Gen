"""Pima County Consolidated Justice Court eviction calendar.

Every residential eviction in Pima County is filed in the Consolidated
Justice Court, and each one gets an "Eviction Action" hearing on the court's
public calendar: https://www.jp.pima.gov/NewCalendar2018/

How to get the page for this importer:

1. Open the calendar, choose Case Type ``CV`` and Event Type
   ``Eviction Action`` and a date range (the last 7 days works well).
2. Click *Load Calendar*, then save the results page (Ctrl+S, "Webpage, HTML
   only") into ``data/inbox/``.
3. Run ``leadgen fetch --source pima_jp_calendar --file data/inbox/*.html``.

The parser does not depend on exact column positions: it looks for rows that
carry a justice-court case number and pulls the parties, the hearing date
and, when the page has one, an address column. Rows that don't mention an
eviction are skipped unless ``assume_eviction`` is set (useful when the page
was already filtered to eviction hearings).

The calendar usually lists parties, not the property address. Those leads are
stored with the landlord (plaintiff) name and no address; the landlord is the
person who pays for the clean-out, so they are still worth a call. See
docs/DATA_SOURCES.md for how to fill in addresses.

A fully automatic fetch (no saving pages by hand) needs the live form, which
was not reachable from the environment this was built in. It is the next step
once the form's fields can be inspected.
"""

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Lead
from .base import Source

CALENDAR_URL = "https://www.jp.pima.gov/NewCalendar2018/"
CASE_PAGE_BASE = "https://www.jp.pima.gov/CaseSearch/"

# Calendar rows and case pages describe the same cases, keyed by case number,
# so both are stored under one source name and update the same row.
JP_SOURCE = "pima_jp_calendar"

CASE_RE = re.compile(r"\b([A-Z]{2}\d{2}-\d{4,7}(?:-[A-Z]{1,3})?)\b")
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
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
            link = tr.find("a", href=re.compile(r"jcDisplayCase", re.IGNORECASE))
            cells = [_clean(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
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


class PimaJpCalendar(Source):
    name = "pima_jp_calendar"
    description = "Pima County Justice Court eviction hearings (saved calendar pages)"

    def fetch(self, since, until, paths=None, assume_eviction=False, **options):
        if not paths:
            raise SystemExit(
                "pima_jp_calendar reads saved calendar pages. Save the results of "
                f"{CALENDAR_URL} (Case Type CV, Event Type 'Eviction Action') and pass "
                "them with --file. See the module docstring for the steps."
            )
        for p in paths:
            html = Path(p).read_text(encoding="utf-8", errors="replace")
            for lead in parse_calendar_html(html, assume_eviction=assume_eviction):
                # Hearing dates run ahead of filing dates, so upcoming hearings
                # past ``until`` are kept; only old ones are dropped.
                if lead.event_date and lead.event_date < since:
                    continue
                yield lead
