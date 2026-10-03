"""The lead list Lead Desk shows: which leads are in the chosen view, the
columns the page gets for each, and the server-side filtering, sorting and
paging that keep every response small however many leads there are."""

from . import outreach
from .tucson_codes import CODE_LABELS, code_of
from .util import az_today

LEAD_FIELDS = (
    "id",
    "source",
    "source_id",
    "lead_type",
    "event_date",
    "address",
    "city",
    "zip",
    "lat",
    "lon",
    "parcel",
    "plaintiff",
    "defendant",
    "description",
    "url",
    "status",
    "notes",
    "first_seen",
    "owner_name",
    "owner_address",
    "owner_city",
    "owner_state",
    "owner_zip",
    "owner_absentee",
    "owner_entity",
    "property_use",
    "year_built",
    "enriched_at",
    "channel",
    "assigned_at",
    "responded_at",
    "quote_amount",
    "job_revenue",
    "owner_phone",
    "owner_email",
    "owner_website",
    "contact_source",
    "contact_name",
    "contact_checked_at",
    "eviction_notice",
    "case_status",
    "next_court_date",
    "case_checked_at",
    "unit",
    "address_source",
    "case_stage",
    "judgment_date",
    "writ_date",
    "added_by_hand",
)
# The default view: eviction cases with a notice filed or further along
# (judgment, writ), plus cases Steve imported himself whose case page hasn't
# been read yet (so an import shows up at once, marked "case not checked";
# once read, the notice rule applies). Dismissed cases, and closed ones that
# never reached a judgment, drop out.
_ENDED = (
    "(COALESCE(case_stage, '') = 'dismissed' OR LOWER(COALESCE(case_status, '')) LIKE 'dismiss%' "
    "OR (LOWER(COALESCE(case_status, '')) LIKE 'closed%' "
    "AND COALESCE(case_stage, '') NOT IN ('judgment', 'writ')))"
)
LEAD_VIEWS = {
    "eviction_notice": (
        "lead_type = 'eviction' AND (eviction_notice = 1 OR case_stage IN ('judgment', 'writ') "
        f"OR (added_by_hand = 1 AND eviction_notice IS NULL)) AND NOT {_ENDED}"
    ),
    "evictions": "lead_type = 'eviction'",
    "all": "1=1",
}
# Statuses the "Open" filter hides, and those "active" (work lists) hides.
CLOSED = ("stale", "skip", "lost", "won")
INACTIVE = ("stale", "skip")
PAGE_SIZE = 100
MAX_PAGE = 500
# Search box: these columns are searched, case-insensitively.
SEARCHED = (
    "address",
    "owner_name",
    "plaintiff",
    "defendant",
    "source_id",
    "description",
    "parcel",
    "notes",
    "owner_phone",
    "owner_email",
    "contact_name",
)


def in_view(settings):
    """SQL condition for the leads the chosen view shows."""
    view = LEAD_VIEWS.get((settings or {}).get("lead_view")) or LEAD_VIEWS["eviction_notice"]
    return f"duplicate_of IS NULL AND (in_pima = 1 OR in_pima IS NULL) AND {view}"


def lead_dict(r, settings, owner_counts, today=None):
    """One lead as the page gets it: its columns plus priority, what its date
    is, the latest court event, and which outreach methods can work it."""
    r = dict(zip(r.keys(), r))  # one plain dict: much faster to read than a database row
    d = {k: r[k] for k in LEAD_FIELDS}
    code = code_of(r["description"]) if r["lead_type"] == "code_violation" else None
    d["code"] = code
    d["code_label"] = CODE_LABELS.get(code) or (
        "Vacant / nuisance building" if "VACANT/NUISANCE" in (r["description"] or "").upper() else None
    )
    d["score"] = outreach.score(r, owner_counts, today=today)
    d["owner_lead_count"] = owner_counts.get(r["owner_name"], 0) if r["owner_name"] else 0
    d["eligible"] = outreach.eligible_channels(r)
    d["door_hanger_problem"] = outreach.door_hanger_problem(r)
    d["date_label"] = outreach.date_label(r)
    d["latest_label"], d["latest_date"] = outreach.latest_event(r, today)
    d["miles"] = outreach.miles_between(settings.get("base_lat"), settings.get("base_lon"), r["lat"], r["lon"])
    return d


def lead_dicts(conn, settings, today=None):
    """Every lead in the view (without its contact history)."""
    rows = conn.execute(f"SELECT * FROM leads WHERE {in_view(settings)} ORDER BY event_date DESC, id DESC").fetchall()
    owner_counts = {}
    for r in rows:
        if r["owner_name"]:
            owner_counts[r["owner_name"]] = owner_counts.get(r["owner_name"], 0) + 1
    today = today or az_today()
    return [lead_dict(r, settings, owner_counts, today) for r in rows]


def attach_touches(conn, leads, everything=False):
    """Add each lead's logged contacts (``touches``), oldest first."""
    by_lead = {}
    if everything:
        rows = conn.execute("SELECT * FROM touches ORDER BY id").fetchall()
    else:
        ids = [l["id"] for l in leads]
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            if chunk:
                rows += conn.execute(
                    f"SELECT * FROM touches WHERE lead_id IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk
                ).fetchall()
    for t in rows:
        by_lead.setdefault(t["lead_id"], []).append(dict(t))
    for l in leads:
        l["touches"] = by_lead.get(l["id"], [])
    return leads


def one_lead(conn, settings, lead_id):
    """One lead with its contact history, whether or not the view shows it."""
    r = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not r:
        return None
    owner_counts = {}
    if r["owner_name"]:
        n = conn.execute(
            f"SELECT COUNT(*) AS n FROM leads WHERE {in_view(settings)} AND owner_name = ?", (r["owner_name"],)
        ).fetchone()["n"]
        owner_counts[r["owner_name"]] = n
    return attach_touches(conn, [lead_dict(r, settings, owner_counts)])[0]


def _keep(l, p, touched):
    status = p.get("status", "open")
    if status == "open" and l["status"] in CLOSED:
        return False
    if status == "active" and l["status"] in INACTIVE:
        return False
    if status not in ("", "open", "active") and l["status"] != status:
        return False
    kind = p.get("type") or ""
    tests = {
        "eviction": l["lead_type"] == "eviction",
        "code_violation": l["lead_type"] == "code_violation",
        "absentee": bool(l["owner_absentee"]),
        "entity": bool(l["owner_entity"]),
        "has_phone": bool(l["owner_phone"]),
        "no_phone": not l["owner_phone"],
        "no_address": not l["address"],
        "guessed_address": l["address_source"] == "landlord",
    }
    if kind in tests and not tests[kind]:
        return False
    channel = p.get("channel") or ""
    if channel == "none" and l["channel"]:
        return False
    if channel and channel != "none" and l["channel"] != channel:
        return False
    if p.get("untouched") and l["id"] in touched:
        return False
    q = (p.get("q") or "").strip().lower()
    if q and q not in " ".join(str(l[k] or "") for k in SEARCHED).lower():
        return False
    return True


def sort_leads(leads, key="score"):
    """Highest priority first; or newest first by the latest real event
    (filing, judgment or writ; cases not read yet, which only have a hearing
    date, come last); or closest first."""
    by_date = lambda l: (l["latest_date"] or "", l["id"])
    if key == "date":
        leads.sort(key=by_date, reverse=True)
    elif key == "miles":
        leads.sort(key=lambda l: (l["miles"] is None, l["miles"] or 0, -l["id"]))
    else:
        leads.sort(key=by_date, reverse=True)
        leads.sort(key=lambda l: -l["score"])  # stable: newest first among equal scores
    return leads


def _int(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def page(conn, settings, params):
    """One page of the filtered, sorted lead list: ``{"leads", "total",
    "offset", "limit"}``. ``params`` are the page's filters (status, type,
    channel, q, sort, untouched) and ``offset`` / ``limit``."""
    leads = lead_dicts(conn, settings)
    touched = set()
    if params.get("untouched"):
        touched = {r["lead_id"] for r in conn.execute("SELECT DISTINCT lead_id FROM touches").fetchall()}
    rows = sort_leads([l for l in leads if _keep(l, params, touched)], params.get("sort") or "score")
    limit = _int(params.get("limit"), PAGE_SIZE, 1, MAX_PAGE)
    offset = _int(params.get("offset"), 0, 0, max(0, len(rows) - 1))
    shown = attach_touches(conn, rows[offset : offset + limit])
    return {"leads": shown, "total": len(rows), "offset": offset, "limit": limit}


def counts(conn, settings):
    """The numbers the header and the Outreach tab show, counted in the database."""
    inactive = ", ".join(f"'{s}'" for s in INACTIVE)
    untouched = "NOT EXISTS (SELECT 1 FROM touches t WHERE t.lead_id = leads.id)"
    r = conn.execute(
        "SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN status NOT IN ({inactive}) THEN 1 ELSE 0 END) AS active, "
        f"SUM(CASE WHEN status NOT IN ({inactive}) AND channel IS NOT NULL THEN 1 ELSE 0 END) AS assigned, "
        "SUM(CASE WHEN enriched_at IS NULL THEN 1 ELSE 0 END) AS owners_pending, "
        "SUM(CASE WHEN owner_phone IS NOT NULL THEN 1 ELSE 0 END) AS with_phone, "
        "SUM(CASE WHEN owner_email IS NOT NULL THEN 1 ELSE 0 END) AS with_email, "
        "SUM(CASE WHEN channel IS NULL AND status = 'new' THEN 1 ELSE 0 END) AS unassigned "
        f"FROM leads WHERE {in_view(settings)}"
    ).fetchone()
    out = {k: int(r[k] or 0) for k in ("total", "active", "assigned", "owners_pending", "with_phone", "with_email")}
    out["unassigned"] = int(r["unassigned"] or 0)
    out["channels"] = {c: {"active": 0, "to_do": 0} for c in outreach.CHANNELS}
    for row in conn.execute(
        "SELECT channel, COUNT(*) AS n, "
        f"SUM(CASE WHEN status = 'new' AND {untouched} THEN 1 ELSE 0 END) AS to_do "
        f"FROM leads WHERE {in_view(settings)} AND status NOT IN ({inactive}) AND channel IS NOT NULL "
        "GROUP BY channel"
    ).fetchall():
        if row["channel"] in out["channels"]:
            out["channels"][row["channel"]] = {"active": int(row["n"] or 0), "to_do": int(row["to_do"] or 0)}
    return out
