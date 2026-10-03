"""Find office phone numbers, emails and websites for the companies behind
the leads: apartment complexes, property managers, LLC and trust owners, and
eviction plaintiffs.

Only businesses are looked up. Owners who are people get their numbers from
Steve or a skip-tracing file (see contacts.py), not from this module.

Providers, tried in order:

``osm``      OpenStreetMap (Overpass API). Free, no key. Finds businesses
             mapped at or right next to the property (an apartment complex's
             leasing office, for example) with a phone, email or website tag.
             Data is ODbL: "(c) OpenStreetMap contributors".
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
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from . import config, db
from .contacts import clean_email, clean_phone
from .util import Conn, LeadRow, Log, StopCheck, az_now, is_multifamily, is_paused, utc_now

_log = logging.getLogger(__name__)

# Public Overpass servers, tried in order when one refuses or is overloaded.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_MAX_AGE_DAYS = 30
# Google's free monthly allowance for Text Search Enterprise, the search tier
# that returns phone numbers (1,000 a month as of October 2026).
GOOGLE_MONTHLY_LIMIT = 1000
# Most Google searches in one day (Thomas's cap, October 2026).
GOOGLE_DAILY_LIMIT = 30
TUCSON = (32.2226, -110.9747)

_NAME_NOISE = {
    "LLC",
    "L",
    "C",
    "INC",
    "CORP",
    "CO",
    "COMPANY",
    "LP",
    "LLLP",
    "LTD",
    "THE",
    "OF",
    "AND",
    "TR",
    "TRS",
    "TRUST",
    "TRUSTEE",
    "AZ",
    "ARIZONA",
    "TUCSON",
    "&",
}


@dataclass
class Contact:
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    source: Optional[str] = None
    matched_name: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def empty(self) -> bool:
        return not (self.phone or self.email or self.website)


def name_tokens(name: Optional[str]) -> set[str]:
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    return {w for w in words if w not in _NAME_NOISE and len(w) > 1}


def names_match(a: Optional[str], b: Optional[str]) -> bool:
    """True when two business names plausibly refer to the same company."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small & big) / len(small) >= 0.6


_BUSINESS_WORDS = {
    "LLC",
    "LLLP",
    "LP",
    "LTD",
    "INC",
    "CORP",
    "CORPORATION",
    "CO",
    "COMPANY",
    "GROUP",
    "PROPERTIES",
    "PROPERTY",
    "HOLDINGS",
    "INVESTMENTS",
    "INVESTMENT",
    "HOMES",
    "REALTY",
    "REAL",
    "MANAGEMENT",
    "MGMT",
    "RESIDENTIAL",
    "COMMUNITIES",
    "APARTMENTS",
    "APARTMENT",
    "RENTALS",
    "RENTAL",
    "CAPITAL",
    "PARTNERS",
    "PARTNERSHIP",
    "VENTURES",
    "FUND",
    "ASSOCIATES",
    "ASSN",
    "ASSOCIATION",
    "BANK",
    "LENDING",
    "DEVELOPMENT",
    "ENTERPRISES",
    "HOUSING",
    "VILLAGE",
}
_SPLIT_RE = re.compile(r"\s*(?:\bATTN\b:?|\bC/O\b|%)\s*", re.I)


def split_owner(name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'SUMMIT RIDGE AZ LLC ATTN: DASMEN RESIDENTIAL' ->
    ('SUMMIT RIDGE AZ LLC', 'DASMEN RESIDENTIAL')."""
    parts = _SPLIT_RE.split((name or "").strip(), maxsplit=1)
    owner = parts[0].strip(" ,") or None
    attn = parts[1].strip(" ,") if len(parts) > 1 else None
    return owner, attn or None


def is_business(name: Optional[str]) -> bool:
    """A company, not a person or a family/living trust."""
    words = set(re.findall(r"[A-Z]+", (name or "").upper()))
    return bool(words & _BUSINESS_WORDS)


def lookup_targets(lead: LeadRow) -> tuple[list[str], bool]:
    """Business names to search for this lead, best first, and whether to
    look for a business at the property itself (apartment leasing office)."""
    names: list[str] = []
    for raw in (lead["plaintiff"], lead["owner_name"]):
        owner, attn = split_owner(raw)
        # "ATTN:" usually names the management company: the one to call.
        for n in (attn, owner):
            if n and is_business(n) and n.upper() not in (x.upper() for x in names):
                names.append(n)
    site = bool(lead["address"]) and (is_multifamily(lead["property_use"]) or bool(names))
    return names, site


# ---------------------------------------------------------------- providers --


class ProviderUnavailable(Exception):
    pass


# The lookup services in words, for messages.
PROVIDER_LABELS = {"osm": "OpenStreetMap", "google": "Google Places"}


class LimitReached(ProviderUnavailable):
    """The Google daily or monthly lookup limit is used up: not a failure."""


def _is_key_problem(prov: Any, error: BaseException) -> bool:
    """True when Google refused the request itself: a missing, wrong or
    restricted API key (or billing off), which Steve fixes in Settings."""
    response = getattr(error, "response", None)
    return prov.name == "google" and response is not None and response.status_code in (400, 401, 403)


class OsmProvider:
    name = "osm"
    # A server that fails this many times in a row is dropped for the rest of
    # the run; once every server is dropped, OSM lookups are skipped instead of
    # waiting on each company. Worst case is servers x max_failures x timeout.
    max_failures = 2

    def __init__(self, session: Any = None, radius_m: int = 80, delay: float = 1.0, timeout: float = 15) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.radius = radius_m
        self.delay = delay
        self.timeout = timeout
        self.strikes: dict[str, int] = {}
        self.urls = list(OVERPASS_URLS)

    def find(self, lead: LeadRow, business_name: Optional[str]) -> Optional[Contact]:
        if lead["lat"] is None or lead["lon"] is None:
            return None
        if not self.urls:
            raise ProviderUnavailable("Overpass servers not responding; skipped for this run")
        q = f"""
        [out:json][timeout:10];
        (
          nwr(around:{self.radius},{lead["lat"]},{lead["lon"]})["phone"];
          nwr(around:{self.radius},{lead["lat"]},{lead["lon"]})["contact:phone"];
          nwr(around:{self.radius},{lead["lat"]},{lead["lon"]})["email"];
          nwr(around:{self.radius},{lead["lat"]},{lead["lon"]})["contact:email"];
          nwr(around:{self.radius},{lead["lat"]},{lead["lon"]})["website"];
        );
        out tags center 20;
        """
        last_error: Exception = ProviderUnavailable("no Overpass server to ask")
        for url in list(self.urls):
            try:
                resp = self.session.post(
                    url, data={"data": q}, timeout=self.timeout, headers={"Accept": "application/json"}
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                last_error = e
                self.strikes[url] = self.strikes.get(url, 0) + 1
                if self.strikes[url] >= self.max_failures:
                    self.urls.remove(url)
                continue
            self.strikes[url] = 0
            if self.urls[0] != url:  # ask the server that answered first next time
                self.urls.remove(url)
                self.urls.insert(0, url)
            time.sleep(self.delay)
            return pick_osm(resp.json().get("elements") or [], business_name)
        raise last_error


def pick_osm(elements: list[dict], business_name: Optional[str]) -> Optional[Contact]:
    best: Optional[dict] = None
    best_rank: tuple[int, ...] = (0,)
    for el in elements:
        t = el.get("tags") or {}
        name = t.get("name") or t.get("operator") or ""
        housing = (
            t.get("building") in ("apartments", "residential")
            or t.get("landuse") == "residential"
            or t.get("office") in ("property_management", "estate_agent")
            or "apartment" in name.lower()
        )
        matched = bool(business_name) and (
            names_match(name, business_name) or names_match(t.get("operator"), business_name)
        )
        if not (matched or housing):
            continue
        # Name match beats "some housing nearby"; then prefer the entry with
        # more ways to reach someone.
        reach = sum(bool(t.get(k)) for k in ("phone", "contact:phone", "email", "contact:email"))
        rank = (2 if matched else 1, int(housing), reach)
        if rank > best_rank:
            best_rank, best = rank, t
    if not best:
        return None
    return Contact(
        phone=clean_phone(best.get("phone") or best.get("contact:phone")),
        email=clean_email(best.get("email") or best.get("contact:email")),
        website=best.get("website") or best.get("contact:website"),
        source="osm",
        matched_name=best.get("name") or best.get("operator"),
    )


class GoogleBudget:
    """Counts Google searches per day and per calendar month (Arizona time)
    and refuses more than ``daily`` a day or ``limit`` a month, so the lookup
    stays inside the free allowance unless the limits are raised in Settings.

    A limit of 0 allows no searches; ``None`` means no limit. Each search is
    reserved with one conditional UPDATE per counter, so two lookups running
    at once (the daily check and the button) can't go over a limit together.
    """

    LEGACY_KEY = "google_usage"  # where older versions kept the counts, in settings

    def __init__(
        self, conn: Conn, limit: Optional[int] = GOOGLE_MONTHLY_LIMIT, daily: Optional[int] = GOOGLE_DAILY_LIMIT
    ) -> None:
        self.conn = conn
        self.limit = limit
        self.daily = daily

    def _keys(self, now: Optional[datetime] = None) -> tuple[str, str, datetime]:
        local = az_now(now)
        return f"google:month:{local:%Y-%m}", f"google:day:{local:%Y-%m-%d}", local

    def _count(self, key: str) -> Optional[int]:
        row = self.conn.execute("SELECT n FROM counters WHERE key = ?", (key,)).fetchone()
        return row["n"] if row else None

    def _legacy(self, local: datetime) -> tuple[int, int]:
        usage = db.get_settings(self.conn).get(self.LEGACY_KEY) or {}
        month = usage.get("count", 0) if usage.get("month") == f"{local:%Y-%m}" else 0
        day = usage.get("day_count", 0) if usage.get("day") == f"{local:%Y-%m-%d}" else 0
        return month, day

    def _usage(self, now: Optional[datetime] = None) -> tuple[int, int]:
        month_key, day_key, local = self._keys(now)
        month, day = self._count(month_key), self._count(day_key)
        if month is None or day is None:
            old_month, old_day = self._legacy(local)
            month = old_month if month is None else month
            day = old_day if day is None else day
        return month, day

    def used(self, now: Optional[datetime] = None) -> int:
        return self._usage(now)[0]

    def used_today(self, now: Optional[datetime] = None) -> int:
        return self._usage(now)[1]

    def blocked(self, now: Optional[datetime] = None) -> Optional[str]:
        """Why no more searches are allowed right now, or None."""
        count, today = self._usage(now)
        if self.daily == 0 or self.limit == 0:
            return "Google lookups are set to 0 in Settings"
        if self.daily is not None and today >= self.daily:
            return f"daily limit of {self.daily} Google lookups reached; more tomorrow"
        if self.limit is not None and count >= self.limit:
            return f"monthly limit of {self.limit} Google lookups reached; raise it in Settings"
        return None

    def _reserve(self, key: str, limit: Optional[int]) -> bool:
        if limit is None:
            cur = self.conn.execute("UPDATE counters SET n = n + 1 WHERE key = ?", (key,))
        else:
            cur = self.conn.execute("UPDATE counters SET n = n + 1 WHERE key = ? AND n < ?", (key, limit))
        self.conn.commit()
        return cur.rowcount == 1

    def take(self, now: Optional[datetime] = None) -> bool:
        """Reserve one search. False when a limit is reached."""
        month_key, day_key, local = self._keys(now)
        if self._count(month_key) is None or self._count(day_key) is None:
            old_month, old_day = self._legacy(local)
            for key, start in ((month_key, old_month), (day_key, old_day)):
                self.conn.execute(
                    "INSERT INTO counters (key, n) VALUES (?, ?) ON CONFLICT(key) DO NOTHING", (key, start)
                )
            self.conn.commit()
        if not self._reserve(day_key, self.daily):
            return False
        if not self._reserve(month_key, self.limit):
            self.conn.execute("UPDATE counters SET n = n - 1 WHERE key = ?", (day_key,))
            self.conn.commit()
            return False
        return True


class GooglePlacesProvider:
    name = "google"
    # Google searches are capped (30 a day), so spend them only on eviction
    # cases with an eviction notice filed; find_contacts reaches those newest
    # first. The free providers still try every lead.
    only_eviction_notices = True

    def __init__(self, api_key: str, session: Any = None, budget: Optional[GoogleBudget] = None) -> None:
        self.key = api_key
        self.session = session or requests.Session()
        self.budget = budget

    def _search(self, text: str, lat: Any = None, lon: Any = None) -> list[dict]:
        if self.budget is not None and not self.budget.take():
            raise LimitReached(self.budget.blocked() or "Google lookup limit reached")
        lat, lon = (lat, lon) if lat is not None else TUCSON
        resp = self.session.post(
            PLACES_URL,
            json={
                "textQuery": text,
                "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": 50000.0}},
                "maxResultCount": 5,
            },
            headers={
                "X-Goog-Api-Key": self.key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.nationalPhoneNumber,"
                "places.websiteUri,places.formattedAddress",
            },
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("places") or []

    def find(self, lead: LeadRow, business_name: Optional[str]) -> Optional[Contact]:
        if business_name:
            for p in self._search(f"{business_name} Tucson AZ", lead["lat"], lead["lon"]):
                name = (p.get("displayName") or {}).get("text", "")
                if names_match(name, business_name):
                    return _places_contact(p)
        if lead["address"] and is_multifamily(lead["property_use"]):
            number = (re.match(r"\s*(\d+)", lead["address"]) or [None, None])[1]
            for p in self._search(f"apartments {lead['address']} Tucson AZ", lead["lat"], lead["lon"]):
                if number and number in (p.get("formattedAddress") or ""):
                    return _places_contact(p)
        return None


def _places_contact(p: dict) -> Contact:
    return Contact(
        phone=clean_phone(p.get("nationalPhoneNumber")),
        website=p.get("websiteUri"),
        source="google",
        matched_name=(p.get("displayName") or {}).get("text"),
        extra={"place_id": p.get("id")},
    )


# ------------------------------------------------------------ website scan --

_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b([2-9]\d{2})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SKIP_EMAIL = re.compile(r"(example\.|sentry|wixpress|\.png|\.jpg|noreply|no-reply)", re.I)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.tels: list[str] = []
        self.mails: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if href.lower().startswith("tel:"):
            self.tels.append(href[4:])
        elif href.lower().startswith("mailto:"):
            self.mails.append(href[7:].split("?")[0])
        else:
            self.links.append(href)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def scan_html(html: str, base_url: str) -> tuple[list[str], list[str], list[str]]:
    """Return (phones, emails, contact_page_urls) found in one page."""
    p = _LinkParser()
    p.feed(html)
    text = " ".join(p.text)
    phones = [clean_phone(t) for t in p.tels] + [clean_phone("".join(m.groups())) for m in _PHONE_RE.finditer(text)]
    emails = [clean_email(m) for m in p.mails] + [clean_email(m) for m in _EMAIL_RE.findall(text)]
    host = urlparse(base_url).netloc
    contact_pages: list[str] = []
    for href in p.links:
        url = urljoin(base_url, href)
        if urlparse(url).netloc == host and re.search(r"contact|about|office|leasing", url, re.I):
            if url not in contact_pages:
                contact_pages.append(url)
    found_phones = [x for x in dict.fromkeys(phones) if x]
    found_emails = [x for x in dict.fromkeys(emails) if x and not _SKIP_EMAIL.search(x)]
    return found_phones, found_emails, contact_pages[:3]


class WebsiteScanner:
    def __init__(self, session: Any = None) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    def _allowed(self, url: str) -> bool:
        root = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        rp = RobotFileParser()
        try:
            resp = self.session.get(root + "/robots.txt", timeout=10)
            if resp.status_code >= 400:
                return True
            rp.parse(resp.text.splitlines())
            return rp.can_fetch(config.USER_AGENT, url)
        except requests.RequestException:
            return True

    def scan(self, url: Optional[str]) -> Optional[Contact]:
        if not url:
            return None
        if not urlparse(url).scheme:
            url = "https://" + url
        if not self._allowed(url):
            return None
        phones: list[str] = []
        emails: list[str] = []
        queue = [url]
        seen: set[str] = set()
        while queue and len(seen) < 4:
            page = queue.pop(0)
            if page in seen:
                continue
            seen.add(page)
            try:
                resp = self.session.get(page, timeout=15)
                if resp.status_code >= 400 or "html" not in resp.headers.get("Content-Type", "html"):
                    continue
            except requests.RequestException:
                continue
            p, e, more = scan_html(resp.text, page)
            phones += p
            emails += e
            queue += [m for m in more if m not in seen]
        phones = list(dict.fromkeys(phones))
        emails = list(dict.fromkeys(emails))
        if not (phones or emails):
            return None
        return Contact(
            phone=phones[0] if phones else None, email=emails[0] if emails else None, website=url, source="website"
        )


# ------------------------------------------------------------------ runner --


def _reach(c: Contact) -> tuple[bool, bool, bool]:
    return (bool(c.phone), bool(c.email), bool(c.website))


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
    """Look up phone/email/website for business owners and landlords.

    A lookup that fails for a passing reason is tried again within the run
    (``retry_delays``, default ``RETRY_DELAYS``) before the lead waits for
    the next run.

    One lookup per company: every lead with the same owner/landlord name gets
    the result. Values entered by hand (contact_source = 'manual') are never
    replaced. ``progress(done, total)`` is called after each lead and
    ``should_stop()`` before each (True stops early). Returns counts.
    """
    scanner = scanner if scanner is not None else WebsiteScanner()
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
                 event_date DESC, id DESC
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
                    if c and not c.empty() and (contact is None or _reach(c) > _reach(contact)):
                        contact = c
                    if contact and contact.phone and contact.email:
                        break
                if contact and contact.phone:
                    break
            if contact and contact.website and not (contact.phone and contact.email):
                try:
                    w = scanner.scan(contact.website) if scanner else None
                except Exception as e:  # the company's site down: keep what the provider found
                    _log.warning("website scan failed for lead %s: %s", lead["id"], type(e).__name__)
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
