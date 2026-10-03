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
import sys
from datetime import datetime, timedelta

from . import db
from .enrich import ParcelClient, enrich
from .geocode import CensusGeocoder
from .lookup import find_contacts, providers_from
from .outreach import merged_settings
from .sources import SOURCES
from .sources.pima_jp_calendar import CalendarClient
from .sources.pima_jp_case import update_cases
from .util import az_now, az_today, is_paused, now_iso

CALENDAR_DAYS_AHEAD = 30
CASE_PAGES_PER_RUN = 400  # about 10 minutes at the polite pace; the rest wait for tomorrow


STEP_LABELS = {
    "tucson_code_cases": "Tucson code cases", "evictions": "Justice Court calendar",
    "cases": "eviction case pages", "owners": "owners and landlords", "geocode": "map locations",
    "contacts": "landlord phones", "stale": "old leads",
}


def _step(summary, name, fn, log):
    log(f"checking {STEP_LABELS.get(name, name)}")
    try:
        summary[name] = fn()
    except Exception as e:  # one source being down shouldn't stop the rest
        summary[name] = {"error": f"{type(e).__name__}: {e}"}
        log(f"{name} failed: {type(e).__name__}: {e}")


def _upsert_all(conn, leads):
    counts = {"new": 0, "updated": 0}
    for lead in leads:
        counts[db.upsert(conn, lead)] += 1
    conn.commit()
    return counts


def geocode_new(conn, geocoder=None, limit=100):
    geocoder = geocoder or CensusGeocoder()
    counts = {"geocoded": 0, "not_found": 0}
    for row in db.needs_geocode(conn, limit=limit):
        try:
            result = geocoder.geocode(row["address"], row["city"], row["zip"])
        except Exception:
            continue  # network trouble: try again next run
        db.save_geocode(conn, row["id"], result)
        counts["geocoded" if result else "not_found"] += 1
        conn.commit()
    return counts


def run_daily(conn, stale_days=30, today=None, calendar=None, case_client=None,
              parcel_client=None, geocoder=None, providers=None, contact_limit=60,
              case_limit=None, days_ahead=CALENDAR_DAYS_AHEAD, code_cases=None, log=print):
    """Run every step and return a summary dict. Each step's failure is logged
    and recorded, and the next step still runs."""
    today = today or az_today()
    summary = {"started_at": now_iso()}
    settings = merged_settings(db.get_settings(conn))
    if is_paused(settings):
        # The kill switch: no court, county or lookup requests at all.
        log("paused: nothing checked (turn the pause off in Settings, or unset LEADDESK_PAUSED)")
        summary.update(paused=True, finished_at=now_iso())
        return summary

    since = (today - timedelta(days=30)).isoformat()
    _step(summary, "tucson_code_cases",
          lambda: _upsert_all(conn, (code_cases or SOURCES["tucson_code_cases"]()).fetch(
              since, today.isoformat())),
          log)

    def evictions():
        until = (today + timedelta(days=days_ahead)).isoformat()
        if calendar is not None:
            return _upsert_all(conn, calendar.fetch(today.isoformat(), until))
        client = CalendarClient()
        counts = _upsert_all(conn, SOURCES["pima_jp_calendar"]().fetch(
            today.isoformat(), until, client=client))
        counts["pages"] = client.pages
        return counts
    _step(summary, "evictions", evictions, log)

    _step(summary, "cases", lambda: update_cases(conn, case_client, limit=case_limit or CASE_PAGES_PER_RUN,
                                                 scheduled=True, log=log),
          log)
    _step(summary, "owners", lambda: enrich(conn, parcel_client or ParcelClient()), log)
    _step(summary, "geocode", lambda: geocode_new(conn, geocoder), log)

    def contacts():
        provs = providers if providers is not None else providers_from(settings, conn=conn)
        return find_contacts(conn, provs, limit=contact_limit, lead_types=("eviction",), log=log)
    _step(summary, "contacts", contacts, log)

    _step(summary, "stale", lambda: db.mark_stale(conn, stale_days, today=today), log)
    summary["finished_at"] = now_iso()
    db.put_settings(conn, {"last_daily_run": today.isoformat(), "last_daily_summary": summary})
    conn.commit()
    return summary


def due(conn, now=None, hour=6):
    """True when today's run hasn't happened yet and it's past ``hour`` local time."""
    now = now or az_now().replace(tzinfo=None)
    last = db.get_settings(conn).get("last_daily_run")
    return now.hour >= hour and last != now.date().isoformat()


def describe(summary):
    """One line for logs and the Lead Desk header."""
    if summary.get("paused"):
        return "paused: nothing was checked"

    def n(step, key):
        v = summary.get(step) or {}
        return v.get(key, 0) if isinstance(v, dict) else 0

    parts = [
        f"{n('evictions', 'new')} new evictions",
        f"{n('cases', 'with_notice')} with an eviction notice",
        f"{n('contacts', 'found')} landlord contacts found",
        f"{n('tucson_code_cases', 'new')} new code cases",
    ]
    errors = [k for k, v in summary.items() if isinstance(v, dict) and "error" in v]
    if errors:
        parts.append("failed: " + ", ".join(errors))
    return ", ".join(parts)


def counts_only(summary):
    """The summary with error messages cut to the error type, for public logs
    (an error message can quote a request that carries a landlord's name)."""
    out = {}
    for k, v in summary.items():
        if isinstance(v, dict) and "error" in v:
            v = {"error": str(v["error"]).split(":", 1)[0]}
        out[k] = v
    return out


def main_log(summary, out=sys.stdout, public=False):
    print(datetime.now().strftime("%Y-%m-%d %H:%M"), describe(summary), file=out)
    print(json.dumps(counts_only(summary) if public else summary, default=str), file=out)
