"""Small helpers shared across Lead Desk: clocks, CSV columns, text files."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

# Arizona keeps Mountain Standard Time all year (no daylight saving time), so
# a fixed offset is exact. Business days (today's leads, the Google daily
# limit, stale dates) turn over at midnight here, not at UTC midnight, which
# matters on Vercel and GitHub Actions where the server clock is UTC.
ARIZONA = timezone(timedelta(hours=-7))


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    """Timestamp for the database: UTC, seconds, ISO 8601."""
    return utc_now().isoformat()


def az_now(now: datetime | None = None) -> datetime:
    return (now or utc_now()).astimezone(ARIZONA)


def az_today(now: datetime | None = None) -> date:
    """Today's date in Tucson."""
    return az_now(now).date()


def pick(row: dict, keys) -> str | None:
    """The first non-blank value among ``keys`` in a CSV row, matching column
    names case-insensitively and ignoring surrounding spaces."""
    lowered = {(k or "").strip().lower(): v for k, v in row.items() if k}
    for k in keys:
        v = lowered.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def decode_text(data: bytes) -> str:
    """Text of an uploaded file. Excel often saves CSV as Windows-1252
    rather than UTF-8, so fall back to that instead of mangling accents."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# One definition of "apartment-style" property use (assessor USE_DESC text):
# buildings with several units, where a leasing office or manager answers.
_MULTIFAMILY_RE = re.compile(r"APART|MULTI|MFR|CONDO|TOWNHOUSE|MOBILE HOME PARK")


def is_multifamily(use: str | None) -> bool:
    return bool(_MULTIFAMILY_RE.search((use or "").upper()))


def is_residential(use: str | None) -> bool:
    """Any home a landlord could rent out: multifamily, or a use the assessor
    labels residential."""
    return is_multifamily(use) or "RESID" in (use or "").upper()


def env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def is_paused(settings: dict | None = None) -> bool:
    """The kill switch: Settings "Pause" or LEADDESK_PAUSED=1 in the
    environment stops the daily check, case page reads and all lookups."""
    return bool((settings or {}).get("paused")) or env_flag("LEADDESK_PAUSED")


PAUSED_MESSAGE = ("Lead Desk is paused, so nothing was checked or looked up. Turn the pause off "
                  "in Settings (and remove LEADDESK_PAUSED if it is set) to start again.")
