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

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from ..config import USER_AGENT
from ..models import Lead
from ..util import Conn, Log, StopCheck, az_today
from .base import Source
from .pima_jp_calendar import CASE_RE, JP_SOURCE

CASE_URL = "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID={id}"
_log = logging.getLogger(__name__)

DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
LONG_DATE_RE = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4})(?:\s+at\s+(\d{1,2}:\d{2}\s*[AP]M))?")
NOTICE_RE = re.compile(
    r"EVICTION\s+NOTICE|NOTICE\s+TO\s+VACATE|\b(?:5|FIVE|10|TEN|30|THIRTY)[- ]DAY\s+NOTICE", re.IGNORECASE
)
EVICTION_RE = re.compile(r"EVICT|DETAINER", re.IGNORECASE)
# The clean-out moment: the court rules for the landlord, then a writ of
# restitution lets the constable lock the tenant out, often leaving belongings.
#
# Each document and calendar event is read as one of these (``paper_kind``):
#   judgment       judgment entered for the landlord (plaintiff)
#   writ           a writ of restitution issued (or served: a lockout)
#   set_aside      the judgment was set aside or vacated (by the court)
#   writ_quashed   the writ was quashed or recalled
#   dismissed      the case was dismissed (stipulated, voluntary, by order)
#   for_tenant     judgment for the defendant (tenant): the landlord lost
#   satisfied      satisfaction of judgment: the tenant paid
# Asking for something is not getting it: a motion, application, request or
# petition counts only once the court grants it, and nothing denied counts.
# Writs of garnishment or execution are about money, not a lockout.
JUDGMENT_RE = re.compile(r"\bJUDG(?:E)?MENT\b", re.IGNORECASE)
WRIT_RE = re.compile(
    r"\bWRIT\b[^|]{0,20}\bRESTITUTION\b|\bRESTITUTION\b[^|]{0,10}\bWRIT\b|\bLOCK[- ]?OUT\b", re.IGNORECASE
)
DISMISS_RE = re.compile(r"\bDISMISS", re.IGNORECASE)
_ASKED_RE = re.compile(r"\b(?:MOTION|APPLICATION|APPLY|REQUEST|PETITION|PROPOSED|OBJECTION|RESPONSE|REPLY)\b", re.I)
_GRANTED_RE = re.compile(r"\bGRANT(?:ED|ING|S)?\b", re.IGNORECASE)
_REFUSED_RE = re.compile(r"\bDEN(?:Y|IED|IES|YING)\b|\bWITHDRAWN\b|\bSTRICKEN\b", re.IGNORECASE)
_SATISFIED_RE = re.compile(r"\bSATISF(?:Y|IED|ACTION)\b", re.IGNORECASE)
_UNDONE_RE = re.compile(r"\bSET[- ]?ASIDE\b|\bVACAT(?:E|ED|ING)\b|\bREVERS(?:E|ED)\b", re.IGNORECASE)
_QUASHED_RE = re.compile(r"\bQUASH(?:ED|ING)?\b|\bRECALL(?:ED)?\b", re.IGNORECASE)
_FOR_TENANT_RE = re.compile(r"\b(?:FOR|IN FAVOU?R OF)\s+(?:THE\s+)?(?:DEFENDANT|TENANT|RESPONDENT)S?\b", re.IGNORECASE)
_MONEY_ONLY_RE = re.compile(r"\bGARNISH|\bEXECUTION\b|\bDEBTOR\b|\bEXAM(?:INATION)?\b", re.IGNORECASE)
# The stages that mean the eviction is over without a clean-out lead.
ENDED_STAGES = ("dismissed", "satisfied", "closed")
# Same-day order when papers share a date: the ending ones are read last.
_KIND_ORDER = {
    "judgment": 0,
    "writ": 1,
    "set_aside": 2,
    "writ_quashed": 2,
    "dismissed": 3,
    "for_tenant": 3,
    "satisfied": 3,
}


def _clean(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _iso(text: Optional[str]) -> Optional[str]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    month, day, year = (int(x) for x in m.groups())
    return datetime(year, month, day).date().isoformat()


def _human(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d:%b} {d.day}, {d.year}"


def _hearing_text(event: dict) -> str:
    """ "Eviction Action Oct 14, 2026 2:00 PM", in the same date format as the app."""
    day = _iso(event.get("DATE"))
    when = _human(day) if day else (event.get("DATE") or "")
    time_ = re.sub(r"^0", "", (event.get("TIME") or "").strip())
    return " ".join(x for x in (event.get("EVENT"), when, time_) if x)


def _label(text: str, label: str) -> Optional[str]:
    """Value after ``Label:`` in the page text, up to the next label."""
    m = re.search(re.escape(label) + r"\s*:?\s*(.+?)(?=\s+[A-Z][A-Za-z ]{2,25}:|$)", text)
    return _clean(m.group(1)) if m else None


def _tables(soup: Any) -> Iterator[tuple[list[str], list[dict]]]:
    """Yield (headers, rows) for each table, headers upper-cased."""
    for table in soup.find_all("table"):
        headers: Optional[list[str]] = None
        rows: list[dict] = []
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


def case_id(text: Optional[str]) -> Optional[str]:
    """Case page ID from a link, or a bare number. ``None`` otherwise."""
    text = (text or "").strip()
    if text.isdigit():
        return text
    ids = parse_qs(urlparse(text).query).get("ID") or parse_qs(urlparse(text).query).get("id")
    if ids and ids[0].isdigit() and "jcdisplaycase" in text.lower():
        return ids[0]
    return None


def is_case_page(html: str) -> bool:
    return "Document SubType" in html or ("Case Number" in html and "Case Status" in html and "Matter Type" in html)


def paper_kind(text: str) -> Optional[str]:
    """What one document or calendar event (its cells joined) means for the
    case: one of the kinds listed above ``JUDGMENT_RE``, or ``None``."""
    if _REFUSED_RE.search(text):
        return None  # "Motion ... denied", "Writ denied", "Request withdrawn"
    if _ASKED_RE.search(text) and not _GRANTED_RE.search(text):
        return None  # asked for, not (yet) granted
    judgment = bool(JUDGMENT_RE.search(text))
    if _SATISFIED_RE.search(text):
        # A partial payment doesn't end an eviction.
        return None if re.search(r"\bPARTIAL", text, re.IGNORECASE) else "satisfied"
    if _MONEY_ONLY_RE.search(text):
        return None  # collecting the money (garnishment, debtor exam), not the unit
    if judgment and _UNDONE_RE.search(text):
        return "set_aside"
    if WRIT_RE.search(text) and _QUASHED_RE.search(text):
        return "writ_quashed"
    if DISMISS_RE.search(text):
        return "dismissed"
    if WRIT_RE.search(text):
        return "writ"
    if judgment:
        return "for_tenant" if _FOR_TENANT_RE.search(text) else "judgment"
    return None


def _row_date(row: dict, keys: tuple[str, ...] = ("FILE DATE", "DATE", "FILED")) -> str:
    for k in keys:
        d = _iso(row.get(k))
        if d:
            return d
    return ""


def case_stage(
    documents: list[dict],
    events: list[dict],
    status: Optional[str],
    notice: bool,
    party_judgments: Optional[list[dict]] = None,
    today: Optional[date] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    """``(stage, judgment date, writ date)`` from a case's papers.

    The papers are read in date order and the latest one that decides
    anything wins: a dismissal or satisfaction after a judgment ends the
    case, a writ after a dismissal brings it back, a set-aside undoes the
    judgment. Papers and events dated after ``today`` (upcoming hearings)
    count for nothing. ``party_judgments`` are the parties table's
    "Judgment For" entries (``{"for": "Plaintiff", "date": iso}``).

    Then the case status: dismissed ends the case unless a writ is in
    force; closed or disposed ends it unless a judgment or writ is.
    Stages: filed, notice, judgment, writ, dismissed, satisfied, closed.
    """
    today_iso = (today or az_today()).isoformat()
    found: list[tuple[str, int, int, str]] = []  # (date, order, position, kind)
    papers = [(_row_date(r), " ".join(r.values())) for r in documents + events]
    for won_by in party_judgments or ():
        who = str(won_by.get("for") or "").upper()
        side = "Defendant" if "DEFENDANT" in who or "RESPONDENT" in who else "Plaintiff"
        papers.append((str(won_by.get("date") or ""), "Judgment for " + side))
    for i, (d, text) in enumerate(papers):
        kind = paper_kind(text)
        if not kind or (d and d > today_iso):
            continue  # decides nothing, or hasn't happened yet
        # An undated paper is read after the dated ones (its date unknown).
        found.append((d or "9999", _KIND_ORDER[kind], i, kind))
    judgment: Optional[str] = None
    writ: Optional[str] = None
    ended: Optional[str] = None
    for d, _order, _i, kind in sorted(found):
        day = "" if d == "9999" else d
        if kind == "judgment":
            # The latest judgment in force counts (an amended judgment
            # replaces the first); an undated one never replaces a dated one.
            judgment = day or (judgment if judgment is not None and not ended else day)
            ended = None
        elif kind == "writ":
            writ = writ if writ is not None and not ended else day
            ended = None
        elif kind == "set_aside":
            judgment = writ = None
        elif kind == "writ_quashed":
            writ = None
        else:
            ended = "satisfied" if kind == "satisfied" else "dismissed"
    if ended:
        return ended, None, None
    state = (status or "").strip().lower()
    if state.startswith("dismiss") and writ is None:
        return "dismissed", None, None
    if state.startswith(("closed", "dispos")) and writ is None and judgment is None:
        return "closed", None, None
    if writ is not None:
        return "writ", judgment, writ
    if judgment is not None:
        return "judgment", judgment, None
    return ("notice" if notice else "filed"), None, None


STAGE_TEXT = {
    "writ": "Writ of restitution issued",
    "judgment": "Judgment for the landlord",
    "dismissed": "Case dismissed",
    "satisfied": "Judgment satisfied (paid)",
    "closed": "Case closed with no judgment for the landlord",
}


def case_summary(
    status: Optional[str],
    notice: bool,
    stage: str,
    judgment_date: Optional[str],
    writ_date: Optional[str],
    events: list[dict],
) -> str:
    """The lead's description: status, notice, stage and the eviction hearing."""
    hearing = next((e for e in events if EVICTION_RE.search(" ".join(e.values()))), None)
    when = {"writ": writ_date, "judgment": judgment_date}.get(stage)
    stage_text = STAGE_TEXT.get(stage)
    if stage_text and when:
        stage_text += f" {_human(when)}"
    summary = [
        f"Case {status.lower()}" if status else None,
        "Eviction notice filed" if notice else "No eviction notice on file yet",
        stage_text,
        _hearing_text(hearing) if hearing else None,
    ]
    return " | ".join(s for s in summary if s)


def parse_case_html(html: str, url: Optional[str] = None, today: Optional[date] = None) -> Optional[Lead]:
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
    party_judgments: list[dict] = []  # the parties table's "Judgment For" entries
    for headers, rows in _tables(soup):
        joined = " ".join(headers)
        if "NAME" in headers and ("ATTORNEY" in joined or "JUDGMENT" in joined):
            for r in rows:
                role = (r.get("ROLE") or r.get(headers[0]) or "").upper()
                name = r.get("NAME")
                if not name:
                    continue
                won_by = (r.get("JUDGMENT FOR") or "").strip()
                if won_by:
                    entry = {"for": won_by, "date": _iso(r.get("JUDGMENT DATE")) or ""}
                    if entry not in party_judgments:
                        party_judgments.append(entry)
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

    stage, judgment_date, writ_date = case_stage(documents, events, status, notice, party_judgments, today)
    return Lead(
        source=JP_SOURCE,
        source_id=case,
        lead_type="eviction" if is_eviction else "civil",
        event_date=filed,
        in_pima=True,
        plaintiff="; ".join(plaintiffs) or None,
        defendant="; ".join(defendants) or None,
        description=case_summary(status, notice, stage, judgment_date, writ_date, events),
        url=url,
        eviction_notice=notice,
        case_status=status,
        next_court_date=next_date,
        case_stage=stage,
        judgment_date=judgment_date or None,
        writ_date=writ_date or None,
        raw={"documents": documents, "events": events, "judgments": party_judgments},
    )


class CaseClient:
    def __init__(self, session: Any = None, delay: float = 1.5) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self._last = 0.0

    def fetch(self, id_or_url: str) -> Optional[Lead]:
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

    def fetch(
        self,
        since: str,
        until: str,
        paths: Optional[list] = None,
        cases: Optional[list] = None,
        client: Any = None,
        **options: Any,
    ) -> Iterator[Lead]:
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


def split_case_inputs(text: Optional[str]) -> tuple[list[str], list[str]]:
    """Case links/IDs from pasted text; also returns entries that aren't links."""
    ids: list[str] = []
    unknown: list[str] = []
    for token in re.split(r"[\s,;]+", text or ""):
        if not token:
            continue
        cid = case_id(token)
        if cid and cid not in ids:
            ids.append(cid)
        elif not cid:
            unknown.append(token)
    return ids, unknown


def add_cases(conn: Conn, text: str, client: Any = None, log: Log = print, should_stop: StopCheck = None) -> dict:
    """Read each pasted case link or ID and store it. Returns counts.
    ``should_stop()`` is asked before each case (True stops, e.g. paused)."""
    from .. import db

    ids, unknown = split_case_inputs(text)
    counts: dict[str, Any] = {"new": 0, "updated": 0, "with_notice": 0, "failed": 0, "skipped": unknown}
    client = client or CaseClient()
    for cid in ids:
        if should_stop and should_stop():
            counts["stopped_early"] = True
            break
        try:
            lead = client.fetch(cid)
        except requests.RequestException as e:
            counts["failed"] += 1
            log(f"case {cid}: the court's case page couldn't be read")
            _log.debug("case %s: %s: %s", cid, type(e).__name__, e)
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
# still waiting for a notice daily. A case that ended (dismissed, satisfied,
# closed) is read once a week while it is young, since a stipulated
# dismissal can turn into a judgment and writ when the tenant misses a payment.
RECHECK_DAYS = 3
UNCONFIRMED_RECHECK_HOURS = 20
ENDED_RECHECK_DAYS = 7
ENDED_WATCH_DAYS = 60

# A case that ended without a clean-out: the stage says so, or (rows read
# before the stage did) the court's status says dismissed with no writ, or
# closed / disposed with no judgment or writ.
ENDED_SQL = (
    f"(COALESCE(case_stage, '') IN ({', '.join(repr(s) for s in ENDED_STAGES)}) "
    "OR (LOWER(COALESCE(case_status, '')) LIKE 'dismiss%' AND COALESCE(case_stage, '') <> 'writ') "
    "OR ((LOWER(COALESCE(case_status, '')) LIKE 'closed%' OR LOWER(COALESCE(case_status, '')) LIKE 'dispos%') "
    "AND COALESCE(case_stage, '') NOT IN ('judgment', 'writ')))"
)
_CASE_PAGE_SQL = "lead_type = 'eviction' AND url LIKE '%jcDisplayCase%'"
_OPEN_CASE_SQL = f"{_CASE_PAGE_SQL} AND NOT {ENDED_SQL}"


def cases_due(conn: Conn, now: Optional[datetime] = None, limit: Optional[int] = None) -> list:
    """Cases the daily run should re-read, most urgent first: never read,
    then those whose court date has passed since the last read, then open
    cases not read for a while, then young ended cases not read for a week
    (in case a writ follows a dismissal). Older ended cases are left out."""
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    today = az_today(now)
    recent = (now - timedelta(days=RECHECK_DAYS)).isoformat()
    unconfirmed = (now - timedelta(hours=UNCONFIRMED_RECHECK_HOURS)).isoformat()
    ended_recent = (now - timedelta(days=ENDED_RECHECK_DAYS)).isoformat()
    young = (today - timedelta(days=ENDED_WATCH_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id, url, case_checked_at, next_court_date, eviction_notice, case_stage, event_date, "
        f"CASE WHEN {ENDED_SQL} THEN 1 ELSE 0 END AS ended FROM leads WHERE {_CASE_PAGE_SQL} ORDER BY id"
    ).fetchall()
    due = []
    for r in rows:
        checked = r["case_checked_at"]
        court = (r["next_court_date"] or "")[:10]
        if r["ended"]:
            filed = str(r["event_date"] or "")[:10]
            rank: Optional[int] = 3 if checked and checked <= ended_recent and filed >= young else None
        elif not checked:
            rank = 0
        elif court and court < today.isoformat() and checked[:10] <= court:
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


# Version of the stage rules above. When it changes, cases already stored
# are re-read from their saved papers at startup (``rederive_stages``).
STAGE_RULES_VERSION = 3
# Old (version 1) rule, only to tell whether a stored judgment came from
# the papers or from the parties table, which older rows didn't keep.
_V1_JUDGMENT_RE = re.compile(r"\bJUDGMENT\b(?![^|]{0,40}\b(?:DEFENDANT|DENIED|VACATED|SET ASIDE)\b)", re.I)
_LONG_AGO = "2000-01-01T00:00:00+00:00"


def rederive_stages(conn: Conn, today: Optional[date] = None) -> int:
    """Work out every stored case's stage again with the current rules, from
    the papers saved with it, so a stage the old rules got wrong (a
    satisfaction or set-aside read as a judgment, an application read as a
    writ, a dismissal that didn't end the case) doesn't linger. Cases read
    before the parties table was kept are also queued to be read again
    first (their last-read time is moved back). Returns the rows changed."""
    import json

    changed = 0
    rows = conn.execute(
        "SELECT id, raw_json, case_status, eviction_notice, case_stage, judgment_date, writ_date, "
        f"description, case_checked_at FROM leads WHERE {_CASE_PAGE_SQL} AND case_checked_at IS NOT NULL"
    ).fetchall()
    for r in rows:
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except ValueError:
            raw = {}
        updates: dict[str, Any] = {}
        if "judgments" not in raw:
            updates["case_checked_at"] = _LONG_AGO  # read it again soon, parties table and all
        if isinstance(raw.get("documents"), list) and isinstance(raw.get("events"), list):
            documents, events = raw["documents"], raw["events"]
            judgments = raw.get("judgments")
            if judgments is None:
                papers = [" ".join(p.values()) for p in documents + events]
                from_papers = any(_V1_JUDGMENT_RE.search(p) for p in papers)
                judgments = (
                    [] if from_papers or not r["judgment_date"] else [{"for": "Plaintiff", "date": r["judgment_date"]}]
                )
            notice = bool(r["eviction_notice"])
            stage, judgment_date, writ_date = case_stage(documents, events, r["case_status"], notice, judgments, today)
            updates.update(
                case_stage=stage,
                judgment_date=judgment_date or None,
                writ_date=writ_date or None,
                description=case_summary(r["case_status"], notice, stage, judgment_date, writ_date, events),
            )
        updates = {k: v for k, v in updates.items() if r[k] != v}
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*updates.values(), r["id"]])
            changed += 1
    conn.commit()
    return changed


def update_cases(
    conn: Conn,
    client: Any = None,
    limit: Optional[int] = None,
    max_age_hours: int = 12,
    log: Log = print,
    scheduled: bool = False,
    progress: Optional[Callable[[int, int], Any]] = None,
    should_stop: StopCheck = None,
) -> dict:
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
    counts: dict[str, Any] = {"checked": 0, "with_notice": 0, "failed": 0, "total": len(rows)}
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
            log(f"lead {r['id']}: the court's case page couldn't be read")
            _log.debug("lead %s: %s: %s", r["id"], type(e).__name__, e)
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
