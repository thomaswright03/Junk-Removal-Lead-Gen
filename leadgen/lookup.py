"""Find office phone numbers, emails and websites for the companies behind
the leads: apartment complexes, property managers, LLC and trust owners, and
eviction plaintiffs.

Only businesses are looked up. Owners who are people get their numbers from
Steve or a skip-tracing file (see contacts.py), not from this module.

Providers, tried in order:

``osm``      OpenStreetMap. Free, no key. Finds businesses mapped at or
             right next to the property (an apartment complex's leasing
             office, for example) with a phone, email or website tag
             (Overpass API), and, for a lead with no mapped property (most
             evictions), a business of the same name in the Tucson area
             (OpenStreetMap's own search, Nominatim, at most one search a
             second as its usage policy asks). Few landlord companies are on
             OpenStreetMap, so this finds some apartment complexes and
             management offices, not most landlords. Data is ODbL:
             "(c) OpenStreetMap contributors".
``google``   Google Places Text Search. Needs a Google Maps Platform API key
             (GOOGLE_PLACES_API_KEY, or the Lead Desk settings). Searches the
             owner's name around Tucson, and "apartments at <address>" for
             apartment properties. Google's terms limit how long results may be
             kept, so Google-sourced contacts are looked up again after 30
             days.
``website``  Reads the business's own website (found by either provider): the
             home page and up to three contact pages, for tel: / mailto: links
             and phone numbers. Honors robots.txt.
"""

import logging
import os
import time
from datetime import timedelta
from typing import Any, Callable, Optional

import requests

from .business import Contact, contact_reach, lookup_targets
from .providers import (
    GOOGLE_DAILY_LIMIT,
    GOOGLE_MAX_AGE_DAYS,
    GOOGLE_MONTHLY_LIMIT,
    PROVIDER_LABELS,
    GoogleBudget,
    GooglePlacesProvider,
    LimitReached,
    OsmProvider,
    ProviderUnavailable,
    WebsiteScanner,
    _is_key_problem,
)
from .util import Conn, LeadRow, Log, StopCheck, is_paused, utc_now

_log = logging.getLogger(__name__)

# ------------------------------------------------------------------ runner --


def google_key_in_use(settings: Optional[dict]) -> str:
    """The Google key lookups would use: the environment's, else Settings'."""
    return os.environ.get("GOOGLE_PLACES_API_KEY") or (settings or {}).get("google_places_api_key") or ""


def providers_from(settings: Optional[dict] = None, google_key: Optional[str] = None, conn: Conn = None) -> list:
    """OpenStreetMap, plus Google when a key is set and Google is turned on in
    Settings. None at all while Lead Desk is paused. With ``conn``, Google
    searches are counted against the daily and monthly limits in settings."""
    settings = settings or {}
    if is_paused(settings):
        return []
    key = google_key or google_key_in_use(settings)
    out: list[Any] = [OsmProvider()]
    if key and settings.get("google_enabled", True) is not False:
        limit = settings.get("google_monthly_limit", GOOGLE_MONTHLY_LIMIT)
        daily = settings.get("google_daily_limit", GOOGLE_DAILY_LIMIT)
        budget = (
            GoogleBudget(conn, None if limit is None else int(limit), None if daily is None else int(daily))
            if conn is not None
            else None
        )
        out.append(GooglePlacesProvider(key, budget=budget))
    return out


# A lookup that fails with a network error, a timeout or a busy server (429,
# 5xx) is tried again after these pauses (seconds) before the lead is left
# for the next run. Once a provider has failed every try for this many
# companies in a run it is down, not blipping: no more retries for it.
RETRY_DELAYS = (2.0, 5.0)
RETRY_GIVE_UP_AFTER = 2


def _is_transient(error: BaseException) -> bool:
    """A failure worth trying again in a moment: the connection or a busy
    server, not a refused key or a bad request."""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(error, "response", None)
    return response is not None and (response.status_code == 429 or response.status_code >= 500)


def _find_with_retry(
    prov: Any,
    lead: LeadRow,
    name: Optional[str],
    delays: tuple[float, ...],
    should_stop: StopCheck = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> Optional[Contact]:
    """``prov.find``, tried again after each of ``delays`` while the failure
    is transient (see ``_is_transient``) and Lead Desk isn't paused."""
    for delay in (*delays, None):
        try:
            return prov.find(lead, name)
        except Exception as e:
            if delay is None or isinstance(e, ProviderUnavailable) or not _is_transient(e):
                raise
            if should_stop and should_stop():
                raise
            _log.info("%s lookup failed (%s); trying again in %ss", prov.name, type(e).__name__, delay)
            sleep(delay)
    return None  # not reached: the last try returns or raises


def _has_notice(lead: LeadRow) -> bool:
    return lead["lead_type"] == "eviction" and bool(lead["eviction_notice"])


def find_contacts(
    conn: Conn,
    providers: list,
    scanner: Any = None,
    limit: Optional[int] = None,
    refresh: bool = False,
    log: Log = print,
    lead_types: Optional[tuple[str, ...]] = None,
    progress: Optional[Callable[[int, int], Any]] = None,
    should_stop: StopCheck = None,
    retry_delays: Optional[tuple[float, ...]] = None,
) -> dict:
    """Look up phone/email/website for business owners and landlords, the
    best-ranked leads first (the order the lead list shows them in).

    A lookup that fails for a passing reason is tried again within the run
    (``retry_delays``, default ``RETRY_DELAYS``) before the lead waits for
    the next run.

    One lookup per company: every lead with the same owner/landlord name gets
    the result. Values entered by hand (contact_source = 'manual') are never
    replaced. ``progress(done, total)`` is called after each lead and
    ``should_stop()`` before each (True stops early). Returns counts.
    """
    scanner = scanner if scanner is not None else WebsiteScanner()
    # In the lead list's order (best first: writs, judgments, then priority),
    # so a run that stops at its limit has looked up the top of the list.
    from . import db, leadlist, outreach

    leadlist.refresh_ranking(conn, outreach.merged_settings(db.get_settings(conn)))
    stale_google = (utc_now() - timedelta(days=GOOGLE_MAX_AGE_DAYS)).isoformat()
    due = (
        ""
        if refresh
        else ("AND (contact_checked_at IS NULL OR (contact_source LIKE 'google%' AND contact_checked_at < ?))")
    )
    rows = conn.execute(
        f"""
        SELECT * FROM leads
        WHERE duplicate_of IS NULL AND status NOT IN ('stale', 'skip', 'lost', 'won')
          AND COALESCE(contact_source, '') != 'manual'
          {due}
        ORDER BY CASE WHEN lead_type = 'eviction' AND eviction_notice = 1 THEN 0
                      WHEN lead_type = 'eviction' THEN 1 ELSE 2 END,
                 COALESCE(stage_rank, 0) DESC, COALESCE(rank_score, 0) DESC,
                 COALESCE(rank_latest, '') DESC, id DESC
        """,
        () if refresh else (stale_google,),
    ).fetchall()
    if lead_types:
        rows = [r for r in rows if r["lead_type"] in lead_types]
    counts: dict[str, Any] = {"checked": 0, "found": 0, "not_found": 0, "skipped_people": 0, "errors": 0}
    if not providers:  # paused, or nothing to look up with
        return counts
    done: dict[str, tuple[Optional[Contact], bool]] = {}  # business name -> (Contact or None, failed)
    gave_up: dict[str, int] = {}  # provider -> companies it failed every retry for
    # Provider -> lookups that failed; and the providers that stopped
    # answering this run (skipped from then on). Reported once each at the end.
    failures: dict[str, int] = {}
    down: set[str] = set()
    retry_delays = RETRY_DELAYS if retry_delays is None else retry_delays
    for i, lead in enumerate(rows):
        if progress and i:
            progress(i, len(rows))
        if limit and counts["checked"] >= limit:
            break
        if should_stop and should_stop():
            counts["stopped_early"] = True
            break
        names, site = lookup_targets(lead)
        if not names and not site:
            counts["skipped_people"] += 1
            continue
        key = names[0].upper() if names else f"site:{lead['id']}"
        if key in done:
            contact, failed = done[key]
        else:
            counts["checked"] += 1
            contact, failed = None, False
            candidates: list[Optional[str]] = [*names] or [None]
            for name in candidates:
                for prov in providers:
                    if getattr(prov, "only_eviction_notices", False) and not _has_notice(lead):
                        continue
                    if prov.name in down:
                        counts["errors"] += 1
                        failures[prov.name] = failures.get(prov.name, 0) + 1
                        failed = True
                        continue  # not answering this run: don't wait on it again
                    delays = retry_delays if gave_up.get(prov.name, 0) < RETRY_GIVE_UP_AFTER else ()
                    try:
                        c = _find_with_retry(prov, lead, name, delays, should_stop)
                    except LimitReached:
                        counts["over_limit"] = counts.get("over_limit", 0) + 1
                        failed = True
                        continue
                    except Exception as e:  # one provider failing shouldn't stop the run
                        if delays and _is_transient(e):
                            gave_up[prov.name] = gave_up.get(prov.name, 0) + 1
                        counts["errors"] += 1
                        failures[prov.name] = failures.get(prov.name, 0) + 1
                        if isinstance(e, ProviderUnavailable):
                            down.add(prov.name)
                        if _is_key_problem(prov, e):
                            counts["error_cause"] = "google_key"
                        failed = True
                        _log.debug("%s lookup failed for lead %s: %s: %s", prov.name, lead["id"], type(e).__name__, e)
                        continue
                    if c and not c.empty() and (contact is None or contact_reach(c) > contact_reach(contact)):
                        contact = c
                    if contact and contact.phone and contact.email:
                        break
                if contact and contact.phone:
                    break
            if contact and contact.website and not (contact.phone and contact.email):
                try:
                    w = scanner.scan(contact.website) if scanner else None
                except Exception as e:  # the company's site down: keep what the provider found
                    _log.debug("website scan failed for lead %s: %s", lead["id"], type(e).__name__)
                    counts["website_errors"] = counts.get("website_errors", 0) + 1
                    w = None
                if w:
                    contact.phone = contact.phone or w.phone
                    contact.email = contact.email or w.email
                    contact.source = f"{contact.source}+website"
            done[key] = (contact, failed)
        now = utc_now().isoformat()
        if failed and not (contact and contact.phone):
            continue  # a provider was down or over its limit: try again next run
        if contact and not contact.empty():
            counts["found"] += 1
            conn.execute(
                "UPDATE leads SET owner_phone = COALESCE(?, owner_phone), "
                "owner_email = COALESCE(?, owner_email), owner_website = COALESCE(?, owner_website), "
                "contact_source = ?, contact_name = ?, contact_checked_at = ? WHERE id = ?",
                (contact.phone, contact.email, contact.website, contact.source, contact.matched_name, now, lead["id"]),
            )
        else:
            counts["not_found"] += 1
            conn.execute("UPDATE leads SET contact_checked_at = ? WHERE id = ?", (now, lead["id"]))
        conn.commit()
    for name, n in failures.items():
        label = PROVIDER_LABELS.get(name, name)
        lookups = f"{n} lookup{'' if n == 1 else 's'}"
        if name in down:
            log(f"  {label} wasn't responding: {lookups} skipped or failed this run; they're tried again next run")
        elif name == "google" and counts.get("error_cause") == "google_key":
            log(f"  {label} refused the key: {lookups} failed; check the Google key in Settings")
        else:
            log(f"  {label}: {lookups} failed; they're tried again next run")
    return counts
