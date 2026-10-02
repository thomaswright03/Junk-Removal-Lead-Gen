"""Owner phone numbers and emails.

Public property records have the owner's name and mailing address but no
phone or email. Those come from Steve looking them up, or from a
skip-tracing service: export the owners that still need a number, upload
that file to the service, and import the file it sends back.
"""

import csv
import io
import re

from .normalize import normalize_address

_PHONE_KEYS = ("phone", "phone 1", "phone1", "mobile", "cell", "mobile phone", "cell phone",
               "primary phone", "phone number", "wireless 1", "landline 1", "owner phone")
_EMAIL_KEYS = ("email", "email 1", "email1", "e-mail", "email address", "primary email",
               "owner email")
_PARCEL_KEYS = ("parcel", "apn", "parcel number", "parcel id")
_ADDRESS_KEYS = ("property address", "site address", "address", "property street")
_NAME_KEYS = ("owner_name", "owner name", "owner", "name", "full name")
_ID_KEYS = ("lead_id", "lead id")


def clean_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def clean_email(value):
    value = (value or "").strip()
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else None


def _pick(row, keys):
    lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
    for k in keys:
        if lowered.get(k):
            return lowered[k]
    return None


def import_contacts(conn, text):
    """Fill owner_phone / owner_email from a CSV. Matches each row to leads by
    lead id, parcel, property address, then owner name. Returns counts."""
    rows = list(csv.DictReader(io.StringIO(text)))
    leads = conn.execute(
        "SELECT id, parcel, address_norm, owner_name, plaintiff FROM leads"
    ).fetchall()
    by_id = {str(l["id"]): [l["id"]] for l in leads}
    by_parcel, by_addr, by_name = {}, {}, {}
    for l in leads:
        if l["parcel"]:
            by_parcel.setdefault(l["parcel"].upper(), []).append(l["id"])
        if l["address_norm"]:
            by_addr.setdefault(l["address_norm"], []).append(l["id"])
        for n in (l["owner_name"], l["plaintiff"]):
            if n:
                by_name.setdefault(n.upper().strip(), []).append(l["id"])
    counts = {"rows": len(rows), "matched": 0, "updated": 0, "no_match": 0}
    for row in rows:
        phone = clean_phone(_pick(row, _PHONE_KEYS))
        email = clean_email(_pick(row, _EMAIL_KEYS))
        ids = (by_id.get(_pick(row, _ID_KEYS) or "")
               or by_parcel.get((_pick(row, _PARCEL_KEYS) or "").upper())
               or by_addr.get(normalize_address(_pick(row, _ADDRESS_KEYS)))
               or by_name.get((_pick(row, _NAME_KEYS) or "").upper().strip()))
        if not ids:
            counts["no_match"] += 1
            continue
        counts["matched"] += 1
        if not (phone or email):
            continue
        for lead_id in ids:
            conn.execute(
                "UPDATE leads SET owner_phone = COALESCE(?, owner_phone), "
                "owner_email = COALESCE(?, owner_email), contact_source = 'import' WHERE id = ?",
                (phone, email, lead_id),
            )
            counts["updated"] += 1
    conn.commit()
    return counts


def skiptrace_csv(leads):
    """CSV of owners still missing a phone, in the column layout most
    skip-tracing services accept."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["lead_id", "owner_name", "mailing_address", "mailing_city", "mailing_state",
                "mailing_zip", "property_address", "property_city", "property_state",
                "property_zip", "parcel"])
    seen = set()
    for l in leads:
        if l.get("owner_phone") or not l.get("owner_name"):
            continue
        key = (l["owner_name"], l.get("owner_address"))
        if key in seen:
            continue
        seen.add(key)
        w.writerow([l["id"], l["owner_name"], l.get("owner_address") or "",
                    l.get("owner_city") or "", l.get("owner_state") or "",
                    l.get("owner_zip") or "", l.get("address") or "", l.get("city") or "Tucson",
                    "AZ", l.get("zip") or "", l.get("parcel") or ""])
    return buf.getvalue()
