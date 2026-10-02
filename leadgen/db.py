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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .normalize import extract_zip, normalize_address

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
CREATE INDEX IF NOT EXISTS leads_address_norm ON leads(address_norm);
CREATE INDEX IF NOT EXISTS leads_event_date ON leads(event_date);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status);
"""

STATUSES = ("new", "contacted", "quoted", "won", "lost", "skip", "stale")

# Columns a fetch is allowed to refresh on an existing row. A blank value
# from upstream never wipes out a value we already have.
_REFRESHABLE = (
    "lead_type", "event_date", "address", "address_norm", "city", "zip",
    "lat", "lon", "in_pima", "plaintiff", "defendant", "description", "url",
)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path):
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, lead):
    """Insert or refresh one Lead. Returns ``"new"``, ``"updated"``."""
    now = _now()
    d = lead.to_dict()
    d["address_norm"] = normalize_address(d["address"])
    if not d["zip"]:
        d["zip"] = extract_zip(d["address"])
    if d["in_pima"] is not None:
        d["in_pima"] = int(bool(d["in_pima"]))
    raw = json.dumps(d.pop("raw") or {}, default=str, sort_keys=True)

    existing = conn.execute(
        "SELECT * FROM leads WHERE source = ? AND source_id = ?",
        (d["source"], d["source_id"]),
    ).fetchone()

    if existing:
        sets, args = ["last_seen = ?", "raw_json = ?"], [now, raw]
        for col in _REFRESHABLE:
            if d[col] not in (None, ""):
                sets.append(f"{col} = ?")
                args.append(d[col])
        args.append(existing["id"])
        conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", args)
        if d["address_norm"] and existing["duplicate_of"] is None:
            _link_duplicate(conn, existing["id"], d["address_norm"])
        return "updated"

    cur = conn.execute(
        """
        INSERT INTO leads (source, source_id, lead_type, event_date, address,
            address_norm, city, zip, lat, lon, in_pima, plaintiff, defendant,
            description, url, first_seen, last_seen, raw_json)
        VALUES (:source, :source_id, :lead_type, :event_date, :address,
            :address_norm, :city, :zip, :lat, :lon, :in_pima, :plaintiff,
            :defendant, :description, :url, :now, :now, :raw)
        """,
        {**d, "now": now, "raw": raw},
    )
    if d["address_norm"]:
        _link_duplicate(conn, cur.lastrowid, d["address_norm"])
    return "new"


def _link_duplicate(conn, row_id, address_norm):
    first = conn.execute(
        "SELECT id FROM leads WHERE address_norm = ? AND id != ? AND duplicate_of IS NULL "
        "ORDER BY id LIMIT 1",
        (address_norm, row_id),
    ).fetchone()
    if first and first["id"] < row_id:
        conn.execute("UPDATE leads SET duplicate_of = ? WHERE id = ?", (first["id"], row_id))


def mark_stale(conn, days, today=None):
    """Move ``new`` leads whose event is older than ``days`` to ``stale``."""
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "UPDATE leads SET status = 'stale' WHERE status = 'new' "
        "AND event_date IS NOT NULL AND event_date < ?",
        (cutoff,),
    )
    return cur.rowcount


def set_status(conn, lead_id, status, notes=None):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if notes is None:
        cur = conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    else:
        cur = conn.execute(
            "UPDATE leads SET status = ?, notes = ? WHERE id = ?", (status, notes, lead_id)
        )
    return cur.rowcount


def query(conn, since=None, statuses=None, lead_types=None, include_duplicates=False,
          only_pima=False):
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


def needs_geocode(conn, limit=None):
    sql = (
        "SELECT * FROM leads WHERE address IS NOT NULL AND lat IS NULL "
        "AND geocode_tried = 0 ORDER BY id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def save_geocode(conn, lead_id, result):
    if result is None:
        conn.execute("UPDATE leads SET geocode_tried = 1 WHERE id = ?", (lead_id,))
        return
    conn.execute(
        "UPDATE leads SET geocode_tried = 1, lat = ?, lon = ?, in_pima = ?, "
        "zip = COALESCE(zip, ?), city = COALESCE(city, ?) WHERE id = ?",
        (result.lat, result.lon, int(result.in_pima), result.zip, result.city, lead_id),
    )
