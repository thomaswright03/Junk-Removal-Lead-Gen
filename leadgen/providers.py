"""The phone lookup's providers: OpenStreetMap (Overpass and Nominatim),
Google Places (with its daily and monthly limits) and the business's own
website. See lookup.py for the order they are tried in and what each finds."""

import logging
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from . import config, db
from .business import NAME_NOISE, Contact, contact_reach, name_tokens, names_match
from .contacts import clean_email, clean_phone
from .util import Conn, LeadRow, az_now, is_multifamily

_log = logging.getLogger(__name__)

# Public Overpass servers, tried in order when one refuses or is overloaded.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# OpenStreetMap's search by name, and the area it searches (Tucson and the
# rest of eastern Pima County: west, south, east, north).
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
TUCSON_AREA = (-111.6, 31.8, -110.5, 32.6)
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_MAX_AGE_DAYS = 30
# Google's free monthly allowance for Text Search Enterprise, the search tier
# that returns phone numbers (1,000 a month as of October 2026).
GOOGLE_MONTHLY_LIMIT = 1000
# Most Google searches in one day (Thomas's cap, October 2026).
GOOGLE_DAILY_LIMIT = 30
TUCSON = (32.2226, -110.9747)


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
        self.search_strikes = 0  # name searches failed in a row

    def find(self, lead: LeadRow, business_name: Optional[str]) -> Optional[Contact]:
        """A business at the property (when it is on the map), else one
        with the company's name in the Tucson area."""
        found = None
        if lead["lat"] is not None and lead["lon"] is not None:
            found = self._nearby(lead, business_name)
        if found is None and business_name:
            found = self._by_name(business_name)
        return found

    def _by_name(self, business_name: str) -> Optional[Contact]:
        words = [w for w in re.findall(r"[A-Z0-9']+", business_name.upper()) if w not in NAME_NOISE]
        if len(" ".join(words)) < 4:
            return None  # nothing distinctive to search for
        if self.search_strikes >= self.max_failures:
            raise ProviderUnavailable("OpenStreetMap search not responding; skipped for this run")
        west, south, east, north = TUCSON_AREA
        try:
            params: dict[str, Any] = {
                "q": " ".join(words),
                "format": "jsonv2",
                "extratags": 1,
                "limit": 5,
                "viewbox": f"{west},{north},{east},{south}",
                "bounded": 1,
            }
            resp = self.session.get(
                NOMINATIM_URL, params=params, timeout=self.timeout, headers={"Accept": "application/json"}
            )
            resp.raise_for_status()
        except requests.RequestException:
            self.search_strikes += 1
            raise
        self.search_strikes = 0
        time.sleep(self.delay)
        best: Optional[Contact] = None
        wanted = name_tokens(business_name)
        for place in resp.json() or []:
            name = place.get("name") or ""
            tags = place.get("extratags") or {}
            # Nothing ties a place found by name to the lead but the name,
            # so every word of the landlord's name must be in it.
            if not wanted or not wanted <= name_tokens(name):
                continue
            c = Contact(
                phone=clean_phone(tags.get("phone") or tags.get("contact:phone")),
                email=clean_email(tags.get("email") or tags.get("contact:email")),
                website=tags.get("website") or tags.get("contact:website") or tags.get("url"),
                source="osm",
                matched_name=name,
            )
            if not c.empty() and (best is None or contact_reach(c) > contact_reach(best)):
                best = c
        return best

    def _nearby(self, lead: LeadRow, business_name: Optional[str]) -> Optional[Contact]:
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
