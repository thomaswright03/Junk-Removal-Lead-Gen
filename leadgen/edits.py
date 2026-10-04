"""Lead Desk's edits to one lead: the fields Steve changes on it (status,
notes, method, phone, email, address, quote and revenue), a number found
for a landlord put on its other leads, the contacts he logs, removes and
puts back, and the first-phones pass's skipped landlords. ``App`` (web.py)
gets these from ``LeadEdits``."""

import sys
import traceback
from typing import Any

from . import db, leadlist, outreach, phonepass
from .contacts import clean_email, clean_phone
from .dealing import BY_HAND
from .enrich import ParcelClient, enrich_lead
from .forms import (
    DUPLICATE_TOUCH_SECONDS,
    EDITABLE,
    MAX_CONTACT_CENTS,
    FieldError,
    NotFound,
    _address_fields,
    _lead_id,
    _seconds_between,
    field,
    money_value,
    notes_value,
)
from .geocode import CensusGeocoder
from .util import Conn, is_paused, now_iso


class LeadEdits:
    """The part of Lead Desk's ``App`` that changes a lead and its contacts."""

    # Set up by App (web.py).
    geocoder: Any
    parcel_client: Any

    def conn(self) -> Conn:
        raise NotImplementedError

    def settings(self, conn: Conn) -> dict:
        raise NotImplementedError

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
            revenue = fields.get("revenue_cents")
            if revenue and revenue != row["revenue_cents"] and "status" not in fields:
                # Job revenue means the job was done: the lead is Won, from
                # the page, the command line or any other caller alike. A lead
                # marked Lost or Skip only moves when the caller says so.
                if row["status"] in ("lost", "skip"):
                    was = "Lost" if row["status"] == "lost" else "Skip"
                    raise FieldError(
                        f"This lead is marked {was}. Job revenue usually means the job was done: mark it Won, "
                        f"or keep it {was} and save the amount with its status.",
                        "job_revenue",
                    )
                if row["status"] != "won":
                    fields["status"] = "won"
                    fields.setdefault("responded_at", now_iso())
            if fields.get("responded_at") and row["responded_at"]:
                fields.pop("responded_at")  # keep the first response time
            if "channel" in fields and fields["channel"] == row["channel"]:
                fields.pop("channel")
            if "channel" in fields:
                fields["assign_round"] = None  # set by hand, not by an Assign leads round
                fields["assigned_by"] = BY_HAND if fields["channel"] else None
                if fields["channel"] and not row["assigned_at"]:
                    fields["assigned_at"] = now_iso()
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*fields.values(), lead_id])
                conn.commit()
            also = 0
            if body.get("same_landlord") is True and fields.get("owner_phone"):
                also = self._same_landlord_phone(conn, row, fields["owner_phone"])
            if address:
                message = self._locate(conn, lead_id)
        return {"ok": True, **({"message": message} if message else {}), **({"also": also} if also else {})}

    def _same_landlord_phone(self, conn: Conn, row: Any, phone: str) -> int:
        """A number Steve found for a landlord, on that landlord's other open
        leads that have no number yet (entered by hand, like this one).
        Returns how many leads it was added to."""
        key = outreach.landlord_key(row)
        if key.startswith("lead:"):
            return 0
        closed = ", ".join(f"'{s}'" for s in leadlist.CLOSED)
        others = conn.execute(
            "SELECT id, plaintiff, owner_name FROM leads WHERE id <> ? AND duplicate_of IS NULL "
            f"AND COALESCE(owner_phone, '') = '' AND status NOT IN ({closed}) "
            "AND (plaintiff IS NOT NULL OR owner_name IS NOT NULL)",
            (row["id"],),
        ).fetchall()
        ids = [r["id"] for r in others if outreach.landlord_key(r) == key]
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE leads SET owner_phone = ?, contact_source = 'manual' WHERE id IN ({marks})",
                [phone, *chunk],
            )
        conn.commit()
        return len(ids)

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

    def skip_landlord(self, body: dict) -> dict:
        """The first-phones pass: skip a landlord with no number to be found, or bring it back."""
        with self.conn() as conn:
            return phonepass.set_skipped(conn, body.get("key"), body.get("skip") is not False)

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
        # The check for a contact just logged and the insert run under one
        # lock, so identical requests at the same moment store one contact.
        with self.conn() as conn, db.write_lock(conn, ids):
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
                    updates.update(channel=channel, assigned_at=now, assigned_by=BY_HAND)
                if row["status"] == "new":
                    updates["status"] = "contacted"
                if updates:
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*updates.values(), lead_id])
        return {"ok": True, "logged": logged, "duplicates": duplicates}

    # What a removed contact keeps, so Undo can put the same entry back.
    _TOUCH_COLUMNS = ("lead_id", "channel", "kind", "cost", "cost_cents", "notes", "created_at")

    def delete_touch(self, body: dict) -> dict:
        """Remove one logged contact. The answer carries the entry as it was
        (``removed``), which ``restore_touch`` puts back for Undo."""
        touch_id = _lead_id(body.get("id"), "contact")
        with self.conn() as conn:
            row = conn.execute("SELECT * FROM touches WHERE id = ?", (touch_id,)).fetchone()
            if not row:
                raise NotFound("That contact was already removed. Reload the page.")
            conn.execute("DELETE FROM touches WHERE id = ?", (touch_id,))
            conn.commit()
        return {"ok": True, "removed": {"id": row["id"], **{k: row[k] for k in self._TOUCH_COLUMNS}}}

    def restore_touch(self, body: dict) -> dict:
        """Undo a removal: the same entry (its id, time, method, kind, cost
        and notes) back in the lead's history."""
        entry = body.get("removed")
        if not isinstance(entry, dict):
            raise ValueError("Nothing to put back. Reload the page.")
        touch_id = _lead_id(entry.get("id"), "contact")
        lead_id = _lead_id(entry.get("lead_id"))
        if entry.get("channel") not in outreach.CHANNELS or entry.get("kind") not in dict(
            outreach.TOUCH_KINDS[entry["channel"]]
        ):
            raise ValueError("That contact can't be put back. Log it again from the lead.")
        cents = entry.get("cost_cents")
        whole = isinstance(cents, int) and not isinstance(cents, bool)
        if cents is not None and not (whole and 0 <= cents <= MAX_CONTACT_CENTS):
            raise ValueError("That contact can't be put back. Log it again from the lead.")
        if not isinstance(entry.get("created_at"), str) or len(entry["created_at"]) > 40:
            raise ValueError("That contact can't be put back. Log it again from the lead.")
        notes = notes_value(entry.get("notes"))
        with self.conn() as conn:
            if not conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone():
                raise NotFound("That lead no longer exists. Reload the page.")
            if conn.execute("SELECT id FROM touches WHERE id = ?", (touch_id,)).fetchone():
                return {"ok": True, "restored": False}  # already back (Undo pressed twice)
            conn.execute(
                "INSERT INTO touches (id, lead_id, channel, kind, cost, cost_cents, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    touch_id,
                    lead_id,
                    entry["channel"],
                    entry["kind"],
                    cents / 100 if cents is not None else 0,  # the old dollar column, from the checked cents
                    cents,
                    notes,
                    entry["created_at"],
                ),
            )
            conn.commit()
        return {"ok": True, "restored": True}
