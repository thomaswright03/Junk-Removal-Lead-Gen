"""Look up the owner of record for each lead from the Pima County Assessor.

Uses the county-wide parcel layer (PAREGION) that Pima County GIS publishes
and the City of Tucson serves as a public ArcGIS map service. One row per
parcel with the taxpayer/owner name and mailing address, the site address,
use code and year built:
https://mapdata.tucsonaz.gov/arcgis/rest/services/PublicMaps/PropertyHousing/MapServer/17

Leads with a parcel number are looked up by parcel; leads with only an
address are matched on the parcel's site address.
"""

import re
from typing import Any, Iterable, Optional

import requests

from . import config, db
from .normalize import normalize_address
from .util import Conn, LeadRow, StopCheck, is_dwelling_use, is_residential, now_iso

PARCEL_LAYER = "https://mapdata.tucsonaz.gov/arcgis/rest/services/PublicMaps/PropertyHousing/MapServer/17"
FIELDS = (
    "PARCEL",
    "ADDRESSEE",
    "ADDRESS",
    "CITY",
    "STATE_PROVINCE",
    "POSTAL_CODE",
    "MAIL1",
    "MAIL2",
    "MAIL3",
    "SITE_ADDRESS",
    "SITE_ZIP",
    "SITE_ZIPCITY",
    "USE_DESC",
    "PPT_DESC",
    "YearBuilt",
    "LAT",
    "LON",
)
BATCH = 50

_ENTITY_RE = re.compile(
    r"\b(LLC|L L C|LLLP|LP|INC|CORP|CORPORATION|CO|COMPANY|TRUST|TR|TRS|TRUSTEE|"
    r"PROPERTIES|PROPERTY|HOLDINGS|INVESTMENTS?|HOMES|REALTY|REAL ESTATE|MANAGEMENT|"
    r"MGMT|BANK|ASSN|ASSOCIATION|FUND|PARTNERS|PARTNERSHIP|VENTURES|CAPITAL|RENTALS?|"
    r"APARTMENTS?|ESTATE OF)\b"
)


def is_entity(name: Optional[str]) -> bool:
    """True for owners that are companies, trusts or estates, not people."""
    return bool(name and _ENTITY_RE.search(name.upper()))


def _sql_str(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def owner_fields(attrs: dict) -> dict:
    """Map one parcel record to the lead's owner_* columns."""
    name = (attrs.get("ADDRESSEE") or attrs.get("MAIL1") or "").strip() or None
    mail = (attrs.get("ADDRESS") or attrs.get("MAIL2") or "").strip() or None
    site = (attrs.get("SITE_ADDRESS") or "").strip() or None
    absentee = None
    if mail and site:
        absentee = int(normalize_address(mail) != normalize_address(site))
    return {
        "owner_name": name,
        "owner_address": mail,
        "owner_city": (attrs.get("CITY") or "").strip() or None,
        "owner_state": (attrs.get("STATE_PROVINCE") or "").strip() or None,
        "owner_zip": (attrs.get("POSTAL_CODE") or "").strip() or None,
        "owner_absentee": absentee,
        "owner_entity": int(is_entity(name)) if name else None,
        "property_use": (attrs.get("USE_DESC") or attrs.get("PPT_DESC") or "").strip() or None,
        "year_built": (str(attrs.get("YearBuilt") or "")).strip() or None,
    }


class ParcelClient:
    def __init__(self, session: Any = None) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    def query(self, where: str, limit: Optional[int] = None) -> list[dict]:
        params: dict[str, Any] = {
            "where": where,
            "outFields": ",".join(FIELDS),
            "returnGeometry": "false",
            "f": "json",
        }
        if limit:
            params["resultRecordCount"] = limit
        resp = self.session.get(f"{PARCEL_LAYER}/query", params=params, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS error: {payload['error']}")
        return [f.get("attributes") or {} for f in payload.get("features") or []]

    def by_parcels(self, parcels: Iterable[Optional[str]]) -> dict:
        found: dict = {}
        parcels = sorted({p for p in parcels if p})
        for i in range(0, len(parcels), BATCH):
            chunk = parcels[i : i + BATCH]
            where = f"PARCEL IN ({','.join(_sql_str(p) for p in chunk)})"
            for attrs in self.query(where):
                found.setdefault(attrs.get("PARCEL"), attrs)
        return found

    def by_site_address(self, address: Optional[str]) -> Optional[dict]:
        """Best-effort match of a street address to a parcel."""
        norm = normalize_address(address)
        if not norm:
            return None
        street = norm.split(" UNIT ")[0]
        rows = self.query(f"SITE_ADDRESS = {_sql_str(street)}", limit=5)
        return rows[0] if rows else None

    def by_owner(self, name: Optional[str], limit: int = 200) -> list[dict]:
        """Parcels whose owner name starts with ``name`` (for landlords)."""
        name = re.sub(r"\s+", " ", (name or "").upper()).strip()
        if len(name) < 4:
            return []
        return self.query(f"ADDRESSEE LIKE {_sql_str(name + '%')}", limit=limit)


def landlord_name(plaintiff: Optional[str]) -> Optional[str]:
    """First plaintiff as the assessor writes owner names, if it's a company."""
    first = (plaintiff or "").split(";")[0]
    name = re.sub(r"[.,]", " ", first.upper())
    name = re.sub(r"\s+", " ", name).strip()
    return name if is_entity(name) or re.search(r"\b(LTD|LP|LLLP|PARTNERS)\b", name) else None


def landlord_property(client: Any, plaintiff: Optional[str]) -> tuple[Optional[dict], Optional[dict]]:
    """The landlord's property from the assessor, when the court case has no address.

    Returns ``(owner_attrs, site_attrs)``: owner_attrs fills the owner columns
    whenever the landlord owns anything in the county; site_attrs is set only
    when all their residential parcels share one site address, so the eviction
    almost certainly happened there.
    """
    name = landlord_name(plaintiff)
    if not name:
        return None, None
    rows = client.by_owner(name, limit=200)
    if not rows:
        return None, None
    homes = [
        r
        for r in rows
        if is_residential(r.get("USE_DESC") or r.get("PPT_DESC")) and (r.get("SITE_ADDRESS") or "").strip()
    ]
    sites = {normalize_address(r["SITE_ADDRESS"]).split(" UNIT ")[0] for r in homes}
    return rows[0], (homes[0] if len(sites) == 1 else None)


# ``address_source`` of an address inferred from the landlord's only property:
# shown as "landlord's only property, confirm" until Steve confirms or edits it.
LANDLORD_SOURCE = "landlord"


def enrich_landlords(
    conn: Conn, client: Any = None, limit: Optional[int] = None, should_stop: StopCheck = None
) -> dict:
    """Owner and, when clear, property for eviction leads that have no address.
    The property is a guess (court cases carry no address), so it is stored
    with ``address_source = "landlord"``."""
    client = client or ParcelClient()
    sql = (
        "SELECT id, plaintiff FROM leads WHERE duplicate_of IS NULL AND lead_type = 'eviction' "
        "AND address IS NULL AND parcel IS NULL AND plaintiff IS NOT NULL "
        "AND enriched_at IS NULL ORDER BY id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    now = now_iso()
    counts = {"found": 0, "with_property": 0, "not_found": 0}
    for r in conn.execute(sql).fetchall():
        if should_stop and should_stop():
            counts["stopped_early"] = True
            break
        try:
            owner, site = landlord_property(client, r["plaintiff"])
        except Exception:
            continue  # assessor unreachable: try again next run
        fields = {}
        if owner:
            fields = owner_fields(owner)
            fields["owner_absentee"] = None  # unknown without the eviction's address
            counts["found"] += 1
        else:
            counts["not_found"] += 1
        if site:
            fields.update(owner_fields(site))
            fields.update(
                address=site["SITE_ADDRESS"].strip(),
                address_source=LANDLORD_SOURCE,
                parcel=site.get("PARCEL"),
                zip=(str(site.get("SITE_ZIP") or "").strip() or None),
            )
            fields["address_norm"] = normalize_address(fields["address"])
            if site.get("LAT") and site.get("LON"):
                fields.update(lat=float(site["LAT"]), lon=float(site["LON"]))
            counts["with_property"] += 1
        fields["enriched_at"] = now
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", [*fields.values(), r["id"]])
    conn.commit()
    return counts


def enrich_lead(conn: Conn, client: Any, row: LeadRow, attrs: Optional[dict] = None, now: Optional[str] = None) -> bool:
    """Owner columns for one lead (``row`` has id, parcel, address) from its
    parcel record, or by matching its address. Returns True when found."""
    if attrs is None and row["address"]:
        try:
            attrs = client.by_site_address(row["address"])
        except Exception:
            attrs = None
    now = now or now_iso()
    if not attrs:
        conn.execute("UPDATE leads SET enriched_at = ? WHERE id = ?", (now, row["id"]))
        return False
    fields = owner_fields(attrs)
    if not row["parcel"] and attrs.get("PARCEL"):
        fields["parcel"] = attrs["PARCEL"]
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE leads SET {sets}, enriched_at = ? WHERE id = ?", [*fields.values(), now, row["id"]])
    return True


def fix_inferred_addresses(conn: Conn) -> int:
    """Bring addresses guessed by older versions in line with today's rules:
    mark them as the landlord's property, and drop those on a parcel nobody
    lives in (a condominium's common area, vacant land) so the landlord is
    looked up again. Returns how many were dropped."""
    # Once per database: court records carry no property address (the live
    # calendar has no address column), so an address on a court case that
    # wasn't typed in or imported came from the landlord's parcels.
    if not db.get_settings(conn).get("inferred_addresses_tagged"):
        conn.execute(
            "UPDATE leads SET address_source = ? WHERE source = 'pima_jp_calendar' AND address IS NOT NULL "
            "AND address_source IS NULL AND parcel IS NOT NULL AND enriched_at IS NOT NULL "
            "AND COALESCE(added_by_hand, 0) = 0",
            (LANDLORD_SOURCE,),
        )
        db.put_settings(conn, {"inferred_addresses_tagged": True})
    rows = conn.execute(
        "SELECT id, property_use FROM leads WHERE address_source = ? AND address IS NOT NULL", (LANDLORD_SOURCE,)
    ).fetchall()
    dropped = [r["id"] for r in rows if not is_dwelling_use(r["property_use"])]
    for lead_id in dropped:
        conn.execute(
            "UPDATE leads SET address = NULL, address_norm = NULL, parcel = NULL, zip = NULL, lat = NULL, "
            "lon = NULL, geocode_tried = 0, address_source = NULL, property_use = NULL, year_built = NULL, "
            "enriched_at = NULL WHERE id = ?",
            (lead_id,),
        )
    conn.commit()
    return len(dropped)


def enrich(
    conn: Conn, client: Any = None, limit: Optional[int] = None, refresh: bool = False, should_stop: StopCheck = None
) -> dict:
    """Fill owner_* columns. Returns counts by outcome."""
    client = client or ParcelClient()
    fix_inferred_addresses(conn)
    landlords = enrich_landlords(conn, client, limit=limit, should_stop=should_stop)
    sql = (
        "SELECT id, parcel, address FROM leads WHERE duplicate_of IS NULL "
        "AND (parcel IS NOT NULL OR address IS NOT NULL)"
    )
    if not refresh:
        sql += " AND enriched_at IS NULL"
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    now = now_iso()
    counts = {
        "found": landlords["found"],
        "not_found": landlords["not_found"],
        "landlord_property": landlords["with_property"],
    }
    if should_stop and should_stop():
        counts["stopped_early"] = True
        return counts
    by_parcel = client.by_parcels(r["parcel"] for r in rows if r["parcel"])
    for r in rows:
        if should_stop and should_stop():
            counts["stopped_early"] = True
            break
        found = enrich_lead(conn, client, r, by_parcel.get(r["parcel"]) if r["parcel"] else None, now)
        counts["found" if found else "not_found"] += 1
    conn.commit()
    return counts
