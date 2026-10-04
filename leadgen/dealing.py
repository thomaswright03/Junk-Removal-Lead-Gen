"""Assign leads: the unassigned leads dealt across the outreach methods so
that the Results tab compares like with like.

A fair comparison
-----------------
The Results tab compares channels, so each channel has to get the same kind
of leads. ``assign`` makes that so:

- A round uses only leads that every ticked channel can work (door hangers
  need a property address, and a unit number or Steve's confirmation at an
  apartment or condo parcel), so no channel gets the leads the others can't.
  Asked to (``fit``), it then deals leads only some ticked methods can work
  to one they can use, outside the balanced split.
- Every lead of one landlord or owner goes to the same channel, in this round
  and later ones, so no company hears from two channels.
- Leads are grouped by kind (address or not, eviction or code case), sorted by
  score, and dealt in small blocks in random order, so each channel gets the
  same mix of kinds and a similar spread of scores.

``results.results`` reports the mix each channel actually got and
``results.comparison`` says whether the channels can be ranked yet.
"""

import random
import uuid
from typing import Any, Optional

from .outreach import CHANNELS, _get, door_hanger_problem, eligible_channels, has_contact, landlord_key, rank_key
from .util import Conn, LeadRow, now_iso

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
