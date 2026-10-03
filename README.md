# Junk Removal Lead Gen

Builds a list of properties in Pima County, Arizona that are likely to need a
clean-out: recent evictions and code-enforcement cases for junk, debris, yard
waste and vacant buildings. Made for Steve's junk-removal business (based at
8790 N Wellside Dr, Tucson).

Phase 1 (this) is the lead list. Phase 2 will add price estimates
(volume, distance, stairs, hazards).

Where the data comes from, what's automatic, and the legal limits on using it:
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Setup

Python 3.9 or newer.

```sh
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Lead Desk (the web app)

```sh
leadgen serve
```

Opens http://127.0.0.1:8765 in your browser. It only runs on your computer and
uses the same database as the commands below.

- **Leads**: by default only eviction cases with an eviction notice filed in
  the court case (the "Show" menu switches to all evictions, or all leads
  including Tucson code cases). Paste Justice Court case links
  (`jcDisplayCase.aspx?ID=...`) into **Add cases**, and Lead Desk reads each
  case page for the eviction notice, parties and next court date. Each open
  lead is ranked by score, with the owner of record from
  the Pima County Assessor (name, mailing address, whether they live
  elsewhere, whether it's an LLC or trust). Click a lead for details, the
  owner's other properties, and to log outreach and results.
- **Outreach**: the experiment. "Assign leads" deals the best unassigned
  leads evenly across three channels: door hanger at the property, phone
  call to the owner, and landlord / property-manager outreach. Each channel
  has its own work queue: a driving route for door hangers, a call list with a
  script, and a list of companies to pitch.
- **Results**: per channel, how many leads were contacted, responded, were
  quoted and won, what was spent, revenue, cost per job and revenue per
  dollar.
- **Settings**: business name and phone, a tracking phone number and cost
  per contact for each channel, and the message templates.

**Owner phone and email.** Public property records have no phone numbers or
emails, so they come from three places, all shown in the Phone and Email
columns:

1. **Find landlord phones & emails** (or `leadgen contacts find`) looks up
   office numbers for businesses only: LLC/trust owners, eviction landlords,
   and apartment complexes. It checks OpenStreetMap for a business mapped at
   the property, Google Places when a Google Maps Platform API key is set
   (Settings, or `GOOGLE_PLACES_API_KEY`), and then the company's own website
   for a phone and email. One lookup covers every lead with the same company.
   Owners who are people are never looked up this way.
2. **Download skip-trace list** (or `leadgen contacts export`) writes the
   owners still missing a phone in the format skip-tracing services take.
   Send it to one (BatchSkipTracing, PropStream and similar charge per
   record), then **Import phones / emails** (or `leadgen contacts import
   --file ...`) the file they send back. Rows are matched by lead id, parcel,
   property address or owner name.
3. Type a number into a lead by hand. Hand-entered contacts are never
   overwritten by a lookup.

Check found numbers before calling, and scrub personal cell numbers against
the Do Not Call registry before any cold call.

**Refresh data** pulls new Tucson cases, looks up owners and re-reads open
eviction case pages for new documents. **Update court cases** re-reads the
case pages on demand. **Import court page / CSV** takes a saved Justice Court
case page or calendar page, or any CSV.

Score: up to 40 points for what the case says (vacant building, dumping and
trash/debris highest, weeds lowest; evictions 35), +20 if the owner's mailing
address is elsewhere, +10 for a company/trust owner, +10 if the owner has
several leads, +15 if under a week old (+8 under two weeks).

## Daily use from the command line

```sh
leadgen run
```

That pulls new City of Tucson code cases, looks up each owner from the county
assessor, geocodes addresses and checks they
are in Pima County, marks leads older than 30 days as stale, and writes
`exports/leads-YYYY-MM-DD.csv` and `exports/leads-YYYY-MM-DD.html`. Open the
HTML file in a browser to search and filter; open the CSV in Excel or import it
into a CRM.

Add evictions from the Justice Court calendar (see
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md#pima-county-consolidated-justice-court-evictions)
for how to save the page):

```sh
leadgen fetch --source pima_jp_calendar --assume-eviction --file data/inbox/calendar.html
```

Add or re-check eviction cases by their case page links:

```sh
leadgen cases add "https://www.jp.pima.gov/CaseSearch/jcDisplayCase.aspx?ID=1234567"
leadgen cases update
```

Add any spreadsheet of leads (a records-request export, a writ list, referrals):

```sh
leadgen fetch --source csv_import --lead-type eviction --file data/inbox/filings.csv
```

Columns are matched by name: `address`, `case number`, `date filed`,
`landlord`/`plaintiff`, `tenant`/`defendant`, `notes`, and a few variants.

Track follow-up:

```sh
leadgen list                         # current leads with their ids
leadgen status 42 contacted --notes "left voicemail with property manager"
leadgen status 42 won
```

Statuses: `new, contacted, quoted, won, lost, skip, stale`. Re-running a fetch
never overwrites a status or notes.

### Other commands

| Command | What it does |
|---|---|
| `leadgen sources` | List the sources and which ones run automatically |
| `leadgen fetch --days 14` | Fetch only, with a 14-day look-back |
| `leadgen contacts find` | Look up business phone/email/website for landlords and LLC owners |
| `leadgen contacts export` / `import --file x.csv` | Skip-trace list out, phone/email file in |
| `leadgen enrich` | Look up owners for leads that don't have one yet |
| `leadgen geocode` | Geocode leads that have an address but no coordinates |
| `leadgen age` | Mark leads older than `--stale-days` (default 30) as stale |
| `leadgen export --format html --include-stale` | Export everything, old leads too |

Data lives in `data/leads.db` (SQLite) by default; set `LEADGEN_DB` or pass
`--db` to change it.

### Running it on a schedule

On Windows, use Task Scheduler to run `leadgen run` daily from the project
folder. On macOS or Linux:

```cron
0 6 * * * cd /path/to/Junk-Removal-Lead-Gen && .venv/bin/leadgen run >> data/run.log 2>&1
```

Don't run it as a scheduled GitHub Action that uploads results: this
repository is public and the leads contain names and addresses.

## How de-duplication works

Each upstream record (`source` + case number) is stored once and refreshed on
later runs. Addresses are normalized ("123 North Main Street Apt 4" and
"123 N MAIN ST #4" match), and when a second record lands on an address that's
already listed, it is linked to the first one and hidden from exports
(`--include-duplicates` shows it).

## Tests

```sh
pytest
```
