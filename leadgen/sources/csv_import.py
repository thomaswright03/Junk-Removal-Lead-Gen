"""Import leads from any CSV: a records-request export from the justice court,
a constable's writ list, a list Steve keeps himself, etc.

Columns are matched by name, case-insensitively; the first alias that exists
wins. Only an address or a case number is required.
"""

import csv
import hashlib
from datetime import datetime
from pathlib import Path

from ..models import Lead
from .base import Source

ALIASES = {
    "source_id": ("case_number", "case number", "case no", "case", "id", "record id"),
    "address": ("address", "property address", "street address", "premises", "location"),
    "city": ("city",),
    "zip": ("zip", "zip code", "zipcode", "postal code"),
    "event_date": ("date", "filed", "date filed", "filing date", "event date", "hearing date",
                   "writ date"),
    "plaintiff": ("plaintiff", "landlord", "owner", "property manager"),
    "defendant": ("defendant", "tenant", "occupant"),
    "lead_type": ("type", "lead type", "lead_type"),
    "description": ("description", "notes", "note", "details"),
}


def _pick(row, keys):
    lowered = {k.strip().lower(): v for k, v in row.items() if k}
    for k in keys:
        v = lowered.get(k)
        if v not in (None, ""):
            return v.strip()
    return None


def _iso_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip()[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return value


def read_csv(path, source_name="csv_import", default_type="manual"):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            fields = {k: _pick(row, a) for k, a in ALIASES.items()}
            if not fields["address"] and not fields["source_id"]:
                continue
            source_id = fields["source_id"] or hashlib.sha1(
                f"{fields['address']}|{fields['event_date']}".upper().encode()
            ).hexdigest()[:16]
            yield Lead(
                source=source_name,
                source_id=source_id,
                lead_type=(fields["lead_type"] or default_type).lower(),
                event_date=_iso_date(fields["event_date"]),
                address=fields["address"],
                city=fields["city"],
                zip=fields["zip"],
                plaintiff=fields["plaintiff"],
                defendant=fields["defendant"],
                description=fields["description"],
                raw={"file": Path(path).name, **row},
            )


class CsvImport(Source):
    name = "csv_import"
    description = "Any CSV of leads (records requests, writ lists, manual lists)"

    def fetch(self, since, until, paths=None, lead_type="manual", **options):
        if not paths:
            raise SystemExit("csv_import needs --file path/to/leads.csv")
        for p in paths:
            yield from read_csv(p, default_type=lead_type)
