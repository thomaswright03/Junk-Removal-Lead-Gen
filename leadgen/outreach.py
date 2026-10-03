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

# Who each method reaches and what it offers, by template (see
# ``template_key``): on an eviction the phone call and the landlord pitch both
# reach the landlord, so they make different offers (this one unit now vs. a
# standing rate for every turnover), and Results compares methods within one
# kind of lead at a time.
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


def template_key(channel: str, lead_type: Optional[str]) -> str:
    """The message template (and pitch) for a method on a kind of lead:
    phone calls on evictions use the landlord script."""
    return "phone_eviction" if channel == "phone" and lead_type == "eviction" else channel


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
        # A judgment, and above all a writ of restitution (lockout), means the
        # tenant is out or about to be: the unit needs clearing now.
        stage = _get(lead, "case_stage")
        if stage == "writ":
            parts.append(("writ issued", 25))
        elif stage == "judgment":
            parts.append(("judgment", 15))
    else:
        desc = (lead["description"] or "").upper()
        code = "VACANT" if "VACANT/NUISANCE" in desc else code_of(lead["description"])
        what = _SHORT_LABELS.get(code or "")
        parts.append((f"Code case: {what}" if what else "Code case", _TYPE_POINTS.get(code or "", 20)))
    if lead["owner_absentee"]:
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
# Every writ case comes before every judgment case, which comes before every
# other lead; priority points order the leads within a stage.
STAGE_RANK = {"writ": 2, "judgment": 1}


def stage_rank(lead: LeadRow) -> int:
    """2 for an eviction with a writ, 1 for one with a judgment, else 0."""
    if _get(lead, "lead_type") != "eviction":
        return 0
    return STAGE_RANK.get(str(_get(lead, "case_stage") or ""), 0)


def rank_key(lead: LeadRow) -> tuple[int, int]:
    """Sort key, best first: stage (writ, judgment, the rest), then the
    priority number already on the lead (``score``)."""
    return (-stage_rank(lead), -int(_get(lead, "score") or 0))


def score(lead: LeadRow, owner_lead_counts: Optional[dict] = None, today: Optional[date] = None) -> int:
    """0-100ish. ``lead`` is a dict/row with the leads table's columns."""
    return sum(points for _label, points in score_parts(lead, owner_lead_counts, today))


def door_hanger_problem(lead: LeadRow) -> Optional[str]:
    """Why a door hanger can't go to this lead yet, or None when it can:
    ``"no_address"``, or ``"needs_unit"`` for an apartment / condo parcel with
    no unit number (a complex has no single door to hang it on) until Steve
    types the unit or confirms the address."""
    if not lead["address"]:
        return "no_address"
    if _get(lead, "unit") or _get(lead, "address_source") == "confirmed":
        return None
    if is_multifamily(_get(lead, "property_use")):
        return "needs_unit"
    return None


def eligible_channels(lead: LeadRow) -> list[str]:
    out = []
    if door_hanger_problem(lead) is None:
        out.append("door_hanger")
    if lead["owner_name"] or lead["plaintiff"]:
        out.append("phone")
    if lead["owner_entity"] or lead["plaintiff"]:
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
# the unassigned pool; their contact history is kept.
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


# How a lead got its method (``assigned_by``): dealt by an Assign leads
# round, sent to the method already working its landlord, or set by hand.
BY_ROUND, FOLLOWED, BY_HAND = "round", "followed", "hand"


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


def split_preview(conn: Conn, leads: list) -> dict:
    """What Assign leads can hand out before Steve presses it: for each
    combination of methods, how many unassigned leads every one of them can
    work (``combos``, keyed "door_hanger+phone"), how many go to the method
    already working their landlord (``followed``), and the combination to
    tick by default (``suggested``: the most methods that still have leads,
    then the most leads)."""
    taken = _taken(conn)
    followed = 0
    eligible_sets = []
    for lead in _pool(leads):
        eligible = set(eligible_channels(lead))
        ch = taken.get(landlord_key(lead))
        if ch:
            followed += ch in eligible
            continue
        eligible_sets.append(eligible)
    combos = {"+".join(c): sum(1 for e in eligible_sets if e.issuperset(c)) for c in _combos()}
    workable = [c for c in _combos() if combos["+".join(c)]]
    best = max(workable, key=lambda c: (len(c), combos["+".join(c)]), default=())
    return {"combos": combos, "followed": followed, "suggested": list(best)}


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


def assign(conn: Conn, leads: list, count: int, channels: list, seed: Any = None) -> dict:
    """Deal up to ``count`` of the best unassigned leads across ``channels``
    so that each channel gets a like-for-like share (see the module notes).

    Returns counts: ``{"assigned": {channel: n}, "followed": {channel: n},
    "left_out": {"needs_address": n, "needs_unit": n, "no_contact": n},
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

    taken = _taken(conn)
    pool = _pool(leads)
    out: dict[str, Any] = {
        "assigned": {c: 0 for c in channels},
        "followed": {},
        "left_out": {"needs_address": 0, "needs_unit": 0, "no_contact": 0},
        "round": round_id,
    }

    def give(lead: LeadRow, ch: str, how: str) -> None:
        conn.execute(
            "UPDATE leads SET channel = ?, assigned_at = ?, assign_round = ?, assigned_by = ? WHERE id = ?",
            (ch, now, round_id, how, lead["id"]),
        )

    followed = 0
    clusters: dict[str, list] = {}  # landlord -> leads, in score order
    for lead in pool:
        eligible = eligible_channels(lead)
        key = landlord_key(lead)
        if key in taken:
            ch = taken[key]
            if ch in eligible and followed < count:
                give(lead, ch, FOLLOWED)
                out["followed"][ch] = out["followed"].get(ch, 0) + 1
                followed += 1
            continue
        missing = [c for c in channels if c not in eligible]
        if missing:
            if missing == ["door_hanger"]:
                reason = "needs_unit" if door_hanger_problem(lead) == "needs_unit" else "needs_address"
            else:
                reason = "no_contact"
            out["left_out"][reason] += 1
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
    by_stratum: dict[tuple[bool, bool], list] = {}
    for group in chosen:
        by_stratum.setdefault(_stratum(group[0]), []).append(group)
    for stratum in sorted(by_stratum):
        groups = by_stratum[stratum]
        have = {c: 0 for c in channels}
        for i in range(0, len(groups), len(channels)):
            block = sorted(groups[i : i + len(channels)], key=len, reverse=True)
            free = channels[:]
            rng.shuffle(free)
            for group in block:
                ch = min(free, key=lambda c: have[c])
                free.remove(ch)
                for lead in group:
                    give(lead, ch, BY_ROUND)
                have[ch] += len(group)
                out["assigned"][ch] += len(group)
    conn.commit()
    return out


def how_assigned(row: LeadRow) -> str:
    """``round``, ``followed`` or ``hand``. Leads from before ``assigned_by``
    was kept: dealt by a round when they carry its id, else by hand."""
    by = _get(row, "assigned_by")
    if by in (BY_ROUND, FOLLOWED, BY_HAND):
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
    by_hand = how.count(BY_HAND)
    rows = [r for r, h in zip(rows, how) if h != FOLLOWED]
    n = len(rows)
    if not n:
        return {
            "leads": 0,
            "with_address": None,
            "evictions": None,
            "avg_score": None,
            "set_by_hand": by_hand,
            "followed": followed,
        }
    return {
        "leads": n,
        "with_address": sum(1 for r in rows if door_hanger_problem(r) is None) / n,
        "evictions": sum(1 for r in rows if r["lead_type"] == "eviction") / n,
        "avg_score": round(sum(r["_score"] for r in rows) / n, 1),
        "set_by_hand": by_hand,
        "followed": followed,
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
    owner_counts = {}
    for r in conn.execute(
        "SELECT owner_name, COUNT(*) AS n FROM leads WHERE owner_name IS NOT NULL "
        "AND duplicate_of IS NULL GROUP BY owner_name"
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
    fair = not reasons
    thin = [r["label"] for r in active if r["touched"] < MIN_CONTACTS]
    if fair and thin:
        reasons.append(
            f"Fewer than {MIN_CONTACTS} contacts logged for: {', '.join(thin)}. One job can still swing the ranking."
        )
    return {"fair": fair, "ready": fair and not thin, "reasons": reasons, "notes": notes}
