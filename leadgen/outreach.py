"""Outreach experiment: score leads, split them across channels, measure
which channel turns leads into paid jobs for the least money.

Channels
--------
door_hanger       Steve (or a helper) leaves a hanger at the property.
phone             Call the owner (number looked up by hand; check Do Not Call).
property_manager  Call/email the landlord, property manager or LLC that owns
                  it, pitching a standing clean-out rate, not one job.

Each lead gets at most one channel so results are comparable. Touches (a visit, a
call, an email) carry a cost; the result of the lead (responded,
quoted, won and revenue) is credited to its channel.
"""

import math
import random
from datetime import date, datetime, timezone

from .tucson_codes import code_of

CHANNELS = {
    "door_hanger": "Door hanger at property",
    "phone": "Phone call to owner",
    "property_manager": "Landlord / property manager",
}

DEFAULT_SETTINGS = {
    "business_name": "Steve's Junk Removal",
    "business_phone": "",
    "base_address": "8790 N Wellside Dr, Tucson, AZ",
    "base_lat": None,
    "base_lon": None,
    "costs": {"door_hanger": 0.35, "phone": 0.0, "property_manager": 0.0},
    "tracking_numbers": {"door_hanger": "", "phone": "", "property_manager": ""},
    "templates": {
        "door_hanger": (
            "Need this property cleared? Junk, furniture, appliances, yard debris. "
            "Free quote: {phone}. {business}"
        ),
        "phone": (
            "Hi, this is Steve with {business}. I'm calling about {address}. "
            "We help owners clear junk and yard debris, including when the City has "
            "opened a case. Would a free quote help?"
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
    "VACANT": 40, "DUMP": 35, "PMMULT": 35, "REFS": 30, "RSTOR": 30, "DILAP": 30,
    "TREES": 15, "WEEDS": 15,
}


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
    today = today or date.today()
    points = 0
    if lead["lead_type"] == "eviction":
        points += 35
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


def assign(conn, leads, count, channels, seed=None):
    """Split the best ``count`` unassigned leads evenly across ``channels``.

    Leads are taken in score order and dealt out in rounds: within each round
    of len(channels) leads the order is shuffled, so every channel gets a
    similar mix of strong and weak leads. A lead only goes to a channel it is
    eligible for. Returns {channel: n}.
    """
    rng = random.Random(seed)
    channels = [c for c in channels if c in CHANNELS]
    pool = [l for l in leads if not l["channel"] and l["status"] == "new"]
    pool.sort(key=lambda l: l["score"], reverse=True)
    counts = {c: 0 for c in channels}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    picked = 0
    for lead in pool:
        if picked >= count:
            break
        options = [c for c in channels if c in eligible_channels(lead)]
        if not options:
            continue
        low = min(counts[c] for c in options)
        choices = [c for c in options if counts[c] == low]
        ch = rng.choice(choices)
        conn.execute("UPDATE leads SET channel = ?, assigned_at = ? WHERE id = ?",
                     (ch, now, lead["id"]))
        counts[ch] += 1
        picked += 1
    conn.commit()
    return counts


def results(conn):
    """Per-channel funnel and cost numbers."""
    rows = conn.execute(
        """
        SELECT l.channel,
               COUNT(*)                                             AS assigned,
               SUM(CASE WHEN t.n > 0 THEN 1 ELSE 0 END)             AS touched,
               SUM(CASE WHEN l.responded_at IS NOT NULL
                         OR l.status IN ('responded','quoted','won') THEN 1 ELSE 0 END) AS responded,
               SUM(CASE WHEN l.status IN ('quoted','won')
                         OR l.quote_amount IS NOT NULL THEN 1 ELSE 0 END) AS quoted,
               SUM(CASE WHEN l.status = 'won' THEN 1 ELSE 0 END)   AS won,
               COALESCE(SUM(l.job_revenue), 0)                      AS revenue,
               COALESCE(SUM(t.cost), 0)                             AS cost
        FROM leads l
        LEFT JOIN (SELECT lead_id, COUNT(*) AS n, SUM(cost) AS cost
                   FROM touches GROUP BY lead_id) t ON t.lead_id = l.id
        WHERE l.channel IS NOT NULL
        GROUP BY l.channel
        """
    ).fetchall()
    by = {r["channel"]: dict(r) for r in rows}
    out = []
    for ch, label in CHANNELS.items():
        r = by.get(ch, {"assigned": 0, "touched": 0, "responded": 0, "quoted": 0,
                        "won": 0, "revenue": 0, "cost": 0})
        touched = r["touched"] or 0
        out.append({
            "channel": ch,
            "label": label,
            **{k: r[k] or 0 for k in ("assigned", "touched", "responded", "quoted", "won")},
            "revenue": round(r["revenue"] or 0, 2),
            "cost": round(r["cost"] or 0, 2),
            "response_rate": (r["responded"] or 0) / touched if touched else None,
            "win_rate": (r["won"] or 0) / touched if touched else None,
            "cost_per_win": (r["cost"] / r["won"]) if r["won"] else None,
            "revenue_per_dollar": (r["revenue"] / r["cost"]) if r["cost"] else None,
        })
    return out
