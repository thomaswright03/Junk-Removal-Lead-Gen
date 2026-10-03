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
  need a property address), so no channel gets the leads the others can't.
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

from .tucson_codes import code_of
from .util import az_today, now_iso

CHANNELS = {
    "door_hanger": "Door hanger at property",
    "phone": "Phone call to owner",
    "property_manager": "Landlord / property manager",
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

DEFAULT_SETTINGS = {
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


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def merged_settings(stored):
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_SETTINGS.items()}
    for k, v in (stored or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def score(lead, owner_lead_counts=None, today=None):
    """0-100ish. ``lead`` is a dict/row with the leads table's columns."""
    today = today or az_today()
    points = 0
    if lead["lead_type"] == "eviction":
        points += 35
        # A judgment, and above all a writ of restitution (lockout), means the
        # tenant is out or about to be: the unit needs clearing now.
        stage = _get(lead, "case_stage")
        points += 25 if stage == "writ" else 15 if stage == "judgment" else 0
    else:
        desc = (lead["description"] or "").upper()
        if "VACANT/NUISANCE" in desc:
            points += _TYPE_POINTS["VACANT"]
        else:
            points += _TYPE_POINTS.get(code_of(lead["description"]), 20)
    if lead["owner_absentee"]:
        points += 20
    if lead["owner_entity"]:
        points += 10
    if owner_lead_counts and lead["owner_name"]:
        if owner_lead_counts.get(lead["owner_name"], 0) > 1:
            points += 10
    if lead["event_date"]:
        try:
            age = (today - date.fromisoformat(lead["event_date"][:10])).days
            points += 15 if age <= 7 else 8 if age <= 14 else 0
        except ValueError:
            pass
    return points


def eligible_channels(lead):
    out = []
    if lead["address"]:
        out.append("door_hanger")
    if lead["owner_name"] or lead["plaintiff"]:
        out.append("phone")
    if lead["owner_entity"] or lead["plaintiff"]:
        out.append("property_manager")
    return out


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def miles_between(lat1, lon1, lat2, lon2):
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


def retire_channels(conn):
    marks = ",".join("?" * len(RETIRED_CHANNELS))
    cur = conn.execute(
        f"UPDATE leads SET channel = NULL, assigned_at = NULL WHERE channel IN ({marks})",
        RETIRED_CHANNELS,
    )
    conn.commit()
    return cur.rowcount


def landlord_key(lead):
    """Who answers for a lead: the eviction's landlord (first plaintiff), else
    the owner of record. Leads with neither stand alone."""
    for raw in (lead["plaintiff"], lead["owner_name"]):
        name = re.sub(r"[^A-Z0-9]+", " ", (raw or "").split(";")[0].upper()).strip()
        if name:
            return name
    return f"lead:{lead['id']}"


def _stratum(lead):
    return (bool(lead["address"]), lead["lead_type"] == "eviction")


def assign(conn, leads, count, channels, seed=None):
    """Deal up to ``count`` of the best unassigned leads across ``channels``
    so that each channel gets a like-for-like share (see the module notes).

    Returns counts: ``{"assigned": {channel: n}, "followed": {channel: n},
    "left_out": {"needs_address": n, "no_contact": n}, "round": id}``.
    ``followed`` are leads whose landlord already has a channel from an
    earlier round: they go to that channel, outside the balanced split.
    """
    rng = random.Random(seed)
    channels = [c for c in dict.fromkeys(channels) if c in CHANNELS]
    if not channels:
        raise ValueError("Tick at least one outreach method.")
    count = max(0, int(count))
    round_id = now_iso() + "-" + uuid.uuid4().hex[:6]
    now = now_iso()

    taken = {}
    for r in conn.execute(
        "SELECT id, plaintiff, owner_name, channel FROM leads WHERE channel IS NOT NULL ORDER BY assigned_at, id"
    ).fetchall():
        taken.setdefault(landlord_key(r), r["channel"])

    pool = [l for l in leads if not l["channel"] and l["status"] == "new"]
    pool.sort(key=lambda l: (-l["score"], l["id"]))
    out = {
        "assigned": {c: 0 for c in channels},
        "followed": {},
        "left_out": {"needs_address": 0, "no_contact": 0},
        "round": round_id,
    }

    def give(lead, ch, round_):
        conn.execute(
            "UPDATE leads SET channel = ?, assigned_at = ?, assign_round = ? WHERE id = ?",
            (ch, now, round_, lead["id"]),
        )

    picked = 0
    clusters = {}  # landlord -> leads, in score order
    for lead in pool:
        eligible = eligible_channels(lead)
        key = landlord_key(lead)
        if key in taken:
            ch = taken[key]
            if ch in eligible and picked < count:
                give(lead, ch, None)
                out["followed"][ch] = out["followed"].get(ch, 0) + 1
                picked += 1
            continue
        missing = [c for c in channels if c not in eligible]
        if missing:
            out["left_out"]["needs_address" if missing == ["door_hanger"] else "no_contact"] += 1
            continue
        clusters.setdefault(key, []).append(lead)

    # Best landlords first, until the round is full. A landlord's leads stay
    # together, so a big one that doesn't fit waits for the next round.
    chosen = []
    for _key, group in sorted(clusters.items(), key=lambda kv: (-kv[1][0]["score"], kv[0])):
        if picked + len(group) > count:
            continue
        chosen.append(group)
        picked += len(group)

    # Deal within each kind of lead, best first, in blocks of len(channels):
    # every channel gets one group per block, in random order, the larger
    # groups going to the channels that are behind on leads of that kind.
    by_stratum = {}
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
                    give(lead, ch, round_id)
                have[ch] += len(group)
                out["assigned"][ch] += len(group)
    conn.commit()
    return out


def _mix(rows):
    """What kind of leads a channel got."""
    n = len(rows)
    if not n:
        return {"leads": 0, "with_address": None, "evictions": None, "avg_score": None, "set_by_hand": 0}
    return {
        "leads": n,
        "with_address": sum(1 for r in rows if r["address"]) / n,
        "evictions": sum(1 for r in rows if r["lead_type"] == "eviction") / n,
        "avg_score": round(sum(r["_score"] for r in rows) / n, 1),
        "set_by_hand": sum(1 for r in rows if not r["assign_round"]),
    }


def results(conn, today=None):
    """Per-channel funnel and cost numbers, and the mix of leads each got."""
    rows = conn.execute(
        """
        SELECT l.*, COALESCE(t.n, 0) AS touch_count, COALESCE(t.cost, 0) AS touch_cost
        FROM leads l
        LEFT JOIN (SELECT lead_id, COUNT(*) AS n, SUM(cost) AS cost
                   FROM touches GROUP BY lead_id) t ON t.lead_id = l.id
        WHERE l.channel IS NOT NULL
        """
    ).fetchall()
    owner_counts = {}
    for r in conn.execute(
        "SELECT owner_name, COUNT(*) AS n FROM leads WHERE owner_name IS NOT NULL "
        "AND duplicate_of IS NULL GROUP BY owner_name"
    ).fetchall():
        owner_counts[r["owner_name"]] = r["n"]
    by = {}
    for r in rows:
        d = dict(r)
        d["_score"] = score(r, owner_counts, today=today)
        by.setdefault(r["channel"], []).append(d)
    out = []
    for ch, label in CHANNELS.items():
        rs = by.get(ch, [])
        touched = sum(1 for r in rs if r["touch_count"])
        responded = sum(1 for r in rs if r["responded_at"] or r["status"] in ("responded", "quoted", "won"))
        quoted = sum(1 for r in rs if r["status"] in ("quoted", "won") or r["quote_amount"] is not None)
        won = sum(1 for r in rs if r["status"] == "won")
        revenue = sum(float(r["job_revenue"] or 0) for r in rs)
        cost = sum(float(r["touch_cost"] or 0) for r in rs)
        out.append(
            {
                "channel": ch,
                "label": label,
                "assigned": len(rs),
                "touched": touched,
                "responded": responded,
                "quoted": quoted,
                "won": won,
                "revenue": round(revenue, 2),
                "cost": round(cost, 2),
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


def comparison(results_rows):
    """Can the channels be ranked yet? ``{"fair": bool, "ready": bool,
    "reasons": [plain sentences]}``. ``fair`` means the channels got the same
    mix of leads; ``ready`` adds that each has enough contacts to judge."""
    active = [r for r in results_rows if r["assigned"]]
    reasons = []
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
                reasons.append(f"Many {r['label']} leads were set by hand rather than split with Assign leads.")
    fair = not reasons
    thin = [r["label"] for r in active if r["touched"] < MIN_CONTACTS]
    if fair and thin:
        reasons.append(
            f"Fewer than {MIN_CONTACTS} contacts logged for: {', '.join(thin)}. One job can still swing the ranking."
        )
    return {"fair": fair, "ready": fair and not thin, "reasons": reasons}
