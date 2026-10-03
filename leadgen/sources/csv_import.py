"""Import leads from any CSV: a records-request export from the justice court,
a constable's writ list, a list Steve keeps himself, etc.

Columns are matched by name, case-insensitively; the first alias that exists
wins. Only an address or a case number is required.
"""

import csv
import hashlib
import io
from datetime import datetime
from pathlib import Path

from ..models import Lead
from ..util import decode_text, pick
from .base import Source

ALIASES = {
    "source_id": ("case_number", "case number", "case no", "case", "id", "record id"),
    "address": ("address", "property address", "street address", "premises", "location"),
    "city": ("city",),
    "parcel": ("parcel", "apn", "parcel number", "parcel id"),
    "zip": ("zip", "zip code", "zipcode", "postal code"),
    "event_date": ("date", "filed", "date filed", "filing date", "event date", "hearing date", "writ date"),
    "plaintiff": ("plaintiff", "landlord", "owner", "property manager"),
    "defendant": ("defendant", "tenant", "occupant"),
    "lead_type": ("type", "lead type", "lead_type"),
    "description": ("description", "notes", "note", "details"),
}


def _iso_date(value):
    """ISO date from the common spreadsheet formats; None when unreadable."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value.strip()[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def read_csv_text(text, name="upload.csv", source_name="csv_import", default_type="manual"):
    """Leads from CSV text. A date that can't be read is left empty, and the
    lead's ``raw["unreadable_date"]`` keeps what the file said."""
    for row in csv.DictReader(io.StringIO(text)):
        fields = {k: pick(row, a) for k, a in ALIASES.items()}
        if not fields["address"] and not fields["source_id"]:
            continue
        source_id = (
            fields["source_id"]
            or hashlib.sha1(f"{fields['address']}|{fields['event_date']}".upper().encode()).hexdigest()[:16]
        )
        event_date = _iso_date(fields["event_date"])
        raw = {"file": name, **{k: v for k, v in row.items() if k}}
        if fields["event_date"] and not event_date:
            raw["unreadable_date"] = fields["event_date"]
        yield Lead(
            source=source_name,
            source_id=source_id,
            lead_type=(fields["lead_type"] or default_type).lower(),
            event_date=event_date,
            address=fields["address"],
            city=fields["city"],
            zip=fields["zip"],
            parcel=fields["parcel"],
            plaintiff=fields["plaintiff"],
            defendant=fields["defendant"],
            description=fields["description"],
            raw=raw,
        )


def read_csv(path, source_name="csv_import", default_type="manual"):
    text = decode_text(Path(path).read_bytes())
    yield from read_csv_text(text, Path(path).name, source_name, default_type)


class CsvImport(Source):
    name = "csv_import"
    description = "Any CSV of leads (records requests, writ lists, manual lists)"

    def fetch(self, since, until, paths=None, lead_type="manual", **options):
        if not paths:
            raise SystemExit("csv_import needs --file path/to/leads.csv")
        for p in paths:
            yield from read_csv(p, default_type=lead_type)
