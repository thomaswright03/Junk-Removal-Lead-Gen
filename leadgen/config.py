"""Defaults shared across the pipeline. Override most of them from the CLI."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("LEADGEN_DATA_DIR", "data"))
DB_PATH = Path(os.environ.get("LEADGEN_DB", DATA_DIR / "leads.db"))
# The hosted database (Turso) when Lead Desk runs online; see docs/VERCEL.md.
# When set, it is used instead of DB_PATH.
DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")

# Leads whose event date is older than this are "stale" and left out of exports
# unless asked for.
STALE_AFTER_DAYS = 30

# Steve's yard. Phase 2 (pricing) measures drive distance from here.
BASE_ADDRESS = "8790 N Wellside Dr, Tucson, AZ"

# Pima County, AZ. FIPS 04019.
PIMA_COUNTY_FIPS = "04019"
# Rough bounding box (lon/lat) used as a cheap sanity check when a geocoder
# does not report a county.
PIMA_BBOX = (-113.34, 31.33, -110.45, 32.52)

USER_AGENT = "leadgen/0.1 (+https://github.com/thomaswright03/Junk-Removal-Lead-Gen)"
HTTP_TIMEOUT = 30
