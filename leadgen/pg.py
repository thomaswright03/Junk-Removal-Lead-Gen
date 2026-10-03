"""The lead database on Postgres (Neon), for running Lead Desk online.

The code is written for SQLite. This module gives a Postgres connection the
small part of the ``sqlite3`` interface the code uses, and translates the SQL
on the way through:

- ``?`` and ``:name`` parameters become ``%s`` and ``%(name)s``
- ``True``/``False`` parameters are stored as 1/0, as SQLite does
- ``DESC`` sorts empty values last, as SQLite does
- an ``INSERT`` into leads or touches reports the new row's id (``lastrowid``)

Each statement commits on its own (autocommit), so ``commit()`` is a no-op
and an error never leaves the connection stuck in a failed transaction.

Used when the database is a ``postgres://`` URL, normally from the
``DATABASE_URL`` environment variable that Vercel's Neon integration sets.
"""

import re
from typing import Any, Iterable, Iterator, Literal, Optional

URL_PREFIXES = ("postgres://", "postgresql://")

# Postgres version of db.SCHEMA. Columns from db._ADDED_COLUMNS are added
# after this, with REAL mapped to DOUBLE PRECISION (Postgres REAL is 4 bytes).
SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    lead_type     TEXT NOT NULL,
    event_date    TEXT,
    address       TEXT,
    address_norm  TEXT,
    city          TEXT,
    zip           TEXT,
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    in_pima       INTEGER,
    plaintiff     TEXT,
    defendant     TEXT,
    description   TEXT,
    url           TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    notes         TEXT,
    duplicate_of  BIGINT REFERENCES leads(id),
    geocode_tried INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    raw_json      TEXT,
    UNIQUE (source, source_id)
);
CREATE TABLE IF NOT EXISTS touches (
    id         BIGSERIAL PRIMARY KEY,
    lead_id    BIGINT NOT NULL REFERENCES leads(id),
    channel    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    cost       DOUBLE PRECISION NOT NULL DEFAULT 0,
    notes      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS touches_lead ON touches(lead_id);
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
CREATE INDEX IF NOT EXISTS leads_status ON leads(status)
"""

TYPES = {"REAL": "DOUBLE PRECISION"}


def is_url(path: Any) -> bool:
    return str(path).startswith(URL_PREFIXES)


_TOKEN = re.compile(r"'(?:[^']|'')*'|\?|(?<!:):([A-Za-z_]\w*)|%|\bDESC\b", re.I)


def translate(sql: str) -> tuple[str, bool]:
    """SQLite-style SQL to psycopg's: returns (sql, uses_named_params)."""
    named = False

    def sub(m: re.Match) -> str:
        nonlocal named
        tok = m.group(0)
        if tok.startswith("'"):
            return tok.replace("%", "%%")
        if tok == "?":
            return "%s"
        if tok == "%":
            return "%%"
        if m.group(1):
            named = True
            return f"%({m.group(1)})s"
        return tok + " NULLS LAST"  # DESC

    return _TOKEN.sub(sub, sql), named


def _value(v: Any) -> Any:
    return int(v) if isinstance(v, bool) else v


def params_for(params: Any) -> Any:
    if isinstance(params, dict):
        return {k: _value(v) for k, v in params.items()}
    return [_value(v) for v in (params or ())]


_INSERT_ID = re.compile(r"^\s*INSERT\s+INTO\s+(leads|touches)\b", re.I)


class Row(tuple):
    """Like ``sqlite3.Row``: ``row[0]``, ``row["name"]``, ``row.keys()``, ``dict(row)``."""

    _cols: list

    def __new__(cls, cols: list, values: Any) -> "Row":
        row = super().__new__(cls, values)
        row._cols = cols
        return row

    def keys(self) -> list:
        return list(self._cols)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._cols.index(key.lower()))
            except ValueError:
                raise IndexError(f"No item with that key: {key}") from None
        return tuple.__getitem__(self, key)


class Cursor:
    def __init__(
        self, cols: Iterable = (), rows: Iterable = (), rowcount: int = -1, lastrowid: Optional[int] = None
    ) -> None:
        self.rows = [Row(list(cols), r) for r in rows]
        self.description = tuple((c, None, None, None, None, None, None) for c in cols) or None
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self._i = 0

    def fetchone(self) -> Optional[Row]:
        if self._i >= len(self.rows):
            return None
        self._i += 1
        return self.rows[self._i - 1]

    def fetchall(self) -> list[Row]:
        rest, self._i = self.rows[self._i :], len(self.rows)
        return rest

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


class Connection:
    def __init__(self, url: str, connect: Any = None) -> None:
        if connect is None:
            import psycopg

            connect = psycopg.connect
        # prepare_threshold=None: no server-side prepared statements, which
        # Neon's connection pooler (PgBouncer) may not keep between requests.
        self.raw = connect(url, autocommit=True, prepare_threshold=None)

    def execute(self, sql: str, params: Any = ()) -> Cursor:
        sql, _ = translate(sql)
        returning = bool(_INSERT_ID.match(sql)) and "RETURNING" not in sql.upper()
        if returning:
            sql += " RETURNING id"
        cur = self.raw.execute(sql, params_for(params))
        cols = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
        lastrowid = rows[0][0] if returning and rows else None
        return Cursor(cols, [] if returning else rows, cur.rowcount, lastrowid)

    def executescript(self, script: str) -> Cursor:
        for stmt in (s.strip() for s in script.split(";")):
            if stmt:
                self.raw.execute(stmt)
        return Cursor()

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: Any) -> Literal[False]:
        self.close()  # like db's SQLite connections: a ``with`` block's end closes it
        return False
