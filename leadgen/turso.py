"""A hosted copy of the lead database on Turso, for running Lead Desk online.

Turso is SQLite served over HTTP, so the same SQL works unchanged. This module
gives it the small part of the ``sqlite3`` connection interface the rest of
the code uses (``execute``, ``executescript``, rows readable by column name),
talking to Turso's HTTP API: https://docs.turso.tech/sdk/http/reference

Each statement commits on its own; ``commit()`` is a no-op.

Used when the database is a ``libsql://`` URL, normally from the
``TURSO_DATABASE_URL`` and ``TURSO_AUTH_TOKEN`` environment variables.
"""

import base64
import os
import re

import requests

URL_PREFIXES = ("libsql://", "turso://", "https://", "http://")


class Error(Exception):
    pass


def is_url(path):
    return str(path).startswith(URL_PREFIXES)


def http_url(url):
    """libsql://db-org.turso.io -> https://db-org.turso.io"""
    for prefix in ("libsql://", "turso://"):
        if url.startswith(prefix):
            return "https://" + url[len(prefix):]
    return url


def encode(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def decode(v):
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        return base64.b64decode(v.get("base64") or "")
    return v.get("value")


class Row(tuple):
    """Like ``sqlite3.Row``: ``row[0]``, ``row["name"]``, ``row.keys()``, ``dict(row)``."""

    def __new__(cls, cols, values):
        row = super().__new__(cls, values)
        row._cols = cols
        return row

    def keys(self):
        return list(self._cols)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._cols.index(key))
            except ValueError:
                lowered = [c.lower() for c in self._cols]
                if key.lower() in lowered:
                    return tuple.__getitem__(self, lowered.index(key.lower()))
                raise IndexError(f"No item with that key: {key}") from None
        return tuple.__getitem__(self, key)


class Cursor:
    def __init__(self, result=None):
        result = result or {}
        cols = [c.get("name") for c in result.get("cols") or []]
        self.rows = [Row(cols, [decode(v) for v in r]) for r in result.get("rows") or []]
        self.description = tuple((c, None, None, None, None, None, None) for c in cols) or None
        self.rowcount = result.get("affected_row_count", -1)
        last = result.get("last_insert_rowid")
        self.lastrowid = int(last) if last is not None else None
        self._i = 0

    def fetchone(self):
        if self._i >= len(self.rows):
            return None
        self._i += 1
        return self.rows[self._i - 1]

    def fetchall(self):
        rest, self._i = self.rows[self._i:], len(self.rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


def split_script(script):
    """Statements in a schema script (no semicolons inside strings)."""
    return [s.strip() for s in script.split(";") if s.strip()]


_NAMED = re.compile(r"'(?:[^']|'')*'|:([A-Za-z_]\w*)")


def stmt(sql, params=()):
    """A Turso statement. ``:name`` parameters (a dict, as sqlite3 takes) are
    sent as positional ``?`` arguments."""
    if isinstance(params, dict):
        args = []

        def sub(m):
            if m.group(1) is None:
                return m.group(0)  # a quoted string: leave it alone
            args.append(params[m.group(1)])
            return "?"
        sql, params = _NAMED.sub(sub, sql), args
    return {"sql": sql, "args": [encode(p) for p in params]}


class Connection:
    def __init__(self, url, auth_token=None, session=None, timeout=30):
        self.url = http_url(url).rstrip("/") + "/v2/pipeline"
        self.session = session or requests.Session()
        token = auth_token if auth_token is not None else os.environ.get("TURSO_AUTH_TOKEN", "")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def _pipeline(self, stmts):
        body = {"requests": [{"type": "execute", "stmt": s} for s in stmts] + [{"type": "close"}]}
        resp = self.session.post(self.url, json=body, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise Error(f"Turso refused the request ({resp.status_code}): check TURSO_AUTH_TOKEN")
        resp.raise_for_status()
        out = []
        for res in resp.json().get("results", [])[:len(stmts)]:
            if res.get("type") == "error":
                raise Error((res.get("error") or {}).get("message") or "Turso error")
            out.append(Cursor((res.get("response") or {}).get("result")))
        return out

    def execute(self, sql, params=()):
        return self._pipeline([stmt(sql, params)])[0]

    def executemany(self, sql, seq):
        stmts = [stmt(sql, params) for params in seq]
        cursors = self._pipeline(stmts) if stmts else []
        cur = Cursor()
        cur.rowcount = sum(max(c.rowcount, 0) for c in cursors)
        return cur

    def executescript(self, script):
        self._pipeline([{"sql": s, "args": []} for s in split_script(script)])
        return Cursor()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
