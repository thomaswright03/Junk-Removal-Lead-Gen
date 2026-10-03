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
from ..util import az_today
from .base import Source
from .pima_jp_calendar import CASE_RE, JP_SOURCE

CASE_URL = "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={id}"

DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
LONG_DATE_RE = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})(?:\s+at\s+(\d{1,2}:\d{2}\s*[AP]M))?")
NOTICE_RE = re.compile(
    r"EVICTION\s+NOTICE|NOTICE\s+TO\s+VACATE|\b(?:5|FIVE|10|TEN|30|THIRTY)[- ]DAY\s+NOTICE", re.IGNORECASE
)
EVICTION_RE = re.compile(r"EVICT|DETAINER", re.IGNORECASE)
# The clean-out moment: the court rules for the landlord, then a writ of
# restitution lets the constable lock the tenant out, often leaving belongings.
JUDGMENT_RE = re.compile(r"\bJUDGMENT\b(?![^|]{0,40}\b(?:DEFENDANT|DENIED|VACATED|SET ASIDE)\b)", re.IGNORECASE)
WRIT_RE = re.compile(r"\bWRIT\b|\bLOCK[- ]?OUT\b", re.IGNORECASE)
DISMISS_RE = re.compile(r"\bDISMISS", re.IGNORECASE)
# Stages in order; the lead shows the furthest one reached.
STAGES = ("filed", "notice", "judgment", "writ")


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _iso(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    month, day, year = (int(x) for x in m.groups())
    return datetime(year, month, day).date().isoformat()


def _human(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d:%b} {d.day}, {d.year}"


def _hearing_text(event):
    """ "Eviction Action Oct 14, 2026 2:00 PM", in the same date format as the app."""
    day = _iso(event.get("DATE"))
    when = _human(day) if day else (event.get("DATE") or "")
    time_ = re.sub(r"^0", "", (event.get("TIME") or "").strip())
    return " ".join(x for x in (event.get("EVENT"), when, time_) if x)


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
    return "Document SubType" in html or ("Case Number" in html and "Case Status" in html and "Matter Type" in html)


def _first_date(rows, regex, date_keys=("FILE DATE", "DATE", "FILED")):
    """Earliest date among table rows whose text matches ``regex``; ``""``
    when a row matches but has no readable date, ``None`` when none match."""
    found = None
    for r in rows:
        if not regex.search(" ".join(r.values())):
            continue
        dates = [_iso(r.get(k)) for k in date_keys if _iso(r.get(k))]
        d = dates[0] if dates else ""
        if found is None or (d and (not found or d < found)):
            found = d
    return found


def parse_case_html(html, url=None, today=None):
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
    judgment_for_landlord = None  # date ("" when undated) from the parties table
    for headers, rows in _tables(soup):
        joined = " ".join(headers)
        if "NAME" in headers and ("ATTORNEY" in joined or "JUDGMENT" in joined):
            for r in rows:
                role = (r.get("ROLE") or r.get(headers[0]) or "").upper()
                name = r.get("NAME")
                if not name:
                    continue
                won_by = (r.get("JUDGMENT FOR") or "").upper()
                if won_by and (
                    "PLAINTIFF" in won_by
                    or "PETITIONER" in won_by
                    or ("PLAINTIFF" in role and "DEFENDANT" not in won_by)
                ):
                    judgment_for_landlord = _iso(r.get("JUDGMENT DATE")) or ""
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
        # No "Next Court Date" label: the earliest hearing on or after today.
        today_iso = (today or az_today()).isoformat()
        upcoming = sorted(d for d in (_iso(e.get("DATE")) for e in events) if d and d >= today_iso)
        next_date = upcoming[0] if upcoming else None

    papers = documents + events
    judgment_date = _first_date(papers, JUDGMENT_RE)
    if judgment_for_landlord is not None and (
        judgment_date is None or (judgment_for_landlord and judgment_for_landlord < judgment_date)
    ):
        judgment_date = judgment_for_landlord
    writ_date = _first_date(papers, WRIT_RE)
    dismissed = bool(DISMISS_RE.search(status or "")) or _first_date(papers, DISMISS_RE) is not None
    if writ_date is not None:
        stage = "writ"
    elif judgment_date is not None:
        stage = "judgment"
    elif dismissed:
        stage = "dismissed"
    elif notice:
        stage = "notice"
    else:
        stage = "filed"

    hearing = next((e for e in events if EVICTION_RE.search(" ".join(e.values()))), None)
    stage_text = {
        "writ": "Writ of restitution issued" + (f" {_human(writ_date)}" if writ_date else ""),
        "judgment": "Judgment for the landlord" + (f" {_human(judgment_date)}" if judgment_date else ""),
        "dismissed": "Case dismissed",
    }.get(stage)
    summary = [
        f"Case {status.lower()}" if status else None,
        "Eviction notice filed" if notice else "No eviction notice on file yet",
        stage_text,
        _hearing_text(hearing) if hearing else None,
    ]
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
        case_stage=stage,
        judgment_date=judgment_date or None,
        writ_date=writ_date or None,
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
        # Steve added it himself, so it shows in the default view either way.
        conn.execute(
            "UPDATE leads SET added_by_hand = 1 WHERE source = ? AND source_id = ?", (lead.source, lead.source_id)
        )
        counts["with_notice"] += int(bool(lead.eviction_notice))
        conn.commit()
    return counts


# How often the daily run re-reads an open case: cases with a notice (or
# further along) every few days, and the day after each court date; cases
# still waiting for a notice daily.
RECHECK_DAYS = 3
UNCONFIRMED_RECHECK_HOURS = 20

_OPEN_CASE_SQL = (
    "lead_type = 'eviction' AND url LIKE '%jcDisplayCase%' "
    "AND LOWER(COALESCE(case_status, '')) NOT LIKE 'closed%' "
    "AND LOWER(COALESCE(case_status, '')) NOT LIKE 'dismiss%' "
    "AND COALESCE(case_stage, '') != 'dismissed'"
)


def cases_due(conn, now=None, limit=None):
    """Open eviction cases the daily run should re-read, most urgent first:
    never read, then those whose court date has passed since the last read,
    then those not read for a while. Closed and dismissed cases are left out."""
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    today = az_today(now).isoformat()
    recent = (now - timedelta(days=RECHECK_DAYS)).isoformat()
    unconfirmed = (now - timedelta(hours=UNCONFIRMED_RECHECK_HOURS)).isoformat()
    rows = conn.execute(
        "SELECT id, url, case_checked_at, next_court_date, eviction_notice, case_stage FROM leads "
        f"WHERE {_OPEN_CASE_SQL} ORDER BY id"
    ).fetchall()
    due = []
    for r in rows:
        checked = r["case_checked_at"]
        court = (r["next_court_date"] or "")[:10]
        if not checked:
            rank = 0
        elif court and court < today and checked[:10] <= court:
            rank = 1  # the hearing happened since the last read: what was decided?
        elif r["eviction_notice"] or r["case_stage"] in ("judgment", "writ"):
            rank = 2 if checked <= recent else None
        else:
            rank = 2 if checked <= unconfirmed else None
        if rank is not None:
            due.append((rank, checked or "", r["id"], r))
    due.sort(key=lambda t: t[:3])
    rows = [t[3] for t in due]
    return rows[:limit] if limit else rows


def update_cases(
    conn, client=None, limit=None, max_age_hours=12, log=print, scheduled=False, progress=None, should_stop=None
):
    """Re-read case pages for open eviction leads.

    ``scheduled`` (the daily run) picks cases with ``cases_due``; otherwise
    every open case not read in ``max_age_hours``, never-read ones first.
    ``progress(done, total)`` is called after each case and ``should_stop()``
    before each; it returns True to stop early (cancel, or out of time).
    Stops after five failures in a row, since the court site is probably down.
    """
    from .. import db

    if scheduled:
        rows = cases_due(conn, limit=limit)
    else:
        cutoff = (
            datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=max_age_hours)
        ).isoformat()  # same format db.upsert writes
        rows = conn.execute(
            f"SELECT id, url FROM leads WHERE {_OPEN_CASE_SQL} "
            "AND (case_checked_at IS NULL OR case_checked_at <= ?) "
            "ORDER BY case_checked_at IS NOT NULL, id",
            (cutoff,),
        ).fetchall()
        if limit:
            rows = rows[:limit]
    counts = {"checked": 0, "with_notice": 0, "failed": 0, "total": len(rows)}
    client = client or CaseClient()
    failures = 0
    for i, r in enumerate(rows):
        if should_stop and should_stop():
            counts["stopped_early"] = True
            break
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
        finally:
            if progress:
                progress(i + 1, len(rows))
        failures = 0
        if lead:
            db.upsert(conn, lead)
            counts["checked"] += 1
            counts["with_notice"] += int(bool(lead.eviction_notice))
            conn.commit()
    return counts
