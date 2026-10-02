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

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from . import config
from .contacts import clean_email, clean_phone
from .enrich import is_entity

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_MAX_AGE_DAYS = 30
TUCSON = (32.2226, -110.9747)

_NAME_NOISE = {
    "LLC", "L", "C", "INC", "CORP", "CO", "COMPANY", "LP", "LLLP", "LTD", "THE", "OF", "AND",
    "TR", "TRS", "TRUST", "TRUSTEE", "AZ", "ARIZONA", "TUCSON", "&",
}


@dataclass
class Contact:
    phone: str = None
    email: str = None
    website: str = None
    source: str = None
    matched_name: str = None
    extra: dict = field(default_factory=dict)

    def empty(self):
        return not (self.phone or self.email or self.website)


def name_tokens(name):
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    return {w for w in words if w not in _NAME_NOISE and len(w) > 1}


def names_match(a, b):
    """True when two business names plausibly refer to the same company."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small & big) / len(small) >= 0.6


def is_multifamily(use):
    u = (use or "").upper()
    return any(k in u for k in ("APART", "MULTI", "MFR", "CONDO", "TOWNHOUSE", "MOBILE HOME PARK"))


def lookup_targets(lead):
    """What to search for this lead: (business name or None, search the site?)."""
    name = None
    if lead["plaintiff"]:
        name = lead["plaintiff"]
    elif lead["owner_name"] and (lead["owner_entity"] or is_entity(lead["owner_name"])):
        name = lead["owner_name"]
    site = bool(lead["address"]) and (is_multifamily(lead["property_use"]) or name is not None)
    return name, site


# ---------------------------------------------------------------- providers --

class OsmProvider:
    name = "osm"

    def __init__(self, session=None, radius_m=80, delay=1.0):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", config.USER_AGENT)
        self.radius = radius_m
        self.delay = delay

    def find(self, lead, business_name):
        if lead["lat"] is None or lead["lon"] is None:
            return None
        q = f"""
        [out:json][timeout:25];
        (
          nwr(around:{self.radius},{lead['lat']},{lead['lon']})["phone"];
          nwr(around:{self.radius},{lead['lat']},{lead['lon']})["contact:phone"];
          nwr(around:{self.radius},{lead['lat']},{lead['lon']})["email"];
          nwr(around:{self.radius},{lead['lat']},{lead['lon']})["contact:email"];
          nwr(around:{self.radius},{lead['lat']},{lead['lon']})["website"];
        );
        out tags center 20;
        """
        resp = self.session.post(OVERPASS_URL, data={"data": q}, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        time.sleep(self.delay)
        return pick_osm(resp.json().get("elements") or [], business_name)


def pick_osm(elements, business_name):
    best, best_rank = None, (0,)
    for el in elements:
        t = el.get("tags") or {}
        name = t.get("name") or t.get("operator") or ""
        housing = (t.get("building") in ("apartments", "residential")
                   or t.get("landuse") == "residential"
                   or t.get("office") in ("property_management", "estate_agent")
                   or "apartment" in name.lower())
        matched = bool(business_name) and (names_match(name, business_name)
                                           or names_match(t.get("operator"), business_name))
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


class GooglePlacesProvider:
    name = "google"

    def __init__(self, api_key, session=None):
        self.key = api_key
        self.session = session or requests.Session()

    def _search(self, text, lat=None, lon=None):
        lat, lon = (lat, lon) if lat is not None else TUCSON
        resp = self.session.post(
            PLACES_URL,
            json={
                "textQuery": text,
                "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon},
                                            "radius": 50000.0}},
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

    def find(self, lead, business_name):
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


def _places_contact(p):
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
    def __init__(self):
        super().__init__()
        self.links, self.tels, self.mails, self.text = [], [], [], []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if href.lower().startswith("tel:"):
            self.tels.append(href[4:])
        elif href.lower().startswith("mailto:"):
            self.mails.append(href[7:].split("?")[0])
        else:
            self.links.append(href)

    def handle_data(self, data):
        self.text.append(data)


def scan_html(html, base_url):
    """Return (phones, emails, contact_page_urls) found in one page."""
    p = _LinkParser()
    p.feed(html)
    text = " ".join(p.text)
    phones = [clean_phone(t) for t in p.tels] + [
        clean_phone("".join(m.groups())) for m in _PHONE_RE.finditer(text)]
    emails = [clean_email(m) for m in p.mails] + [
        clean_email(m) for m in _EMAIL_RE.findall(text)]
    host = urlparse(base_url).netloc
    contact_pages = []
    for href in p.links:
        url = urljoin(base_url, href)
        if urlparse(url).netloc == host and re.search(r"contact|about|office|leasing", url, re.I):
            if url not in contact_pages:
                contact_pages.append(url)
    phones = [x for x in dict.fromkeys(phones) if x]
    emails = [x for x in dict.fromkeys(emails) if x and not _SKIP_EMAIL.search(x)]
    return phones, emails, contact_pages[:3]


class WebsiteScanner:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", config.USER_AGENT)

    def _allowed(self, url):
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

    def scan(self, url):
        if not url:
            return None
        if not urlparse(url).scheme:
            url = "https://" + url
        if not self._allowed(url):
            return None
        phones, emails, queue = [], [], [url]
        seen = set()
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
        return Contact(phone=phones[0] if phones else None, email=emails[0] if emails else None,
                       website=url, source="website")


# ------------------------------------------------------------------ runner --

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def providers_from(settings=None, google_key=None):
    import os

    key = google_key or os.environ.get("GOOGLE_PLACES_API_KEY") or (settings or {}).get(
        "google_places_api_key")
    out = [OsmProvider()]
    if key:
        out.append(GooglePlacesProvider(key))
    return out


def find_contacts(conn, providers, scanner=None, limit=None, refresh=False, log=print):
    """Look up phone/email/website for business owners and landlords.

    One lookup per company: every lead with the same owner/landlord name gets
    the result. Values entered by hand (contact_source = 'manual') are never
    replaced. Returns counts.
    """
    scanner = scanner if scanner is not None else WebsiteScanner()
    stale_google = (_now() - timedelta(days=GOOGLE_MAX_AGE_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT * FROM leads
        WHERE duplicate_of IS NULL AND status NOT IN ('stale', 'skip', 'lost', 'won')
          AND COALESCE(contact_source, '') != 'manual'
          AND (? OR contact_checked_at IS NULL
               OR (contact_source LIKE 'google%' AND contact_checked_at < ?))
        ORDER BY event_date DESC
        """,
        (int(bool(refresh)), stale_google),
    ).fetchall()
    counts = {"checked": 0, "found": 0, "not_found": 0, "skipped_people": 0, "errors": 0}
    done = {}  # business name -> Contact or None
    for lead in rows:
        if limit and counts["checked"] >= limit:
            break
        name, site = lookup_targets(lead)
        if not name and not site:
            counts["skipped_people"] += 1
            continue
        key = (name or "").upper().strip() or f"site:{lead['id']}"
        if key in done:
            contact, failed = done[key]
        else:
            counts["checked"] += 1
            contact, failed = None, False
            for prov in providers:
                try:
                    c = prov.find(lead, name)
                except Exception as e:  # one provider failing shouldn't stop the run
                    counts["errors"] += 1
                    failed = True
                    log(f"  {prov.name} lookup failed for {name or lead['address']}: {e}")
                    continue
                if c and not c.empty():
                    contact = c
                    if c.phone and c.email:
                        break
            if contact and contact.website and not (contact.phone and contact.email):
                try:
                    w = scanner.scan(contact.website) if scanner else None
                except Exception:
                    w = None
                if w:
                    contact.phone = contact.phone or w.phone
                    contact.email = contact.email or w.email
                    contact.source = f"{contact.source}+website"
            done[key] = (contact, failed)
        now = _now().isoformat()
        if contact and not contact.empty():
            counts["found"] += 1
            conn.execute(
                "UPDATE leads SET owner_phone = COALESCE(?, owner_phone), "
                "owner_email = COALESCE(?, owner_email), owner_website = COALESCE(?, owner_website), "
                "contact_source = ?, contact_name = ?, contact_checked_at = ? WHERE id = ?",
                (contact.phone, contact.email, contact.website, contact.source,
                 contact.matched_name, now, lead["id"]),
            )
        elif failed:
            continue  # couldn't reach a provider: try this lead again next run
        else:
            counts["not_found"] += 1
            conn.execute("UPDATE leads SET contact_checked_at = ? WHERE id = ?", (now, lead["id"]))
        conn.commit()
    return counts
