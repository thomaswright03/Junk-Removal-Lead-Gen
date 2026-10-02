# Data sources for Pima County clean-out leads

Researched October 2026. "Built" means there is a working importer in
`leadgen/sources/`.

## Summary

| Source | What it gives | Address? | Access | Status |
|---|---|---|---|---|
| City of Tucson code cases (last 60 days) | Junk/debris, weeds, outdoor storage, dumping, vacant buildings | Yes, plus parcel and lat/lon | Public ArcGIS REST API, no key | **Built, automatic** |
| Pima County Consolidated Justice Court calendar | Every eviction hearing in the county: case number, landlord, tenant, hearing date | Usually no | Public web form, no CAPTCHA seen | **Built, from saved pages**; automatic fetch is next |
| Justice Court records request | Eviction filings with property addresses | Yes | Online request form, may cost a fee | Import with `csv_import` once a file arrives |
| Pima County Assessor parcels (owner lookup) | Owner name and mailing address, property use, year built | n/a (enrichment) | Public ArcGIS REST API, no key | **Built, automatic** |
| Any CSV (constable lists, Steve's own leads) | Whatever columns it has | Usually | n/a | **Built** (`csv_import`) |
| Pima County Recorder, notices of trustee sale | Upcoming foreclosures | Yes | Recorder document search | Not built |
| Superior Court probate filings | Estate clean-outs | Sometimes | eAccess, paid per document or subscription | Not built |
| Pima County code enforcement (unincorporated areas) | Same signal as Tucson's, outside city limits | Probably | Not yet found as open data | Not built |

## City of Tucson code-enforcement cases (built)

- Dataset page: https://gisdata.tucsonaz.gov/datasets/code-cases-last-60-days
- API: `https://mapdata.tucsonaz.gov/arcgis/rest/services/PublicMaps/PermitsCode/MapServer/103/query`
- Fields: `CASENUMBER, status, OPENEDDATE, CLOSEDDATE, DESCRIPTION, MainAddress, PARCEL, NHA, LAT, LON, CaseType`.
  Case numbers look like `CE-VIO0926-03672`.
- A query for cases opened since 2026-09-02 returned about 2,600 cases across
  all types (checked from the web on 2026-10-02).
- `CaseType` values seen: Building, Zoning, Property Maintenance, Minimum
  Housing, Vacant/Nuisance Buildings, Homeless Camp, VANS Program, Other.
- Inspectors start `DESCRIPTION` with a violation code. The importer keeps open
  cases with `PMMULT` (trash/debris/weeds), `REFS` (refuse), `DUMP`, `RSTOR`
  (outdoor storage), `DILAP`, `WEEDS`, `TREES`, any Vacant/Nuisance Buildings
  case, and anything whose description mentions junk, debris, trash, furniture,
  appliances, mattresses and similar. `--all-cases` turns the filter off.
- City of Tucson only. Oro Valley, Marana, Sahuarita, South Tucson and
  unincorporated Pima County are not in it.
- These leads are the property **owner** (look up the parcel on the Pima County
  Assessor site to get the owner's mailing address), who has a city deadline to
  clean up.

## Pima County Assessor parcels (owner lookup, built)

- API: `https://mapdata.tucsonaz.gov/arcgis/rest/services/PublicMaps/PropertyHousing/MapServer/17`
  (layer `PAREGION`, "all parcels in Pima County", maintained by Pima County
  GIS and served by the City of Tucson).
- Fields used: `PARCEL`, `ADDRESSEE` (owner / taxpayer), `ADDRESS`, `CITY`,
  `STATE_PROVINCE`, `POSTAL_CODE` (mailing address), `SITE_ADDRESS`,
  `USE_DESC`, `YearBuilt`. Checked on 2026-10-02 with parcel `10610001E`.
- Tucson code cases carry a parcel number, so the match is exact. Leads with
  only an address are matched on `SITE_ADDRESS`.
- An owner is flagged **absentee** when the mailing address differs from the
  property address, and **entity** when the name looks like a company, trust
  or estate.
- "Other properties this owner has" in the app searches `ADDRESSEE` by name
  prefix, which is how an eviction plaintiff's portfolio can be found.

## Pima County Consolidated Justice Court (evictions)

All residential evictions (forcible/special detainer) in Pima County are filed
in the Consolidated Justice Court at 240 N. Stone Ave, Tucson.

- **Calendar**: https://www.jp.pima.gov/NewCalendar2018/ - search by date range,
  Case Type `CV` and Event Type `Eviction Action`. This is the only public
  place found that lists evictions by date rather than by name or case
  number, so it is the best source for "all recent evictions".
- **Case search**: https://www.jp.pima.gov/CaseSearch/ - by name, case number
  or complaint number only. Useful to look up one case, not to list them.
- **Records request**: https://www.jp.pima.gov/OnlineRecordsRequest/Default.aspx -
  the way to get filings **with property addresses**. Worth asking the court
  whether they can send a recurring report of new eviction filings; drop each
  report into `leadgen fetch --source csv_import --lead-type eviction --file ...`.
- **Arizona Public Access / eAccess do not cover it.** The statewide Public
  Access site lists Pima Consolidated Justice Court as not included
  (https://apps.azcourts.gov/publicaccess/courtsnotinc.aspx), and eAccess is
  Superior Court only.
- **Writs of restitution** (the lockout, which is when a clean-out is needed)
  are served by the Pima County constables. No public writ schedule was
  found. The calendar's later events on a case are the closest signal.

The calendar gives parties, not addresses. The plaintiff is the landlord or
property manager, and that is who pays for the clean-out after a lockout, so
an eviction lead without an address is still a name to call. Large plaintiffs
(apartment complexes, property managers) show up over and over; a short list of
them is worth building by hand.

### Why the calendar is imported from saved pages for now

This tool was built in a cloud environment whose network policy blocks
`*.pima.gov` and `*.tucsonaz.gov`, so the live calendar form could not be
inspected or tested. The parser reads a saved results page and does not rely
on exact column positions. Making it fully automatic means submitting the form
from a normal network and mapping its fields; that is the next step.

## Other options reviewed

- **Pima County Sheriff civil enforcement** handles some writs, but publishes
  no list.
- **Third-party "court records" sites** (statusrecords, courtcasefinder, etc.)
  resell the same data with their own terms; not used.
- **Geocoding**: U.S. Census Bureau geocoder
  (https://geocoding.geo.census.gov/geocoder/). Free, no key, public domain,
  and it returns the county, so "is this in Pima County" is answered directly.
  OpenStreetMap Nominatim was the spec's suggestion, but its usage policy
  forbids bulk and systematic geocoding on the public server.

## Legal and terms-of-use notes

This is a summary for planning, not legal advice. Steve should check with an
Arizona attorney before starting outreach.

1. **Court records are public, with limits.** Arizona court records are open
   under Arizona Supreme Court Rule 123, but bulk or compiled data requests
   go through the court and can be refused or need an agreement. Pulling one
   calendar page a day is ordinary public use; hammering the site or getting
   around any CAPTCHA or login is not. The tool identifies itself with a
   `User-Agent` and makes few requests.
2. **Sealed eviction cases.** A.R.S. 33-1379 requires the court to seal an
   eviction case when it is dismissed before judgment, decided for the
   tenant, or set aside by stipulation, and sealed cases may not be sold or
   released in a bulk transfer. Leads are a snapshot: a case can be sealed
   after it was pulled. Do not resell or share the list, and drop a lead if
   you learn its case was sealed.
3. **Don't let this become a tenant-screening product.** Using the list to
   market Steve's own hauling is fine. Selling or giving it to landlords to
   judge tenants would make it a consumer report under the Fair Credit
   Reporting Act.
4. **Contact the landlord, not the tenant.** The landlord pays for the
   clean-out, and approaching people who were just evicted is both a weak lead
   and a reputational risk.
5. **Outreach rules.** Cold calls and texts fall under the federal TCPA and the
   Do Not Call registry (texts and autodialed calls to cell phones need prior
   consent), and Arizona has its own telephone-solicitation statute (A.R.S.
   44-1271 and following). Emails need CAN-SPAM basics (real sender, physical
   address, opt-out). Mailers and in-person visits to a property manager's
   office are the least regulated.
6. **This repository is public.** Lead data (names and addresses) must never be
   committed. `data/`, `exports/`, `*.db` and `*.csv` are git-ignored for that
   reason.
7. **Tucson open data license**: not shown on the dataset page; Tucson Open
   Data is published for public reuse, but confirm on the site before
   redistributing the raw data.
