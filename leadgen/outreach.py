"""Outreach experiment: score leads, split them across channels, measure
which channel turns leads into paid jobs for the least money.

Channels
--------
door_hanger       Steve (or a helper) leaves a hanger at the property: reaches
                  whoever is at the property (tenant, neighbour, owner).
phone             Call the owner about this one property: a single clean-out
                  job (number looked up by hand; check Do Not Call).
property_manager  Call/email the landlord, property manager or LLC that owns
                  it, pitching a standing clean-out rate, not one job.

A fair comparison
-----------------
The Results tab compares channels, so each channel has to get the same kind
of leads. ``assign`` makes that so:

- A round uses only leads that every ticked channel can work (door hangers
  need a property address, and a unit number or Steve's confirmation at an
  apartment or condo parcel), so no channel gets the leads the others can't.
- Every lead of one landlord or owner goes to the same channel, in this round
  and later ones, so no company hears from two channels.
- Leads are grouped by kind (address or not, eviction or code case), sorted by
  score, and dealt in small blocks in random order, so each channel gets the
  same mix of kinds and a similar spread of scores.

``results`` reports the mix each channel actually got and ``comparison``
says whether the channels can be ranked yet.

Touches (a visit, a call, an email) carry a cost; the result of the lead
(responded, quoted, won and revenue) is credited to its channel.
"""

import math
import random
import re
import uuid
from datetime import date
from typing import Any, Optional

from .enrich import is_entity
from .tucson_codes import code_of
from .util import Conn, LeadRow, az_today, is_multifamily, now_iso

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


# Channels that were tried and dropped. Leads still sitting in one go back to
# the unassigned pool; their contact history is kept. Kept on purpose, run at
# every start: postcards were dropped for good (docs/DECISIONS.md), and a
# database restored from before then, or written by an older copy of Lead
# Desk, must not bring a postcard queue back.
RETIRED_CHANNELS = ("postcard",)


def retire_channels(conn: Conn) -> int:
    marks = ",".join("?" * len(RETIRED_CHANNELS))
    cur = conn.execute(
        f"UPDATE leads SET channel = NULL, assigned_at = NULL WHERE channel IN ({marks})",
        RETIRED_CHANNELS,
    )
    conn.commit()
    return cur.rowcount


def landlord_key(lead: LeadRow) -> str:
    """Who answers for a lead: the eviction's landlord (first plaintiff), else
    the owner of record. Leads with neither stand alone."""
    for raw in (lead["plaintiff"], lead["owner_name"]):
        name = re.sub(r"[^A-Z0-9]+", " ", (raw or "").split(";")[0].upper()).strip()
        if name:
            return name
    return f"lead:{lead['id']}"


def _stratum(lead: LeadRow) -> tuple[bool, bool]:
    # "Has an address" means one a door hanger can go to (see door_hanger_problem).
    return (door_hanger_problem(lead) is None, lead["lead_type"] == "eviction")


# Why a lead was left out of a round that includes door hangers.
_ADDRESS_REASON = {"no_address": "needs_address", "unconfirmed": "needs_confirm", "needs_unit": "needs_unit"}

# How many of the leads a round left out it names (the page links them).
LEFT_OUT_SHOWN = 12

# How a lead got its method (``assigned_by``): dealt by an Assign leads
# round, sent to the method already working its landlord, set by hand, or
# dealt by a round to the one ticked method it could use (``fit``: outside
# the balanced split, so left out of Results' mix like followed leads).
BY_ROUND, FOLLOWED, BY_HAND, BY_FIT = "round", "followed", "hand", "fit"


def _taken(conn: Conn) -> dict[str, str]:
    """Landlord -> the method already working them."""
    taken: dict[str, str] = {}
    for r in conn.execute(
        "SELECT id, plaintiff, owner_name, channel FROM leads WHERE channel IS NOT NULL ORDER BY assigned_at, id"
    ).fetchall():
        taken.setdefault(landlord_key(r), r["channel"])
    return taken


def _pool(leads: list) -> list:
    pool = [l for l in leads if not l["channel"] and l["status"] == "new"]
    pool.sort(key=lambda l: (*rank_key(l), l["id"]))
    return pool


def _combos() -> list[tuple[str, ...]]:
    names = list(CHANNELS)
    out = []
    for mask in range(1, 2 ** len(names)):
        out.append(tuple(c for i, c in enumerate(names) if mask >> i & 1))
    return out


# The kinds of lead an Assign leads round can be limited to ("" is both).
ROUND_KINDS = ("eviction", "code_violation")


def _suggest(combos: dict, evictions: Optional[dict] = None) -> list[str]:
    """The methods to tick by default: two or more methods if any can work
    leads together, then (with ``evictions``, for a round of both kinds) the
    set that takes in the most evictions, then the most methods, then the
    most leads."""
    workable: list[tuple[str, ...]] = [c for c in _combos() if combos["+".join(c)]]
    ev = evictions or {}

    def rank(c: tuple[str, ...]) -> tuple[bool, int, int, int]:
        return (len(c) > 1, ev.get("+".join(c), 0), len(c), combos["+".join(c)])

    if not workable:
        return []
    best: tuple[str, ...] = max(workable, key=rank)
    return list(best)


def split_preview(conn: Conn, leads: list) -> dict:
    """What Assign leads can hand out before Steve presses it: for each
    combination of methods, how many unassigned leads every one of them can
    work (``combos``, keyed "door_hanger+phone"; ``evictions`` counts the
    evictions among them), how many go to the method already working their
    landlord (``followed``), and the combination to tick by default
    (``suggested``: two or more methods, the most evictions, then the most
    methods and leads). ``by_kind`` has the same for a round of evictions
    only and of City code cases only, and ``kind`` is the kind of round to
    offer first: evictions when there are any (Results compares methods
    within one kind of lead, so a round of one kind measures it cleanly).

    A call or email method only counts leads with a phone or email. The
    same numbers counting leads that have none yet (Steve looks the number
    up himself) are under ``with_unreachable``, so the page can say how many
    more there are and offer to include them."""
    taken = _taken(conn)
    pool = _pool(leads)

    def tally(need_contact: bool) -> dict:
        followed = {"": 0, **{k: 0 for k in ROUND_KINDS}}
        eligible_sets: list[tuple[set, str]] = []
        for lead in pool:
            eligible = set(eligible_channels(lead, need_contact))
            kind = str(lead["lead_type"])
            ch = taken.get(landlord_key(lead))
            if ch:
                if ch in eligible:
                    followed[""] += 1
                    if kind in followed:
                        followed[kind] += 1
                continue
            eligible_sets.append((eligible, kind))

        def count(kinds: Optional[tuple] = None) -> dict:
            return {
                "+".join(c): sum(1 for e, k in eligible_sets if e.issuperset(c) and (kinds is None or k in kinds))
                for c in _combos()
            }

        combos, evictions = count(), count(("eviction",))
        by_kind: dict[str, dict[str, Any]] = {}
        for kind in ROUND_KINDS:
            kc = evictions if kind == "eviction" else count((kind,))
            by_kind[kind] = {
                "combos": kc,
                "suggested": _suggest(kc),
                "leads": sum(1 for _e, k in eligible_sets if k == kind),
                "followed": followed[kind],
            }
        return {
            "combos": combos,
            "evictions": evictions,
            "followed": followed[""],
            "suggested": _suggest(combos, evictions),
            "by_kind": by_kind,
        }

    ready, everyone = tally(True), tally(False)
    # Evictions first whenever there are any, even with no phone found yet:
    # the page then says how many are waiting for a number.
    kind = next((k for k in ROUND_KINDS if any(everyone["by_kind"][k]["combos"].values())), "")
    return {**ready, "kind": kind, "with_unreachable": everyone}


def check_channels(channels: Any, single_method: bool = False) -> list[str]:
    """The methods for an Assign leads round, checked: a list of known
    method names, at least two unless ``single_method`` (a round with one
    method compares nothing, so the page asks first)."""
    if not isinstance(channels, (list, tuple)) or not all(isinstance(c, str) for c in channels):
        raise ValueError("Outreach methods must be a list of method names.")
    unknown = [c for c in dict.fromkeys(channels) if c not in CHANNELS]
    if unknown:
        raise ValueError(
            f"Unknown outreach method{'' if len(unknown) == 1 else 's'}: {', '.join(unknown)}. "
            f"The methods are: {', '.join(CHANNELS)}."
        )
    chosen = list(dict.fromkeys(channels))
    if not chosen:
        raise ValueError("Tick at least one outreach method.")
    if len(chosen) < 2 and not single_method:
        raise ValueError(
            "With one method ticked the round can't compare methods. Tick two or more, "
            "or confirm that you want a one-method round."
        )
    return chosen


def assign(
    conn: Conn,
    leads: list,
    count: int,
    channels: list,
    seed: Any = None,
    lead_type: str = "",
    preview: bool = False,
    include_unreachable: bool = False,
    fit: bool = False,
) -> dict:
    """Deal up to ``count`` of the best unassigned leads across ``channels``
    so that each channel gets a like-for-like share (see the module notes).
    With ``fit``, leads that only some of the ticked methods can work (a
    phone but no confirmed address, say) are dealt too, after the balanced
    split, each to a method it can use (``fitted``); they are kept apart in
    Results, as the balanced split didn't choose them.
    ``lead_type`` ("eviction" or "code_violation") limits the round to one
    kind of lead. ``preview`` works the round out without saving anything:
    which leads it would take (the page says so before Steve confirms).
    A call or email method gets only leads with a phone or email, unless
    ``include_unreachable`` (Steve chose to look the numbers up himself).
    Each method gets floor(N/M) or ceil(N/M) of the N leads dealt (all of
    one landlord's leads go together, which can tip that by a group).

    Returns counts: ``{"assigned": {channel: n}, "followed": {channel: n}, "fitted": {channel: n},
    "kinds": {lead type: n} (the leads dealt, fitted ones too, not those followed),
    "left_out": {"needs_address": n, "needs_confirm": n, "needs_unit": n, "no_contact": n},
    "left_out_leads": [{"id", "label", "reason"}] (the first few of them),
    "round": id}``.
    ``followed`` are leads whose landlord already has a channel from an
    earlier round: they go to that channel, outside the balanced split, so
    they don't use up ``count`` (at most ``count`` of them go in one round),
    and they are stored as followed, not as dealt or set by hand.
    """
    rng = random.Random(seed)
    channels = check_channels(channels, single_method=True)
    count = max(0, int(count))
    round_id = now_iso() + "-" + uuid.uuid4().hex[:6]
    now = now_iso()

    if lead_type and lead_type not in ROUND_KINDS:
        raise ValueError("A round can be evictions only, City code cases only, or both.")
    taken = _taken(conn)
    pool = [l for l in _pool(leads) if not lead_type or l["lead_type"] == lead_type]
    out: dict[str, Any] = {
        "assigned": {c: 0 for c in channels},
        "followed": {},
        "fitted": {},
        "kinds": {},
        "left_out": {"needs_address": 0, "needs_confirm": 0, "needs_unit": 0, "no_contact": 0},
        "left_out_leads": [],
        "lead_type": lead_type,
        "preview": preview,
        # Dealt leads with no phone or email (only with include_unreachable).
        "without_contact": 0,
        "round": None if preview else round_id,
    }

    def give(lead: LeadRow, ch: str, how: str) -> None:
        if how in (BY_ROUND, BY_FIT):
            kind = str(lead["lead_type"])
            out["kinds"][kind] = out["kinds"].get(kind, 0) + 1
        if ch != "door_hanger" and not has_contact(lead):
            out["without_contact"] += 1
        if preview:
            return
        conn.execute(
            "UPDATE leads SET channel = ?, assigned_at = ?, assign_round = ?, assigned_by = ? WHERE id = ?",
            (ch, now, round_id, how, lead["id"]),
        )

    followed = 0
    clusters: dict[str, list] = {}  # landlord -> leads, in score order
    partial: dict[str, list] = {}  # landlord -> leads only some ticked methods can work
    for lead in pool:
        eligible = eligible_channels(lead, need_contact=not include_unreachable)
        key = landlord_key(lead)
        if key in taken:
            ch = taken[key]
            if ch in eligible and followed < count:
                give(lead, ch, FOLLOWED)
                out["followed"][ch] = out["followed"].get(ch, 0) + 1
                followed += 1
            continue
        missing = [c for c in channels if c not in eligible]
        if missing and fit and len(missing) < len(channels):
            partial.setdefault(key, []).append(lead)
            continue
        if missing:
            if missing == ["door_hanger"]:
                reason = _ADDRESS_REASON.get(door_hanger_problem(lead) or "", "needs_address")
            else:
                reason = "no_contact"  # no phone or email yet (or no one to contact)
            out["left_out"][reason] += 1
            if len(out["left_out_leads"]) < LEFT_OUT_SHOWN:
                label = lead["address"] or (lead["plaintiff"] or "").split(";")[0] or lead["owner_name"]
                out["left_out_leads"].append({"id": lead["id"], "label": label or lead["source_id"], "reason": reason})
            continue
        clusters.setdefault(key, []).append(lead)

    # Best landlords first, until the round is full. A landlord's leads stay
    # together, so a big one that doesn't fit waits for the next round.
    picked = 0
    chosen = []
    for _key, group in sorted(clusters.items(), key=lambda kv: (*rank_key(kv[1][0]), kv[0])):
        if picked + len(group) > count:
            continue
        chosen.append(group)
        picked += len(group)

    # Deal within each kind of lead, best first, in blocks of len(channels):
    # every channel gets one group per block, in random order, the larger
    # groups going to the channels that are behind on leads of that kind.
    # Ties go to the channel with the fewest leads in the whole round, so
    # the leftovers of each kind don't all land on one channel: a round of
    # N single leads gives every channel floor(N/M) or ceil(N/M).
    by_stratum: dict[tuple[bool, bool], list] = {}
    for group in chosen:
        by_stratum.setdefault(_stratum(group[0]), []).append(group)
    total = out["assigned"]
    for stratum in sorted(by_stratum):
        groups = by_stratum[stratum]
        have = {c: 0 for c in channels}
        for i in range(0, len(groups), len(channels)):
            block = sorted(groups[i : i + len(channels)], key=len, reverse=True)
            free = channels[:]
            rng.shuffle(free)
            for group in block:
                ch = min(free, key=lambda c: (have[c], total[c]))
                free.remove(ch)
                for lead in group:
                    give(lead, ch, BY_ROUND)
                have[ch] += len(group)
                total[ch] += len(group)

    # Leads only some ticked methods can work (``fit``): best landlords first,
    # while the round has room, each landlord to the method it can use that
    # has the fewest leads so far (all of its leads to that one method).
    fitted = 0
    for _key, group in sorted(partial.items(), key=lambda kv: (*rank_key(kv[1][0]), kv[0])):
        if picked + fitted + len(group) > count:
            continue
        usable = [c for c in channels if all(c in eligible_channels(l, not include_unreachable) for l in group)]
        if not usable:
            continue
        ch = min(usable, key=lambda c: (total[c] + out["fitted"].get(c, 0), channels.index(c)))
        for lead in group:
            give(lead, ch, BY_FIT)
        out["fitted"][ch] = out["fitted"].get(ch, 0) + len(group)
        fitted += len(group)
    if not preview:
        conn.commit()
    return out


def how_assigned(row: LeadRow) -> str:
    """``round``, ``followed`` or ``hand``. Leads from before ``assigned_by``
    was kept: dealt by a round when they carry its id, else by hand."""
    by = _get(row, "assigned_by")
    if by in (BY_ROUND, FOLLOWED, BY_HAND, BY_FIT):
        return str(by)
    return BY_ROUND if _get(row, "assign_round") else BY_HAND


def tag_followed_leads(conn: Conn) -> int:
    """Once per database: older versions stored leads that followed their
    landlord's method with no round id, the same as leads set by hand. They
    carry the assignment time of the round that sent them, which a lead set
    by hand never does, so mark those as followed."""
    cur = conn.execute(
        "UPDATE leads SET assigned_by = ? WHERE assigned_by IS NULL AND channel IS NOT NULL "
        "AND assign_round IS NULL AND assigned_at IN "
        "(SELECT assigned_at FROM leads WHERE assign_round IS NOT NULL)",
        (FOLLOWED,),
    )
    conn.commit()
    return cur.rowcount


def _mix(rows: list) -> dict:
    """What kind of leads a channel got. Leads that followed their landlord's
    method are counted apart and left out of the shares and the average, as
    the balanced split didn't choose them."""
    how = [how_assigned(r) for r in rows]
    followed = how.count(FOLLOWED)
    fitted = how.count(BY_FIT)
    by_hand = how.count(BY_HAND)
    rows = [r for r, h in zip(rows, how) if h not in (FOLLOWED, BY_FIT)]
    n = len(rows)
    if not n:
        return {
            "leads": 0,
            "with_address": None,
            "evictions": None,
            "avg_score": None,
            "set_by_hand": by_hand,
            "followed": followed,
            "fitted": fitted,
        }
    return {
        "leads": n,
        "with_address": sum(1 for r in rows if door_hanger_problem(r) is None) / n,
        "evictions": sum(1 for r in rows if r["lead_type"] == "eviction") / n,
        "avg_score": round(sum(r["_score"] for r in rows) / n, 1),
        "set_by_hand": by_hand,
        "followed": followed,
        "fitted": fitted,
    }


def results(conn: Conn, today: Optional[date] = None, lead_type: Optional[str] = None) -> list[dict]:
    """Per-channel funnel and cost numbers, and the mix of leads each got;
    only leads of ``lead_type`` when given (see ``COMPARISON_BASIS``)."""
    where, args = ("AND l.lead_type = ?", (lead_type,)) if lead_type else ("", ())
    rows = conn.execute(
        f"""
        SELECT l.*, COALESCE(t.n, 0) AS touch_count, COALESCE(t.cost_cents, 0) AS touch_cents
        FROM leads l
        LEFT JOIN (SELECT lead_id, COUNT(*) AS n, SUM(cost_cents) AS cost_cents
                   FROM touches GROUP BY lead_id) t ON t.lead_id = l.id
        WHERE l.channel IS NOT NULL {where}
        """,
        args,
    ).fetchall()
    # Repeat owners, counted for the owners of these leads only.
    owner_counts = {}
    names = sorted({r["owner_name"] for r in rows if r["owner_name"]})
    for i in range(0, len(names), 500):
        chunk = names[i : i + 500]
        for r in conn.execute(
            f"SELECT owner_name, COUNT(*) AS n FROM leads WHERE owner_name IN ({','.join('?' * len(chunk))}) "
            "AND duplicate_of IS NULL GROUP BY owner_name",
            chunk,
        ).fetchall():
            owner_counts[r["owner_name"]] = r["n"]
    by: dict[str, list] = {}
    for r in rows:
        d = dict(r)
        d["_score"] = score(r, owner_counts, today=today)
        by.setdefault(r["channel"], []).append(d)
    out = []
    for ch, label in CHANNELS.items():
        rs = by.get(ch, [])
        touched = sum(1 for r in rs if r["touch_count"])
        responded = sum(1 for r in rs if r["responded_at"] or r["status"] in ("responded", "quoted", "won"))
        quoted = sum(1 for r in rs if r["status"] in ("quoted", "won") or r["quote_cents"] is not None)
        won = sum(1 for r in rs if r["status"] == "won")
        # Sums in whole cents, so the totals are exact.
        revenue = sum(int(r["revenue_cents"] or 0) for r in rs) / 100
        cost = sum(int(r["touch_cents"] or 0) for r in rs) / 100
        out.append(
            {
                "channel": ch,
                "label": label,
                "assigned": len(rs),
                "touched": touched,
                "responded": responded,
                "quoted": quoted,
                "won": won,
                "revenue": revenue,
                "cost": cost,
                "response_rate": responded / touched if touched else None,
                "win_rate": won / touched if touched else None,
                "cost_per_win": cost / won if won else None,
                "revenue_per_dollar": revenue / cost if cost else None,
                "mix": _mix(rs),
            }
        )
    return out


# How far apart the channels' mixes may be and still count as like-for-like.
MIX_TOLERANCE = {"with_address": 0.15, "evictions": 0.15, "avg_score": 8}
MIN_CONTACTS = 20


def comparison(results_rows: list[dict]) -> dict:
    """Can the channels be ranked yet? ``{"fair": bool, "ready": bool,
    "reasons": [plain sentences], "notes": [plain sentences]}``. ``notes``
    say how many leads followed their landlord's method (not a reason the
    comparison is unfair: those leads are left out of the mix). ``fair`` means the channels got the same
    mix of leads; ``ready`` adds that each has enough contacts to judge."""
    active = [r for r in results_rows if r["assigned"]]
    reasons: list[str] = []
    notes: list[str] = []
    if len(active) < 2:
        reasons.append("Only one outreach method has leads so far, so there is nothing to compare.")
    else:
        names = {
            "with_address": "share of leads with a property address",
            "evictions": "share of evictions vs. code cases",
            "avg_score": "average priority",
        }
        for key, limit in MIX_TOLERANCE.items():
            vals = [r["mix"][key] for r in active if r["mix"][key] is not None]
            if vals and max(vals) - min(vals) > limit:
                reasons.append(
                    f"The methods got a different {names[key]}, so their results reflect the leads, not the method."
                )
        for r in active:
            if r["mix"]["set_by_hand"] > 0.2 * r["assigned"]:
                n = r["mix"]["set_by_hand"]
                reasons.append(
                    f"{n} {r['label']} lead{'' if n == 1 else 's'} had the method set by hand on the lead "
                    "rather than split with Assign leads."
                )
    for r in active:
        n = r["mix"]["followed"]
        if n:
            notes.append(
                f"{n} {r['label']} lead{' went' if n == 1 else 's went'} to the method already working "
                "their landlord; they are left out of the mix below."
            )
        n = r["mix"].get("fitted", 0)
        if n:
            notes.append(
                f"{n} {r['label']} lead{' was' if n == 1 else 's were'} dealt to it outside the balanced split, "
                "as not every ticked method could work them; left out of the mix below."
            )
    fair = not reasons
    thin = [r["label"] for r in active if r["touched"] < MIN_CONTACTS]
    if fair and thin:
        reasons.append(
            f"Fewer than {MIN_CONTACTS} contacts logged for: {', '.join(thin)}. One job can still swing the ranking."
        )
    return {"fair": fair, "ready": fair and not thin, "reasons": reasons, "notes": notes}
