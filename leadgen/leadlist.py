"""The lead list Lead Desk shows: which leads are in the chosen view, the
columns the page gets for each, and the server-side filtering, sorting and
paging that keep every response small however many leads there are."""

import copy
import json
import math
import sqlite3
import threading
import uuid
from collections import OrderedDict
from datetime import date, timedelta
from typing import Any, Callable, Optional

from . import db, outreach
from .business import lookup_targets
from .sources.pima_jp_case import ENDED_SQL
from .tucson_codes import CODE_LABELS, code_of
from .util import Conn, LeadRow, az_today, is_multifamily

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
# Evictions with a notice filed or further along (judgment, writ), plus
# cases Steve imported himself whose case page hasn't been read yet (so an
# import shows up at once, marked "case not checked"; once read, the notice
# rule applies). Cases that ended (dismissed, judgment satisfied, closed or
# disposed with no judgment or writ) drop out; see pima_jp_case.ENDED_SQL.
_ENDED = ENDED_SQL
_EVICTION_NOTICE = (
    "lead_type = 'eviction' AND (eviction_notice = 1 OR case_stage IN ('judgment', 'writ') "
    f"OR (added_by_hand = 1 AND eviction_notice IS NULL)) AND NOT {_ENDED}"
)
LEAD_VIEWS = {
    # The default (the owner's rule): evictions with a notice or further along.
    "eviction_notice": _EVICTION_NOTICE,
    "evictions": "lead_type = 'eviction'",
    "all": "1=1",
}
DEFAULT_VIEW = "eviction_notice"
# A property address that isn't a guess from the landlord's parcels.
KNOWN_ADDRESS = "(address IS NOT NULL AND COALESCE(address_source, '') <> 'landlord')"
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


# ---- remembered numbers -----------------------------------------------------
# Counts over every lead (the header, the view menu, Results...) are kept in
# memory and used again until a lead or a logged contact changes (the
# database's data version, see db._version_counters), so a search or a new
# page doesn't count the whole table again.
_MEMO: "OrderedDict[tuple, Any]" = OrderedDict()
_MEMO_LOCK = threading.Lock()
MEMO_SIZE = 64


def data_version(conn: Conn) -> Optional[tuple]:
    """``(database token, data version)``, or None when the database has none."""
    found = {
        r["key"]: r["n"]
        for r in conn.execute("SELECT key, n FROM counters WHERE key IN ('db_token', 'data_version')").fetchall()
    }
    if "db_token" not in found or "data_version" not in found:
        return None
    return (found["db_token"], found["data_version"])


def memo(conn: Conn, key: Any, work: Callable[[], Any]) -> Any:
    """``work()``, or what it returned before for the same ``key`` when no
    lead or contact has changed since. ``key`` must name everything else the
    answer depends on (settings, today...)."""
    version = data_version(conn)
    if version is None:
        return work()
    full = (version, json.dumps(key, sort_keys=True, default=str))
    with _MEMO_LOCK:
        if full in _MEMO:
            _MEMO.move_to_end(full)
            return copy.deepcopy(_MEMO[full])
    value = work()
    with _MEMO_LOCK:
        _MEMO[full] = copy.deepcopy(value)
        while len(_MEMO) > MEMO_SIZE:
            _MEMO.popitem(last=False)
    return value


def status_condition(status: str) -> tuple[str, list]:
    """SQL condition and arguments for the Status filter (see ``_keep``)."""
    if status == "open":
        return f"status NOT IN ({', '.join(repr(s) for s in CLOSED)})", []
    if status == "active":
        return f"status NOT IN ({', '.join(repr(s) for s in INACTIVE)})", []
    if status:
        return "status = ?", [status]
    return "1=1", []


def view_counts(conn: Conn, status: str = "open") -> dict[str, int]:
    """How many leads each choice of "Show" has, with the Status filter the
    list uses, so the number next to a view is what the list shows."""
    cond, args = status_condition(status)
    sums = ", ".join(f"SUM(CASE WHEN {sql} THEN 1 ELSE 0 END) AS v_{name}" for name, sql in LEAD_VIEWS.items())
    r = conn.execute(
        f"SELECT {sums}, "
        # Case pages not read yet: hidden from the notice views until they are.
        "SUM(CASE WHEN lead_type = 'eviction' AND case_checked_at IS NULL "
        "AND url LIKE '%jcDisplayCase%' THEN 1 ELSE 0 END) AS v_unchecked, "
        # City code cases: only in "All leads", so the menu says how many there are.
        "SUM(CASE WHEN lead_type = 'code_violation' THEN 1 ELSE 0 END) AS v_code_cases "
        f"FROM leads WHERE duplicate_of IS NULL AND (in_pima = 1 OR in_pima IS NULL) AND {cond}",
        args,
    ).fetchone()
    return {name: int(r["v_" + name] or 0) for name in (*LEAD_VIEWS, "unchecked", "code_cases")}


def in_view(settings: Optional[dict]) -> str:
    """SQL condition for the leads the chosen view shows."""
    view = LEAD_VIEWS.get(str((settings or {}).get("lead_view"))) or LEAD_VIEWS[DEFAULT_VIEW]
    return f"duplicate_of IS NULL AND (in_pima = 1 OR in_pima IS NULL) AND {view}"


def reach(lead: Any) -> str:
    """How Steve can reach a lead now: ``"both"`` (a phone or email and a
    usable property address), ``"contact"`` (phone or email only),
    ``"address"`` (a usable address only: a door hanger or a visit) or
    ``"none"``. Usable is what a door hanger can go to (see
    ``outreach.door_hanger_problem``): not a guess from the landlord's
    parcels until confirmed, and with a unit number where the parcel has
    several homes. The Outreach split uses the same rule."""
    contact = outreach.has_contact(lead)
    address = outreach.door_hanger_problem(lead) is None
    return "both" if contact and address else "contact" if contact else "address" if address else "none"


def lead_dict(r: LeadRow, settings: dict, owner_counts: dict, today: Optional[date] = None) -> dict:
    """One lead as the page gets it: its columns plus priority, what its date
    is, the latest court event, and which outreach methods can work it."""
    r = dict(zip(r.keys(), r))  # one plain dict: much faster to read than a database row
    d = {k: r[k] for k in LEAD_FIELDS}
    # Money is kept in whole cents; the page gets dollars.
    d["quote_amount"] = db.dollars(r.get("quote_cents"))
    d["job_revenue"] = db.dollars(r.get("revenue_cents"))
    code = code_of(r["description"]) if r["lead_type"] == "code_violation" else None
    d["code"] = code
    d["code_label"] = CODE_LABELS.get(code or "") or (
        "Vacant / nuisance building" if "VACANT/NUISANCE" in (r["description"] or "").upper() else None
    )
    d["score_parts"] = outreach.score_parts(r, owner_counts, today=today)
    d["score"] = sum(points for _label, points in d["score_parts"])
    d["stage_rank"] = outreach.stage_rank(r, today)
    d["owner_lead_count"] = owner_counts.get(r["owner_name"], 0) if r["owner_name"] else 0
    d["owner_first"] = outreach.owner_first_name(r)
    # Methods that can work it now (Assign leads), and those that could
    # once a phone or email is found (the lead page lets Steve pick those).
    d["ready"] = outreach.eligible_channels(r)
    d["eligible"] = outreach.eligible_channels(r, need_contact=False)
    d["door_hanger_problem"] = outreach.door_hanger_problem(r)
    d["multi_home"] = is_multifamily(r["property_use"])  # more than one home on the parcel
    d["reach"] = reach(r)
    d["date_label"] = outreach.date_label(r)
    d["latest_label"], d["latest_date"] = outreach.latest_event(r, today)
    d["miles"] = outreach.miles_between(settings.get("base_lat"), settings.get("base_lon"), r["lat"], r["lon"])
    return d


def lead_dicts(conn: Conn, settings: dict, today: Optional[date] = None) -> list[dict]:
    """Every lead in the view (without its contact history)."""
    rows = conn.execute(f"SELECT * FROM leads WHERE {in_view(settings)} ORDER BY event_date DESC, id DESC").fetchall()
    owner_counts: dict[str, int] = {}
    for r in rows:
        if r["owner_name"]:
            owner_counts[r["owner_name"]] = owner_counts.get(r["owner_name"], 0) + 1
    today = today or az_today()
    return [lead_dict(r, settings, owner_counts, today) for r in rows]


def attach_touches(conn: Conn, leads: list[dict], everything: bool = False) -> list[dict]:
    """Add each lead's logged contacts (``touches``), oldest first."""
    by_lead: dict[Any, list] = {}
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
        t = dict(t)
        t["cost"] = db.dollars(t.get("cost_cents")) or 0
        by_lead.setdefault(t["lead_id"], []).append(t)
    for l in leads:
        l["touches"] = by_lead.get(l["id"], [])
    return leads


def one_lead(conn: Conn, settings: dict, lead_id: int) -> Optional[dict]:
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


def sort_leads(leads: list[dict], key: str = "score") -> list[dict]:
    """Highest priority first (recent writ cases, then recent judgments, then the rest, each
    by priority; see ``outreach.rank_key``); or newest first by the latest real event
    (filing, judgment or writ; cases not read yet, which only have a hearing
    date, come last); or closest first."""
    by_date = lambda l: (l["latest_date"] or "", l["id"])
    if key == "contact":
        # Work lists: leads with a phone number first, then an email, each
        # by priority, so the calls Steve can make come before the searches.
        leads.sort(key=by_date, reverse=True)
        leads.sort(key=lambda l: (not l["owner_phone"], not l["owner_email"], *outreach.rank_key(l)))
    elif key == "date":
        leads.sort(key=by_date, reverse=True)
    elif key == "miles":
        leads.sort(key=lambda l: (l["miles"] is None, l["miles"] or 0, -l["id"]))
    else:
        leads.sort(key=by_date, reverse=True)
        leads.sort(key=outreach.rank_key)  # stable: newest first among equal scores
    return leads


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


# ---- ranking, filtering and paging in the database -------------------------
#
# The list is filtered, ranked and paged in SQL, so a request costs about the
# same however many leads have built up: priority (``outreach.score_parts``)
# and the rest of a lead's page fields are worked out in Python only for the
# page shown. For that, each lead keeps its rank in columns (see
# db.RANK_COLUMNS): its stage (writ, judgment, the rest), its priority in the
# chosen view, its latest real event, and a few things easier to work out in
# Python than in SQL (a code case's violation code, whether its parcel has
# several homes, the company a phone lookup would search for).
#
# A database trigger empties ``derived_src`` whenever a column the rank comes
# from changes (db.RANK_INPUTS), and every new row starts empty, so
# ``refresh_ranking`` only works out those rows again; priorities are worked
# out again for the whole view when any row changed (an owner's lead count is
# shared between leads), when the view changes, and once a day (recency).
# tests/test_lead_list.py checks the stored rank against the Python rules.

# How long SQLite waits on another connection's write by default (sqlite3's 5 s).
SQLITE_BUSY_MS = 5000


def _truthy(col: str) -> str:
    return f"COALESCE({col}, 0) <> 0"


def _filled(col: str) -> str:
    return f"COALESCE({col}, '') <> ''"


def _iso_or_null(col: str, today_iso: str) -> str:
    """The column's date when it is an ISO date on or before today, else NULL."""
    return f"CASE WHEN {col} LIKE '____-__-__%' AND SUBSTR({col}, 1, 10) <= '{today_iso}' THEN SUBSTR({col}, 1, 10) END"


def _later(a: str, b: str) -> str:
    """The later of two ISO dates either of which may be NULL."""
    return f"CASE WHEN {b} IS NULL OR {a} >= {b} THEN COALESCE({a}, {b}) ELSE {b} END"


def _rank_sets(today: date) -> str:
    """SET clauses for a row's points that don't depend on other rows or the
    day (kind of lead, absentee and company owner; see
    ``outreach.score_parts``) and its latest real event on or before today
    (``outreach.latest_event``; '' when none). The stage's rank and points
    depend on the day (a judgment stops counting as recent), so they are
    worked out with the day's priorities (``_stage_sql``)."""
    t = today.isoformat()
    hearing = "(lead_type IN ('eviction', 'civil') AND source = 'pima_jp_calendar' AND case_checked_at IS NULL)"
    filed = f"CASE WHEN {hearing} THEN NULL ELSE {_iso_or_null('event_date', t)} END"
    latest = _later(_later(filed, _iso_or_null("judgment_date", t)), _iso_or_null("writ_date", t))
    points = " ".join(f"WHEN '{code}' THEN {n}" for code, n in outreach._TYPE_POINTS.items())
    return (
        f"base_points = (CASE WHEN lead_type = 'eviction' THEN 35 ELSE CASE rank_code {points} ELSE 20 END END"
        f" + CASE WHEN {_truthy('owner_absentee')} AND lead_type <> 'eviction' THEN 20 ELSE 0 END"
        f" + CASE WHEN {_truthy('owner_entity')} THEN 10 ELSE 0 END), "
        f"rank_latest = COALESCE({latest}, '')"
    )


def _stage_sql(today: date, values: dict[str, int]) -> str:
    """``values[stage]`` for an eviction at a writ or judgment whose latest
    event is recent (``outreach.fresh_stage``), else 0."""
    cutoff = (today - timedelta(days=outreach.STAGE_FRESH_DAYS)).isoformat()
    fresh = f"lead_type = 'eviction' AND rank_latest >= '{cutoff}'"
    whens = " ".join(f"WHEN {fresh} AND case_stage = '{stage}' THEN {n}" for stage, n in values.items())
    return f"(CASE {whens} ELSE 0 END)"


def _case_of(column: str, rows: list[tuple], kind: str) -> tuple[str, list]:
    """``CAST(CASE id WHEN ? THEN ? ... END AS kind)`` for ``[(id, value)]``."""
    sql = " ".join("WHEN ? THEN ?" for _ in rows)
    return f"{column} = CAST(CASE id {sql} END AS {kind})", [x for pair in rows for x in pair]


def _view_name(settings: Optional[dict]) -> str:
    view = str((settings or {}).get("lead_view"))
    return view if view in LEAD_VIEWS else DEFAULT_VIEW


# Postgres errors that mean another writer got there first (deadlock,
# serialization failure, lock not available): the next request catches up.
_PG_BUSY = ("40P01", "40001", "55P03")


def _busy(e: Exception) -> bool:
    if isinstance(e, sqlite3.OperationalError):
        return "locked" in str(e) or "busy" in str(e)
    return getattr(e, "sqlstate", None) in _PG_BUSY


def refresh_ranking(conn: Conn, settings: dict, today: Optional[date] = None) -> bool:
    """Bring the stored rank up to date (see the notes above): rows a change
    emptied, then every priority when anything changed, the view changed or
    a day passed. Returns True when anything was worked out again.

    On SQLite another connection may be writing (the daily check): rather
    than wait on it, this request uses the rank as stored and a later one
    catches up."""
    today = today or az_today()
    lite = isinstance(conn, sqlite3.Connection)
    if lite:
        conn.execute("PRAGMA busy_timeout = 200")
    try:
        stored = db.get_settings(conn)
        view, day = _view_name(settings), today.isoformat()
        last_day = stored.get("rank_day")
        changed = last_day != day or stored.get("rank_view") != view
        if last_day and last_day != day:
            # An event dated after the day ranks were worked out may now be in the past.
            conn.execute(
                "UPDATE leads SET derived_src = NULL WHERE derived_src IS NOT NULL AND ("
                + " OR ".join(f"SUBSTR({c}, 1, 10) > ?" for c in ("event_date", "judgment_date", "writ_date"))
                + ")",
                [last_day] * 3,
            )
        # Claim the rows to work out with a token: a row changed while this
        # runs is emptied again by the trigger, so it isn't marked done here.
        token = f"pending {uuid.uuid4().hex}"
        claimed = conn.execute(
            # (Written so the index on derived_src finds them: no scan when none.)
            "UPDATE leads SET derived_src = ? WHERE derived_src IS NULL OR derived_src < ? OR derived_src > ?",
            (token, db.RANK_VERSION, db.RANK_VERSION),
        ).rowcount
        if claimed:
            changed = True
            # Rows per statement: 7 parameters each, under SQLite's limit
            # (999 before SQLite 3.32, 32,766 since).
            step = 100 if lite and sqlite3.sqlite_version_info < (3, 32) else 1000
            rows = conn.execute(
                "SELECT id, description, property_use, plaintiff, owner_name, address FROM leads WHERE derived_src = ?",
                (token,),
            ).fetchall()
            for i in range(0, len(rows), step):
                chunk = rows[i : i + step]
                codes, multis, names = [], [], []
                for r in chunk:
                    desc = r["description"] or ""
                    codes.append((r["id"], "VACANT" if "VACANT/NUISANCE" in desc.upper() else code_of(desc)))
                    multis.append((r["id"], int(is_multifamily(r["property_use"]))))
                    found, _site = lookup_targets(r)
                    names.append((r["id"], found[0].upper() if found else None))
                parts = [
                    _case_of("rank_code", codes, "TEXT"),
                    _case_of("multi_home", multis, "INTEGER"),
                    _case_of("lookup_name", names, "TEXT"),
                ]
                ids = [r["id"] for r in chunk]
                conn.execute(
                    f"UPDATE leads SET {', '.join(sql for sql, _ in parts)} "
                    f"WHERE derived_src = ? AND id IN ({','.join('?' * len(ids))})",
                    [x for _, args in parts for x in args] + [token, *ids],
                )
            conn.execute(
                f"UPDATE leads SET {_rank_sets(today)}, derived_src = ? WHERE derived_src = ?", (db.RANK_VERSION, token)
            )
        if changed:
            # Priority in the view: add a repeat owner's points and recency.
            c7, c14 = (today - timedelta(days=7)).isoformat(), (today - timedelta(days=14)).isoformat()
            repeat = (
                f"SELECT owner_name FROM leads o WHERE {in_view(settings)} AND COALESCE(owner_name, '') <> '' "
                "GROUP BY owner_name HAVING COUNT(*) > 1"
            )
            score = (
                f"(COALESCE(base_points, 0) + {_stage_sql(today, outreach.STAGE_POINTS)} "
                f"+ CASE WHEN rank_latest >= '{c7}' THEN 15 WHEN rank_latest >= '{c14}' THEN 8 ELSE 0 END "
                f"+ CASE WHEN owner_name IN ({repeat}) THEN 10 ELSE 0 END)"
            )
            stage = _stage_sql(today, outreach.STAGE_RANK)
            conn.execute(
                f"UPDATE leads SET rank_score = {score}, stage_rank = {stage} "
                f"WHERE COALESCE(rank_score, -1) <> {score} OR COALESCE(stage_rank, -1) <> {stage}"
            )
            db.put_settings(conn, {"rank_day": day, "rank_view": view})
        conn.commit()
        return changed
    except Exception as e:
        if not _busy(e):
            raise
        if lite:
            conn.rollback()
        return False
    finally:
        if lite:
            conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_MS}")


# A door hanger can go to the address (outreach.door_hanger_problem is None).
DOOR_HANGER_OK = (
    f"({_filled('address')} AND ({_filled('unit')} OR COALESCE(address_source, '') = 'confirmed' "
    f"OR (COALESCE(address_source, '') <> 'landlord' AND COALESCE(multi_home, 0) = 0)))"
)
# A phone number or email for the owner or landlord (outreach.has_contact).
HAS_CONTACT = f"({_filled('owner_phone')} OR {_filled('owner_email')})"
# Can be called, emailed or visited now (``reach`` is not "none").
REACHABLE = f"({HAS_CONTACT} OR {DOOR_HANGER_OK})"
_TYPE_FILTERS = {
    "eviction": "lead_type = 'eviction'",
    "code_violation": "lead_type = 'code_violation'",
    "absentee": _truthy("owner_absentee"),
    "entity": _truthy("owner_entity"),
    "has_phone": _filled("owner_phone"),
    "no_phone": f"NOT {_filled('owner_phone')}",
    "no_address": f"NOT {_filled('address')}",
    "guessed_address": "address_source = 'landlord'",
    # The address work queue: evictions with no address a door hanger can go to.
    "address_work": f"lead_type = 'eviction' AND NOT {DOOR_HANGER_OK}",
    "reachable": REACHABLE,
    "unreachable": f"NOT {REACHABLE}",
}


def _order_sql(key: str, settings: dict) -> str:
    """ORDER BY for ``sort_leads``'s orders, on the stored rank."""
    newest = "rank_latest DESC, id DESC"
    best = f"stage_rank DESC, rank_score DESC, {newest}"
    if key == "contact":
        return (
            f"CASE WHEN {_filled('owner_phone')} THEN 0 ELSE 1 END, "
            f"CASE WHEN {_filled('owner_email')} THEN 0 ELSE 1 END, {best}"
        )
    if key == "date":
        return newest
    if key == "miles":
        lat, lon = outreach._num(settings.get("base_lat")), outreach._num(settings.get("base_lon"))
        if lat is None or lon is None:
            return "id DESC"
        # Straight-line distance on a flat map: the same order as the
        # great-circle miles shown, at the scale of one county.
        k = math.cos(math.radians(lat))
        dist = f"((lat - {lat!r}) * (lat - {lat!r}) + (lon - {lon!r}) * {k!r} * (lon - {lon!r}) * {k!r})"
        return f"CASE WHEN lat IS NULL OR lon IS NULL THEN 1 ELSE 0 END, {dist}, id DESC"
    return best


def _where_sql(params: dict) -> tuple[str, list]:
    """The page's filters (Status, kind, outreach method, untouched, search) as SQL."""
    cond, args = status_condition(params.get("status", "open"))
    where = [cond]
    kind = params.get("type") or ""
    if kind in _TYPE_FILTERS:
        where.append(_TYPE_FILTERS[kind])
    channel = params.get("channel") or ""
    if channel == "none":
        where.append(f"NOT {_filled('channel')}")
    elif channel:
        where.append("channel = ?")
        args.append(channel)
    if params.get("untouched"):
        where.append("NOT EXISTS (SELECT 1 FROM touches t WHERE t.lead_id = leads.id)")
    q = (params.get("q") or "").strip().lower()
    if q:
        text = " || ' ' || ".join(f"COALESCE({k}, '')" for k in SEARCHED)
        like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(f"LOWER({text}) LIKE ? ESCAPE '\\'")
        args.append(f"%{like}%")
    return " AND ".join(f"({w})" for w in where), args


def owner_counts_for(conn: Conn, settings: dict, names: list) -> dict:
    """How many leads in the view each of these owners has."""
    names = sorted({n for n in names if n})
    out: dict[str, int] = {}
    for i in range(0, len(names), 500):
        chunk = names[i : i + 500]
        for r in conn.execute(
            f"SELECT owner_name, COUNT(*) AS n FROM leads WHERE {in_view(settings)} "
            f"AND owner_name IN ({','.join('?' * len(chunk))}) GROUP BY owner_name",
            chunk,
        ).fetchall():
            out[r["owner_name"]] = int(r["n"])
    return out


def page(
    conn: Conn,
    settings: dict,
    params: dict,
    today: Optional[date] = None,
    view: Optional[str] = None,
    refresh: bool = True,
) -> dict:
    """One page of the filtered, sorted lead list: ``{"leads", "total",
    "offset", "limit"}``. ``params`` are the page's filters (status, type,
    channel, q, sort, untouched) and ``offset`` / ``limit``. Filtering,
    ranking and paging happen in the database; only the page's leads are
    built in Python. ``view`` lists another view's leads (ranked as in the
    chosen one). ``refresh=False`` when the caller has just brought the
    rank up to date (``refresh_ranking``)."""
    today = today or az_today()
    if refresh:
        refresh_ranking(conn, settings, today)
    where, args = _where_sql(params)
    shown_view = in_view({"lead_view": view}) if view else in_view(settings)
    base = f"FROM leads WHERE {shown_view} AND {where}"
    total = memo(
        conn, ("total", base, args), lambda: int(conn.execute(f"SELECT COUNT(*) AS n {base}", args).fetchone()["n"])
    )
    limit = _int(params.get("limit"), PAGE_SIZE, 1, MAX_PAGE)
    # Past the end (the list shrank since the page was drawn): the last page.
    last_page = (max(0, total - 1) // limit) * limit
    offset = _int(params.get("offset"), 0, 0, last_page)
    rows = conn.execute(
        f"SELECT * {base} ORDER BY {_order_sql(params.get('sort') or 'score', settings)} LIMIT ? OFFSET ?",
        [*args, limit, offset],
    ).fetchall()
    owners = owner_counts_for(conn, settings, [r["owner_name"] for r in rows])
    shown = attach_touches(conn, [lead_dict(r, settings, owners, today) for r in rows])
    return {"leads": shown, "total": total, "offset": offset, "limit": limit}


def counts(conn: Conn, settings: dict, refresh: bool = True) -> dict:
    """The numbers the header and the Outreach tab show, counted in the database."""
    if refresh:
        refresh_ranking(conn, settings)
    inactive = ", ".join(f"'{s}'" for s in INACTIVE)
    closed = ", ".join(f"'{s}'" for s in CLOSED)
    untouched = "NOT EXISTS (SELECT 1 FROM touches t WHERE t.lead_id = leads.id)"
    r = conn.execute(
        "SELECT COUNT(*) AS total, "
        # Open leads that can be called, emailed or visited now: until there is
        # one, the Outreach and Results tabs say what unlocks them.
        f"SUM(CASE WHEN status NOT IN ({closed}) AND {REACHABLE} THEN 1 ELSE 0 END) AS reachable, "
        f"SUM(CASE WHEN status NOT IN ({inactive}) THEN 1 ELSE 0 END) AS active, "
        f"SUM(CASE WHEN status NOT IN ({inactive}) AND channel IS NOT NULL THEN 1 ELSE 0 END) AS assigned, "
        "SUM(CASE WHEN enriched_at IS NULL THEN 1 ELSE 0 END) AS owners_pending, "
        "SUM(CASE WHEN owner_phone IS NOT NULL THEN 1 ELSE 0 END) AS with_phone, "
        "SUM(CASE WHEN owner_email IS NOT NULL THEN 1 ELSE 0 END) AS with_email, "
        "SUM(CASE WHEN channel IS NULL AND status = 'new' THEN 1 ELSE 0 END) AS unassigned "
        f"FROM leads WHERE {in_view(settings)}"
    ).fetchone()
    out: dict[str, Any] = {
        k: int(r[k] or 0)
        for k in ("total", "reachable", "active", "assigned", "owners_pending", "with_phone", "with_email")
    }
    out["unassigned"] = int(r["unassigned"] or 0)
    out.update(eviction_reach(conn, settings, refresh=False))
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


def eviction_reach(conn: Conn, settings: dict, refresh: bool = True) -> dict:
    """How many open eviction leads can be reached now, by the same rules
    the Outreach split uses (``reach``): ``evictions_open``,
    ``evictions_with_address`` (an address a door hanger can go to: typed,
    confirmed, imported or from the court, with a unit where the parcel
    has several homes), ``evictions_with_contact`` (a phone or email) and
    ``evictions_reachable`` (either).

    And what each way of reaching more would yield, for the Leads tab's
    setup guide: ``lookup_leads`` (leads with no phone or email whose
    landlord or owner is a company not looked up yet; a business lookup
    can only find companies) and how many companies that is
    (``lookup_companies``); ``records_leads`` (leads with no usable address,
    which a court records request can fill)."""
    if refresh:
        refresh_ranking(conn, settings)
    closed = ", ".join(f"'{s}'" for s in CLOSED)
    lookup = f"NOT {HAS_CONTACT} AND contact_checked_at IS NULL AND lookup_name IS NOT NULL"
    r = conn.execute(
        "SELECT COUNT(*) AS evictions_open, "
        f"SUM(CASE WHEN {DOOR_HANGER_OK} THEN 1 ELSE 0 END) AS evictions_with_address, "
        f"SUM(CASE WHEN {HAS_CONTACT} THEN 1 ELSE 0 END) AS evictions_with_contact, "
        f"SUM(CASE WHEN {REACHABLE} THEN 1 ELSE 0 END) AS evictions_reachable, "
        f"SUM(CASE WHEN NOT {DOOR_HANGER_OK} THEN 1 ELSE 0 END) AS records_leads, "
        f"SUM(CASE WHEN {lookup} THEN 1 ELSE 0 END) AS lookup_leads, "
        f"COUNT(DISTINCT CASE WHEN {lookup} THEN lookup_name END) AS lookup_companies "
        f"FROM leads WHERE {in_view(settings)} AND lead_type = 'eviction' AND status NOT IN ({closed})"
    ).fetchone()
    return {k: int(r[k] or 0) for k in r.keys()}


# ---- addresses: progress and the court records request ----------------------

# How often to ask the court for a records request of new filings.
RECORDS_REQUEST_DAYS = 14
# How far back the first records request asks.
BACKFILL_DAYS = 30
ADDRESS_HISTORY_DAYS = 60


def record_address_share(conn: Conn, settings: dict, today: Optional[date] = None) -> None:
    """Keep one snapshot a day of how many open eviction leads have a known
    (typed, confirmed or imported) address, so the Leads tab can say whether
    the share is rising."""
    today = today or az_today()
    c = counts(conn, settings)
    history = dict(settings.get("address_history") or {})
    history[today.isoformat()] = [c["evictions_with_address"], c["evictions_open"]]
    cutoff = (today - timedelta(days=ADDRESS_HISTORY_DAYS)).isoformat()
    db.put_settings(conn, {"address_history": {k: v for k, v in sorted(history.items()) if k >= cutoff}})


def address_progress(conn: Conn, settings: dict, today: Optional[date] = None) -> dict:
    """The address numbers the Leads tab shows: the share a week ago (the
    latest daily snapshot at least seven days old), and the records request
    (when one was last sent and last imported, which dates to ask for next,
    when the next one is due, and whether Lead Desk is waiting for a file)."""
    today = today or az_today()
    history = settings.get("address_history") or {}
    week = (today - timedelta(days=7)).isoformat()
    older = [k for k in history if k <= week]
    then = history[max(older)] if older else None
    last = settings.get("last_records_import") or {}
    last_date = last.get("date")
    # Steve says when he sent a request ("I've sent it"): the next one is due
    # two weeks after the latest request or import, and asks from there on.
    requested = (settings.get("records_requested") or {}).get("date")
    latest = max(d for d in (last_date, requested) if d) if (last_date or requested) else None
    if latest:
        start = latest
    else:
        row = conn.execute(
            f"SELECT MIN(event_date) AS d FROM leads WHERE {in_view(settings)} AND lead_type = 'eviction' "
            f"AND NOT {KNOWN_ADDRESS} AND status NOT IN ({', '.join(repr(s) for s in CLOSED)})"
        ).fetchone()
        # The first request reaches back a month at least: the court
        # calendar lists only upcoming hearings, so the judgments and writs
        # of the last weeks (the units to clear now) only come in this way.
        backfill = (today - timedelta(days=BACKFILL_DAYS)).isoformat()
        start = min(str(row["d"])[:10] if row and row["d"] else backfill, backfill)
    due_on = (date.fromisoformat(latest) + timedelta(days=RECORDS_REQUEST_DAYS)).isoformat() if latest else None
    return {
        "week_ago": {"date": max(older), "with_address": then[0], "open": then[1]} if then else None,
        "records": {
            "last_import": last_date,
            "last_filled": last.get("filled"),
            "last_request": requested,
            # A request was sent and its file hasn't been imported yet.
            "waiting": bool(requested and (not last_date or requested > last_date)),
            "request_from": start,
            "request_to": today.isoformat(),
            "due_on": due_on,
            "due": not due_on or due_on <= today.isoformat(),
            "every_days": RECORDS_REQUEST_DAYS,
            # No request sent or file imported yet: the first one is the backfill.
            "first": not latest,
        },
    }


def samples(conn: Conn, settings: dict) -> dict:
    """The best open lead of each kind, from any view, for previews of the
    message templates: ``{"eviction": lead, "code_violation": lead}``,
    leaving out a kind there is no lead of."""
    out = {}
    for kind in ("eviction", "code_violation"):
        found = page(conn, settings, {"type": kind, "limit": 1}, view="all")["leads"]
        if found:
            out[kind] = found[0]
    return out
