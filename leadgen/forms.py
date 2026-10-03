"""Checking what the Lead Desk page sends: settings, money amounts, lead
ids, counts and notes. Each check raises ValueError with a plain sentence
the page shows as it is."""

from datetime import datetime, timedelta

from . import outreach
from .leadlist import LEAD_VIEWS
from .normalize import extract_zip, normalize_address
from .util import az_today

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


_TEXT_SETTINGS = {
    "business_name": "Business name",
    "business_phone": "Main phone",
    "base_address": "Base address",
    "google_places_api_key": "Google key",
}


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
    for key, label in (
        ("google_monthly_limit", "Google lookups per month"),
        ("google_daily_limit", "Google lookups per day"),
    ):
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
        values["costs"] = {
            c: (money_value(v, f"Cost per contact for {outreach.CHANNELS[c]}", MAX_CONTACT_CENTS) or 0) / 100
            for c, v in costs.items()
        }
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
            or not all(isinstance(v, str) and len(v) < 5000 for v in tpl.values())
        ):
            raise ValueError("Messages must be text for each outreach method.")
        values["templates"] = dict(tpl)
    return values


UNRECOGNISED_FILE = (
    "Nothing in that file looks like a lead. Lead Desk can import a saved Justice Court case page, "
    "a saved court calendar results page, or a CSV with an address or case number column."
)


class NotFound(LookupError):
    """Answered with 404 and the message."""


def _lead_id(value, what="lead"):
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


def money_value(value, label, most=MAX_JOB_CENTS):
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
    return int(round(amount * 100))


def _seconds_between(a, b):
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _address_fields(address, unit):
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


def notes_value(value, label="Notes"):
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


def count_value(value, default=40):
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


def odd_dates(leads, today=None, days=365):
    """How many imported leads have a date more than a year before or after
    today: most likely a typo (1900, 2062) that would make the lead look
    years old, or not due for years."""
    today = today or az_today()
    low, high = (today - timedelta(days=days)).isoformat(), (today + timedelta(days=days)).isoformat()
    return sum(1 for l in leads if l.event_date and not (low <= l.event_date[:10] <= high))
