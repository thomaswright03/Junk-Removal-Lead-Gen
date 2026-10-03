"""Owner phone numbers and emails.

Public property records have the owner's name and mailing address but no
phone or email. Those come from Steve looking them up, or from a
skip-tracing service: export the owners that still need a number, upload
that file to the service, and import the file it sends back.
"""

import csv
import io
import re
from typing import Any, Optional

from .normalize import normalize_address
from .util import Conn, LeadRow, pick

_PHONE_KEYS = (
    "phone",
    "phone 1",
    "phone1",
    "mobile",
    "cell",
    "mobile phone",
    "cell phone",
    "primary phone",
    "phone number",
    "wireless 1",
    "landline 1",
    "owner phone",
)
_EMAIL_KEYS = ("email", "email 1", "email1", "e-mail", "email address", "primary email", "owner email")
_PARCEL_KEYS = ("parcel", "apn", "parcel number", "parcel id")
_ADDRESS_KEYS = ("property address", "site address", "address", "property street")
_NAME_KEYS = ("owner_name", "owner name", "owner", "name", "full name")
_ID_KEYS = ("lead_id", "lead id")


def clean_phone(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def clean_email(value: Any) -> Optional[str]:
    value = (value or "").strip()
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else None


CLOSED_STATUSES = ("stale", "skip", "lost", "won")


def import_contacts(conn: Conn, text: str) -> dict:
    """Fill owner_phone / owner_email from a CSV (a skip-tracing service's
    file, or Steve's own list).

    Each row is matched to leads by lead id, parcel, property address, then
    owner name, and fills every open lead with the same owner and mailing
    address (a skip-trace file has one row per owner). An import only fills
    empty fields: it never changes a phone or email entered by hand, and it
    keeps a number a lead already has. Returns counts, including
    ``skipped_manual`` (rows that matched a hand-entered contact, left alone)
    and ``kept_existing`` (leads that already had a different number).
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    leads = conn.execute(
        "SELECT id, parcel, address_norm, owner_name, owner_address, plaintiff, owner_phone, "
        "owner_email, contact_source, status FROM leads"
    ).fetchall()
    by_id = {str(l["id"]): dict(l) for l in leads}
    by_parcel: dict[str, list] = {}
    by_addr: dict[str, list] = {}
    by_name: dict[str, list] = {}
    by_owner: dict[Any, list] = {}
    for l in leads:
        if l["parcel"]:
            by_parcel.setdefault(l["parcel"].upper(), []).append(l["id"])
        if l["address_norm"]:
            by_addr.setdefault(l["address_norm"], []).append(l["id"])
        for n in (l["owner_name"], l["plaintiff"]):
            if n:
                by_name.setdefault(n.upper().strip(), []).append(l["id"])
        if l["owner_name"]:
            by_owner.setdefault(_owner_key(l), []).append(l["id"])
    counts = {"rows": len(rows), "matched": 0, "updated": 0, "no_match": 0, "skipped_manual": 0, "kept_existing": 0}
    for row in rows:
        phone = clean_phone(pick(row, _PHONE_KEYS))
        email = clean_email(pick(row, _EMAIL_KEYS))
        lead_id = pick(row, _ID_KEYS) or ""
        ids = (
            ([by_id[lead_id]["id"]] if lead_id in by_id else None)
            or by_parcel.get((pick(row, _PARCEL_KEYS) or "").upper())
            or by_addr.get(normalize_address(pick(row, _ADDRESS_KEYS)) or "")
            or by_name.get((pick(row, _NAME_KEYS) or "").upper().strip())
        )
        if not ids:
            counts["no_match"] += 1
            continue
        counts["matched"] += 1
        if not (phone or email):
            continue
        targets = list(dict.fromkeys(ids))
        for i in ids:
            l = by_id[str(i)]
            if l["owner_name"]:
                targets += [
                    j
                    for j in by_owner.get(_owner_key(l), [])
                    if j not in targets and by_id[str(j)]["status"] not in CLOSED_STATUSES
                ]
        manual_hit = False
        for i in targets:
            l = by_id[str(i)]
            if l["contact_source"] == "manual":
                manual_hit = True
                continue
            fill = {}
            if phone and not l["owner_phone"]:
                fill["owner_phone"] = phone
                fill["contact_source"] = "import"
            if email and not l["owner_email"]:
                fill["owner_email"] = email
                if not (l["owner_phone"] or l["contact_source"]):
                    fill["contact_source"] = "import"
            if (phone and l["owner_phone"] and l["owner_phone"] != phone) or (
                email and l["owner_email"] and l["owner_email"].lower() != email.lower()
            ):
                counts["kept_existing"] += 1
            if fill:
                sets = ", ".join(f"{k} = ?" for k in fill)
                conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*fill.values(), i])
                l.update(fill)
                counts["updated"] += 1
        counts["skipped_manual"] += int(manual_hit)
    conn.commit()
    return counts


def _owner_key(lead: LeadRow) -> tuple[str, str]:
    return ((lead["owner_name"] or "").upper().strip(), (lead["owner_address"] or "").upper().strip())


def skiptrace_csv(leads: list) -> str:
    """CSV of owners still missing a phone, in the column layout most
    skip-tracing services accept."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "lead_id",
            "owner_name",
            "mailing_address",
            "mailing_city",
            "mailing_state",
            "mailing_zip",
            "property_address",
            "property_city",
            "property_state",
            "property_zip",
            "parcel",
        ]
    )
    seen = set()
    for l in leads:
        if l.get("owner_phone") or not l.get("owner_name"):
            continue
        key = (l["owner_name"], l.get("owner_address"))
        if key in seen:
            continue
        seen.add(key)
        w.writerow(
            [
                l["id"],
                l["owner_name"],
                l.get("owner_address") or "",
                l.get("owner_city") or "",
                l.get("owner_state") or "",
                l.get("owner_zip") or "",
                l.get("address") or "",
                l.get("city") or "Tucson",
                "AZ",
                l.get("zip") or "",
                l.get("parcel") or "",
            ]
        )
    return buf.getvalue()
