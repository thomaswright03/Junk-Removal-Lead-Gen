"""Lead Desk online: the Postgres adapter and the Vercel WSGI app.

With TEST_DATABASE_URL set (CI), every test here and in the other files runs
against real Postgres (see conftest.py); without it, against SQLite."""

import base64
import io
import json

import pytest

from leadgen import pg, wsgi


def test_sql_translation():
    sql, named = pg.translate(
        "SELECT * FROM leads WHERE a = ? AND url LIKE '%jc%' AND b = :b ORDER BY x DESC, y")
    assert sql == ("SELECT * FROM leads WHERE a = %s AND url LIKE '%%jc%%' AND b = %(b)s "
                   "ORDER BY x DESC NULLS LAST, y")
    assert named
    assert pg.translate("SELECT 'it''s ?' , ':x'")[0] == "SELECT 'it''s ?' , ':x'"
    assert pg.translate("SELECT a::text")[0] == "SELECT a::text"


def test_true_false_stored_as_numbers():
    assert pg.params_for((True, False, None, 1.5, "x")) == [1, 0, None, 1.5, "x"]
    assert pg.params_for({"a": True}) == {"a": 1}


def test_rows_by_name_and_index():
    cur = pg.Cursor(["id", "owner_name"], [(3, "X LLC")])
    row = cur.fetchone()
    assert row[0] == 3 and row["owner_name"] == "X LLC" and dict(row) == {"id": 3, "owner_name": "X LLC"}
    assert cur.fetchone() is None


def test_insert_reports_new_id():
    class Raw:
        def execute(self, sql, params):
            self.sql = sql
            return type("C", (), {"description": [type("D", (), {"name": "id"})()],
                                  "fetchall": lambda s: [(41,)], "rowcount": 1})()

    raw = Raw()
    conn = pg.Connection("postgres://x", connect=lambda *a, **kw: raw)
    cur = conn.execute("INSERT INTO touches (lead_id) VALUES (?)", (1,))
    assert raw.sql.endswith("RETURNING id") and cur.lastrowid == 41


def call(environ_extra=None, method="GET", path="/api/state", body=b"", password=None):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "HTTP_HOST": "lead-desk.vercel.app", "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body)}
    if password is not None:
        environ["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(f"steve:{password}".encode()).decode()
    environ.update(environ_extra or {})
    seen = {}
    out = b"".join(wsgi.app(environ, lambda status, headers: seen.update(status=status, headers=dict(headers))))
    return seen["status"], seen["headers"], out


@pytest.fixture
def online(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://set-by-vercel")
    monkeypatch.setattr(wsgi, "database_url", lambda: str(tmp_path / "online.db"))
    monkeypatch.setenv("LEADDESK_PASSWORD", "hauling")
    monkeypatch.delenv("LEADDESK_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(wsgi, "_app", None)


def test_setup_page_until_configured(monkeypatch):
    for k in ("DATABASE_URL", "POSTGRES_URL", "LEADDESK_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    status, _, out = call()
    assert status.startswith("503") and b"DATABASE_URL" in out and b"LEADDESK_PASSWORD" in out


def test_password_required(online):
    status, headers, _ = call()
    assert status.startswith("401") and headers["WWW-Authenticate"].startswith("Basic")
    assert call(password="wrong")[0].startswith("401")
    status, headers, out = call(password="hauling")
    assert status.startswith("200") and headers["X-Robots-Tag"] == "noindex"
    assert "leads" in json.loads(out)


def test_page_and_settings_online(online):
    status, _, out = call(path="/", password="hauling")
    assert status.startswith("200") and b"Lead Desk" in out
    status, _, _ = call(method="POST", path="/api/settings", password="hauling",
                        body=json.dumps({"lead_view": "evictions"}).encode())
    assert status.startswith("200")
    state = json.loads(call(password="hauling")[2])
    assert state["settings"]["lead_view"] == "evictions"


def test_check_button_online_without_github_token(online):
    status, _, out = call(method="POST", path="/api/refresh", body=b"{}", password="hauling")
    r = json.loads(out)
    assert status.startswith("200") and r["started"] is False and "every morning" in r["message"]


def test_cross_site_post_refused(online):
    status, _, _ = call({"HTTP_ORIGIN": "https://evil.example"}, method="POST",
                        path="/api/settings", body=b"{}", password="hauling")
    assert status.startswith("403")


def test_counts_only_drops_error_details():
    from leadgen.daily import counts_only
    s = counts_only({"owners": {"error": "HTTPError: 500 for url: ...ADDRESSEE LIKE 'X LLC%'"},
                     "evictions": {"new": 3}})
    assert s == {"owners": {"error": "HTTPError"}, "evictions": {"new": 3}}
