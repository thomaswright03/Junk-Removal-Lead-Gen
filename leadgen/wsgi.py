"""Lead Desk online: the WSGI app Vercel runs (``[tool.vercel] entrypoint``).

Same pages and API as ``leadgen serve``, with three differences:

- Leads live in the Turso database named by TURSO_DATABASE_URL and
  TURSO_AUTH_TOKEN, because Vercel keeps no files between requests.
- Every request needs the password in LEADDESK_PASSWORD (the browser asks
  for it once; any user name works). Without a password set, nothing is shown.
- The daily check runs on GitHub Actions (.github/workflows/daily.yml), not
  inside Lead Desk.

Setup steps: docs/VERCEL.md.
"""

import base64
import hmac
import os

from .web import App, encode_body, handle

REALM = "Lead Desk"
_app = None


def missing_settings():
    return [k for k in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN", "LEADDESK_PASSWORD")
            if not os.environ.get(k)]


def password_ok(header, password):
    """True when an ``Authorization: Basic ...`` header carries ``password``."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    given = decoded.split(":", 1)[1] if ":" in decoded else ""
    return hmac.compare_digest(given.encode(), password.encode())


SETUP_PAGE = """<!doctype html><meta charset="utf-8"><title>Lead Desk setup</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<body style="font-family: system-ui, sans-serif; max-width: 640px; margin: 48px auto; padding: 0 16px">
<h1>Lead Desk isn't set up yet</h1>
<p>Add these environment variables in Vercel (Project, Settings, Environment Variables),
then redeploy:</p><ul>{items}</ul>
<p>The steps are in <code>docs/VERCEL.md</code> in the repository.</p></body>"""


class Headers:
    """``.get`` over a WSGI environ, the way ``handle`` reads headers."""

    def __init__(self, environ):
        self.environ = environ

    def get(self, name, default=None):
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return self.environ.get(key, default)
        return self.environ.get("HTTP_" + key, default)


def _respond(start_response, status, body, ctype, extra=()):
    data = encode_body(body)
    reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
               404: "Not Found", 405: "Method Not Allowed", 500: "Internal Server Error",
               503: "Service Unavailable"}
    start_response(f"{status} {reasons.get(status, '')}".strip(), [
        ("Content-Type", ctype), ("Content-Length", str(len(data))),
        ("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex"), *extra,
    ])
    return [data]


def get_app():
    global _app
    if _app is None:
        _app = App(os.environ["TURSO_DATABASE_URL"], serverless=True)
    return _app


def app(environ, start_response):
    missing = missing_settings()
    if missing:
        items = "".join(f"<li><code>{k}</code></li>" for k in missing)
        return _respond(start_response, 503, SETUP_PAGE.format(items=items), "text/html; charset=utf-8")
    if not password_ok(environ.get("HTTP_AUTHORIZATION"), os.environ["LEADDESK_PASSWORD"]):
        return _respond(start_response, 401, {"error": "password needed"}, "application/json",
                        [("WWW-Authenticate", f'Basic realm="{REALM}", charset="UTF-8"')])
    method = environ.get("REQUEST_METHOD", "GET")
    body = b""
    if method == "POST":
        n = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(n) if n else b""
    status, out, ctype = handle(get_app(), method, environ.get("PATH_INFO") or "/",
                                environ.get("QUERY_STRING", ""), Headers(environ), body)
    return _respond(start_response, status, out, ctype)
