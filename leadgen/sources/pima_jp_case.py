"""Pima County Justice Court case pages (``jcDisplayCase.aspx``).

Each case has a public page at
``https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID=<number>`` that
lists the parties, upcoming hearings and the documents filed. An eviction
case shows a ``CIV – EVICTION NOTICE`` document once the landlord files the
notice they served on the tenant, which is what Lead Desk's "evictions with
a notice" view filters on.

The page has no property address. Leads from it carry the landlord
(plaintiff), the tenants (defendants), the filing date and the next court
date; the owner lookup in Lead Desk can then list the landlord's properties.

Case pages are read one at a time, only for case links someone saved or
pasted, with a pause between requests. This module never walks ID ranges.
"""

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ..config import USER_AGENT
from ..models import Lead
from .base import Source
from .pima_jp_calendar import CASE_RE, JP_SOURCE

CASE_URL = "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={id}"

DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
LONG_DATE_RE = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})(?:\s+at\s+(\d{1,2}:\d{2}\s*[AP]M))?")
NOTICE_RE = re.compile(r"EVICTION\s+NOTICE|NOTICE\s+TO\s+VACATE|\b(?:5|FIVE|10|TEN|30|THIRTY)[- ]DAY\s+NOTICE",
                       re.IGNORECASE)
EVICTION_RE = re.compile(r"EVICT|DETAINER", re.IGNORECASE)


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _iso(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    month, day, year = (int(x) for x in m.groups())
    return datetime(year, month, day).date().isoformat()


def _label(text, label):
    """Value after ``Label:`` in the page text, up to the next label."""
    m = re.search(re.escape(label) + r"\s*:?\s*(.+?)(?=\s+[A-Z][A-Za-z ]{2,25}:|$)", text)
    return _clean(m.group(1)) if m else None


def _tables(soup):
    """Yield (headers, rows) for each table, headers upper-cased."""
    for table in soup.find_all("table"):
        headers, rows = None, []
        for tr in table.find_all("tr"):
            if tr.find_parent("table") is not table or tr.find("table"):
                continue  # rows of a nested table, or a layout row wrapping one
            cells = [_clean(c.get_text(" ")) for c in tr.find_all(["th", "td"], recursive=False)]
            if not any(cells):
                continue
            if headers is None:
                headers = [c.upper() for c in cells]
            else:
                rows.append(dict(zip(headers, cells)))
        if headers:
            yield headers, rows


def case_id(text):
    """Case page ID from a link, or a bare number. ``None`` otherwise."""
    text = (text or "").strip()
    if text.isdigit():
        return text
    ids = parse_qs(urlparse(text).query).get("ID") or parse_qs(urlparse(text).query).get("id")
    if ids and ids[0].isdigit() and "jcdisplaycase" in text.lower():
        return ids[0]
    return None


def is_case_page(html):
    return "Document SubType" in html or ("Case Number" in html and "Case Status" in html
                                          and "Matter Type" in html)


def parse_case_html(html, url=None):
    """Return a Lead for one saved or fetched case page, or ``None``."""
    soup = BeautifulSoup(html, "html.parser")
    text = _clean(soup.get_text(" "))
    case_m = re.search(r"Case Number\s*:?\s*" + CASE_RE.pattern, text) or CASE_RE.search(text)
    if not case_m:
        return None
    case = case_m.group(1)
    if not case.startswith("CV"):
        return None
    if not url:
        # A saved page keeps its own address in the form action.
        m = re.search(r"jcDisplayCase\.aspx\?ID=(\d+)", html, re.IGNORECASE)
        url = CASE_URL.format(id=m.group(1)) if m else None

    plaintiffs, defendants, events, documents = [], [], [], []
    for headers, rows in _tables(soup):
        joined = " ".join(headers)
        if "NAME" in headers and ("ATTORNEY" in joined or "JUDGMENT" in joined):
            for r in rows:
                role = (r.get("ROLE") or r.get(headers[0]) or "").upper()
                name = r.get("NAME")
                if not name:
                    continue
                if "PLAINTIFF" in role or "PETITIONER" in role:
                    plaintiffs.append(name)
                elif "DEFENDANT" in role or "RESPONDENT" in role:
                    defendants.append(name)
        elif "EVENT" in headers and "DATE" in headers:
            events.extend(rows)
        elif any("DOCUMENT" in h for h in headers):
            documents.extend(rows)

    doc_text = " | ".join(" ".join(d.values()) for d in documents)
    event_text = " | ".join(" ".join(e.values()) for e in events)
    notice = bool(NOTICE_RE.search(doc_text))
    is_eviction = case.endswith("-EA") or notice or bool(EVICTION_RE.search(event_text + doc_text))

    filed = _iso(_label(text, "Filed"))
    status = _label(text, "Case Status")
    if status:
        status = status.split(" Assigned")[0].split(" Next")[0]
    next_date = None
    nm = re.search(r"Next Court Date\s*:?\s*(?:[A-Z][a-z]+day,\s*)?" + LONG_DATE_RE.pattern, text)
    if nm:
        stamp = nm.group(1) + (" " + nm.group(2).replace(" ", "") if nm.group(2) else "")
        for fmt in ("%B %d, %Y %I:%M%p", "%B %d, %Y"):
            try:
                next_date = datetime.strptime(stamp, fmt).isoformat(sep=" ", timespec="minutes")
                break
            except ValueError:
                pass
    if not next_date:
        upcoming = sorted(_iso(e.get("DATE")) for e in events if _iso(e.get("DATE")))
        next_date = upcoming[-1] if upcoming else None

    hearing = next((e for e in events if EVICTION_RE.search(" ".join(e.values()))), None)
    summary = [f"Case {status.lower()}" if status else None,
               "Eviction notice filed" if notice else "No eviction notice on file yet",
               f"{hearing.get('EVENT')} {hearing.get('DATE')} {hearing.get('TIME') or ''}".strip()
               if hearing else None]
    return Lead(
        source=JP_SOURCE,
        source_id=case,
        lead_type="eviction" if is_eviction else "civil",
        event_date=filed,
        in_pima=True,
        plaintiff="; ".join(plaintiffs) or None,
        defendant="; ".join(defendants) or None,
        description=" | ".join(s for s in summary if s),
        url=url,
        eviction_notice=notice,
        case_status=status,
        next_court_date=next_date,
        raw={"documents": documents, "events": events},
    )


class CaseClient:
    def __init__(self, session=None, delay=1.5):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self._last = 0.0

    def fetch(self, id_or_url):
        cid = case_id(id_or_url)
        if not cid:
            raise ValueError(f"not a Justice Court case link: {id_or_url}")
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = CASE_URL.format(id=cid)
        resp = self.session.get(url, timeout=30)
        self._last = time.monotonic()
        resp.raise_for_status()
        return parse_case_html(resp.text, url=url)


class PimaJpCase(Source):
    name = "pima_jp_case"
    description = "Pima County Justice Court case pages (links, IDs or saved pages)"

    def fetch(self, since, until, paths=None, cases=None, client=None, **options):
        """Read saved case pages (``paths``) and/or case links or IDs (``cases``)."""
        if not (paths or cases):
            raise SystemExit("pima_jp_case needs saved case pages (--file) or case links (--case).")
        for p in paths or ():
            lead = parse_case_html(Path(p).read_text(encoding="utf-8", errors="replace"))
            if lead:
                yield lead
        client = client or CaseClient()
        for c in cases or ():
            lead = client.fetch(c)
            if lead:
                yield lead


def split_case_inputs(text):
    """Case links/IDs from pasted text; also returns entries that aren't links."""
    ids, unknown = [], []
    for token in re.split(r"[\s,;]+", text or ""):
        if not token:
            continue
        cid = case_id(token)
        if cid and cid not in ids:
            ids.append(cid)
        elif not cid:
            unknown.append(token)
    return ids, unknown


def add_cases(conn, text, client=None, log=print):
    """Read each pasted case link or ID and store it. Returns counts."""
    from .. import db

    ids, unknown = split_case_inputs(text)
    counts = {"new": 0, "updated": 0, "with_notice": 0, "failed": 0, "skipped": unknown}
    client = client or CaseClient()
    for cid in ids:
        try:
            lead = client.fetch(cid)
        except requests.RequestException as e:
            counts["failed"] += 1
            log(f"case {cid}: {type(e).__name__}")
            continue
        if not lead:
            counts["failed"] += 1
            log(f"case {cid}: not a civil case page")
            continue
        counts[db.upsert(conn, lead)] += 1
        counts["with_notice"] += int(bool(lead.eviction_notice))
        conn.commit()
    return counts


def update_cases(conn, client=None, limit=None, max_age_hours=12, log=print,
                 only_unconfirmed=False):
    """Re-read case pages for open eviction leads not checked recently.

    Cases never read come first. ``only_unconfirmed`` skips cases already known
    to have an eviction notice (the daily run uses it to keep requests down).
    Stops after five failures in a row, since the court site is probably down.
    """
    from .. import db

    cutoff = (datetime.now(timezone.utc).replace(microsecond=0)
              - timedelta(hours=max_age_hours)).isoformat()  # same format db.upsert writes
    rows = conn.execute(
        "SELECT id, url FROM leads WHERE lead_type = 'eviction' AND url LIKE '%jcDisplayCase%' "
        "AND (case_status IS NULL OR case_status NOT LIKE 'Closed%') "
        "AND (case_checked_at IS NULL OR case_checked_at <= ?) "
        + ("AND COALESCE(eviction_notice, 0) = 0 " if only_unconfirmed else "")
        + "ORDER BY case_checked_at IS NOT NULL, id",
        (cutoff,),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    counts = {"checked": 0, "with_notice": 0, "failed": 0}
    client = client or CaseClient()
    failures = 0
    for r in rows:
        try:
            lead = client.fetch(r["url"])
        except requests.RequestException as e:
            counts["failed"] += 1
            failures += 1
            log(f"lead {r['id']}: {type(e).__name__}")
            if failures >= 5:
                log("court case pages not responding; stopping for this run")
                break
            continue
        failures = 0
        if lead:
            db.upsert(conn, lead)
            counts["checked"] += 1
            counts["with_notice"] += int(bool(lead.eviction_notice))
            conn.commit()
    return counts
