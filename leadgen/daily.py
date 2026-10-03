"""The daily run: new evictions in, eviction notices checked, landlord phones found.

``leadgen daily`` does, in order:

1. New City of Tucson code cases (last 30 days).
2. Eviction hearings on the Justice Court calendar for the next few weeks,
   which is where new eviction filings show up.
3. Eviction case pages: each one not read yet, each open case whose court
   date has passed since it was last read, and the other open cases every
   few days (daily while they have no notice yet), for the eviction notice,
   judgment, writ of restitution and court dates.
4. Owners from the county assessor; for evictions, the landlord's property.
5. Coordinates for new addresses.
6. Phone, email and website for the landlords and owners of new evictions.
7. Old leads marked stale.

Nothing runs while Lead Desk is paused (Settings, or LEADDESK_PAUSED=1).

Lead Desk runs this once a day while it is open, and ``leadgen schedule``
installs a daily job on this computer so it runs even when Lead Desk isn't.
"""

import json
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional, TextIO

from . import db
from .enrich import ParcelClient, enrich
from .geocode import CensusGeocoder
from .lookup import find_contacts, providers_from
from .models import Lead
from .outreach import merged_settings
from .sources import SOURCES
from .sources.pima_jp_calendar import CalendarClient
from .sources.pima_jp_case import update_cases
from .util import Conn, Log, StopCheck, az_now, az_today, is_paused, now_iso

log_ = logging.getLogger(__name__)

CALENDAR_DAYS_AHEAD = 30
CASE_PAGES_PER_RUN = 400  # about 10 minutes at the polite pace; the rest wait for tomorrow


STEP_LABELS = {
    "tucson_code_cases": "Tucson code cases",
    "evictions": "Justice Court calendar",
    "cases": "eviction case pages",
    "owners": "owners and landlords",
    "geocode": "map locations",
    "contacts": "landlord phones",
    "stale": "old leads",
}


def _step(summary: dict, name: str, fn: Callable[[], Any], log: Log) -> None:
    log(f"checking {STEP_LABELS.get(name, name)}")
    try:
        summary[name] = fn()
    except Exception as e:  # one source being down shouldn't stop the rest
        summary[name] = {"error": f"{type(e).__name__}: {e}"}
        log(f"{name} failed: {type(e).__name__}: {e}")


def _upsert_all(conn: Conn, leads: Iterable[Lead]) -> dict:
    counts = {"new": 0, "updated": 0}
    for lead in leads:
        counts[db.upsert(conn, lead)] += 1
    conn.commit()
    return counts


def geocode_new(conn: Conn, geocoder: Any = None, limit: int = 100, should_stop: StopCheck = None) -> dict:
    geocoder = geocoder or CensusGeocoder()
    counts: dict[str, Any] = {"geocoded": 0, "not_found": 0}
    for row in db.needs_geocode(conn, limit=limit):
        if should_stop and should_stop():
            counts["stopped_early"] = True
            break
        try:
            result = geocoder.geocode(row["address"], row["city"], row["zip"])
        except Exception as e:  # network trouble: try again next run
            log_.warning("map lookup failed for lead %s: %s", row["id"], type(e).__name__)
            counts["errors"] = counts.get("errors", 0) + 1
            continue
        db.save_geocode(conn, row["id"], result)
        counts["geocoded" if result else "not_found"] += 1
        conn.commit()
    return counts


def run_daily(
    conn: Conn,
    stale_days: int = 30,
    today: Optional[date] = None,
    calendar: Any = None,
    case_client: Any = None,
    parcel_client: Any = None,
    geocoder: Any = None,
    providers: Optional[list] = None,
    contact_limit: int = 60,
    case_limit: Optional[int] = None,
    days_ahead: int = CALENDAR_DAYS_AHEAD,
    code_cases: Any = None,
    log: Log = print,
) -> dict:
    """Run every step and return a summary dict. Each step's failure is logged
    and recorded, and the next step still runs."""
    today = today or az_today()
    summary: dict[str, Any] = {"started_at": now_iso()}
    settings = merged_settings(db.get_settings(conn))
    if is_paused(settings):
        # The kill switch: no court, county or lookup requests at all.
        log("paused: nothing checked (turn the pause off in Settings, or unset LEADDESK_PAUSED)")
        summary.update(paused=True, finished_at=now_iso())
        return summary

    # The pause is checked again before every step, and before every request
    # in the long loops, so pausing stops a run that is already going.
    stop = db.PauseWatch(conn)

    def step(name: str, fn: Callable[[], Any]) -> bool:
        if stop():
            return False
        _step(summary, name, fn, log)
        return not stop.hit

    since = (today - timedelta(days=30)).isoformat()

    def evictions() -> dict:
        until = (today + timedelta(days=days_ahead)).isoformat()
        if calendar is not None:
            return _upsert_all(conn, calendar.fetch(today.isoformat(), until))
        client = CalendarClient()
        counts = _upsert_all(conn, SOURCES["pima_jp_calendar"]().fetch(today.isoformat(), until, client=client))
        counts["pages"] = client.pages
        return counts

    def contacts() -> dict:
        provs = providers if providers is not None else providers_from(settings, conn=conn)
        return find_contacts(conn, provs, limit=contact_limit, lead_types=("eviction",), log=log, should_stop=stop)

    steps = [
        (
            "tucson_code_cases",
            lambda: _upsert_all(conn, (code_cases or SOURCES["tucson_code_cases"]()).fetch(since, today.isoformat())),
        ),
        ("evictions", evictions),
        (
            "cases",
            lambda: update_cases(
                conn,
                case_client,
                limit=case_limit or CASE_PAGES_PER_RUN,
                scheduled=True,
                log=log,
                should_stop=stop,
            ),
        ),
        ("owners", lambda: enrich(conn, parcel_client or ParcelClient(), should_stop=stop)),
        ("geocode", lambda: geocode_new(conn, geocoder, should_stop=stop)),
        ("contacts", contacts),
        ("stale", lambda: db.mark_stale(conn, stale_days, today=today)),
    ]
    for name, fn in steps:
        if not step(name, fn):
            # Paused while running: stop here and say so.
            log(f"paused during {STEP_LABELS.get(name, name)}: stopped")
            summary.update(paused=True, paused_during=name)
            break
    summary["finished_at"] = now_iso()
    values: dict[str, Any] = {"last_daily_summary": summary}
    if summary.get("paused_during"):
        # Stopped part way: not today's run. Once the pause is off it runs
        # again (Lead Desk starts it at once, else its scheduler within
        # minutes) and picks up where it stopped: cases already read today
        # aren't read again.
        values["last_daily_interrupted"] = today.isoformat()
    else:
        values.update(last_daily_run=today.isoformat(), last_daily_interrupted=None)
    db.put_settings(conn, values)
    conn.commit()
    return summary


def due(conn: Conn, now: Optional[datetime] = None, hour: int = 6) -> bool:
    """True when today's run hasn't finished yet and it's past ``hour`` local
    time (a run stopped part way by the pause doesn't count)."""
    now = now or az_now().replace(tzinfo=None)
    last = db.get_settings(conn).get("last_daily_run")
    return now.hour >= hour and last != now.date().isoformat()


def interrupted_today(settings: dict, today: Optional[date] = None) -> bool:
    """True when today's check was stopped part way by the pause and hasn't
    finished since."""
    day = (today or az_today()).isoformat()
    return settings.get("last_daily_interrupted") == day and settings.get("last_daily_run") != day


def describe(summary: dict) -> str:
    """One line for logs and the Lead Desk header."""
    if summary.get("paused") and not summary.get("paused_during"):
        return "paused: nothing was checked"

    def n(step: str, key: str) -> int:
        v = summary.get(step) or {}
        return v.get(key, 0) if isinstance(v, dict) else 0

    parts = [
        f"{n('evictions', 'new')} new evictions",
        f"{n('cases', 'with_notice')} with an eviction notice",
        f"{n('contacts', 'found')} landlord contacts found",
        f"{n('tucson_code_cases', 'new')} new code cases",
    ]
    # Lookups that failed (service down, bad Google key) are not the same as
    # "no number exists", so say how many and that they are tried again.
    failed = n("contacts", "errors")
    if failed:
        lookups = f"{failed} lookup{'' if failed == 1 else 's'}"
        cause = (summary.get("contacts") or {}).get("error_cause")
        hint = "; check the Google key in Settings" if cause == "google_key" else ""
        parts[2] += f", {lookups} failed (will retry tomorrow{hint})"
    for step, what in (("owners", "owner"), ("geocode", "map")):
        failed = n(step, "errors")
        if failed:
            parts.append(f"{failed} {what} lookup{'' if failed == 1 else 's'} failed (will retry tomorrow)")
    errors = [k for k, v in summary.items() if isinstance(v, dict) and "error" in v]
    if errors:
        parts.append("failed: " + ", ".join(errors))
    if summary.get("paused_during"):
        label = STEP_LABELS.get(summary["paused_during"], summary["paused_during"])
        parts.append(f"stopped at {label} because Lead Desk was paused")
    return ", ".join(parts)


# What one failed item of a step is called, for ``problems``.
_FAILED_ITEM = {
    "owners": "owner lookup",
    "geocode": "map lookup",
    "contacts": "phone lookup",
    "cases": "court case read",
}


def problems(summary: Optional[dict]) -> list[str]:
    """What went wrong in a daily check, in a few words each (the Lead Desk
    header shows a warning when there is any)."""
    out = []
    for k, v in (summary or {}).items():
        if not isinstance(v, dict):
            continue
        n = v.get("errors") or v.get("failed") or 0
        if "error" in v:
            out.append(f"{STEP_LABELS.get(k, k)} failed")
        elif n and k in _FAILED_ITEM:
            out.append(f"{n} {_FAILED_ITEM[k]}{'' if n == 1 else 's'} failed")
    return out


def counts_only(summary: dict) -> dict:
    """The summary with error messages cut to the error type, for public logs
    (an error message can quote a request that carries a landlord's name)."""
    out = {}
    for k, v in summary.items():
        if isinstance(v, dict) and "error" in v:
            v = {"error": str(v["error"]).split(":", 1)[0]}
        out[k] = v
    return out


def main_log(summary: dict, out: TextIO = sys.stdout, public: bool = False) -> None:
    print(datetime.now().strftime("%Y-%m-%d %H:%M"), describe(summary), file=out)
    print(json.dumps(counts_only(summary) if public else summary, default=str), file=out)
