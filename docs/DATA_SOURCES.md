# Data sources for Pima County clean-out leads

Researched October 2026. "Built" means there is a working importer in
`leadgen/sources/`.

## Summary

| Source | What it gives | Address? | Access | Status |
|---|---|---|---|---|
| City of Tucson code cases (last 60 days) | Junk/debris, weeds, outdoor storage, dumping, vacant buildings | Yes, plus parcel and lat/lon | Public ArcGIS REST API, no key | **Built, automatic** |
| Pima County Consolidated Justice Court calendar | Every eviction hearing in the county: case number, landlord, tenant, hearing date | Usually no | Public web form, no CAPTCHA seen | **Built, automatic** (daily search of the next 30 days; saved pages can also be imported) |
| Justice Court case pages | Eviction notice, judgment, writ of restitution, case status, next court date | No | Public page per case, read one at a time | **Built, automatic** for cases found on the calendar or pasted as links |
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

## Phone and email lookup (built)

Public records carry no phone or email. `leadgen/lookup.py` finds office
contacts for **businesses only** (LLC/trust owners, eviction plaintiffs,
apartment complexes):

- **OpenStreetMap** via the Overpass API (https://overpass-api.de): businesses
  mapped within 80 m of the property with `phone`, `email` or `website` tags.
  Free, no key; data is ODbL ("(c) OpenStreetMap contributors"). Coverage of
  apartment leasing offices in Tucson is partial.
- **Google Places Text Search (New)**: optional, needs a Google Maps Platform
  key and is billed per request after the monthly credit. Searches the
  company name near the property, or "apartments <address>" for multifamily
  parcels. Google's Maps Platform terms restrict keeping Places content, so
  Google-sourced contacts are re-fetched after 30 days; read the current
  terms before relying on stored results.
- **The company's own website** (from either source): home page plus up to
  three contact/about/leasing pages, honoring robots.txt.

Individual owners' numbers come only from Steve or a skip-tracing file the
user buys and imports (`leadgen contacts export` / `import`).

## Pima County Consolidated Justice Court (evictions)

All residential evictions (forcible/special detainer) in Pima County are filed
in the Consolidated Justice Court at 240 N. Stone Ave, Tucson.

- **Calendar**: https://www.jp.pima.gov/NewCalendar2018/ - search by date range,
  Case Type `CV` and Event Type `Eviction Action`. This is the only public
  place found that lists evictions by date rather than by name or case
  number, so it is the best source for "all recent evictions".
- **Case search**: https://www.jp.pima.gov/CaseSearch/ - by name, case number
  or complaint number only. Useful to look up one case, not to list them.
- **Case pages** (built): each case has a page at
  `https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID=<number>` with the
  filing date, case status, next court date, parties and the documents filed.
  An eviction case lists a `CIV – EVICTION NOTICE` document once the landlord
  files the notice served on the tenant; Lead Desk shows only those cases by
  default. `leadgen/sources/pima_jp_case.py` reads pages only for cases
  linked from the calendar search or pasted by hand, with a pause between
  requests. It does not step through ID numbers:
  the IDs are sequential, but walking them is bulk collection of court
  records, which Arizona Supreme Court Rule 123 treats separately from
  looking up individual cases. Ask the court first if that is ever wanted.
  The page has no property address.
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

### How the calendar is searched (built)

`CalendarClient` in `leadgen/sources/pima_jp_calendar.py` fills in the same
form a person uses: Case Type "Eviction Actions", Event Type "Eviction
Action", a start and end date (`mm-dd-yyyy`), then reads every results page
(50 rows each, ASP.NET `Page$N` postbacks) with a pause between pages. Each
row carries the case number, the parties with their roles ("NAME
(Plaintiff)"), the hearing date and a link to the case page. The daily run
searches today through 30 days out once a day, which is about a dozen
results pages. It then reads case pages, at most 400 a day: new cases first,
then open cases whose court date has passed since they were last read (to
catch the judgment and the writ of restitution, when a unit actually needs
clearing), then other open cases every few days (daily while they have no
eviction notice yet). Closed and dismissed cases aren't read again. That is
the volume of someone checking the calendar each morning, not a bulk
download; the case search form (which has a CAPTCHA) is not used, and case
ID ranges are never scanned.

Everything here stops while Lead Desk is paused (Settings, or
`LEADDESK_PAUSED=1`).

The calendar and case pages have no property address. For a company
landlord, the daily run looks the name up in the assessor's parcel layer;
when all of their residential parcels share one site address (a single
apartment complex), that address is used for the lead. Otherwise the lead
keeps the landlord's name and mailing address only, until Steve types the
address into the lead (from the landlord, or by picking one of the
landlord's other properties); Lead Desk then geocodes it and looks up the
parcel and owner.

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
