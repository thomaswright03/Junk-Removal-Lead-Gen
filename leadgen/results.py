"""The Results tab: each outreach method's funnel, cost and revenue, the mix
of leads it got, and whether the methods can be ranked yet."""

from datetime import date
from typing import Optional

from .dealing import BY_FIT, BY_HAND, FOLLOWED, how_assigned
from .outreach import CHANNELS, door_hanger_problem, score
from .util import Conn


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
