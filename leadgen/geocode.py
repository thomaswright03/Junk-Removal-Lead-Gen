"""Turn street addresses into coordinates and confirm they are in Pima County.

Uses the U.S. Census Bureau geocoder: free, no API key, public-domain output,
and it reports the county directly, so we don't need a separate boundary file.
https://geocoding.geo.census.gov/geocoder/
"""

import time
from dataclasses import dataclass
from typing import Optional

import requests

from . import config

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    in_pima: bool
    matched_address: str = ""
    city: Optional[str] = None
    zip: Optional[str] = None


def in_pima_bbox(lat, lon):
    west, south, east, north = config.PIMA_BBOX
    return west <= lon <= east and south <= lat <= north


def parse_census_response(payload):
    matches = (payload.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    m = matches[0]
    lon, lat = m["coordinates"]["x"], m["coordinates"]["y"]
    counties = (m.get("geographies") or {}).get("Counties") or []
    if counties:
        in_pima = counties[0].get("GEOID") == config.PIMA_COUNTY_FIPS
    else:
        in_pima = in_pima_bbox(lat, lon)
    comps = m.get("addressComponents") or {}
    return GeocodeResult(
        lat=lat,
        lon=lon,
        in_pima=in_pima,
        matched_address=m.get("matchedAddress", ""),
        city=(comps.get("city") or None),
        zip=(comps.get("zip") or None),
    )


class CensusGeocoder:
    def __init__(self, session=None, delay=0.5):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.delay = delay

    def geocode(self, address, city=None, zip_code=None):
        one_line = address
        if city and city.upper() not in address.upper():
            one_line += f", {city}"
        if "AZ" not in one_line.upper() and "ARIZONA" not in one_line.upper():
            one_line += ", AZ"
        if zip_code and zip_code not in one_line:
            one_line += f" {zip_code}"
        resp = self.session.get(
            CENSUS_URL,
            params={
                "address": one_line,
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "layers": "Counties",
                "format": "json",
            },
            timeout=config.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        time.sleep(self.delay)
        return parse_census_response(resp.json())
