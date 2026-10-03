"""SQLite storage for leads.

One row per upstream record (``source`` + ``source_id``). Re-running a fetch
updates the row instead of adding a new one, and ``status`` / ``notes`` that
Steve edits are never overwritten by a fetch.

When a new record lands on an address that is already in the table from a
different record, the new row's ``duplicate_of`` points at the oldest row for
that address, so exports show each property once.
"""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from . import pg
from .models import Lead
from .normalize import extract_zip, normalize_address
from .util import Conn, az_today, is_paused, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    lead_type     TEXT NOT NULL,
    event_date    TEXT,
    address       TEXT,
    address_norm  TEXT,
    city          TEXT,
    zip           TEXT,
    lat           REAL,
    lon           REAL,
    in_pima       INTEGER,
    plaintiff     TEXT,
    defendant     TEXT,
    description   TEXT,
    url           TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    notes         TEXT,
    duplicate_of  INTEGER REFERENCES leads(id),
    geocode_tried INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    raw_json      TEXT,
    UNIQUE (source, source_id)
);
CREATE TABLE IF NOT EXISTS touches (
    id         INTEGER PRIMARY KEY,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    channel    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    cost       REAL NOT NULL DEFAULT 0,
    notes      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS touches_lead ON touches(lead_id);
-- Usage counters (Google lookups per day and month), updated atomically.
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    n   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_address_norm ON leads(address_norm);
CREATE INDEX IF NOT EXISTS leads_event_date ON leads(event_date);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status);
"""

STATUSES = ("new", "contacted", "responded", "quoted", "won", "lost", "skip", "stale")

# Columns added after the first release. ``connect`` adds any that an older
# database is missing.
_ADDED_COLUMNS = {
    "parcel": "TEXT",
    # Owner of record from the Pima County Assessor (see enrich.py).
    "owner_name": "TEXT",
    "owner_address": "TEXT",
    "owner_city": "TEXT",
    "owner_state": "TEXT",
    "owner_zip": "TEXT",
    "owner_absentee": "INTEGER",
    "owner_entity": "INTEGER",
    "property_use": "TEXT",
    "year_built": "TEXT",
    "enriched_at": "TEXT",
    # Outreach experiment (see outreach.py).
    "channel": "TEXT",
    "assigned_at": "TEXT",
    # Which "Assign leads" round dealt the lead; empty when set by hand.
    "assign_round": "TEXT",
    # How the lead got its method: "round" (Assign leads), "followed" (the
    # method already working its landlord) or "hand" (set on the lead).
    "assigned_by": "TEXT",
    "responded_at": "TEXT",
    # Dollar amounts from before money was kept in whole cents; read once
    # into the *_cents columns (see _money_to_cents) and no longer written.
    "quote_amount": "REAL",
    "job_revenue": "REAL",
    # Quote and job revenue in whole cents.
    "quote_cents": "INTEGER",
    "revenue_cents": "INTEGER",
    # Looked up by hand or imported from a skip-tracing file (see contacts.py).
    "owner_phone": "TEXT",
    "owner_email": "TEXT",
    "owner_website": "TEXT",
    # Where the phone/email came from: manual, import, osm, google, website.
    "contact_source": "TEXT",
    "contact_name": "TEXT",  # business name the lookup matched
    "contact_checked_at": "TEXT",
    # Justice Court case page details (see sources/pima_jp_case.py).
    "eviction_notice": "INTEGER",
    "case_status": "TEXT",
    "next_court_date": "TEXT",
    "case_checked_at": "TEXT",
    # How far the case has got: filed, notice, judgment, writ, or ended
    # (dismissed, satisfied, closed); see sources/pima_jp_case.case_stage.
    "case_stage": "TEXT",
    "judgment_date": "TEXT",
    "writ_date": "TEXT",
    # 1 when Steve added the lead himself (pasted link or imported file).
    "added_by_hand": "INTEGER",
    # Unit / apartment number, and "manual" when Steve typed the address in.
    "unit": "TEXT",
    "address_source": "TEXT",
}

# Columns added to touches after the first release. ``cost`` (dollars) is
# kept for older rows; ``cost_cents`` is what's written and read.
_ADDED_TOUCH_COLUMNS = {"cost_cents": "INTEGER"}

# Columns a fetch is allowed to refresh on an existing row. A blank value
# from upstream never wipes out a value we already have.
_REFRESHABLE = (
    "lead_type",
    "event_date",
    "address",
    "address_norm",
    "city",
    "zip",
    "lat",
    "lon",
    "in_pima",
    "plaintiff",
    "defendant",
    "description",
    "url",
    "parcel",
    "eviction_notice",
    "case_status",
    "next_court_date",
    "case_stage",
    "judgment_date",
    "writ_date",
)


_READY_URLS: set = set()  # Postgres databases whose schema was checked by this process


def connect(path: Any) -> Conn:
    """Open the lead database: a SQLite file, or Postgres (Neon) when
    ``path`` is a ``postgres://`` URL (see pg.py)."""
    if pg.is_url(path):
        conn = pg.Connection(str(path))
        if str(path) not in _READY_URLS:
            conn.executescript(pg.SCHEMA)
            for col, kind in _ADDED_COLUMNS.items():
                conn.execute(f"ALTER TABLE leads ADD COLUMN IF NOT EXISTS {col} {pg.TYPES.get(kind, kind)}")
            for col, kind in _ADDED_TOUCH_COLUMNS.items():
                conn.execute(f"ALTER TABLE touches ADD COLUMN IF NOT EXISTS {col} {pg.TYPES.get(kind, kind)}")
            _money_to_cents(conn)
            _upgrade_case_stages(conn)
            _READY_URLS.add(str(path))
        return conn
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    lite = sqlite3.connect(str(path))
    lite.row_factory = sqlite3.Row
    lite.executescript(SCHEMA)
    _migrate(lite)
    return lite


def _migrate(conn: Conn) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    added = [c for c in _ADDED_COLUMNS if c not in have]
    for col in added:
        conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {_ADDED_COLUMNS[col]}")
    have_touch = {r["name"] for r in conn.execute("PRAGMA table_info(touches)")}
    for col, kind in _ADDED_TOUCH_COLUMNS.items():
        if col not in have_touch:
            conn.execute(f"ALTER TABLE touches ADD COLUMN {col} {kind}")
    _money_to_cents(conn)
    _upgrade_case_stages(conn)
    if "parcel" in added:
        # Tucson code cases from before parcels had their own column.
        for row in conn.execute("SELECT id, raw_json FROM leads WHERE source = 'tucson_code_cases'").fetchall():
            parcel = (json.loads(row["raw_json"] or "{}").get("PARCEL") or "").strip()
            if parcel:
                conn.execute("UPDATE leads SET parcel = ? WHERE id = ?", (parcel, row["id"]))
    conn.commit()


def _money_to_cents(conn: Conn) -> None:
    """Dollar amounts saved before money was kept in whole cents, moved into
    the cents columns (only rows not moved yet, so this runs on every open)."""
    for table, dollars, cents in (
        ("leads", "quote_amount", "quote_cents"),
        ("leads", "job_revenue", "revenue_cents"),
        ("touches", "cost", "cost_cents"),
    ):
        todo = f"{cents} IS NULL AND {dollars} IS NOT NULL"
        # Look first: an open that writes nothing takes no write lock.
        if conn.execute(f"SELECT 1 FROM {table} WHERE {todo} LIMIT 1").fetchone():
            conn.execute(f"UPDATE {table} SET {cents} = CAST(ROUND({dollars} * 100) AS INTEGER) WHERE {todo}")
            conn.commit()


def _upgrade_case_stages(conn: Conn) -> None:
    """Once per change of the case stage rules: every stored court case's
    stage worked out again from its saved papers (see
    pima_jp_case.rederive_stages), so a wrong old stage doesn't linger."""
    from .sources.pima_jp_case import STAGE_RULES_VERSION, rederive_stages

    row = conn.execute("SELECT value FROM settings WHERE key = 'case_stage_rules'").fetchone()
    if row and json.loads(row["value"]) == STAGE_RULES_VERSION:
        return
    rederive_stages(conn)
    put_settings(conn, {"case_stage_rules": STAGE_RULES_VERSION})
    conn.commit()


def cents(value: Any) -> Optional[int]:
    """Whole cents from a stored value, or None."""
    return None if value is None else int(value)


def dollars(value: Any) -> Optional[float]:
    """Cents as dollars for the page (exact to the cent), or None."""
    return None if value is None else int(value) / 100


def upsert(conn: Conn, lead: Lead) -> str:
    """Insert or refresh one Lead. Returns ``"new"``, ``"updated"``."""
    now = now_iso()
    d = lead.to_dict()
    d["address_norm"] = normalize_address(d["address"])
    if not d["zip"]:
        d["zip"] = extract_zip(d["address"])
    for col in ("lat", "lon"):
        try:
            d[col] = float(d[col]) if d[col] not in (None, "") else None
        except (TypeError, ValueError):
            d[col] = None
    for col in ("in_pima", "eviction_notice"):
        if d[col] is not None:
            d[col] = int(bool(d[col]))
    if d["eviction_notice"] is not None:  # only case pages set this
        d["case_checked_at"] = now
    raw = json.dumps(d.pop("raw") or {}, default=str, sort_keys=True)

    existing = conn.execute(
        "SELECT * FROM leads WHERE source = ? AND source_id = ?",
        (d["source"], d["source_id"]),
    ).fetchone()

    if existing:
        if "jcdisplaycase" in (existing["url"] or "").lower() and "jcdisplaycase" not in (d["url"] or "").lower():
            d["url"] = None  # a calendar re-import keeps the case page link
        case_page = bool(d.get("case_checked_at"))
        sets, args = ["last_seen = ?"], [now]
        if existing["case_checked_at"] and not case_page:
            # Keep the case page's filing date, summary and papers over calendar rows.
            d["event_date"] = d["description"] = None
        else:
            sets.append("raw_json = ?")
            args.append(raw)
        for col in _REFRESHABLE:
            if d[col] not in (None, ""):
                sets.append(f"{col} = ?")
                args.append(d[col])
            elif case_page and col in ("judgment_date", "writ_date"):
                # A fresh read of the case decides these: a judgment set aside
                # or a case dismissed since the last read clears them.
                sets.append(f"{col} = NULL")
        if d.get("case_checked_at"):
            sets.append("case_checked_at = ?")
            args.append(d["case_checked_at"])
        if existing["status"] == "stale" and any(
            d[col] and d[col] != existing[col] for col in ("judgment_date", "writ_date")
        ):
            # A judgment or writ (lockout) came after the lead went old: the
            # clean-out is now, so it is a new lead again.
            sets.append("status = 'new'")

        args.append(existing["id"])
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", args)
        if d["address_norm"] and existing["duplicate_of"] is None:
            _link_duplicate(conn, existing["id"], d["address_norm"])
        return "updated"

    cur = conn.execute(
        """
        INSERT INTO leads (source, source_id, lead_type, event_date, address,
            address_norm, city, zip, lat, lon, in_pima, parcel, plaintiff,
            defendant, description, url, eviction_notice, case_status, next_court_date,
            case_stage, judgment_date, writ_date, case_checked_at, first_seen, last_seen, raw_json)
        VALUES (:source, :source_id, :lead_type, :event_date, :address,
            :address_norm, :city, :zip, :lat, :lon, :in_pima, :parcel, :plaintiff,
            :defendant, :description, :url, :eviction_notice, :case_status,
            :next_court_date, :case_stage, :judgment_date, :writ_date, :case_checked_at,
            :now, :now, :raw)
        """,
        {"case_checked_at": None, **d, "now": now, "raw": raw},
    )
    if d["address_norm"]:
        _link_duplicate(conn, cur.lastrowid, d["address_norm"])
    return "new"


def _link_duplicate(conn: Conn, row_id: int, address_norm: str) -> None:
    first = conn.execute(
        "SELECT id FROM leads WHERE address_norm = ? AND id != ? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
        (address_norm, row_id),
    ).fetchone()
    if first and first["id"] < row_id:
        conn.execute("UPDATE leads SET duplicate_of = ? WHERE id = ?", (first["id"], row_id))


def mark_stale(conn: Conn, days: int, today: Optional[date] = None) -> int:
    """Move ``new`` leads to ``stale`` when their latest event is older than
    ``days``. For an eviction that is the latest of its filing, judgment and
    writ dates, so a writ that comes weeks after the filing keeps it fresh."""
    today = today or az_today()
    cutoff = (today - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "UPDATE leads SET status = 'stale' WHERE status = 'new' AND event_date IS NOT NULL AND event_date < ? "
        "AND (judgment_date IS NULL OR judgment_date < ?) AND (writ_date IS NULL OR writ_date < ?)",
        (cutoff, cutoff, cutoff),
    )
    return cur.rowcount


def set_status(conn: Conn, lead_id: int, status: str, notes: Optional[str] = None) -> int:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if notes is None:
        cur = conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    else:
        cur = conn.execute("UPDATE leads SET status = ?, notes = ? WHERE id = ?", (status, notes, lead_id))
    return cur.rowcount


def query(
    conn: Conn,
    since: Optional[str] = None,
    statuses: Optional[Sequence[str]] = None,
    lead_types: Optional[Sequence[str]] = None,
    include_duplicates: bool = False,
    only_pima: bool = False,
) -> list:
    sql = ["SELECT * FROM leads WHERE 1=1"]
    args = []
    if since:
        sql.append("AND (event_date IS NULL OR event_date >= ?)")
        args.append(since)
    if statuses:
        sql.append(f"AND status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    if lead_types:
        sql.append(f"AND lead_type IN ({','.join('?' * len(lead_types))})")
        args.extend(lead_types)
    if not include_duplicates:
        sql.append("AND duplicate_of IS NULL")
    if only_pima:
        sql.append("AND (in_pima = 1 OR in_pima IS NULL)")
    sql.append("ORDER BY event_date DESC, id DESC")
    return conn.execute(" ".join(sql), args).fetchall()


def needs_geocode(conn: Conn, limit: Optional[int] = None) -> list:
    sql = "SELECT * FROM leads WHERE address IS NOT NULL AND lat IS NULL AND geocode_tried = 0 ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def save_geocode(conn: Conn, lead_id: int, result: Any) -> None:
    if result is None:
        conn.execute("UPDATE leads SET geocode_tried = 1 WHERE id = ?", (lead_id,))
        return
    conn.execute(
        "UPDATE leads SET geocode_tried = 1, lat = ?, lon = ?, in_pima = ?, "
        "zip = COALESCE(zip, ?), city = COALESCE(city, ?) WHERE id = ?",
        (result.lat, result.lon, int(result.in_pima), result.zip, result.city, lead_id),
    )


class PauseWatch:
    """``should_stop`` for long loops: True once Lead Desk is paused (Settings
    or LEADDESK_PAUSED), checked before each request. ``hit`` says whether it
    stopped the work."""

    def __init__(self, conn: Conn) -> None:
        self.conn = conn
        self.hit = False

    def __call__(self) -> bool:
        if not self.hit and is_paused(get_settings(self.conn)):
            self.hit = True
        return self.hit


def get_settings(conn: Conn) -> dict:
    return {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT * FROM settings")}


def put_settings(conn: Conn, values: dict) -> None:
    for k, v in values.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, json.dumps(v)),
        )
