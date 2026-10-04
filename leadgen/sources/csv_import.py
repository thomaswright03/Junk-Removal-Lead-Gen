"""Import leads from any CSV: a records-request export from the justice court,
a constable's writ list, a list Steve keeps himself, etc.

Columns are matched by name, case-insensitively; the first alias that exists
wins. Only an address or a case number is required. For evictions, judgment
and writ columns (dates, or "yes") and a disposition column set how far the
case got, so a court records request of recent cases brings in the
judgments and writs the court calendar (upcoming hearings only) never shows.
"""

import csv
import hashlib
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

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
    # A records-request file can say how far each case got.
    "judgment_date": ("judgment date", "date of judgment", "judgment entered", "judgment"),
    "writ_date": ("writ date", "writ issued", "date writ issued", "writ of restitution", "writ"),
    "disposition": ("disposition", "case status", "status", "outcome"),
}
_YES = {"y", "yes", "true", "x", "issued", "entered"}


def case_stage_of(fields: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(stage, judgment date, writ date)`` from a row's judgment, writ and
    disposition columns (an eviction's stage, as pima_jp_case.case_stage
    names them), or Nones when the file doesn't say. A column may hold a
    date or just "yes"."""
    disposition = (fields.get("disposition") or "").upper()
    if "DISMISS" in disposition or "DEFENDANT" in disposition:
        return "dismissed", None, None
    if "SATISF" in disposition and "PARTIAL" not in disposition:
        return "satisfied", None, None
    judgment, writ = (_iso_date(fields.get(k)) for k in ("judgment_date", "writ_date"))
    said = {k: (fields.get(k) or "").strip().lower() in _YES for k in ("judgment_date", "writ_date")}
    if writ or said["writ_date"]:
        return "writ", judgment, writ
    if judgment or said["judgment_date"]:
        return "judgment", judgment, None
    return None, None, None


def _iso_date(value: Optional[str]) -> Optional[str]:
    """ISO date from the common spreadsheet formats; None when unreadable."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(value.strip()[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def read_csv_text(
    text: str, name: str = "upload.csv", source_name: str = "csv_import", default_type: str = "manual"
) -> Iterator[Lead]:
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
        lead_type = (fields["lead_type"] or default_type).lower()
        stage, judgment, writ = case_stage_of(fields) if lead_type == "eviction" else (None, None, None)
        yield Lead(
            source=source_name,
            source_id=source_id,
            lead_type=lead_type,
            event_date=event_date,
            address=fields["address"],
            city=fields["city"],
            zip=fields["zip"],
            parcel=fields["parcel"],
            plaintiff=fields["plaintiff"],
            defendant=fields["defendant"],
            description=fields["description"],
            case_stage=stage,
            judgment_date=judgment,
            writ_date=writ,
            raw=raw,
        )


def read_csv(path: Any, source_name: str = "csv_import", default_type: str = "manual") -> Iterator[Lead]:
    text = decode_text(Path(path).read_bytes())
    yield from read_csv_text(text, Path(path).name, source_name, default_type)


class CsvImport(Source):
    name = "csv_import"
    description = "Any CSV of leads (records requests, writ lists, manual lists)"

    def fetch(
        self, since: str, until: str, paths: Optional[list] = None, lead_type: str = "manual", **options: Any
    ) -> Iterator[Lead]:
        if not paths:
            raise SystemExit("csv_import needs --file path/to/leads.csv")
        for p in paths:
            yield from read_csv(p, default_type=lead_type)
