"""Local web app for working the leads: ``leadgen serve``.

Runs on http://127.0.0.1:8765 by default and only listens on this computer.
Everything is stored in the same SQLite file the CLI uses.
"""

import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import daily, db, outreach
from .contacts import clean_email, clean_phone, import_contacts, skiptrace_csv
from .daily import run_daily
from .enrich import ParcelClient, enrich, enrich_lead, owner_fields
from .geocode import CensusGeocoder
from .lookup import GoogleBudget, find_contacts, providers_from
from .normalize import extract_zip, normalize_address
from .sources.csv_import import read_csv_text
from .sources.pima_jp_calendar import parse_calendar_html
from .sources.pima_jp_case import add_cases, is_case_page, parse_case_html, update_cases
from .tucson_codes import CODE_LABELS, code_of
from .util import PAUSED_MESSAGE, az_now, az_today, decode_text, env_flag, is_paused, now_iso

STATIC = Path(__file__).parent / "static"

LEAD_FIELDS = (
    "id", "source", "source_id", "lead_type", "event_date", "address", "city", "zip",
    "lat", "lon", "parcel", "plaintiff", "defendant", "description", "url", "status",
    "notes", "first_seen", "owner_name", "owner_address", "owner_city", "owner_state",
    "owner_zip", "owner_absentee", "owner_entity", "property_use", "year_built",
    "enriched_at", "channel", "assigned_at", "responded_at", "quote_amount", "job_revenue",
    "owner_phone", "owner_email", "owner_website", "contact_source", "contact_name",
    "contact_checked_at", "eviction_notice", "case_status", "next_court_date", "case_checked_at",
    "unit", "address_source", "case_stage", "judgment_date", "writ_date", "added_by_hand",
)
# The default view: eviction cases with a notice filed or further along
# (judgment, writ), plus cases Steve imported himself whose case page hasn't
# been read yet (so an import shows up at once, marked "case not checked";
# once read, the notice rule applies). Dismissed cases, and closed ones that
# never reached a judgment, drop out.
_ENDED = ("(COALESCE(case_stage, '') = 'dismissed' OR LOWER(COALESCE(case_status, '')) LIKE 'dismiss%' "
          "OR (LOWER(COALESCE(case_status, '')) LIKE 'closed%' "
          "AND COALESCE(case_stage, '') NOT IN ('judgment', 'writ')))")
LEAD_VIEWS = {
    "eviction_notice": ("lead_type = 'eviction' AND (eviction_notice = 1 OR case_stage IN ('judgment', 'writ') "
                        f"OR (added_by_hand = 1 AND eviction_notice IS NULL)) AND NOT {_ENDED}"),
    "evictions": "lead_type = 'eviction'",
    "all": "1=1",
}
EDITABLE = {"status", "channel", "notes", "quote_amount", "job_revenue", "responded_at",
            "owner_phone", "owner_email", "address", "unit"}
# A second identical contact logged this soon after the first is a double click.
DUPLICATE_TOUCH_SECONDS = 10


_TEXT_SETTINGS = {"business_name": "Business name", "business_phone": "Main phone",
                  "base_address": "Base address", "google_places_api_key": "Google key"}


def _limit(value, label):
    """A Google lookup limit: a whole number of 0 or more (0 = none allowed),
    or None / "unlimited" for no limit."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("unlimited", "none")):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number, or no limit.") from None
    if n < 0 or n != int(n):
        raise ValueError(f"{label} must be a whole number of 0 or more (0 means none).")
    return int(n)


def validate_settings(body):
    """The settings in ``body`` that Lead Desk knows, checked. Raises
    ValueError naming the field when one has the wrong shape."""
    values = {}
    for key, label in _TEXT_SETTINGS.items():
        if key in body:
            v = body[key]
            if v is None:
                v = ""
            if not isinstance(v, str) or len(v) > 300:
                raise ValueError(f"{label} must be text (up to 300 characters).")
            values[key] = v.strip()
    if "lead_view" in body:
        if body["lead_view"] not in LEAD_VIEWS:
            raise ValueError("Show must be one of: " + ", ".join(LEAD_VIEWS) + ".")
        values["lead_view"] = body["lead_view"]
    for key, label in (("google_monthly_limit", "Google lookups per month"),
                       ("google_daily_limit", "Google lookups per day")):
        if key in body:
            values[key] = _limit(body[key], label)
    for key, label in (("paused", "Pause"), ("google_enabled", "Use Google")):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{label} must be on or off.")
            values[key] = body[key]
    if "costs" in body:
        costs = body["costs"]
        if not isinstance(costs, dict) or set(costs) - set(outreach.CHANNELS):
            raise ValueError("Cost per contact must list a dollar amount for each outreach method.")
        values["costs"] = {c: money_value(v, f"Cost per contact for {outreach.CHANNELS[c]}") or 0.0
                           for c, v in costs.items()}
    if "tracking_numbers" in body:
        nums = body["tracking_numbers"]
        if not isinstance(nums, dict) or set(nums) - set(outreach.CHANNELS) or \
                not all(isinstance(v, str) for v in nums.values()):
            raise ValueError("Tracking numbers must be a phone number (or blank) for each outreach method.")
        values["tracking_numbers"] = {c: v.strip() for c, v in nums.items()}
    if "templates" in body:
        tpl = body["templates"]
        if not isinstance(tpl, dict) or set(tpl) - set(outreach.DEFAULT_SETTINGS["templates"]) or \
                not all(isinstance(v, str) and len(v) < 5000 for v in tpl.values()):
            raise ValueError("Messages must be text for each outreach method.")
        values["templates"] = dict(tpl)
    return values


UNRECOGNISED_FILE = (
    "Nothing in that file looks like a lead. Lead Desk can import a saved Justice Court case page, "
    "a saved court calendar results page, or a CSV with an address or case number column.")

# Online, stop a long job this many seconds into a request (Vercel allows 60).
SERVERLESS_SECONDS = 40


class Job:
    """One background job: progress, result, and a cancel flag."""

    def __init__(self, name, label):
        self.name, self.label = name, label
        self.done, self.total = 0, None
        self.cancel = threading.Event()
        self.result = self.error = None
        self.started_at, self.finished_at = now_iso(), None
        self._thread_running = threading.Event()
        self._thread_running.set()  # running from the moment it's created

    def running(self):
        return self._thread_running.is_set()

    def progress(self, done, total):
        self.done, self.total = done, total

    def describe(self):
        return f"{self.done} of {self.total} done" if self.total else "starting"

    def run(self, work, should_stop=None):
        self._thread_running.set()
        try:
            self.result = work(self, lambda: self.cancel.is_set() or bool(should_stop and should_stop()))
            if self.cancel.is_set():
                self.result["cancelled"] = True
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.error = f"{self.label} stopped with an error ({type(e).__name__}). Try again later."
        finally:
            self.finished_at = now_iso()
            self._thread_running.clear()

    def public(self):
        return {"name": self.name, "label": self.label, "running": self.running(),
                "done": self.done, "total": self.total, "result": self.result, "error": self.error,
                "cancelling": self.cancel.is_set(), "finished_at": self.finished_at}


class NotFound(LookupError):
    """Answered with 404 and the message."""


def _lead_id(value, what="lead"):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Lead Desk didn't say which {what} this is. Reload the page and try again.") \
            from None
    if n <= 0 or isinstance(value, bool):
        raise ValueError(f"Lead Desk didn't say which {what} this is. Reload the page and try again.")
    return n


def money_value(value, label):
    """A dollar amount: a non-negative number, or None for blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a dollar amount, like 250.")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a dollar amount, like 250.") from None
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be a dollar amount, like 250.")
    if amount < 0:
        raise ValueError(f"{label} can't be negative.")
    return round(amount, 2)


def _seconds_between(a, b):
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _address_fields(address, unit):
    """Columns to set when Steve types a property address. The location,
    parcel and owner are looked up again from the new address."""
    if not address:
        return {"address": None, "unit": None, "address_norm": None, "lat": None, "lon": None,
                "in_pima": None, "geocode_tried": 0, "parcel": None, "address_source": "manual"}
    return {"address": address.upper(), "unit": unit, "address_norm": normalize_address(address),
            "zip": extract_zip(address), "lat": None, "lon": None, "in_pima": None,
            "geocode_tried": 0, "parcel": None, "enriched_at": None, "address_source": "manual"}


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
        self.jobs = {}
        self.job_lock = threading.Lock()
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
            public["google_key_from_env"] = bool(os.environ.get("GOOGLE_PLACES_API_KEY"))
            budget = GoogleBudget(conn)
            public["google_used_this_month"] = budget.used()
            public["google_used_today"] = budget.used_today()
            counts = conn.execute(
                "SELECT COUNT(*) AS all_, "
                "SUM(CASE WHEN lead_type = 'eviction' THEN 1 ELSE 0 END) AS evictions, "
                f"SUM(CASE WHEN {LEAD_VIEWS['eviction_notice']} THEN 1 ELSE 0 END) AS eviction_notice, "
                # Case pages not read yet: hidden from the default view until they are.
                "SUM(CASE WHEN lead_type = 'eviction' AND case_checked_at IS NULL "
                "AND url LIKE '%jcDisplayCase%' THEN 1 ELSE 0 END) AS unchecked "
                "FROM leads WHERE duplicate_of IS NULL AND (in_pima = 1 OR in_pima IS NULL)"
            ).fetchone()
            last = db.get_settings(conn).get("last_daily_summary")
            results = outreach.results(conn)
            return {
                "daily": {"running": self.daily_lock.locked(), "message": self.daily_message,
                          "last_run": settings.get("last_daily_run"),
                          "summary": daily.describe(last) if last else None,
                          "next_run": next_daily_run(settings, self.serverless)},
                "jobs": {name: job.public() for name, job in self.jobs.items()},
                "paused": is_paused(settings),
                "paused_by_env": env_flag("LEADDESK_PAUSED"),
                "leads": self.leads(conn, settings),
                "view_counts": {"all": counts["all_"] or 0, "evictions": counts["evictions"] or 0,
                                "eviction_notice": counts["eviction_notice"] or 0,
                                "unchecked": counts["unchecked"] or 0},
                "settings": public,
                "channels": outreach.CHANNELS,
                "touch_kinds": outreach.TOUCH_KINDS,
                "results": results,
                "comparison": outreach.comparison(results),
                "statuses": db.STATUSES,
                "stale_days": self.stale_days,
                "today": az_today().isoformat(),
            }

    # ---- writes ------------------------------------------------------------

    def update_lead(self, body):
        lead_id = _lead_id(body.get("id"))
        raw = body.get("fields") or {}
        if not isinstance(raw, dict):
            raise ValueError("Nothing to save: the request had no fields.")
        fields = {k: v for k, v in raw.items() if k in EDITABLE}
        if "status" in fields and fields["status"] not in db.STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(db.STATUSES)}.")
        if "channel" in fields and fields["channel"] not in (None, "", *outreach.CHANNELS):
            raise ValueError("That outreach method doesn't exist. Pick one from the list.")
        if fields.get("channel") == "":
            fields["channel"] = None
        for name, label in (("quote_amount", "Quote"), ("job_revenue", "Job revenue")):
            if name in fields:
                fields[name] = money_value(fields[name], label)
        for name in ("notes", "responded_at"):
            if name in fields and fields[name] is not None and not isinstance(fields[name], str):
                raise ValueError(f"{name.replace('_', ' ').capitalize()} must be text.")
        if "owner_phone" in fields:
            raw_phone = str(fields["owner_phone"] or "").strip()
            fields["owner_phone"] = clean_phone(raw_phone) if raw_phone else None
            if raw_phone and not fields["owner_phone"]:
                raise ValueError("The phone number needs 10 digits, like (520) 555-0100.")
        if "owner_email" in fields:
            raw_email = str(fields["owner_email"] or "").strip()
            fields["owner_email"] = clean_email(raw_email) if raw_email else None
            if raw_email and not fields["owner_email"]:
                raise ValueError("That email address doesn't look right. Check it and save again.")
        if "owner_phone" in fields or "owner_email" in fields:
            fields["contact_source"] = "manual"
        address = None
        if "address" in fields or "unit" in fields:
            address = (str(fields.pop("address", "") or "")).strip()
            unit = (str(fields.pop("unit", "") or "")).strip().lstrip("#").strip() or None
            if len(address) > 200 or (unit and len(unit) > 20):
                raise ValueError("That address is too long. Type just the street address and unit.")
            if unit and not address:
                raise ValueError("Type the street address as well as the unit.")
            fields.update(_address_fields(address, unit))
        if fields.get("status") in ("responded", "quoted", "won") and "responded_at" not in fields:
            fields["responded_at"] = now_iso()
        message = None
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not row:
                raise NotFound("That lead no longer exists. Reload the page.")
            if fields.get("responded_at") and row["responded_at"]:
                fields.pop("responded_at")  # keep the first response time
            if "channel" in fields and fields["channel"] == row["channel"]:
                fields.pop("channel")
            if "channel" in fields:
                fields["assign_round"] = None  # set by hand, not by an Assign leads round
                if fields["channel"] and not row["assigned_at"]:
                    fields["assigned_at"] = now_iso()
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*fields.values(), lead_id])
                conn.commit()
            if address:
                message = self._locate(conn, lead_id)
        return {"ok": True, **({"message": message} if message else {})}

    def _locate(self, conn, lead_id):
        """Map location, parcel and owner for an address Steve typed in.
        Network trouble leaves the lead for the next daily check to finish."""
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        notes = []
        try:
            result = (self.geocoder or CensusGeocoder()).geocode(row["address"], row["city"], row["zip"])
        except Exception:
            traceback.print_exc(file=sys.stderr)
            result, notes = None, ["Couldn't reach the map service; Lead Desk will try again on the next check."]
        else:
            db.save_geocode(conn, lead_id, result)
            if result is None:
                notes.append("Couldn't find that address on the map. Check the spelling.")
        try:
            attrs = (self.parcel_client or ParcelClient()).by_site_address(row["address"])
        except Exception:
            traceback.print_exc(file=sys.stderr)
            attrs = None
            notes.append("Couldn't reach the county assessor; the owner will be looked up on the next check.")
        else:
            if attrs:
                enrich_lead(conn, None, row, attrs)
            else:
                conn.execute("UPDATE leads SET enriched_at = ? WHERE id = ?", (now_iso(), lead_id))
                notes.append("No county parcel matches that address, so the owner wasn't updated.")
        if result is None and attrs and attrs.get("LAT") and attrs.get("LON"):
            conn.execute("UPDATE leads SET lat = ?, lon = ?, in_pima = 1 WHERE id = ?",
                         (float(attrs["LAT"]), float(attrs["LON"]), lead_id))
            result = True
        conn.commit()
        saved = "Address saved" + (", found on the map" if result else "")
        return saved + ". " + " ".join(notes) if notes else saved + "."

    def add_touches(self, body):
        if body.get("lead_ids") is not None:
            ids = [_lead_id(i) for i in body["lead_ids"]]
        else:
            ids = [_lead_id(body.get("lead_id"))]
        kind = body.get("kind")
        cost = body.get("cost")
        if cost is not None:
            cost = money_value(cost, "Cost")
        notes = body.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("Notes must be text.")
        logged = duplicates = 0
        with self.conn() as conn:
            settings = self.settings(conn)
            rows = {}
            for lead_id in ids:
                row = conn.execute("SELECT id, channel, status FROM leads WHERE id = ?",
                                   (lead_id,)).fetchone()
                if not row:
                    raise NotFound("That lead no longer exists. Reload the page.")
                rows[lead_id] = row
            for lead_id, row in rows.items():
                channel = body.get("channel") or row["channel"]
                if not channel:
                    raise ValueError("Pick an outreach method for this lead before logging outreach.")
                if channel not in outreach.CHANNELS:
                    raise ValueError("That outreach method doesn't exist. Pick one from the list.")
                if kind not in dict(outreach.TOUCH_KINDS[channel]):
                    raise ValueError("That kind of contact isn't one Lead Desk knows for "
                                     f"{outreach.CHANNELS[channel]}.")
                # Two clicks in a row on the same button log one contact.
                last = conn.execute(
                    "SELECT created_at FROM touches WHERE lead_id = ? AND channel = ? AND kind = ? "
                    "ORDER BY id DESC LIMIT 1", (lead_id, channel, kind)).fetchone()
                now = now_iso()
                if last and _seconds_between(last["created_at"], now) < DUPLICATE_TOUCH_SECONDS:
                    duplicates += 1
                    continue
                this_cost = cost if cost is not None else money_value(
                    settings["costs"].get(channel) or 0, "Cost")
                conn.execute(
                    "INSERT INTO touches (lead_id, channel, kind, cost, notes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (lead_id, channel, kind, this_cost, notes, now),
                )
                logged += 1
                updates = {}
                if not row["channel"]:
                    updates.update(channel=channel, assigned_at=now)
                if row["status"] == "new":
                    updates["status"] = "contacted"
                if updates:
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(f"UPDATE leads SET {sets} WHERE id = ?",
                                 [*updates.values(), lead_id])
            conn.commit()
        return {"ok": True, "logged": logged, "duplicates": duplicates}

    def delete_touch(self, body):
        touch_id = _lead_id(body.get("id"), "contact")
        with self.conn() as conn:
            if not conn.execute("SELECT id FROM touches WHERE id = ?", (touch_id,)).fetchone():
                raise NotFound("That contact was already removed. Reload the page.")
            conn.execute("DELETE FROM touches WHERE id = ?", (touch_id,))
            conn.commit()
        return {"ok": True}

    def assign(self, body):
        with self.conn() as conn:
            leads = self.leads(conn)
            return outreach.assign(conn, leads, int(body.get("count") or 40),
                                   body.get("channels") or list(outreach.CHANNELS))

    def save_settings(self, body):
        if not isinstance(body, dict):
            raise ValueError("Nothing to save.")
        values = validate_settings(body)
        with self.conn() as conn:
            current = self.settings(conn)
            if not values.get("google_places_api_key"):
                values.pop("google_places_api_key", None)  # blank field keeps the saved key
            if body.get("clear_google_key"):
                values["google_places_api_key"] = ""
            if values.get("base_address") and values["base_address"] != current["base_address"]:
                values["base_lat"] = values["base_lon"] = None
            db.put_settings(conn, values)
            conn.commit()
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

    def paused(self, conn=None):
        if conn is None:
            with self.conn() as c:
                return is_paused(self.settings(c))
        return is_paused(self.settings(conn))

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

    def _busy(self):
        """What is running now that a new job would collide with, or None."""
        if self.daily_lock.locked():
            return "The daily check is running and does this too. Wait for it to finish."
        for job in self.jobs.values():
            if job.running():
                return f"{job.label} is running ({job.describe()}). Wait for it or cancel it."
        return None

    def start_daily(self, body=None):
        """Start the daily check in the background; the page polls /api/state."""
        if self.paused():
            return {"started": False, "running": False, "paused": True, "message": PAUSED_MESSAGE}
        if self.serverless:
            return start_github_check()
        if self.daily_lock.locked():
            return {"started": False, "running": True,
                    "message": "The daily check is already running."}
        busy = self._busy()
        if busy:
            return {"started": False, "running": False, "message": busy}

        def work():
            try:
                self.refresh(body or {})
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self.daily_message = f"the daily check stopped with an error ({type(e).__name__})"
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
                        if daily.due(conn, hour=hour) and not self.paused(conn):
                            self.start_daily()
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                time.sleep(every_seconds)
        threading.Thread(target=loop, daemon=True, name="scheduler").start()

    # ---- long jobs ("Update court cases", "Find landlord phones") -----------
    # Locally they run in the background like the daily check: the request
    # returns at once, /api/state reports progress, and they can be cancelled.
    # Online (Vercel) a request can't outlive its answer, so they do one
    # batch inside the request, stopping early before Vercel's time limit.

    def _run_job(self, name, label, work):
        if self.paused():
            return {"started": False, "paused": True, "message": PAUSED_MESSAGE}
        with self.job_lock:  # two presses at once start one job
            job = self.jobs.get(name)
            if job and job.running():
                return {"started": False, "running": True,
                        "message": f"{label} is already running ({job.describe()})."}
            busy = self._busy()
            if busy:
                return {"started": False, "running": False, "message": busy}
            job = self.jobs[name] = Job(name, label)
        if self.serverless:
            deadline = time.monotonic() + SERVERLESS_SECONDS
            job.run(work, should_stop=lambda: time.monotonic() > deadline)
            if job.error:
                raise RuntimeError(job.error)
            return {"started": False, "running": False, "result": job.result}
        threading.Thread(target=job.run, args=(work,), daemon=True, name=name).start()
        return {"started": True, "running": True}

    def cancel_job(self, body):
        job = self.jobs.get(body.get("name"))
        if not job or not job.running():
            return {"ok": True, "message": "Nothing to cancel: it already finished."}
        job.cancel.set()
        return {"ok": True, "message": f"Stopping {job.label.lower()} after the current one."}

    def find_contacts(self, body):
        def work(job, should_stop):
            with self.conn() as conn:
                settings = self.settings(conn)
                provs = self.providers if self.providers is not None else providers_from(settings, conn=conn)
                log = []
                counts = find_contacts(conn, provs,
                                       limit=int(body.get("limit") or 0) or self._cap(25),
                                       refresh=bool(body.get("refresh")), log=log.append,
                                       progress=job.progress, should_stop=should_stop)
                counts["google_used"] = any(p.name == "google" for p in provs)
                counts["messages"] = log[:10]
                return counts
        return self._run_job("contacts", "Finding landlord phones", work)

    def add_cases(self, body):
        if self.paused():
            return {"paused": True, "message": PAUSED_MESSAGE}
        log = []
        with self.lock, self.conn() as conn:
            counts = add_cases(conn, body.get("text") or "", self.case_client, log=log.append)
        counts["messages"] = log[:10]
        return counts

    def update_cases(self, body):
        def work(job, should_stop):
            log = []
            with self.conn() as conn:
                counts = update_cases(conn, self.case_client,
                                      limit=int(body.get("limit") or 0) or self._cap(60),
                                      max_age_hours=0 if body.get("force") else 12, log=log.append,
                                      progress=job.progress, should_stop=should_stop)
            counts["messages"] = log[:10]
            return counts
        return self._run_job("cases", "Checking court cases", work)

    def run_enrich(self, body):
        with self.lock, self.conn() as conn:
            return enrich(conn, self.parcel_client or ParcelClient(), limit=self._cap(150),
                          refresh=bool(body.get("refresh")))

    def import_file(self, source, filename, data, lead_type="eviction"):
        """One uploaded file: a saved Justice Court case or calendar page, a
        CSV of leads, or (``source="contacts"``) a CSV of phones and emails.
        Leads imported here are marked as added by hand, so they show in the
        default view right away."""
        text = decode_text(data or b"")
        if source == "contacts":
            with self.lock, self.conn() as conn:
                counts = import_contacts(conn, text)
            if not counts["rows"]:
                raise ValueError("That file has no rows Lead Desk recognises. Import a CSV with a phone "
                                 "or email column and a lead_id, parcel, address or owner name column.")
            return counts
        if source not in ("pima_jp_calendar", "csv_import"):
            raise ValueError("Lead Desk can import a saved court page (.html) or a CSV file.")
        since = (az_today() - timedelta(days=365)).isoformat()
        unreadable = 0
        if is_case_page(text):
            lead = parse_case_html(text)
            if not lead:
                raise ValueError("That case page isn't a civil (CV) case, so there's nothing to import.")
            leads = [lead]
        elif source == "csv_import" or Path(filename or "").suffix.lower() == ".csv":
            leads = list(read_csv_text(text, Path(filename or "upload.csv").name, default_type=lead_type))
            unreadable = sum(1 for l in leads if l.raw.get("unreadable_date"))
        else:
            leads = [l for l in parse_calendar_html(text) if not (l.event_date and l.event_date < since)]
        if not leads:
            raise ValueError(UNRECOGNISED_FILE)
        counts = {"new": 0, "updated": 0, "imported": len(leads), "unreadable_dates": unreadable}
        with self.lock, self.conn() as conn:
            for lead in leads:
                counts[db.upsert(conn, lead)] += 1
                conn.execute("UPDATE leads SET added_by_hand = 1 WHERE source = ? AND source_id = ?",
                             (lead.source, lead.source_id))
            conn.commit()
            if len(leads) == 1 and leads[0].eviction_notice is not None:
                counts["with_notice"] = int(bool(leads[0].eviction_notice))
            counts["waiting_for_case_check"] = sum(
                1 for l in leads if l.lead_type == "eviction" and l.eviction_notice is None
                and "jcdisplaycase" in (l.url or "").lower())
            if not is_paused(self.settings(conn)):
                try:
                    counts["owners"] = enrich(conn, self.parcel_client or ParcelClient())
                except Exception:  # assessor unreachable: the daily check fills owners in
                    traceback.print_exc(file=sys.stderr)
        return counts

    def owner_properties(self, name):
        rows = (self.parcel_client or ParcelClient()).by_owner(name)
        return [
            {"parcel": a.get("PARCEL"), "site_address": a.get("SITE_ADDRESS"),
             "site_zip": a.get("SITE_ZIP"), **owner_fields(a)}
            for a in rows
        ]


def next_daily_run(settings, serverless=False, now=None):
    """When the next daily check runs, in words: "today 6:00 AM" or
    "tomorrow 6:00 AM" (Tucson time)."""
    if is_paused(settings):
        return "paused"
    local = az_now(now)
    ran_today = settings.get("last_daily_run") == local.date().isoformat()
    if local.hour < 6:
        return "today 6:00 AM"
    if not ran_today and not serverless:
        return "within the next few minutes"
    return "tomorrow 6:00 AM"


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
            return 404, {"error": "That page doesn't exist."}, "application/json"
        if method != "POST":
            return 405, {"error": "That request isn't allowed."}, "application/json"
        # Only accept requests from this app's own page.
        origin = headers.get("Origin")
        if origin and urlparse(origin).netloc != headers.get("Host"):
            return 403, {"error": "Requests from other websites are refused."}, "application/json"
        if path == "/api/import":
            return 200, app.import_file(
                q.get("source", ["pima_jp_calendar"])[0], q.get("filename", [""])[0],
                body, q.get("lead_type", ["eviction"])[0]), "application/json"
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
        return 400, {"error": "Lead Desk couldn't read that request. Reload the page and try again."}, \
            "application/json"
    except ValueError as e:
        return 400, {"error": str(e)}, "application/json"
    except Exception:
        # The details go to the server log; the page gets a plain sentence.
        traceback.print_exc(file=sys.stderr)
        action = ACTIONS.get(path, "do that")
        return 500, {"error": f"Lead Desk couldn't {action} because of an unexpected problem. "
                              "Try again; if it keeps happening, restart Lead Desk and look at "
                              "its window (or the Vercel log) for details."}, "application/json"


# What each request does, for error messages.
ACTIONS = {
    "/api/state": "load the leads", "/api/skiptrace.csv": "make the phone-lookup list",
    "/api/owner": "look up the owner's other properties", "/api/import": "import that file",
    "/api/lead": "save the lead", "/api/touch": "log that contact",
    "/api/touch/delete": "remove that contact", "/api/assign": "split the leads",
    "/api/settings": "save the settings", "/api/refresh": "start the check for new evictions",
    "/api/enrich": "look up owners", "/api/find-contacts": "look up landlord phones",
    "/api/cases/add": "add those cases", "/api/cases/update": "update the court cases",
    "/api/job/cancel": "cancel that",
}


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
