"""Violation-code helpers shared by the Tucson source and lead scoring."""

import re
from typing import Optional

_CODE_RE = re.compile(r"^\s*([A-Z]{2,8})\s*[/:-]")

# What the inspectors' codes mean, for display. Seen in the live layer.
CODE_LABELS = {
    "PMMULT": "Trash, debris and other property maintenance",
    "REFS": "Refuse / trash accumulation",
    "DUMP": "Dumping / items piled in alley",
    "RSTOR": "Outdoor storage of items",
    "DILAP": "Dilapidated structure",
    "WEEDS": "Overgrown weeds",
    "TREES": "Overgrown trees",
    "JMV": "Junk motor vehicle",
}


def code_of(description: Optional[str]) -> Optional[str]:
    """Leading violation code of a code-case description, e.g. "WEEDS"."""
    text = description or ""
    # Our stored description is "CaseType | status | DESCRIPTION".
    if " | " in text:
        text = text.split(" | ")[-1]
    m = _CODE_RE.match(text.upper())
    return m.group(1) if m else None
