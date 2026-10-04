"""Outreach methods and how a lead is ranked and can be reached: the
methods' scripts and settings, a lead's priority, its stage and latest
event, and which methods can work it.

Channels
--------
door_hanger       Steve (or a helper) leaves a hanger at the property: reaches
                  whoever is at the property (tenant, neighbour, owner).
phone             Call the owner about this one property: a single clean-out
                  job (number looked up by hand; check Do Not Call).
property_manager  Call/email the landlord, property manager or LLC that owns
                  it, pitching a standing clean-out rate, not one job.

Dealing leads across the methods (Assign leads) is in dealing.py, and the
Results tab's numbers in results.py.

Touches (a visit, a call, an email) carry a cost; the result of the lead
(responded, quoted, won and revenue) is credited to its channel.
"""

import math
import re
from datetime import date
from typing import Any, Optional

from .enrich import is_entity
from .tucson_codes import code_of
from .util import LeadRow, az_today, is_multifamily

CHANNELS = {
    "door_hanger": "Door hanger at property",
    "phone": "Phone call to owner",
    "property_manager": "Landlord / property manager",
}

# Who each method reaches and what it offers, by template (the page uses
# "phone_eviction" for a phone call on an eviction: templateKey in core.js).
# On an eviction the phone call and the landlord pitch both reach the
# landlord, so they make different offers (this one unit now vs. a standing
# rate for every turnover), and Results compares methods within one kind of
# lead at a time.
PITCHES = {
    "door_hanger": {
        "who": "whoever is at the property: the tenant moving out, family, neighbours",
        "offer": "a free quote to clear this property",
    },
    "phone": {
        "who": "the owner of record of this property",
        "offer": "clear this property before the City's deadline",
    },
    "phone_eviction": {
        "who": "the landlord on the case",
        "offer": "a one-time clean-out of this unit after the move-out",
    },
    "property_manager": {
        "who": "the landlord, property manager or owning company",
        "offer": "a standing clean-out rate for all of their turnovers, not one job",
    },
}

# What Results compares within, and how the methods differ there.
LEAD_KINDS = {"eviction": "evictions", "code_violation": "City code cases"}
COMPARISON_BASIS = {
    "eviction": (
        "Compared on eviction leads only. The phone call and the landlord pitch both reach the landlord; "
        "they differ in the offer (one clean-out of this unit vs. a standing rate for every turnover), "
        "so this compares offers as well as methods. Door hangers reach whoever is at the property."
    ),
    "code_violation": (
        "Compared on City code cases only. The phone call reaches the owner about this property; the landlord "
        "pitch offers a company owner a standing rate; door hangers reach whoever is at the property."
    ),
    "": (
        "All leads together mixes evictions and code cases, where the methods reach different people. "
        "Pick evictions or code cases to compare like with like."
    ),
}


# The kinds of contact logged for each method, with their button labels.
TOUCH_KINDS = {
    "door_hanger": [["visited", "Hanger left"], ["talked", "Talked in person"]],
    "phone": [
        ["no_answer", "No answer"],
        ["voicemail", "Left voicemail"],
        ["talked", "Talked"],
        ["bad_number", "Wrong number"],
        ["do_not_call", "Asked not to call"],
    ],
    "property_manager": [["emailed", "Emailed"], ["voicemail", "Left voicemail"], ["talked", "Talked"]],
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "business_name": "Steve's Junk Removal",
    "business_phone": "",
    "base_address": "8790 N Wellside Dr, Tucson, AZ",
    "base_lat": None,
    "base_lon": None,
    # Which leads Lead Desk shows and assigns: "eviction_notice" (eviction
    # cases with an eviction notice filed), "evictions" or "all".
    "lead_view": "eviction_notice",
    # Kill switch: when on, the daily check, court case page reads and every
    # phone/email lookup stop until it is turned off (also LEADDESK_PAUSED=1).
    "paused": False,
    # Optional. Google Maps Platform key for the business phone lookup.
    "google_places_api_key": "",
    # Off: never search Google, even with GOOGLE_PLACES_API_KEY set.
    "google_enabled": True,
    # Most Google searches per month; 0 means none, null means no limit.
    # 1,000 is Google's free monthly allowance for searches that return phone numbers.
    "google_monthly_limit": 1000,
    # Most Google searches per day; 0 means none, null means no limit.
    "google_daily_limit": 30,
    # The Leads tab's setup guide (Google key, court records request) put away.
    "setup_guide_hidden": False,
    # When the last court records request was sent: {"date": iso}, or None.
    "records_requested": None,
    "costs": {"door_hanger": 0.35, "phone": 0.0, "property_manager": 0.0},
    "tracking_numbers": {"door_hanger": "", "phone": "", "property_manager": ""},
    "templates": {
        "door_hanger": (
            "Need this property cleared? Junk, furniture, appliances, yard debris. Free quote: {phone}. {business}"
        ),
        # Phone call to the owner of a property with a City code case.
        "phone": (
            "Hi, this is Steve with {business}. I'm calling about {address}. "
            "We help owners clear junk and yard debris, including when the City has "
            "opened a case. Would a free quote help?"
        ),
        # Phone call to the landlord on an eviction case ({at_address} is
        # " at <address>" when the property is known, otherwise nothing).
        "phone_eviction": (
            "Hi {owner_first}, this is Steve with {business}. We clear out rentals after a "
            "move-out or eviction{at_address}: furniture, trash, appliances and yard debris, "
            "usually within 48 hours, so the unit is ready to show. Could I give you a free "
            "quote? {phone}"
        ),
        "property_manager": (
            "Hi, this is Steve with {business}. We do move-out and eviction "
            "clean-outs around Tucson: furniture, appliances, trash and yard debris, "
            "usually within 48 hours. Could I send you a standing rate for your "
            "turnovers? {phone}"
        ),
    },
}

# Points by violation code / lead type. Higher means more stuff to haul and a
# more motivated owner.
_TYPE_POINTS = {
    "VACANT": 40,
    "DUMP": 35,
    "PMMULT": 35,
    "REFS": 30,
    "RSTOR": 30,
    "DILAP": 30,
    "TREES": 15,
    "WEEDS": 15,
}
# The same codes in a few words, for the priority breakdown.
_SHORT_LABELS = {
    "VACANT": "vacant building",
    "DUMP": "dumping",
    "PMMULT": "trash and debris",
    "REFS": "trash",
    "RSTOR": "outdoor storage",
    "DILAP": "dilapidated building",
    "TREES": "overgrown trees",
    "WEEDS": "weeds",
}


def _get(row: LeadRow, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def owner_first_name(lead: LeadRow) -> str:
    """The first name to greet on a call, or "" when it isn't known to be a
    person (the script then says "Hi there"). The owner of record comes
    first, else the eviction's landlord (first plaintiff).

    - Companies, trusts, apartments and the like (``is_entity``, or the
      assessor's company flag on the owner): "".
    - "LAST, FIRST M" (court and most lists): the word after the comma.
    - The assessor's "LAST FIRST MIDDLE": the second word. A plaintiff
      without a comma is not split this way, as its order isn't known.
    """
    owner = (_get(lead, "owner_name") or "").strip()
    name, from_assessor = (owner, True) if owner else ((_get(lead, "plaintiff") or "").strip(), False)
    name = name.split(";")[0].strip()
    if not name or is_entity(name) or (from_assessor and _get(lead, "owner_entity")):
        return ""
    if "," in name:
        words = name.split(",", 1)[1].split("&")[0].split()
        word = words[0] if words else ""
    elif from_assessor:
        words = name.split("&")[0].split()
        word = words[1] if len(words) > 1 else ""
    else:
        return ""
    word = word.strip(".")
    if not re.fullmatch(r"[A-Za-z][A-Za-z'-]+", word):
        return ""
    return re.sub(r"(^|[-'])([a-z])", lambda m: m.group(1) + m.group(2).upper(), word.lower())


def merged_settings(stored: Optional[dict]) -> dict:
    out: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_SETTINGS.items()}
    for k, v in (stored or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def _iso_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def date_label(lead: LeadRow) -> str:
    """What a lead's ``event_date`` is: "Hearing" for an eviction found on the
    court calendar whose case page hasn't been read yet (the calendar lists
    upcoming hearings), "Filed" once the case page has been read (or for a
    filing from an imported list), "Opened" for a City code case."""
    if lead["lead_type"] == "code_violation":
        return "Opened"
    if lead["lead_type"] in ("eviction", "civil"):
        if _get(lead, "source") == "pima_jp_calendar" and not _get(lead, "case_checked_at"):
            return "Hearing"
        return "Filed"
    return "Dated"


def case_events(lead: LeadRow) -> list[tuple[str, str]]:
    """The lead's real events so far as ``[(label, iso date)]``: filing (or
    opening), judgment and writ. A hearing is not an event that happened."""
    out = []
    label = date_label(lead)
    if label != "Hearing" and _iso_date(lead["event_date"]):
        out.append((label, str(lead["event_date"])[:10]))
    for label, key in (("Judgment", "judgment_date"), ("Writ", "writ_date")):
        if _iso_date(_get(lead, key)):
            out.append((label, str(_get(lead, key))[:10]))
    return out


def latest_event(lead: LeadRow, today: Optional[date] = None) -> tuple[Optional[str], Optional[str]]:
    """``(label, iso date)`` of the most recent real event on or before today
    (an eviction's filing, judgment or writ; a code case's opening), or
    ``(None, None)``. How fresh a lead is is measured from here."""
    today = today or az_today()
    best: tuple[Optional[str], Optional[str]] = (None, None)
    for label, iso in case_events(lead):
        if iso <= today.isoformat() and (best[1] is None or iso >= best[1]):
            best = (label, iso)
    return best


def score_parts(
    lead: LeadRow, owner_lead_counts: Optional[dict] = None, today: Optional[date] = None
) -> list[tuple[str, int]]:
    """What a lead's priority is made of, as ``[(plain label, points)]``:
    the page shows these when the priority number is hovered or focused."""
    today = today or az_today()
    parts: list[tuple[str, int]] = []
    if lead["lead_type"] == "eviction":
        parts.append(("Eviction", 35))
        # A recent judgment, and above all a recent writ of restitution
        # (lockout), means the tenant is out or about to be: the unit needs
        # clearing now. One from months ago was cleared long since.
        stage = fresh_stage(lead, today)
        if stage:
            parts.append(("writ issued" if stage == "writ" else "judgment", STAGE_POINTS[stage]))
    else:
        desc = (lead["description"] or "").upper()
        code = "VACANT" if "VACANT/NUISANCE" in desc else code_of(lead["description"])
        what = _SHORT_LABELS.get(code or "")
        parts.append((f"Code case: {what}" if what else "Code case", _TYPE_POINTS.get(code or "", 20)))
    # An owner who lives elsewhere is a landlord, not someone living there:
    # that says something about a code case. On an eviction the owner looked
    # up is the landlord, whose mailing address is nearly always an office
    # elsewhere, so it would add the same points to every eviction.
    if lead["owner_absentee"] and lead["lead_type"] != "eviction":
        parts.append(("owner lives elsewhere", 20))
    if lead["owner_entity"]:
        parts.append(("company owner", 10))
    if owner_lead_counts and lead["owner_name"]:
        if owner_lead_counts.get(lead["owner_name"], 0) > 1:
            parts.append(("repeat owner", 10))
    # Recency from the latest thing that actually happened (filing, judgment,
    # writ), never from an upcoming hearing or any other future date.
    label, when = latest_event(lead, today)
    if when:
        age = (today - date.fromisoformat(when)).days
        points = 15 if age <= 7 else 8 if age <= 14 else 0
        if points:
            ago = "today" if age == 0 else f"{age} day{'' if age == 1 else 's'} ago"
            parts.append((f"{(label or 'dated').lower()} {ago}", points))
    return parts


# How far an eviction has got, for the list order: a writ (lockout) means the
# unit needs clearing now, a judgment means a writ usually follows within days.
# Every recent writ case comes before every recent judgment case, which comes
# before every other lead; priority points order the leads within a stage.
# Recent means the case's latest real event (its writ, judgment or filing,
# see ``latest_event``) is at most STAGE_FRESH_DAYS old: a judgment from
# months ago is ranked like any other lead, by its points and recency, and
# gets no stage points.
STAGE_RANK = {"writ": 2, "judgment": 1}
STAGE_POINTS = {"writ": 25, "judgment": 15}
STAGE_FRESH_DAYS = 45


def fresh_stage(lead: LeadRow, today: Optional[date] = None) -> str:
    """``"writ"`` or ``"judgment"`` for an eviction at that stage whose latest
    event is within ``STAGE_FRESH_DAYS`` of today, else ``""``."""
    stage = str(_get(lead, "case_stage") or "")
    if _get(lead, "lead_type") != "eviction" or stage not in STAGE_RANK:
        return ""
    today = today or az_today()
    _label, when = latest_event(lead, today)
    if not when or (today - date.fromisoformat(when)).days > STAGE_FRESH_DAYS:
        return ""
    return stage


def stage_rank(lead: LeadRow, today: Optional[date] = None) -> int:
    """2 for an eviction with a recent writ, 1 for one with a recent
    judgment, else 0 (see ``fresh_stage``)."""
    return STAGE_RANK.get(fresh_stage(lead, today), 0)


def rank_key(lead: LeadRow) -> tuple[int, int]:
    """Sort key, best first: stage (recent writ, recent judgment, the rest),
    then the priority number already on the lead (``score``). A lead from
    the lead list carries its ``stage_rank`` (worked out for the day it was
    built); otherwise it is worked out for today."""
    rank = _get(lead, "stage_rank")
    if rank is None:
        rank = stage_rank(lead)
    return (-int(rank), -int(_get(lead, "score") or 0))


def score(lead: LeadRow, owner_lead_counts: Optional[dict] = None, today: Optional[date] = None) -> int:
    """0-100ish. ``lead`` is a dict/row with the leads table's columns."""
    return sum(points for _label, points in score_parts(lead, owner_lead_counts, today))


def door_hanger_problem(lead: LeadRow) -> Optional[str]:
    """Why a door hanger can't go to this lead yet, or None when it can:

    - ``"no_address"``: no property address.
    - ``"unconfirmed"``: the address is a guess from the landlord's parcels
      (``address_source = 'landlord'``) that Steve hasn't confirmed: the
      tenant may never have lived there.
    - ``"needs_unit"``: a parcel with more than one home (apartments, condos,
      a mobile or manufactured home park; see util.is_multifamily) and no
      unit number, so there's no single door to hang it on.

    Typing the unit, or confirming the address on the lead, clears the last two."""
    if not lead["address"]:
        return "no_address"
    if _get(lead, "unit") or _get(lead, "address_source") == "confirmed":
        return None
    if _get(lead, "address_source") == "landlord":
        return "unconfirmed"
    if is_multifamily(_get(lead, "property_use")):
        return "needs_unit"
    return None


def has_contact(lead: LeadRow) -> bool:
    """A phone number or email to reach the owner or landlord with."""
    return bool(_get(lead, "owner_phone") or _get(lead, "owner_email"))


def eligible_channels(lead: LeadRow, need_contact: bool = True) -> list[str]:
    """The methods that can work this lead now: a door hanger needs an
    address it can go to (``door_hanger_problem``); a phone call needs
    someone to call and their phone number; the landlord pitch needs a
    landlord or company owner and a phone or email for them. With
    ``need_contact=False`` the methods that could work it once a number is
    found (the lead page lets Steve set those by hand)."""
    out = []
    if door_hanger_problem(lead) is None:
        out.append("door_hanger")
    if (lead["owner_name"] or lead["plaintiff"]) and (_get(lead, "owner_phone") or not need_contact):
        out.append("phone")
    if (lead["owner_entity"] or lead["plaintiff"]) and (has_contact(lead) or not need_contact):
        out.append("property_manager")
    return out


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def miles_between(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> Optional[float]:
    lat1, lon1, lat2, lon2 = (_num(v) for v in (lat1, lon1, lat2, lon2))
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def landlord_key(lead: LeadRow) -> str:
    """Who answers for a lead: the eviction's landlord (first plaintiff), else
    the owner of record. Leads with neither stand alone."""
    for raw in (lead["plaintiff"], lead["owner_name"]):
        name = re.sub(r"[^A-Z0-9]+", " ", (raw or "").split(";")[0].upper()).strip()
        if name:
            return name
    return f"lead:{lead['id']}"
