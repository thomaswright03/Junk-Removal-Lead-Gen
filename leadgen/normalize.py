"""Address normalization used for de-duplication.

This is deliberately simple: upper-case, strip punctuation, collapse
whitespace, and abbreviate common street words the way USPS does, so that
"123 North Main Street, Apt 4" and "123 N MAIN ST #4" compare equal.
"""

import re

_WORDS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
    "CIRCLE": "CIR", "TERRACE": "TER", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "TRAIL": "TRL", "WAY": "WAY", "LOOP": "LOOP", "VISTA": "VIS",
    "APARTMENT": "APT", "UNIT": "UNIT", "SUITE": "STE", "SPACE": "SPC",
}

_UNIT_RE = re.compile(r"\s(?:APT|UNIT|STE|SPC|LOT|#)\s*([A-Z0-9-]+)\b")
_ZIP_RE = re.compile(r"\b(85\d{3})(?:-\d{4})?\b")


def normalize_address(address):
    """Return a comparison key for an address, or None if it is empty."""
    if not address:
        return None
    s = address.upper()
    s = s.replace("#", " # ")
    s = re.sub(r"[.,;]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Drop a trailing "TUCSON AZ 857xx" style suffix; dedup is on street line.
    s = re.sub(r"\b(?:AZ|ARIZONA)\b.*$", "", s).strip()
    s = re.sub(
        r"\s(?:TUCSON|ORO VALLEY|MARANA|SAHUARITA|SOUTH TUCSON|GREEN VALLEY|VAIL|CATALINA|AJO|ARIVACA)\s*$",
        "",
        s,
    ).strip()
    s = " ".join(_WORDS.get(w, w) for w in s.split(" "))
    s = _UNIT_RE.sub(lambda m: f" UNIT {m.group(1)}", " " + s).strip()
    s = re.sub(r"\s+", " ", s)
    return s or None


def extract_zip(address):
    if not address:
        return None
    m = _ZIP_RE.search(address)
    return m.group(1) if m else None
