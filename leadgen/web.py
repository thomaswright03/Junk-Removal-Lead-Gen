"""Local web app for working the leads: ``leadgen serve``.

Runs on http://127.0.0.1:8765 by default and only listens on this computer.
Everything is stored in the same SQLite file the CLI uses.
"""

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import daily, db, outreach
from .contacts import clean_email, clean_phone, import_contacts, skiptrace_csv
from .enrich import ParcelClient, enrich, owner_fields
from .daily import run_daily
from .geocode import CensusGeocoder
from .lookup import GoogleBudget, find_contacts, providers_from
from .sources import SOURCES
from .sources.pima_jp_case import add_cases, is_case_page, parse_case_html, update_cases
from .tucson_codes import CODE_LABELS, code_of

STATIC = Path(__file__).parent / "static"

LEAD_FIELDS = (
    "id", "source", "source_id", "lead_type", "event_date", "address", "city", "zip",
    "lat", "lon", "parcel", "plaintiff", "defendant", "description", "url", "status",
    "notes", "first_seen", "owner_name", "owner_address", "owner_city", "owner_state",
    "owner_zip", "owner_absentee", "owner_entity", "property_use", "year_built",
    "enriched_at", "channel", "assigned_at", "responded_at", "quote_amount", "job_revenue",
    "owner_phone", "owner_email", "owner_website", "contact_source", "contact_name",
    "contact_checked_at", "eviction_notice", "case_status", "next_court_date", "case_checked_at",
)
LEAD_VIEWS = {
    "eviction_notice": "lead_type = 'eviction' AND eviction_notice = 1",
    "evictions": "lead_type = 'eviction'",
    "all": "1=1",
}
EDITABLE = {"status", "channel", "notes", "quote_amount", "job_revenue", "responded_at",
            "owner_phone", "owner_email"}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class App:
    def __init__(self, db_path, stale_days=30, parcel_client=None, geocoder=None,
                 case_client=None, calendar=None, code_cases=None, providers=None,
                 serverless=False):
        self.db_path = db_path
        # Online (Vercel): a request can't keep running after it answers, so
        # the daily check runs on GitHub Actions instead (see wsgi.py).
        self.serverless = serverless
        self.case_client = case_client
        self.calendar = calendar
        self.code_cases = code_cases
        self.providers = providers
        self.daily_lock = threading.Lock()
        self.daily_message = None
        self.stale_days = stale_days
        self.parcel_client = parcel_client
        self.geocoder = geocoder
        self.lock = threading.Lock()
        with self.conn() as conn:
            outreach.retire_channels(conn)

    def conn(self):
        return db.connect(self.db_path)

    # ---- reads -------------------------------------------------------------

    def settings(self, conn):
        return outreach.merged_settings(db.get_settings(conn))

    def leads(self, conn, settings=None):
        settings = settings or self.settings(conn)
        view = LEAD_VIEWS.get(settings.get("lead_view")) or LEAD_VIEWS["eviction_notice"]
        rows = conn.execute(
            "SELECT * FROM leads WHERE duplicate_of IS NULL "
            f"AND (in_pima = 1 OR in_pima IS NULL) AND {view} "
            "ORDER BY event_date DESC, id DESC"
        ).fetchall()
        owner_counts = {}
        for r in rows:
            if r["owner_name"]:
                owner_counts[r["owner_name"]] = owner_counts.get(r["owner_name"], 0) + 1
        touches = {}
        for t in conn.execute("SELECT * FROM touches ORDER BY id"):
            touches.setdefault(t["lead_id"], []).append(dict(t))
        out = []
        for r in rows:
            d = {k: r[k] for k in LEAD_FIELDS}
            code = code_of(r["description"]) if r["lead_type"] == "code_violation" else None
            d["code"] = code
            d["code_label"] = CODE_LABELS.get(code) or (
                "Vacant / nuisance building" if "VACANT/NUISANCE" in (r["description"] or "").upper()
                else None)
            d["score"] = outreach.score(r, owner_counts)
            d["owner_lead_count"] = owner_counts.get(r["owner_name"], 0) if r["owner_name"] else 0
            d["eligible"] = outreach.eligible_channels(r)
            d["miles"] = outreach.miles_between(settings.get("base_lat"), settings.get("base_lon"),
                                                r["lat"], r["lon"])
            d["touches"] = touches.get(r["id"], [])
            out.append(d)
        return out

    def state(self):
        with self.conn() as conn:
            settings = self.settings(conn)
            public = dict(settings)
            key = public.pop("google_places_api_key", "") or ""
            public["google_key_set"] = bool(key or os.environ.get("GOOGLE_PLACES_API_KEY"))
            budget = GoogleBudget(conn)
            public["google_used_this_month"] = budget.used()
            public["google_used_today"] = budget.used_today()
            counts = conn.execute(
                "SELECT COUNT(*) AS all_, "
                "SUM(CASE WHEN lead_type = 'eviction' THEN 1 ELSE 0 END) AS evictions, "
                "SUM(CASE WHEN lead_type = 'eviction' AND eviction_notice = 1 THEN 1 ELSE 0 END) "
                "AS eviction_notice, "
                "SUM(CASE WHEN lead_type = 'eviction' AND eviction_notice IS NULL THEN 1 ELSE 0 END) "
                "AS unchecked "
                "FROM leads WHERE duplicate_of IS NULL AND (in_pima = 1 OR in_pima IS NULL)"
            ).fetchone()
            last = db.get_settings(conn).get("last_daily_summary")
            return {
                "daily": {"running": self.daily_lock.locked(), "message": self.daily_message,
                          "last_run": settings.get("last_daily_run"),
                          "summary": daily.describe(last) if last else None},
                "leads": self.leads(conn, settings),
                "view_counts": {"all": counts["all_"] or 0, "evictions": counts["evictions"] or 0,
                                "eviction_notice": counts["eviction_notice"] or 0,
                                "unchecked": counts["unchecked"] or 0},
                "settings": public,
                "channels": outreach.CHANNELS,
                "results": outreach.results(conn),
                "statuses": db.STATUSES,
                "stale_days": self.stale_days,
                "today": date.today().isoformat(),
            }

    # ---- writes ------------------------------------------------------------

    def update_lead(self, body):
        fields = {k: v for k, v in (body.get("fields") or {}).items() if k in EDITABLE}
        if "status" in fields and fields["status"] not in db.STATUSES:
            raise ValueError("bad status")
        if "channel" in fields and fields["channel"] not in (None, "", *outreach.CHANNELS):
            raise ValueError("bad channel")
        if fields.get("channel") == "":
            fields["channel"] = None
        if "owner_phone" in fields:
            raw = (fields["owner_phone"] or "").strip()
            fields["owner_phone"] = clean_phone(raw) if raw else None
            if raw and not fields["owner_phone"]:
                raise ValueError("phone number needs 10 digits")
        if "owner_email" in fields:
            raw = (fields["owner_email"] or "").strip()
            fields["owner_email"] = clean_email(raw) if raw else None
            if raw and not fields["owner_email"]:
                raise ValueError("that email address doesn't look right")
        if "owner_phone" in fields or "owner_email" in fields:
            fields["contact_source"] = "manual"
        if fields.get("status") in ("responded", "quoted", "won") and "responded_at" not in fields:
            fields["responded_at"] = _now()
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (body["id"],)).fetchone()
            if not row:
                raise KeyError("no such lead")
            if fields.get("responded_at") and row["responded_at"]:
                fields.pop("responded_at")  # keep the first response time
            if "channel" in fields and fields["channel"] and not row["assigned_at"]:
                fields["assigned_at"] = _now()
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*fields.values(), body["id"]])
        return {"ok": True}

    def add_touches(self, body):
        ids = body.get("lead_ids") or [body["lead_id"]]
        kind = body.get("kind") or "contact"
        with self.conn() as conn:
            settings = self.settings(conn)
            for lead_id in ids:
                row = conn.execute("SELECT channel, status FROM leads WHERE id = ?",
                                   (lead_id,)).fetchone()
                if not row:
                    continue
                channel = body.get("channel") or row["channel"]
                if not channel:
                    raise ValueError("assign a channel before logging outreach")
                cost = body.get("cost")
                if cost is None:
                    cost = float(settings["costs"].get(channel) or 0)
                conn.execute(
                    "INSERT INTO touches (lead_id, channel, kind, cost, notes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (lead_id, channel, kind, cost, body.get("notes"), _now()),
                )
                updates = {}
                if not row["channel"]:
                    updates.update(channel=channel, assigned_at=_now())
                if row["status"] == "new":
                    updates["status"] = "contacted"
                if updates:
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(f"UPDATE leads SET {sets} WHERE id = ?",
                                 [*updates.values(), lead_id])
        return {"ok": True, "logged": len(ids)}

    def assign(self, body):
        with self.conn() as conn:
            leads = self.leads(conn)
            counts = outreach.assign(conn, leads, int(body.get("count") or 40),
                                     body.get("channels") or list(outreach.CHANNELS))
        return {"assigned": counts}

    def save_settings(self, body):
        allowed = set(outreach.DEFAULT_SETTINGS)
        with self.conn() as conn:
            current = self.settings(conn)
            values = {k: v for k, v in body.items() if k in allowed}
            if "lead_view" in values and values["lead_view"] not in LEAD_VIEWS:
                raise ValueError("lead_view must be one of " + ", ".join(LEAD_VIEWS))
            for name in ("google_monthly_limit", "google_daily_limit"):
                if name in values:
                    limit = int(values[name] or 0)
                    if limit < 0:
                        raise ValueError(f"{name} can't be negative")
                    values[name] = limit
            if not values.get("google_places_api_key"):
                values.pop("google_places_api_key", None)  # blank field keeps the saved key
            if body.get("clear_google_key"):
                values["google_places_api_key"] = ""
            if values.get("base_address") and values["base_address"] != current["base_address"]:
                values["base_lat"] = values["base_lon"] = None
            db.put_settings(conn, values)
        self._ensure_base()
        return {"ok": True}

    def _ensure_base(self):
        with self.conn() as conn:
            s = self.settings(conn)
            if s.get("base_lat") is not None:
                return
            try:
                r = (self.geocoder or CensusGeocoder()).geocode(s["base_address"])
            except Exception:
                return
            if r:
                db.put_settings(conn, {"base_lat": r.lat, "base_lon": r.lon})

    def refresh(self, body):
        """Run the daily check now and wait for it (the CLI and CI use this;
        the page uses ``start_daily``)."""
        with self.daily_lock, self.conn() as conn:
            summary = run_daily(conn, stale_days=self.stale_days, case_client=self.case_client,
                                parcel_client=self.parcel_client, calendar=self.calendar,
                                code_cases=self.code_cases, geocoder=self.geocoder,
                                providers=self.providers,
                                case_limit=body.get("case_limit"),
                                contact_limit=int(body.get("contact_limit") or 60),
                                log=self._progress)
        self._ensure_base()
        return summary

    def _progress(self, message):
        self.daily_message = message

    def _cap(self, n):
        """Online, a request must finish within Vercel's time limit, so the
        buttons do a batch at a time; the daily run does the rest."""
        return n if self.serverless else None

    def start_daily(self, body=None):
        """Start the daily check in the background; the page polls /api/state."""
        if self.serverless:
            return start_github_check()
        if self.daily_lock.locked():
            return {"started": False, "running": True}

        def work():
            try:
                self.refresh(body or {})
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self.daily_message = f"daily check failed: {type(e).__name__}: {e}"
            else:
                self.daily_message = None

        threading.Thread(target=work, daemon=True, name="daily").start()
        return {"started": True, "running": True}

    def scheduler(self, hour=6, every_seconds=600):
        """While Lead Desk is open, run the daily check once a day after ``hour``."""
        def loop():
            while True:
                try:
                    with self.conn() as conn:
                        if daily.due(conn, hour=hour):
                            self.start_daily()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                time.sleep(every_seconds)
        threading.Thread(target=loop, daemon=True, name="scheduler").start()

    def find_contacts(self, body):
        with self.lock, self.conn() as conn:
            settings = self.settings(conn)
            log = []
            counts = find_contacts(conn, providers_from(settings, conn=conn),
                                   limit=int(body.get("limit") or 0) or self._cap(25),
                                   refresh=bool(body.get("refresh")), log=log.append)
            counts["google_used"] = any(p.name == "google" for p in providers_from(settings))
            counts["messages"] = log[:10]
            return counts

    def add_cases(self, body):
        log = []
        with self.lock, self.conn() as conn:
            counts = add_cases(conn, body.get("text") or "", self.case_client, log=log.append)
        counts["messages"] = log[:10]
        return counts

    def update_cases(self, body):
        log = []
        with self.lock, self.conn() as conn:
            counts = update_cases(conn, self.case_client, limit=int(body.get("limit") or 0) or self._cap(60),
                                  max_age_hours=0 if body.get("force") else 12, log=log.append)
        counts["messages"] = log[:10]
        return counts

    def run_enrich(self, body):
        with self.lock, self.conn() as conn:
            return enrich(conn, self.parcel_client or ParcelClient(), limit=self._cap(150),
                          refresh=bool(body.get("refresh")))

    def import_file(self, source, filename, data, lead_type="eviction"):
        if source == "contacts":
            with self.lock, self.conn() as conn:
                return import_contacts(conn, data.decode("utf-8-sig", errors="replace"))
        if source not in ("pima_jp_calendar", "csv_import"):
            raise ValueError("source must be pima_jp_calendar or csv_import")
        text = data.decode("utf-8", errors="replace")
        if source == "pima_jp_calendar" and is_case_page(text):
            lead = parse_case_html(text)
            if not lead:
                raise ValueError("that case page isn't a civil (CV) case")
            with self.lock, self.conn() as conn:
                result = db.upsert(conn, lead)
                conn.commit()
            return {"new": int(result == "new"), "updated": int(result == "updated"),
                    "with_notice": int(bool(lead.eviction_notice))}
        suffix = Path(filename or "upload").suffix or ".txt"
        with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as fh:
            fh.write(data)
            path = fh.name
        try:
            since = (date.today() - timedelta(days=365)).isoformat()
            counts = {"new": 0, "updated": 0}
            with self.lock, self.conn() as conn:
                for lead in SOURCES[source]().fetch(since, date.today().isoformat(),
                                                   paths=[path], assume_eviction=False,
                                                   lead_type=lead_type):
                    counts[db.upsert(conn, lead)] += 1
                conn.commit()
                counts["owners"] = enrich(conn, self.parcel_client or ParcelClient())
            return counts
        finally:
            Path(path).unlink(missing_ok=True)

    def owner_properties(self, name):
        rows = (self.parcel_client or ParcelClient()).by_owner(name)
        return [
            {"parcel": a.get("PARCEL"), "site_address": a.get("SITE_ADDRESS"),
             "site_zip": a.get("SITE_ZIP"), **owner_fields(a)}
            for a in rows
        ]


def start_github_check(session=None):
    """Online, "Check for new evictions" starts the daily GitHub Actions run
    (.github/workflows/daily.yml). Needs LEADDESK_GITHUB_TOKEN: a GitHub token
    allowed to run this repository's workflows."""
    import requests

    token = os.environ.get("LEADDESK_GITHUB_TOKEN")
    owner, repo = os.environ.get("VERCEL_GIT_REPO_OWNER"), os.environ.get("VERCEL_GIT_REPO_SLUG")
    if not (token and owner and repo):
        return {"started": False, "running": False,
                "message": "The check runs by itself every morning at 6. To start it from here too, "
                           "add LEADDESK_GITHUB_TOKEN in Vercel (see docs/VERCEL.md)."}
    resp = (session or requests).post(
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/daily.yml/dispatches",
        json={"ref": os.environ.get("LEADDESK_GITHUB_REF", "main")},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30)
    if resp.status_code >= 300:
        raise ValueError(f"GitHub didn't start the check ({resp.status_code}): {resp.text[:200]}")
    return {"started": True, "running": False,
            "message": "Started. The check takes about 15 minutes; reload then to see new leads."}


def render_template(settings, channel, lead):
    owner = (lead.get("owner_name") or lead.get("plaintiff") or "").strip()
    # Assessor names are "LAST FIRST MIDDLE"; the first name is the second word.
    words = owner.split("&")[0].split(",")[0].split()
    first = (words[1].title() if len(words) > 1 and words[1].isalpha() and len(words[1]) > 1
             and not lead.get("owner_entity") else "")
    phone = (settings["tracking_numbers"].get(channel) or settings.get("business_phone")
             or "[phone]")
    text = settings["templates"].get(channel, "")
    for k, v in {
        "{owner}": owner.title() or "Property Owner",
        "{owner_first}": first or "there",
        "{address}": (lead.get("address") or "your property").title(),
        "{phone}": phone,
        "{business}": settings.get("business_name") or "",
    }.items():
        text = text.replace(k, v)
    return text


def handle(app, method, path, query, headers, body):
    """Answer one request. ``headers`` needs only ``.get``. Returns
    (status, body, content type); a body that isn't bytes or str is JSON."""
    q = parse_qs(query or "")
    try:
        if method == "GET":
            if path in ("/", "/index.html"):
                return 200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8"
            if path == "/api/state":
                return 200, app.state(), "application/json"
            if path == "/api/skiptrace.csv":
                with app.conn() as conn:
                    leads = [l for l in app.leads(conn)
                             if l["status"] not in ("stale", "skip", "lost", "won")]
                return 200, skiptrace_csv(leads), "text/csv; charset=utf-8"
            if path == "/api/owner":
                return 200, app.owner_properties(q.get("name", [""])[0]), "application/json"
            return 404, {"error": "not found"}, "application/json"
        if method != "POST":
            return 405, {"error": "method not allowed"}, "application/json"
        # Only accept requests from this app's own page.
        origin = headers.get("Origin")
        if origin and urlparse(origin).netloc != headers.get("Host"):
            return 403, {"error": "cross-origin request refused"}, "application/json"
        if path == "/api/import":
            return 200, app.import_file(
                q.get("source", ["pima_jp_calendar"])[0], q.get("filename", [""])[0],
                body, q.get("lead_type", ["eviction"])[0]), "application/json"
        data = json.loads(body or b"{}")
        routes = {
            "/api/lead": app.update_lead,
            "/api/touch": app.add_touches,
            "/api/assign": app.assign,
            "/api/settings": app.save_settings,
            "/api/refresh": app.start_daily,
            "/api/enrich": app.run_enrich,
            "/api/find-contacts": app.find_contacts,
            "/api/cases/add": app.add_cases,
            "/api/cases/update": app.update_cases,
        }
        if path not in routes:
            return 404, {"error": "not found"}, "application/json"
        return 200, routes[path](data), "application/json"
    except (ValueError, KeyError) as e:
        return 400, {"error": str(e)}, "application/json"
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return 500, {"error": f"{type(e).__name__}: {e}"}, "application/json"


def encode_body(body):
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body, default=str).encode()


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = encode_body(body)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self, method):
        url = urlparse(self.path)
        body = self._body() if method == "POST" else b""
        self._send(*handle(self.app, method, url.path, url.query, self.headers, body))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def serve(db_path, host="127.0.0.1", port=8765, stale_days=30, open_browser=True):
    app = App(db_path, stale_days)
    app._ensure_base()
    app.scheduler()
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"Lead desk running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
