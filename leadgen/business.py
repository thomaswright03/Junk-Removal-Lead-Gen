"""Which companies a lead's phone lookup searches for, and whether a found
business is the same one: the owner and landlord names, cut to the business
(an "ATTN:" management company first), and a loose name match.

Only businesses are looked up. Owners who are people get their numbers from
Steve or a skip-tracing file (see contacts.py)."""

import re
from dataclasses import dataclass, field
from typing import Optional

from .util import LeadRow, is_multifamily

NAME_NOISE = {
    "LLC",
    "L",
    "C",
    "INC",
    "CORP",
    "CO",
    "COMPANY",
    "LP",
    "LLLP",
    "LTD",
    "THE",
    "OF",
    "AND",
    "TR",
    "TRS",
    "TRUST",
    "TRUSTEE",
    "AZ",
    "ARIZONA",
    "TUCSON",
    "&",
}


@dataclass
class Contact:
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    source: Optional[str] = None
    matched_name: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def empty(self) -> bool:
        return not (self.phone or self.email or self.website)


def contact_reach(c: Contact) -> tuple[bool, bool, bool]:
    """What a found contact gives, for keeping the better of two."""
    return (bool(c.phone), bool(c.email), bool(c.website))


def name_tokens(name: Optional[str]) -> set[str]:
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    return {w for w in words if w not in NAME_NOISE and len(w) > 1}


def names_match(a: Optional[str], b: Optional[str]) -> bool:
    """True when two business names plausibly refer to the same company."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small & big) / len(small) >= 0.6


_BUSINESS_WORDS = {
    "LLC",
    "LLLP",
    "LP",
    "LTD",
    "INC",
    "CORP",
    "CORPORATION",
    "CO",
    "COMPANY",
    "GROUP",
    "PROPERTIES",
    "PROPERTY",
    "HOLDINGS",
    "INVESTMENTS",
    "INVESTMENT",
    "HOMES",
    "REALTY",
    "REAL",
    "MANAGEMENT",
    "MGMT",
    "RESIDENTIAL",
    "COMMUNITIES",
    "APARTMENTS",
    "APARTMENT",
    "RENTALS",
    "RENTAL",
    "CAPITAL",
    "PARTNERS",
    "PARTNERSHIP",
    "VENTURES",
    "FUND",
    "ASSOCIATES",
    "ASSN",
    "ASSOCIATION",
    "BANK",
    "LENDING",
    "DEVELOPMENT",
    "ENTERPRISES",
    "HOUSING",
    "VILLAGE",
}
_SPLIT_RE = re.compile(r"\s*(?:\bATTN\b:?|\bC/O\b|%)\s*", re.I)


def split_owner(name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'SUMMIT RIDGE AZ LLC ATTN: DASMEN RESIDENTIAL' ->
    ('SUMMIT RIDGE AZ LLC', 'DASMEN RESIDENTIAL')."""
    parts = _SPLIT_RE.split((name or "").strip(), maxsplit=1)
    owner = parts[0].strip(" ,") or None
    attn = parts[1].strip(" ,") if len(parts) > 1 else None
    return owner, attn or None


def is_business(name: Optional[str]) -> bool:
    """A company, not a person or a family/living trust."""
    words = set(re.findall(r"[A-Z]+", (name or "").upper()))
    return bool(words & _BUSINESS_WORDS)


def lookup_targets(lead: LeadRow) -> tuple[list[str], bool]:
    """Business names to search for this lead, best first, and whether to
    look for a business at the property itself (apartment leasing office)."""
    names: list[str] = []
    for raw in (lead["plaintiff"], lead["owner_name"]):
        owner, attn = split_owner(raw)
        # "ATTN:" usually names the management company: the one to call.
        for n in (attn, owner):
            if n and is_business(n) and n.upper() not in (x.upper() for x in names):
                names.append(n)
    site = bool(lead["address"]) and (is_multifamily(lead["property_use"]) or bool(names))
    return names, site
