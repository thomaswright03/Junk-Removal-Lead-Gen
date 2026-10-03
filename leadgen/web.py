"""Local web app for working the leads: ``leadgen serve``.

Runs on http://127.0.0.1:8765 by default and only listens on this computer.
Everything is stored in the same SQLite file the CLI uses.

This module is the app's logic (``App``); routes.py answers the HTTP
requests, leadlist.py builds the lead list, forms.py checks what the page
sends and jobs.py runs the long jobs.
"""

import logging
import os
import sys
import threading
import traceback
import webbrowser
from contextlib import contextmanager
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from . import daily, db, leadlist, outreach
from .contacts import clean_email, clean_phone, import_contacts
from .enrich import LANDLORD_SOURCE, ParcelClient, enrich, enrich_lead, fix_inferred_addresses, owner_fields
from .forms import (
    DUPLICATE_TOUCH_SECONDS,
    EDITABLE,
    MAX_CONTACT_CENTS,
    NOTES_LIMIT,
    UNRECOGNISED_FILE,
    FieldError,
    NotFound,
    _address_fields,
    _lead_id,
    _seconds_between,
    count_value,
    field,
    money_value,
    notes_value,
    odd_dates,
    validate_settings,
)
from .geocode import CensusGeocoder
from .jobs import Job, JobRunner, next_daily_run, start_github_check
from .leadlist import LEAD_FIELDS, LEAD_VIEWS
from .lookup import GoogleBudget
from .models import Lead
from .routes import ACTIONS, Handler, encode_body, handle
from .sources.csv_import import read_csv_text
from .sources.pima_jp_calendar import parse_calendar_html
from .sources.pima_jp_case import is_case_page, parse_case_html
from .util import PAUSED_MESSAGE, Conn, az_today, decode_text, env_flag, is_paused, now_iso

log = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "LEAD_FIELDS",
    "LEAD_VIEWS",
    "App",
    "Handler",
    "Job",
    "encode_body",
    "handle",
    "next_daily_run",
    "serve",
    "start_github_check",
]


class App(JobRunner):
    def __init__(
        self,
        db_path: Any,
        stale_days: int = 30,
        parcel_client: Any = None,
        geocoder: Any = None,
        case_client: Any = None,
        calendar: Any = None,
        code_cases: Any = None,
        providers: Optional[list] = None,
        serverless: bool = False,
    ) -> None:
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
        self._request = threading.local()
        with self.conn() as conn:
            outreach.retire_channels(conn)
            if not db.get_settings(conn).get("followed_tagged"):
                outreach.tag_followed_leads(conn)
                db.put_settings(conn, {"followed_tagged": True})
                conn.commit()
            fix_inferred_addresses(conn)

    def conn(self) -> Conn:
        """A database connection for a ``with`` block, closed when it ends.
        Inside a request (``request_connection``) every block shares the
        request's one connection, which closes when the request ends."""
        shared = getattr(self._request, "conn", None)
        if shared is not None:
            return _Borrowed(shared)
        return db.connect(self.db_path)

    @contextmanager
    def request_connection(self) -> Iterator[None]:
        """One connection for everything one request does (routes.handle)."""
        if getattr(self._request, "conn", None) is not None:
            yield
            return
        conn = db.connect(self.db_path)
        self._request.conn = conn
        try:
            yield
        finally:
            self._request.conn = None
            conn.close()

    # ---- reads -------------------------------------------------------------

    def settings(self, conn: Conn) -> dict:
        return outreach.merged_settings(db.get_settings(conn))

    def leads(self, conn: Conn, settings: Optional[dict] = None) -> list[dict]:
        """Every lead in the chosen view, with its contact history (for
        Assign leads, the phone-lookup list and scripts; the page gets one
        page at a time, see state)."""
        settings = settings or self.settings(conn)
        return leadlist.attach_touches(conn, leadlist.lead_dicts(conn, settings), everything=True)

    def status(self, conn: Conn = None, settings: Optional[dict] = None) -> dict:
        """What the page polls while a check or job runs: small and quick."""
        if conn is None:
            with self.conn() as c:
                return self.status(c)
        settings = settings or self.settings(conn)
        return {
            "daily": {
                "running": self.daily_lock.locked(),
                "message": self.daily_message,
                "last_run": settings.get("last_daily_run"),
                "interrupted": daily.interrupted_today(settings),
                # Today's check failed outright and is tried again later today.
                "retry": daily.retry_status(settings),
                "summary": daily.describe(settings["last_daily_summary"])
                if settings.get("last_daily_summary")
                else None,
                "next_run": next_daily_run(settings, self.serverless),
                # When the last check finished, and what went wrong in it (the
                # header shows a warning sign when anything did).
                "finished_at": (settings.get("last_daily_summary") or {}).get("finished_at"),
                "problems": daily.problems(settings.get("last_daily_summary")),
            },
            "jobs": {name: job.public() for name, job in self.jobs.items()},
            "paused": is_paused(settings),
            "paused_by_env": env_flag("LEADDESK_PAUSED"),
        }

    def state(self, params: Optional[dict] = None) -> dict:
        """Everything the page shows. Called with params (the page's
        request) it carries one page of leads (list) and the open lead
        (lead) instead of every lead, so its size doesn't grow with the
        database. Without params (the CLI, CI and tests) it has leads,
        every lead in the view."""
        with self.conn() as conn:
            settings = self.settings(conn)
            public = dict(settings)
            public.pop("last_daily_summary", None)
            public.pop("address_history", None)  # sent as "addresses"
            key = public.pop("google_places_api_key", "") or ""
            public["google_key_set"] = bool(key or os.environ.get("GOOGLE_PLACES_API_KEY"))
            public["google_key_from_env"] = bool(os.environ.get("GOOGLE_PLACES_API_KEY"))
            budget = GoogleBudget(conn)
            public["google_used_this_month"] = budget.used()
            public["google_used_today"] = budget.used_today()
            results = outreach.results(conn)
            out = {
                **self.status(conn, settings),
                # Counted with the Leads tab's Status filter, so they match its list.
                "view_counts": leadlist.view_counts(
                    conn, params.get("status", "open") if params and params.get("list") == "leads" else "open"
                ),
                "counts": leadlist.counts(conn, settings),
                "addresses": leadlist.address_progress(conn, settings),
                "settings": public,
                "channels": outreach.CHANNELS,
                "touch_kinds": outreach.TOUCH_KINDS,
                "results": results,
                "comparison": outreach.comparison(results),
                # The same, within one kind of lead at a time (the Results tab's default).
                "results_by_kind": {
                    kind: {"results": r, "comparison": outreach.comparison(r)}
                    for kind, r in ((k, outreach.results(conn, lead_type=k)) for k in outreach.LEAD_KINDS)
                },
                "lead_kinds": outreach.LEAD_KINDS,
                "comparison_basis": outreach.COMPARISON_BASIS,
                "pitches": outreach.PITCHES,
                "statuses": db.STATUSES,
                "stale_days": self.stale_days,
                "notes_limit": NOTES_LIMIT,
                "today": az_today().isoformat(),
            }
            if params is None:
                out["leads"] = self.leads(conn, settings)
                return out
            if params.get("list"):
                out["list"] = leadlist.page(conn, settings, params)
            if params.get("samples"):  # the Settings tab's message previews
                out["samples"] = leadlist.samples(conn, settings)
            if params.get("list") == "queue":  # the Outreach tab
                out["split"] = self.split_preview(conn, settings)
            lead_id = str(params.get("lead") or "")
            out["lead"] = leadlist.one_lead(conn, settings, int(lead_id)) if lead_id.isdigit() else None
            return out

    # ---- writes ------------------------------------------------------------

    def update_lead(self, body: dict) -> dict:
        lead_id = _lead_id(body.get("id"))
        raw = body.get("fields") or {}
        if not isinstance(raw, dict):
            raise ValueError("Nothing to save: the request had no fields.")
        fields = {k: v for k, v in raw.items() if k in EDITABLE}
        if not fields and not raw.get("confirm_address"):
            raise ValueError("Nothing to save.")
        if "status" in fields and fields["status"] not in db.STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(db.STATUSES)}.")
        if "channel" in fields and fields["channel"] not in (None, "", *outreach.CHANNELS):
            raise ValueError("That outreach method doesn't exist. Pick one from the list.")
        if fields.get("channel") == "":
            fields["channel"] = None
        # Money is stored in whole cents; the old dollar columns are cleared
        # so an amount from before can't come back.
        for name, column, label in (
            ("quote_amount", "quote_cents", "Quote"),
            ("job_revenue", "revenue_cents", "Job revenue"),
        ):
            if name in fields:
                with field(name):
                    fields[column] = money_value(fields.pop(name), label)
                fields[name] = None
        if "notes" in fields:
            with field("notes"):
                fields["notes"] = notes_value(fields["notes"])
        if (
            "responded_at" in fields
            and fields["responded_at"] is not None
            and not isinstance(fields["responded_at"], str)
        ):
            raise ValueError("Responded at must be text.")
        if "owner_phone" in fields:
            raw_phone = str(fields["owner_phone"] or "").strip()
            fields["owner_phone"] = clean_phone(raw_phone) if raw_phone else None
            if raw_phone and not fields["owner_phone"]:
                raise FieldError("The phone number needs 10 digits, like (520) 555-0100.", "owner_phone")
        if "owner_email" in fields:
            raw_email = str(fields["owner_email"] or "").strip()
            fields["owner_email"] = clean_email(raw_email) if raw_email else None
            if raw_email and not fields["owner_email"]:
                raise FieldError("That email address doesn't look right. Check it and save again.", "owner_email")
        if "owner_phone" in fields or "owner_email" in fields:
            fields["contact_source"] = "manual"
        address = None
        if "address" in fields or "unit" in fields:
            address = (str(fields.pop("address", "") or "")).strip()
            unit = (str(fields.pop("unit", "") or "")).strip().lstrip("#").strip() or None
            if len(address) > 200:
                raise FieldError("That address is too long. Type just the street address.", "address")
            if unit and len(unit) > 20:
                raise FieldError("That unit is too long. Type just the unit number, like 12B.", "unit")
            if unit and not address:
                raise FieldError("Type the street address as well as the unit.", "address")
            fields.update(_address_fields(address, unit))
        elif raw.get("confirm_address"):
            # Steve checked the property (a guess from the landlord's parcels,
            # or an apartment complex with no unit): door hangers can go.
            fields["address_source"] = "confirmed"
        if fields.get("status") in ("responded", "quoted", "won") and "responded_at" not in fields:
            fields["responded_at"] = now_iso()
        message = None
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if not row:
                raise NotFound("That lead no longer exists. Reload the page.")
            if fields.get("address_source") == "confirmed" and not row["address"]:
                raise ValueError("Add the property address first, then confirm it.")
            if fields.get("responded_at") and row["responded_at"]:
                fields.pop("responded_at")  # keep the first response time
            if "channel" in fields and fields["channel"] == row["channel"]:
                fields.pop("channel")
            if "channel" in fields:
                fields["assign_round"] = None  # set by hand, not by an Assign leads round
                fields["assigned_by"] = outreach.BY_HAND if fields["channel"] else None
                if fields["channel"] and not row["assigned_at"]:
                    fields["assigned_at"] = now_iso()
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*fields.values(), lead_id])
                conn.commit()
            if address:
                message = self._locate(conn, lead_id)
        return {"ok": True, **({"message": message} if message else {})}

    def _locate(self, conn: Conn, lead_id: int) -> str:
        """Map location, parcel and owner for an address Steve typed in.
        Network trouble leaves the lead for the next daily check to finish."""
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if is_paused(self.settings(conn)):
            return (
                "Address saved. Lead Desk is paused, so it wasn't looked up on the map or at the county "
                "assessor; the first check after the pause does that."
            )
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
            conn.execute(
                "UPDATE leads SET lat = ?, lon = ?, in_pima = 1 WHERE id = ?",
                (float(attrs["LAT"]), float(attrs["LON"]), lead_id),
            )
            result = True
        conn.commit()
        saved = "Address saved" + (", found on the map" if result else "")
        return saved + ". " + " ".join(notes) if notes else saved + "."

    def add_touches(self, body: dict) -> dict:
        if body.get("lead_ids") is not None:
            ids = [_lead_id(i) for i in body["lead_ids"]]
        else:
            ids = [_lead_id(body.get("lead_id"))]
        kind = body.get("kind")
        cost = body.get("cost")
        if cost is not None:
            cost = money_value(cost, "Cost", MAX_CONTACT_CENTS)
        notes = notes_value(body.get("notes"))
        logged = duplicates = 0
        with self.conn() as conn:
            settings = self.settings(conn)
            rows = {}
            for lead_id in ids:
                row = conn.execute("SELECT id, channel, status FROM leads WHERE id = ?", (lead_id,)).fetchone()
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
                    raise ValueError(
                        f"That kind of contact isn't one Lead Desk knows for {outreach.CHANNELS[channel]}."
                    )
                # Two clicks in a row on the same button log one contact.
                last = conn.execute(
                    "SELECT created_at FROM touches WHERE lead_id = ? AND channel = ? AND kind = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (lead_id, channel, kind),
                ).fetchone()
                now = now_iso()
                if last and _seconds_between(last["created_at"], now) < DUPLICATE_TOUCH_SECONDS:
                    duplicates += 1
                    continue
                this_cost = (
                    cost
                    if cost is not None
                    else money_value(settings["costs"].get(channel) or 0, "Cost", MAX_CONTACT_CENTS)
                )
                conn.execute(
                    "INSERT INTO touches (lead_id, channel, kind, cost_cents, notes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (lead_id, channel, kind, this_cost, notes, now),
                )
                logged += 1
                updates: dict[str, Any] = {}
                if not row["channel"]:
                    updates.update(channel=channel, assigned_at=now, assigned_by=outreach.BY_HAND)
                if row["status"] == "new":
                    updates["status"] = "contacted"
                if updates:
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*updates.values(), lead_id])
            conn.commit()
        return {"ok": True, "logged": logged, "duplicates": duplicates}

    def delete_touch(self, body: dict) -> dict:
        touch_id = _lead_id(body.get("id"), "contact")
        with self.conn() as conn:
            if not conn.execute("SELECT id FROM touches WHERE id = ?", (touch_id,)).fetchone():
                raise NotFound("That contact was already removed. Reload the page.")
            conn.execute("DELETE FROM touches WHERE id = ?", (touch_id,))
            conn.commit()
        return {"ok": True}

    def assign(self, body: dict) -> dict:
        with field("count"):
            count = count_value(body.get("count"))
        raw = body.get("channels")
        channels = outreach.check_channels(
            list(outreach.CHANNELS) if raw is None else raw, single_method=body.get("single_method") is True
        )
        kind = body.get("lead_type") or ""
        if kind not in ("", *outreach.ROUND_KINDS):
            raise ValueError("A round can be evictions only, City code cases only, or both.")
        with self.conn() as conn:
            leads = leadlist.lead_dicts(conn, self.settings(conn))
            return outreach.assign(conn, leads, count, channels, lead_type=kind, preview=body.get("preview") is True)

    def split_preview(self, conn: Conn, settings: dict) -> dict:
        """What Assign leads can hand out with each choice of methods."""
        return outreach.split_preview(conn, leadlist.lead_dicts(conn, settings))

    def save_settings(self, body: Any) -> dict:
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
        out: dict[str, Any] = {"ok": True}
        if values.get("paused") is False and current.get("paused"):
            # Pause turned off: finish today's check now rather than tomorrow.
            resumed = self.resume_daily()
            if resumed and resumed.get("started"):
                out["daily_started"] = True
                out["message"] = "Lead Desk is running again and is finishing today's check now."
            elif resumed and resumed.get("message"):
                out["message"] = "Lead Desk is running again. " + resumed["message"]
        return out

    def _ensure_base(self) -> None:
        with self.conn() as conn:
            s = self.settings(conn)
            if s.get("base_lat") is not None or is_paused(s):
                return
            try:
                r = (self.geocoder or CensusGeocoder()).geocode(s["base_address"])
            except Exception as e:  # map service down: tried again on the next save or start
                log.warning("base address lookup failed: %s", type(e).__name__)
                return
            if r:
                db.put_settings(conn, {"base_lat": r.lat, "base_lon": r.lon})

    def run_enrich(self, body: dict) -> dict:
        if self.paused():
            return {"paused": True, "message": PAUSED_MESSAGE}
        with self.lock, self.conn() as conn:
            return enrich(
                conn, self.parcel_client or ParcelClient(), limit=self._cap(150), refresh=bool(body.get("refresh"))
            )

    def import_file(
        self, source: str, filename: Optional[str], data: Optional[bytes], lead_type: str = "eviction"
    ) -> dict:
        """One uploaded file: a saved Justice Court case or calendar page, a
        CSV of leads, or (``source="contacts"``) a CSV of phones and emails.
        Leads imported here are marked as added by hand, so they show in the
        default view right away."""
        text = decode_text(data or b"")
        if source == "contacts":
            with self.lock, self.conn() as conn:
                counts = import_contacts(conn, text)
            if not counts["rows"]:
                raise ValueError(
                    "That file has no rows Lead Desk recognises. Import a CSV with a phone "
                    "or email column and a lead_id, parcel, address or owner name column."
                )
            return counts
        if source not in ("pima_jp_calendar", "csv_import"):
            raise ValueError("Lead Desk can import a saved court page (.html) or a CSV file.")
        since = (az_today() - timedelta(days=365)).isoformat()
        unreadable = odd = 0
        if is_case_page(text):
            lead = parse_case_html(text)
            if not lead:
                raise ValueError("That case page isn't a civil (CV) case, so there's nothing to import.")
            leads = [lead]
        elif source == "csv_import" or Path(filename or "").suffix.lower() == ".csv":
            leads = list(read_csv_text(text, Path(filename or "upload.csv").name, default_type=lead_type))
            unreadable = sum(1 for l in leads if l.raw.get("unreadable_date"))
            odd = odd_dates(leads)
        else:
            leads = [l for l in parse_calendar_html(text) if not (l.event_date and l.event_date < since)]
        if not leads:
            raise ValueError(UNRECOGNISED_FILE)
        counts = {
            "new": 0,
            "updated": 0,
            "imported": len(leads),
            "unreadable_dates": unreadable,
            "odd_dates": odd,
        }
        with self.lock, self.conn() as conn:
            if source == "csv_import" or Path(filename or "").suffix.lower() == ".csv":
                # A records-request file: rows for court cases Lead Desk already
                # has fill in their property address instead of adding a lead.
                filled = fill_case_addresses(conn, leads)
                counts["addresses_filled"] = len(filled)
                if filled:
                    # A records request came in: the next one asks from today on.
                    db.put_settings(
                        conn, {"last_records_import": {"date": az_today().isoformat(), "filled": len(filled)}}
                    )
                    leadlist.record_address_share(conn, self.settings(conn))
                leads = [l for l in leads if l.source_id not in filled]
                counts["imported"] -= len(filled)
            for lead in leads:
                counts[db.upsert(conn, lead)] += 1
                conn.execute(
                    "UPDATE leads SET added_by_hand = 1 WHERE source = ? AND source_id = ?",
                    (lead.source, lead.source_id),
                )
            conn.commit()
            if len(leads) == 1 and leads[0].eviction_notice is not None:
                counts["with_notice"] = int(bool(leads[0].eviction_notice))
            counts["waiting_for_case_check"] = sum(
                1
                for l in leads
                if l.lead_type == "eviction" and l.eviction_notice is None and "jcdisplaycase" in (l.url or "").lower()
            )
            if not is_paused(self.settings(conn)):
                try:
                    counts["owners"] = enrich(conn, self.parcel_client or ParcelClient())
                except Exception:  # assessor unreachable: the daily check fills owners in
                    traceback.print_exc(file=sys.stderr)
        return counts

    def owner_properties(self, name: str) -> list[dict]:
        if self.paused():
            raise ValueError(
                "Lead Desk is paused, so the county assessor wasn't asked. Turn the pause off in Settings first."
            )
        rows = (self.parcel_client or ParcelClient()).by_owner(name)
        return [
            {
                "parcel": a.get("PARCEL"),
                "site_address": a.get("SITE_ADDRESS"),
                "site_zip": a.get("SITE_ZIP"),
                **owner_fields(a),
            }
            for a in rows
        ]


class _Borrowed:
    """The request's shared connection, for one ``with`` block: the block's
    end commits (or rolls back after an error) but leaves it open."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> Conn:
        return self._conn

    def __exit__(self, exc_type: Any, *exc: Any) -> Literal[False]:
        if exc_type is None:
            self._conn.commit()
        elif hasattr(self._conn, "rollback"):
            self._conn.rollback()
        return False


def fill_case_addresses(conn: Conn, leads: list[Lead]) -> set[str]:
    """Property addresses from an imported file (a Justice Court records
    request) for eviction cases already in Lead Desk, matched on the case
    number. An address typed or confirmed on the lead is kept; a guess from
    the landlord's parcels is replaced. Returns the case numbers matched."""
    matched = set()
    for lead in leads:
        if not (lead.source_id and lead.address):
            continue
        row = conn.execute(
            "SELECT id, address, address_source FROM leads WHERE source_id = ? AND lead_type = 'eviction' "
            "AND source <> 'csv_import' AND duplicate_of IS NULL",
            (lead.source_id.strip().upper(),),
        ).fetchone()
        if not row:
            continue
        matched.add(lead.source_id)
        if row["address"] and row["address_source"] not in (None, LANDLORD_SOURCE):
            continue  # typed or confirmed by Steve: his wins
        if row["address"] and row["address_source"] is None:
            continue  # already had one from the court
        fields = _address_fields(lead.address.strip(), None)
        fields["address_source"] = "import"
        if lead.zip:
            fields["zip"] = lead.zip
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*fields.values(), row["id"]])
    return matched


def serve(
    db_path: Any, host: str = "127.0.0.1", port: int = 8765, stale_days: int = 30, open_browser: bool = True
) -> None:
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
