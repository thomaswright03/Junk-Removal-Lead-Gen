"""Lead Desk online: the Turso database client and the Vercel WSGI app,
against a stand-in for Turso's HTTP API backed by a local SQLite file."""

import base64
import io
import json
import sqlite3

import pytest

from leadgen import db, turso, wsgi
from leadgen.models import Lead


class FakeTurso:
    """Answers POST /v2/pipeline the way Turso does, from a SQLite file."""

    def __init__(self, path, token="tok"):
        self.sql = sqlite3.connect(str(path), check_same_thread=False)
        self.token = token
        self.headers = {}
        self.posts = 0

    def post(self, url, json=None, timeout=None):
        assert url == "https://leads-steve.turso.io/v2/pipeline"
        self.posts += 1
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            return Resp(401, {})
        results = []
        for req in json["requests"]:
            if req["type"] == "close":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            stmt = req["stmt"]
            try:
                cur = self.sql.execute(stmt["sql"], [turso.decode(a) for a in stmt.get("args", [])])
                rows = cur.fetchall()
                self.sql.commit()
            except sqlite3.Error as e:
                results.append({"type": "error", "error": {"message": str(e)}})
                break
            cols = [{"name": d[0], "decltype": None} for d in cur.description or []]
            results.append({"type": "ok", "response": {"type": "execute", "result": {
                "cols": cols, "rows": [[turso.encode(v) for v in r] for r in rows],
                "affected_row_count": max(cur.rowcount, 0),
                "last_insert_rowid": str(cur.lastrowid) if cur.lastrowid else None}}})
        return Resp(200, {"baton": None, "results": results})

    def close(self):
        pass


class Resp:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


URL = "libsql://leads-steve.turso.io"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fake = FakeTurso(tmp_path / "remote.db")
    monkeypatch.setattr(turso.requests, "Session", lambda: fake)
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    db._READY_URLS.discard(URL)
    return fake


def test_values_round_trip():
    for v in (None, 0, 42, -7, 1.5, "text", b"\x00\x01"):
        assert turso.decode(turso.encode(v)) == v
    assert turso.encode(True) == {"type": "integer", "value": "1"}


def test_rows_by_name_and_index():
    cur = turso.Cursor({"cols": [{"name": "id"}, {"name": "owner_name"}],
                        "rows": [[{"type": "integer", "value": "3"}, {"type": "text", "value": "X LLC"}]]})
    row = cur.fetchone()
    assert row[0] == 3 and row["owner_name"] == "X LLC" and dict(row) == {"id": 3, "owner_name": "X LLC"}
    assert cur.fetchone() is None


def test_lead_database_on_turso(fake):
    conn = db.connect(URL)
    lead = Lead(source="pima_jp_calendar", source_id="CV26-000001-EA", lead_type="eviction",
                event_date="2026-10-05", plaintiff="SAGUARO VISTA APARTMENTS LLC", defendant="DOE, JANE")
    assert db.upsert(conn, lead) == "new"
    assert db.upsert(conn, lead) == "updated"
    db.put_settings(conn, {"lead_view": "all"})
    assert db.get_settings(conn)["lead_view"] == "all"
    row = conn.execute("SELECT * FROM leads").fetchone()
    assert row["plaintiff"] == "SAGUARO VISTA APARTMENTS LLC" and row["id"] == 1
    # The schema is checked once per process, not on every request.
    posts = fake.posts
    db.connect(URL).execute("SELECT 1")
    assert fake.posts == posts + 1


def test_wrong_token_is_explained(fake, monkeypatch):
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "wrong")
    with pytest.raises(turso.Error, match="TURSO_AUTH_TOKEN"):
        db.connect(URL)


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
def online(fake, monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", URL)
    monkeypatch.setenv("LEADDESK_PASSWORD", "hauling")
    monkeypatch.delenv("LEADDESK_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(wsgi, "_app", None)
    return fake


def test_setup_page_until_configured(monkeypatch):
    for k in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "LEADDESK_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    status, _, out = call()
    assert status.startswith("503") and b"LEADDESK_PASSWORD" in out


def test_password_required(online):
    status, headers, _ = call()
    assert status.startswith("401") and headers["WWW-Authenticate"].startswith("Basic")
    assert call(password="wrong")[0].startswith("401")
    status, headers, out = call(password="hauling")
    assert status.startswith("200") and headers["X-Robots-Tag"] == "noindex"
    assert "leads" in json.loads(out)


def test_page_and_settings_online(online):
    status, headers, out = call(path="/", password="hauling")
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


def test_named_parameters_become_positional():
    s = turso.stmt("INSERT INTO t VALUES (:a, ':b', :b, :a)", {"a": 1, "b": "x"})
    assert s["sql"] == "INSERT INTO t VALUES (?, ':b', ?, ?)"
    assert [turso.decode(v) for v in s["args"]] == [1, "x", 1]


def test_daily_run_on_turso(fake):
    from datetime import date

    from test_daily import (CalendarClient, FakeCases, FakeCourt, FakeParcels, FakePhones, NoCodeCases,
                            NoGeocode, PimaJpCalendar, parcel)

    from leadgen import daily
    conn = db.connect(URL)
    cal = PimaJpCalendar()
    cal_fetch = cal.fetch
    cal.fetch = lambda since, until, **kw: cal_fetch(since, until, client=CalendarClient(FakeCourt(), delay=0))
    summary = daily.run_daily(
        conn, today=date(2026, 10, 3), code_cases=NoCodeCases(), calendar=cal, case_client=FakeCases(),
        parcel_client=FakeParcels([parcel("P1", "SAGUARO VISTA APARTMENTS LLC", "100 W SAGUARO VISTA")]),
        geocoder=NoGeocode(), providers=[FakePhones()], log=lambda m: None)
    assert summary["evictions"] == {"new": 3, "updated": 0}
    assert summary["cases"]["with_notice"] == 3 and summary["contacts"]["found"] >= 1
    r = conn.execute("SELECT * FROM leads WHERE source_id = 'CV26-012345-EA'").fetchone()
    assert r["eviction_notice"] == 1 and r["owner_phone"] == "(520) 555-0101"
    assert isinstance(r["lat"], float)
    # Lead Desk online shows what the daily run stored.
    from leadgen.web import App
    app = App(URL, serverless=True)
    app.save_settings({"lead_view": "eviction_notice"})
    assert len(app.state()["leads"]) == 3
