"""Lead Desk's HTTP side: which request does what (``handle``), the page's
static files, and the request handler ``leadgen serve`` uses. wsgi.py
answers through ``handle`` too, online."""

import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .contacts import skiptrace_csv
from .forms import NotFound

STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
_STATIC_NAME = re.compile(r"^/static/([a-z0-9_-]+\.(?:css|js))$")


def static_file(path: str) -> Optional[tuple[bytes, str]]:
    """(body, content type) of one of the page's own files, or None."""
    m = _STATIC_NAME.match(path)
    if not m or not (STATIC / m.group(1)).is_file():
        return None
    name = m.group(1)
    return (STATIC / name).read_bytes(), STATIC_TYPES[Path(name).suffix]


def handle(app: Any, method: str, path: str, query: str, headers: Any, body: bytes) -> tuple[int, Any, str]:
    """Answer one request. ``headers`` needs only ``.get``. Returns
    (status, body, content type); a body that isn't bytes or str is JSON."""
    q = parse_qs(query or "", keep_blank_values=True)
    try:
        if method == "GET":
            if path in ("/", "/index.html"):
                return 200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8"
            if path == "/api/state":
                # The page's view: one page of leads and the open lead, not all of them.
                return 200, app.state({k: v[0] for k, v in q.items()}), "application/json"
            if path == "/api/status":
                return 200, app.status(), "application/json"
            found = static_file(path)
            if found:
                return 200, found[0], found[1]
            if path == "/api/skiptrace.csv":
                with app.conn() as conn:
                    leads = [l for l in app.leads(conn) if l["status"] not in ("stale", "skip", "lost", "won")]
                return 200, skiptrace_csv(leads), "text/csv; charset=utf-8"
            if path == "/api/owner":
                return 200, app.owner_properties(q.get("name", [""])[0]), "application/json"
            if path.startswith("/api/"):
                return 404, {"error": "That page doesn't exist."}, "application/json"
            # A mistyped or old link in the browser: a page, with a way back.
            return 404, (STATIC / "notfound.html").read_bytes(), "text/html; charset=utf-8"
        if method != "POST":
            return 405, {"error": "That request isn't allowed."}, "application/json"
        # Only accept requests from this app's own page.
        origin = headers.get("Origin")
        if origin and urlparse(origin).netloc != headers.get("Host"):
            return 403, {"error": "Requests from other websites are refused."}, "application/json"
        if path == "/api/import":
            return (
                200,
                app.import_file(
                    q.get("source", ["pima_jp_calendar"])[0],
                    q.get("filename", [""])[0],
                    body,
                    q.get("lead_type", ["eviction"])[0],
                ),
                "application/json",
            )
        data = json.loads(body or b"{}")
        routes = {
            "/api/lead": app.update_lead,
            "/api/touch": app.add_touches,
            "/api/touch/delete": app.delete_touch,
            "/api/assign": app.assign,
            "/api/settings": app.save_settings,
            "/api/refresh": app.start_daily,
            "/api/enrich": app.run_enrich,
            "/api/find-contacts": app.find_contacts,
            "/api/cases/add": app.add_cases,
            "/api/cases/update": app.update_cases,
            "/api/job/cancel": app.cancel_job,
        }
        if path not in routes:
            return 404, {"error": "That page doesn't exist."}, "application/json"
        if not isinstance(data, dict):
            raise ValueError("Lead Desk couldn't read that request. Reload the page and try again.")
        return 200, routes[path](data), "application/json"
    except NotFound as e:
        return 404, {"error": str(e)}, "application/json"
    except json.JSONDecodeError:
        return (
            400,
            {"error": "Lead Desk couldn't read that request. Reload the page and try again."},
            "application/json",
        )
    except ValueError as e:
        # A refused form value names its field, so the page can show it there.
        name = getattr(e, "field", None)
        return 400, {"error": str(e), **({"field": name} if name else {})}, "application/json"
    except Exception:
        # The details go to the server log; the page gets a plain sentence.
        traceback.print_exc(file=sys.stderr)
        action = ACTIONS.get(path, "do that")
        return (
            500,
            {
                "error": f"Lead Desk couldn't {action} because of an unexpected problem. "
                "Try again; if it keeps happening, restart Lead Desk and look at "
                "its window (or the Vercel log) for details."
            },
            "application/json",
        )


# What each request does, for error messages.
ACTIONS = {
    "/api/state": "load the leads",
    "/api/status": "check on the running jobs",
    "/api/skiptrace.csv": "make the phone-lookup list",
    "/api/owner": "look up the owner's other properties",
    "/api/import": "import that file",
    "/api/lead": "save the lead",
    "/api/touch": "log that contact",
    "/api/touch/delete": "remove that contact",
    "/api/assign": "split the leads",
    "/api/settings": "save the settings",
    "/api/refresh": "start the check for new evictions",
    "/api/enrich": "look up owners",
    "/api/find-contacts": "look up landlord phones",
    "/api/cases/add": "add those cases",
    "/api/cases/update": "update the court cases",
    "/api/job/cancel": "cancel that",
}


def encode_body(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body, default=str).encode()


class Handler(BaseHTTPRequestHandler):
    app: Any = None

    def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
        pass

    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = encode_body(body)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        body = self._body() if method == "POST" else b""
        self._send(*handle(self.app, method, url.path, url.query, self.headers, body))

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")
