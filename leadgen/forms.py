"""Checking what the Lead Desk page sends: settings, money amounts, lead
ids, counts and notes. Each check raises ValueError with a plain sentence
the page shows as it is."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterator, Optional

from . import outreach
from .leadlist import LEAD_VIEWS
from .normalize import extract_zip, normalize_address
from .util import ARIZONA, az_today

EDITABLE = {
    "status",
    "channel",
    "notes",
    "quote_amount",
    "job_revenue",
    "responded_at",
    "owner_phone",
    "owner_email",
    "address",
    "unit",
}
# A second identical contact logged this soon after the first is a double click.
DUPLICATE_TOUCH_SECONDS = 10


class FieldError(ValueError):
    """A value the page can't save, about one form field (``field``, the
    name the page sent it under): the page shows it under that field."""

    def __init__(self, message: str, field: str) -> None:
        super().__init__(message)
        self.field = field


@contextmanager
def field(name: str) -> Iterator[None]:
    """A ValueError raised inside is about the field ``name``."""
    try:
        yield
    except FieldError:
        raise
    except ValueError as e:
        raise FieldError(str(e), name) from None


_TEXT_SETTINGS = {
    "business_name": "Business name",
    "business_phone": "Main phone",
    "base_address": "Base address",
    "google_places_api_key": "Google key",
}


def _limit(value: Any, label: str) -> Optional[int]:
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


# Every setting the page (or another caller) may send to /api/settings.
SETTINGS_KEYS = {
    *_TEXT_SETTINGS,
    "lead_view",
    "google_monthly_limit",
    "google_daily_limit",
    "paused",
    "google_enabled",
    "setup_guide_hidden",
    "records_requested",
    "costs",
    "tracking_numbers",
    "templates",
    "clear_google_key",
}


def validate_settings(body: dict) -> dict:
    """The settings in ``body``, checked. Raises ValueError naming the field
    when one has the wrong shape, or naming any key Lead Desk doesn't have
    (a misspelt name would otherwise look saved and change nothing)."""
    unknown = sorted(str(k) for k in body if k not in SETTINGS_KEYS)
    if unknown:
        names = ", ".join(f"“{k[:40]}”" for k in unknown[:5])
        raise FieldError(f"Lead Desk has no setting called {names}. Nothing was saved.", unknown[0][:40])
    values: dict[str, Any] = {}
    for key, label in _TEXT_SETTINGS.items():
        if key in body:
            v = body[key]
            if v is None:
                v = ""
            if not isinstance(v, str) or len(v) > 300:
                raise FieldError(f"{label} must be text (up to 300 characters).", key)
            values[key] = v.strip()
    if "business_name" in values and not values["business_name"]:
        # Every message says "Steve with <business>": it can't be blank.
        raise FieldError(
            "Type your business name: every door hanger and call script uses it. Nothing was saved.", "business_name"
        )
    if "lead_view" in body:
        if body["lead_view"] not in LEAD_VIEWS:
            raise ValueError("Show must be one of: " + ", ".join(LEAD_VIEWS) + ".")
        values["lead_view"] = body["lead_view"]
    for key, label in (
        ("google_monthly_limit", "Google lookups per month"),
        ("google_daily_limit", "Google lookups per day"),
    ):
        if key in body:
            with field(key):
                values[key] = _limit(body[key], label)
    for key, label in (
        ("paused", "Pause"),
        ("google_enabled", "Use Google"),
        ("setup_guide_hidden", "Hide the setup guide"),
    ):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError(f"{label} must be on or off.")
            values[key] = body[key]
    if "records_requested" in body:
        # "I've sent the court records request": today, so the next one is due
        # two weeks from now. False takes it back.
        if not isinstance(body["records_requested"], bool):
            raise ValueError("Records request sent must be yes or no.")
        values["records_requested"] = {"date": az_today().isoformat()} if body["records_requested"] else None
    if "costs" in body:
        costs = body["costs"]
        if not isinstance(costs, dict) or set(costs) - set(outreach.CHANNELS):
            raise ValueError("Cost per contact must list a dollar amount for each outreach method.")
        values["costs"] = {}
        for c, v in costs.items():
            with field(f"costs.{c}"):
                cents = money_value(v, f"Cost per contact for {outreach.CHANNELS[c]}", MAX_CONTACT_CENTS)
            values["costs"][c] = (cents or 0) / 100
    if "tracking_numbers" in body:
        nums = body["tracking_numbers"]
        if (
            not isinstance(nums, dict)
            or set(nums) - set(outreach.CHANNELS)
            or not all(isinstance(v, str) for v in nums.values())
        ):
            raise ValueError("Tracking numbers must be a phone number (or blank) for each outreach method.")
        values["tracking_numbers"] = {c: v.strip() for c, v in nums.items()}
    if "templates" in body:
        tpl = body["templates"]
        if (
            not isinstance(tpl, dict)
            or set(tpl) - set(outreach.DEFAULT_SETTINGS["templates"])
            or not all(isinstance(v, str) for v in tpl.values())
        ):
            raise ValueError("Messages must be text for each outreach method.")
        for k, v in tpl.items():
            if len(v) >= TEMPLATE_LIMIT:
                message = f"A message can be up to {TEMPLATE_LIMIT - 1:,} characters. Shorten it."
                raise FieldError(message, f"templates.{k}")
        values["templates"] = dict(tpl)
    return values


# The longest a message template can be, plus one.
TEMPLATE_LIMIT = 5000

UNRECOGNISED_FILE = (
    "Nothing in that file looks like a lead. Lead Desk can import a saved Justice Court case page, "
    "a saved court calendar results page, or a CSV with an address or case number column."
)


class NotFound(LookupError):
    """Answered with 404 and the message."""


def _lead_id(value: Any, what: str = "lead") -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Lead Desk didn't say which {what} this is. Reload the page and try again.") from None
    if n <= 0 or isinstance(value, bool):
        raise ValueError(f"Lead Desk didn't say which {what} this is. Reload the page and try again.")
    return n


# The most one job, quote or contact can be: a typo (an extra zero, a pasted
# number) above these is refused, so it can't skew the Results tab.
MAX_JOB_CENTS = 100_000 * 100
MAX_CONTACT_CENTS = 1_000 * 100


def money_value(value: Any, label: str, most: int = MAX_JOB_CENTS) -> Optional[int]:
    """A dollar amount as whole cents: a non-negative number up to ``most``
    cents, or None for blank."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a dollar amount, like 250.")
    try:
        amount = float(str(value).strip().replace("$", "").replace(",", "")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a dollar amount, like 250.") from None
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be a dollar amount, like 250.")
    if amount < 0:
        raise ValueError(f"{label} can't be negative.")
    if amount * 100 > most:
        raise ValueError(f"{label} can be at most ${most // 100:,}. Check for an extra zero.")
    # Exact decimal arithmetic on the amount as written: whole cents only,
    # never a float rounded one way or the other (12.345 is refused).
    exact = (
        Decimal(str(value).strip().replace("$", "").replace(",", "")) if isinstance(value, str) else Decimal(str(value))
    )
    cents = exact * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"{label} can have at most 2 decimal places (whole cents), like 12.35.")
    return int(cents)


def responded_value(value: Any) -> Optional[str]:
    """When the owner first responded: a date or date-time (ISO, as the page
    and the API send it) no later than now, or None to clear it. Stored as
    UTC ISO text like every other time. The earliest it can be is checked
    against the lead (``check_responded_after_filing``)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    bad = "Responded at must be a date and time, like 2026-10-04T15:30."
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(bad)
    text = value.strip()
    try:
        when = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise ValueError(bad) from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=ARIZONA)  # a time typed on the page is Tucson time
    when = when.astimezone(timezone.utc)
    if when > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("Responded at can't be in the future.")
    if when.year < 2000:
        raise ValueError(bad)
    return when.isoformat()


def check_responded_after_filing(responded_at: str, row: Any) -> None:
    """A response can't come before the lead existed: before its court filing
    or City case opened (or, for a case seen first on the calendar, before
    Lead Desk first saw it)."""
    starts = [str(v)[:10] for v in (row["event_date"], row["first_seen"]) if v and str(v)[:4].isdigit()]
    earliest = min(starts) if starts else None
    if earliest and responded_at[:10] < earliest:
        raise FieldError(
            f"Responded at can't be before the lead was filed ({earliest}). Check the date.", "responded_at"
        )


def _seconds_between(a: Any, b: Any) -> float:
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _address_fields(address: str, unit: Optional[str]) -> dict:
    """Columns to set when Steve types a property address. The location,
    parcel and owner are looked up again from the new address."""
    if not address:
        return {
            "address": None,
            "unit": None,
            "address_norm": None,
            "lat": None,
            "lon": None,
            "in_pima": None,
            "geocode_tried": 0,
            "parcel": None,
            "address_source": "manual",
        }
    return {
        "address": address.upper(),
        "unit": unit,
        "address_norm": normalize_address(address),
        "zip": extract_zip(address),
        "lat": None,
        "lon": None,
        "in_pima": None,
        "geocode_tried": 0,
        "parcel": None,
        "enriched_at": None,
        "address_source": "manual",
    }


# Notes are for a few lines about the call or visit; a pasted page would
# slow every load and break the layout.
NOTES_LIMIT = 2000


def notes_value(value: Any, label: str = "Notes") -> Optional[str]:
    """Note text, or None. Too long or not text raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    if len(value) > NOTES_LIMIT:
        raise ValueError(
            f"{label} can be up to {NOTES_LIMIT:,} characters; this is {len(value):,}. Shorten it and save again."
        )
    return value


def count_value(value: Any, default: int = 40) -> int:
    """ "Leads this round" in Assign leads: a whole number of 1 or more."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError("Leads this round must be a whole number, like 40.")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError("Leads this round must be a whole number, like 40.") from None
    if n != n or n < 1 or n != int(n) or n > 100000:
        raise ValueError("Leads this round must be a whole number, like 40.")
    return int(n)


def odd_dates(leads: list, today: Optional[date] = None, days: int = 365) -> int:
    """How many imported leads have a date more than a year before or after
    today: most likely a typo (1900, 2062) that would make the lead look
    years old, or not due for years."""
    today = today or az_today()
    low, high = (today - timedelta(days=days)).isoformat(), (today + timedelta(days=days)).isoformat()
    return sum(1 for l in leads if l.event_date and not (low <= l.event_date[:10] <= high))
