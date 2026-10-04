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

When a whole step fails (the court calendar or the City's site can't be
reached, say), the run doesn't count as the day's check: it is tried again
later the same day, 30 minutes, then 1 and 2 hours after each failure, at
most three times. A retry that gets through makes the day checked.

Lead Desk runs this once a day while it is open, and ``leadgen schedule``
installs a daily job on this computer so it runs even when Lead Desk isn't.
"""

import json
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional, TextIO

from . import db, leadlist
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
CASE_PAGES_PER_RUN = 400  # about 10 to 15 minutes at the polite pace; the rest wait for tomorrow
# How long a check that reads the most case pages takes, for the page and the README.
CHECK_MINUTES = "about 15 minutes"
# Minutes to wait before each same-day retry after a step failed outright.
RETRY_AFTER_MINUTES = (30, 60, 120)
# A scheduled run this many minutes before a retry is due runs it (the
# schedules start on the hour; a retry due at 8:03 shouldn't wait for 10:00).
RETRY_SLACK_MINUTES = 10


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
    """Run one step. A failure is recorded in the summary (the error itself
    under "error", for ``--debug`` and the server log; the website in words
    under "site") and logged as a plain sentence, and the next step runs."""
    label = STEP_LABELS.get(name, name)
    log(f"checking {label}")
    try:
        summary[name] = fn()
    except Exception as e:  # one source being down shouldn't stop the rest
        summary[name] = {"error": f"{type(e).__name__}: {e}"}
        import requests

        if isinstance(e, requests.RequestException):
            from .cli import site_name

            summary[name]["site"] = site_name(e)
            log(f"{label} failed: {summary[name]['site']} couldn't be reached")
        else:
            log(f"{label} failed because of an unexpected problem (run with --debug for details)")
        log_.debug("%s failed", label, exc_info=True)


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
    now: Optional[datetime] = None,
    progress: Optional[Callable[[str], Any]] = None,
) -> dict:
    """Run every step and return a summary dict. Each step's failure is logged
    and recorded, and the next step still runs. When a step failed outright
    the day isn't counted as checked and a retry is set (see ``retry_pending``).
    ``progress(message)`` hears how far the longest step has got ("reading
    court cases: 120 of 400"), for the Lead Desk header."""
    now = now or az_now().replace(tzinfo=None)
    today = today or now.date()
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
                progress=(lambda done, total: progress(f"reading court cases: {done} of {total}")) if progress else None,
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
    try:
        leadlist.record_address_share(conn, settings, today)
    except Exception:  # a progress number must never fail the check
        log("address progress not saved because of an unexpected problem")
        log_.debug("address progress not saved", exc_info=True)
    values: dict[str, Any] = {"last_daily_summary": summary}
    failed = failed_steps(summary)
    if summary.get("paused_during"):
        # Stopped part way: not today's run. Once the pause is off it runs
        # again (Lead Desk starts it at once, else its scheduler within
        # minutes) and picks up where it stopped: cases already read today
        # aren't read again.
        values["last_daily_interrupted"] = today.isoformat()
    elif failed:
        # A whole step failed (a site down, the network out): try again later
        # today rather than wait for tomorrow. After the last retry the day
        # counts as checked, and tomorrow's run starts over.
        before = settings.get("daily_retry") or {}
        attempt = (before.get("attempt", 0) if before.get("date") == today.isoformat() else 0) + 1
        at = None
        if attempt <= len(RETRY_AFTER_MINUTES):
            at = datetime.combine(today, now.time()) + timedelta(minutes=RETRY_AFTER_MINUTES[attempt - 1])
            if at.date() != today:
                at = None  # not before midnight: tomorrow's run will do
        retry = {"date": today.isoformat(), "attempt": attempt, "failed": failed}
        if at:
            retry["at"] = at.isoformat(timespec="minutes")
            summary["retry_at"] = retry["at"]
            log(f"{', '.join(STEP_LABELS.get(f, f) for f in failed)} failed: trying again at {_clock(at)}")
            values.update(daily_retry=retry, last_daily_interrupted=None)
        else:
            retry["gave_up"] = True
            values.update(daily_retry=retry, last_daily_run=today.isoformat(), last_daily_interrupted=None)
    else:
        values.update(last_daily_run=today.isoformat(), last_daily_interrupted=None, daily_retry=None)
    db.put_settings(conn, values)
    conn.commit()
    return summary


def failed_steps(summary: dict) -> list[str]:
    """The steps that failed outright (not single lookups within one)."""
    return [k for k, v in summary.items() if isinstance(v, dict) and "error" in v]


def _clock(when: datetime) -> str:
    """ "8:03 AM"."""
    return f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def retry_pending(settings: dict, today: Optional[date] = None) -> Optional[dict]:
    """Today's retry, when today's check failed and is to be tried again:
    ``{"at": iso local time, "attempt": n, "failed": [step names]}``."""
    day = (today or az_today()).isoformat()
    retry = settings.get("daily_retry") or {}
    if retry.get("date") == day and retry.get("at") and settings.get("last_daily_run") != day:
        return retry
    return None


def retry_status(settings: dict, today: Optional[date] = None) -> Optional[dict]:
    """Today's pending retry for the Lead Desk header: ``{"time": "8:03 AM",
    "failed": ["Justice Court calendar"], "attempt": n}``, or None."""
    retry = retry_pending(settings, today)
    if not retry:
        return None
    return {
        "time": _clock(datetime.fromisoformat(retry["at"])),
        "failed": [STEP_LABELS.get(f, f) for f in retry.get("failed") or []],
        "attempt": retry.get("attempt", 1),
    }


def retry_line(retry: dict) -> str:
    """ "today at 8:03 AM (retrying: Justice Court calendar failed)"."""
    what = ", ".join(STEP_LABELS.get(f, f) for f in retry.get("failed") or [])
    at = _clock(datetime.fromisoformat(retry["at"]))
    return f"today at {at} (retrying: {what} failed)" if what else f"today at {at} (retrying)"


def due(conn: Conn, now: Optional[datetime] = None, hour: int = 6) -> bool:
    """True when today's run hasn't finished yet and it's past ``hour`` local
    time (a run stopped part way by the pause doesn't count), and, after a
    failed run, its retry time has come (``RETRY_SLACK_MINUTES`` early is fine)."""
    now = now or az_now().replace(tzinfo=None)
    settings = db.get_settings(conn)
    if now.hour < hour or settings.get("last_daily_run") == now.date().isoformat():
        return False
    retry = retry_pending(settings, now.date())
    if retry:
        return now >= datetime.fromisoformat(retry["at"]) - timedelta(minutes=RETRY_SLACK_MINUTES)
    return True


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
    errors = failed_steps(summary)
    if errors:

        def failed_step(step: str) -> str:
            site = (summary.get(step) or {}).get("site")
            label = STEP_LABELS.get(step, step)
            return f"{label} ({site} couldn't be reached)" if site else label

        parts.append("failed: " + ", ".join(failed_step(e) for e in errors))
    if summary.get("retry_at"):
        parts.append(f"trying again at {_clock(datetime.fromisoformat(summary['retry_at']))}")
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


def when_line(when: Optional[datetime] = None) -> str:
    """ "Oct 3, 2026 2:57 PM", in Arizona time like every other time shown."""
    when = az_now(when)
    return f"{when:%b} {when.day}, {when.year} {_clock(when)}"


def main_log(
    summary: dict,
    out: Optional[TextIO] = None,
    public: bool = False,
    debug: bool = False,
    now: Optional[datetime] = None,
) -> None:
    """What ``leadgen daily`` prints: one plain line (Arizona time, counts,
    what failed and when it is tried again). ``debug`` adds the whole
    summary, error details included (cut to the error type when ``public``)."""
    out = out or sys.stdout
    print(when_line(now), describe(summary), file=out)
    if debug:
        print(json.dumps(counts_only(summary) if public else summary, default=str), file=out)
