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

## Daily use

```sh
leadgen run
```

That pulls new City of Tucson code cases, geocodes addresses and checks they
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
