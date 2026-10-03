"""City of Tucson code-enforcement cases (last 60 days).

Public ArcGIS layer behind the Neighborhood Support Network app, published on
Tucson Open Data: https://gisdata.tucsonaz.gov/datasets/code-cases-last-60-days

Each case has an address, parcel, lat/lon, open date and a free-text
description. We keep open cases whose violation code or description means
junk, debris, yard waste or a vacant/neglected building, which are the ones
that turn into clean-outs. Coverage is City of Tucson only; unincorporated Pima County and
the other towns are not in this layer.
"""

from datetime import datetime
from typing import Any, Iterator, Optional

import requests

from .. import config
from ..models import Lead
from ..tucson_codes import code_of
from ..util import ARIZONA
from .base import Source

LAYER_URL = "https://mapdata.tucsonaz.gov/arcgis/rest/services/PublicMaps/PermitsCode/MapServer/103"
PAGE_SIZE = 1000

# Inspectors start DESCRIPTION with a violation code ("WEEDS / overgrown
# weeds in front yard"). These are the codes that mean stuff has to be hauled
# away. Seen in the live layer in October 2026.
CLEANOUT_CODES = {
    "PMMULT",  # multiple property-maintenance violations: trash, debris, weeds
    "REFS",  # refuse / trash accumulation
    "DUMP",  # illegal dumping, items piled in alley
    "RSTOR",  # outdoor storage of items on the property
    "DILAP",  # dilapidated structure
    "WEEDS",  # overgrown weeds (yard debris)
    "TREES",  # overgrown trees (yard debris)
}
CLEANOUT_CASE_TYPES = {"VACANT/NUISANCE BUILDINGS"}

KEYWORDS = (
    "JUNK",
    "DEBRIS",
    "TRASH",
    "RUBBISH",
    "LITTER",
    "GARBAGE",
    "REFUSE",
    "ACCUMULAT",
    "VACANT",
    "ABANDONED HOUSE",
    "ABANDONED HOME",
    "OVERGROWN",
    "DILAPIDAT",
    "HOARD",
    "OUTDOOR STORAGE",
    "FURNITURE",
    "APPLIANCE",
    "MATTRESS",
    "BLIGHT",
    "CLEAN UP",
    "CLEANUP",
    "GREEN WASTE",
    "YARD WASTE",
)


def _epoch_ms_to_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(value / 1000, tz=ARIZONA).date().isoformat()


def violation_code(description: Optional[str]) -> Optional[str]:
    return code_of(description)


def is_closed(attrs: dict) -> bool:
    return str(attrs.get("status") or "").upper().startswith("CLOSED")


def matches_keywords(attrs: dict) -> bool:
    """True when the case looks like something Steve could haul away."""
    description = str(attrs.get("DESCRIPTION") or "").upper()
    if str(attrs.get("CaseType") or "").upper() in CLEANOUT_CASE_TYPES:
        return True
    if violation_code(description) in CLEANOUT_CODES:
        return True
    return any(k in description for k in KEYWORDS)


def feature_to_lead(feature: dict) -> Lead:
    a = feature.get("attributes") or {}
    geom = feature.get("geometry") or {}
    lat = a.get("LAT") or geom.get("y")
    lon = a.get("LON") or geom.get("x")
    case = a.get("CASENUMBER") or str(a.get("OBJECTID"))
    return Lead(
        source="tucson_code_cases",
        source_id=case,
        lead_type="code_violation",
        event_date=_epoch_ms_to_date(a.get("OPENEDDATE")),
        address=(a.get("MainAddress") or "").strip() or None,
        city="Tucson",
        lat=lat,
        lon=lon,
        in_pima=True,
        parcel=(a.get("PARCEL") or "").strip() or None,
        description=" | ".join(str(x) for x in (a.get("CaseType"), a.get("status"), a.get("DESCRIPTION")) if x),
        url="https://gisdata.tucsonaz.gov/datasets/code-cases-last-60-days",
        raw=a,
    )


class TucsonCodeCases(Source):
    name = "tucson_code_cases"
    description = "City of Tucson code-enforcement cases (junk/debris/vacant), last 60 days"

    def __init__(self, session: Any = None) -> None:
        self.session: Any = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    def _query(self, where: str, offset: int) -> dict:
        resp = self.session.get(
            f"{LAYER_URL}/query",
            params={
                "where": where,
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": 4326,
                "orderByFields": "OBJECTID",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
                "f": "json",
            },
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS error: {payload['error']}")
        return payload

    def fetch(
        self, since: str, until: str, paths: Optional[list] = None, all_cases: bool = False, **options: Any
    ) -> Iterator[Lead]:
        # Upper bound is applied client-side; not every ArcGIS server accepts
        # date arithmetic in the where clause.
        where = f"OPENEDDATE >= DATE '{since}'"
        offset = 0
        while True:
            payload = self._query(where, offset)
            features = payload.get("features") or []
            for f in features:
                attrs = f.get("attributes") or {}
                if not all_cases and (is_closed(attrs) or not matches_keywords(attrs)):
                    continue
                lead = feature_to_lead(f)
                if lead.event_date and lead.event_date > until:
                    continue
                yield lead
            if not payload.get("exceededTransferLimit") or not features:
                break
            offset += len(features)
