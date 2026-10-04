"""The first phones: a short pass over the landlords of the best eviction
leads, so that a new list can be worked the day it arrives.

The court gives no phone number and no property address. Lead Desk looks up
company landlords by itself (OpenStreetMap, and Google Places when a key is
set), but it finds only some, and it never looks up a private landlord. The
pass lists the landlords of the open eviction leads in list order (the
landlord of the best lead first), ten at a time, each with its search links
and a box for the number: one number covers every lead of that landlord, so
ten numbers reach at least the top ten leads. A landlord Steve can't find a
number for can be skipped, so the next one moves up.

``auto_yield`` says what the automatic lookup has found so far, so the page
can set expectations plainly."""

from typing import Any

from . import db, leadlist, outreach
from .util import Conn

PASS_SIZE = 10
# Rows read to make up a pass (open eviction leads, best first). Far more
# than ten passes' worth of landlords.
_ROWS = 3000
# Most landlords kept as skipped.
MAX_SKIPPED = 500
SKIPPED_KEY = "phone_pass_skipped"

# Where a number came from when Lead Desk found it by itself.
MANUAL_SOURCES = ("manual", "import")


def _open_evictions(settings: dict) -> str:
    closed = ", ".join(repr(s) for s in leadlist.CLOSED)
    return f"{leadlist.in_view(settings)} AND lead_type = 'eviction' AND status NOT IN ({closed})"


def skipped(settings: dict) -> list[str]:
    value = settings.get(SKIPPED_KEY) or []
    return [str(k) for k in value] if isinstance(value, list) else []


def landlords(conn: Conn, settings: dict, offset: int = 0, size: int = PASS_SIZE) -> dict:
    """One pass: ``size`` landlords from ``offset`` (skipped ones left out),
    each ``{"key", "name", "lead_id", "source_id", "leads", "score",
    "stage", "reached", "phone", "email"}``: ``reached`` once every one of
    its open leads has a phone or email, and ``phone`` / ``email`` the first
    found on any of them (a number on one lead can be saved on the rest).
    ``lead_id`` is a lead of the landlord with no number when there is one. Also ``done`` (how many of them are
    reached), ``more`` (another pass after this one), ``skipped`` (how many
    landlords are skipped) and ``top`` (of the best ``size`` open eviction
    leads, how many can be reached now). The rank must be up to date
    (``leadlist.refresh_ranking``)."""
    leadlist.refresh_ranking(conn, settings)
    offset = max(0, int(offset or 0))
    skip = set(skipped(settings))
    rows = conn.execute(
        "SELECT id, source_id, plaintiff, owner_name, owner_phone, owner_email, rank_score, stage_rank "
        f"FROM leads WHERE {_open_evictions(settings)} "
        "ORDER BY stage_rank DESC, rank_score DESC, rank_latest DESC, id DESC LIMIT ?",
        (_ROWS,),
    ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = outreach.landlord_key(r)
        if key.startswith("lead:"):
            continue  # no landlord named: nobody to search for
        g = groups.get(key)
        if g is None:
            name = (r["plaintiff"] or r["owner_name"] or "").split(";")[0].strip()
            g = groups[key] = {
                "key": key,
                "name": name,
                "lead_id": r["id"],
                "source_id": r["source_id"],
                "leads": 0,
                "score": int(r["rank_score"] or 0),
                "stage": {2: "writ", 1: "judgment"}.get(int(r["stage_rank"] or 0), ""),
                "reached": True,
                "phone": None,
                "email": None,
            }
        g["leads"] += 1
        if r["owner_phone"] or r["owner_email"]:
            g["phone"] = g["phone"] or r["owner_phone"]
            g["email"] = g["email"] or r["owner_email"]
        else:
            if g["reached"]:
                g["lead_id"], g["source_id"] = r["id"], r["source_id"]  # save the number from a lead without one
            g["reached"] = False  # a lead of this landlord has no number yet
    order = [g for g in groups.values() if g["key"] not in skip]
    page = order[offset : offset + size]
    return {
        "offset": offset,
        "size": size,
        "landlords": page,
        "done": sum(1 for g in page if g["reached"]),
        "more": len(order) > offset + size,
        "skipped": len([k for k in skip if k in groups]),
        "top": top_reach(conn, settings, size),
    }


def top_reach(conn: Conn, settings: dict, size: int = PASS_SIZE) -> dict:
    """Of the best ``size`` open eviction leads (list order): how many there
    are (``leads``), how many can be reached now (``reached``), how many
    have a company landlord the free lookup can search for (``lookable``;
    it never looks up a private landlord), how many it has looked up
    (``looked_up``) and found a phone or email for by itself (``found``).
    The rank must be up to date."""
    manual = ", ".join(repr(s) for s in MANUAL_SOURCES)
    auto_found = (
        f"COALESCE(contact_source, '') NOT IN ({manual}, '') "
        f"AND {leadlist.HAS_CONTACT} AND contact_checked_at IS NOT NULL"
    )
    r = conn.execute(
        f"SELECT COUNT(*) AS n, SUM(CASE WHEN {leadlist.REACHABLE} THEN 1 ELSE 0 END) AS reached, "
        "SUM(CASE WHEN lookup_name IS NOT NULL THEN 1 ELSE 0 END) AS lookable, "
        "SUM(CASE WHEN contact_checked_at IS NOT NULL THEN 1 ELSE 0 END) AS looked_up, "
        f"SUM(CASE WHEN {auto_found} THEN 1 ELSE 0 END) AS found FROM "
        f"(SELECT * FROM leads WHERE {_open_evictions(settings)} "
        "ORDER BY stage_rank DESC, rank_score DESC, rank_latest DESC, id DESC LIMIT ?) best",
        (size,),
    ).fetchone()
    return {
        k: int(r[c] or 0) for k, c in (("leads", "n"), *((x, x) for x in ("reached", "lookable", "looked_up", "found")))
    }


def set_skipped(conn: Conn, key: Any, skip: bool) -> dict:
    """Skip a landlord in the pass (no number to be found), or bring it back;
    ``key`` "*" with ``skip`` false brings every skipped landlord back."""
    if not isinstance(key, str) or not key.strip() or len(key) > 300:
        raise ValueError("That landlord isn't in the list any more. Reload the page.")
    current = skipped(db.get_settings(conn))
    if key == "*" and not skip:
        current = []
    elif skip and key not in current:
        current = [*current, key][-MAX_SKIPPED:]
    elif not skip:
        current = [k for k in current if k != key]
    db.put_settings(conn, {SKIPPED_KEY: current})
    conn.commit()
    return {"ok": True, "skipped": len(current)}


def auto_yield(conn: Conn, settings: dict) -> dict:
    """What the automatic phone lookup has done for the open eviction leads:
    ``looked_up`` landlords it has searched for and ``found`` those it found
    a phone or email for (by itself, not typed or imported)."""
    who = "COALESCE(lookup_name, plaintiff, owner_name, source_id)"
    manual = ", ".join(repr(s) for s in MANUAL_SOURCES)
    found = (
        f"COALESCE(contact_source, '') NOT IN ({manual}, '') "
        f"AND {leadlist.HAS_CONTACT} AND contact_checked_at IS NOT NULL"
    )
    r = conn.execute(
        f"SELECT COUNT(DISTINCT CASE WHEN contact_checked_at IS NOT NULL THEN {who} END) AS looked_up, "
        f"COUNT(DISTINCT CASE WHEN {found} THEN {who} END) AS found "
        f"FROM leads WHERE {_open_evictions(settings)}"
    ).fetchone()
    return {"looked_up": int(r["looked_up"] or 0), "found": int(r["found"] or 0)}
