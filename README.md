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

To put Lead Desk online (Vercel, behind a password, with the daily check on
GitHub Actions), follow [docs/VERCEL.md](docs/VERCEL.md).

**New evictions arrive by themselves.** Every morning (and whenever you press
**Check for new evictions**) Lead Desk searches the Justice Court calendar
for eviction hearings in the next 30 days, reads each new case page for the
eviction notice, looks up the landlord in the county assessor's records (and
their property, when they own just one complex), then looks up the landlord's
phone, email and website. To have this run every morning even when Lead Desk
isn't open, run once:

```sh
leadgen schedule install      # macOS: runs `leadgen daily` at 6:00 every day
```

Phone numbers for landlords come from OpenStreetMap and company websites,
which cover only some businesses. A Google Places API key (Settings) finds
most apartment complexes and property managers; without one, expect many
evictions to still need a number (use the skip-trace export below).
Google gives 1,000 of these searches a month free, then charges about $35
per 1,000; Lead Desk stops at 30 a day and 1,000 a month unless you change
the limits in Settings. A limit of 0 allows no Google searches; "no limit" is
a separate box.

**Stopping everything (kill switch).** Tick **Pause Lead Desk** in Settings,
or set the environment variable `LEADDESK_PAUSED=1` (on Vercel, and as a
repository variable for the GitHub Actions daily check). While paused, the
daily check, court case page reads, City code case fetches, owner (county
assessor) and map lookups, and every phone/email lookup, Google included,
make no requests at all, in Lead Desk and from the command line (`leadgen
fetch`, `enrich`, `geocode`, `run`, `cases`, `contacts find` stop with the
paused message; fetching from saved files with `--file` still works); the
buttons say Lead Desk is paused. Pause takes effect the moment it is ticked,
without "Save settings".
Pausing also stops a check or job that is already running, before its next
request to the court or a lookup service, and its result says it was paused.
A daily check stopped this way doesn't count as the day's check: turning the
pause off finishes it straight away (online, it starts the GitHub run when
`LEADDESK_GITHUB_TOKEN` is set; otherwise the header says to press **Check
for new evictions**), and cases already read that day aren't read again.
Leads and notes stay as they are. To stop only Google, untick **Use Google
lookups** (this works even when the key comes from `GOOGLE_PLACES_API_KEY`)
or set its limits to 0.

- **Leads**: by default eviction cases with an eviction notice filed in the
  court case, or further along (a judgment for the landlord, or a writ of
  restitution, the lockout that leaves belongings behind), plus cases you
  imported whose case page hasn't been read yet (marked "case not checked").
  Dismissed cases, and closed cases that never reached a judgment, drop out.
  The "Show" menu switches to all evictions, or all leads including City
  code cases (it says how many code cases there are; code cases cover the
  City of Tucson only, not unincorporated Pima County, Marana, Oro Valley,
  Sahuarita or South Tucson). The header is one short line (open leads, when
  the last check finished, a warning sign if a lookup failed); **Details**
  shows the full summary, how many court cases are still waiting to be
  checked and when the next check runs. The Leads tab also says how many
  open eviction leads have a confirmed or typed address, and an eviction
  with no address shows a "Find the address" checklist. Paste Justice Court case links
  (`jcDisplayCase.aspx?ID=...`) into **Add cases** to add cases by hand;
  Lead Desk reads each case page for the eviction notice, judgment, writ,
  parties and next court date. Each open lead is ranked by priority, with the
  owner of record from the Pima County Assessor (name, mailing address,
  whether they live elsewhere, whether it's an LLC or trust). Click a lead
  (or Tab to it and press Enter) for details, the owner's other properties,
  and to log outreach and results. Court case pages carry no property
  address. When a landlord owns just one residential property in the county
  (never a condominium common area or vacant land), Lead Desk uses it, marked
  "landlord's only complex — confirm" in the list, the lead and the route
  sheet until you press **Confirm address** or type the address yourself.
  About half of eviction leads need an address typed in (or picked from the
  landlord's other properties); Lead Desk then finds it on the map, looks up
  the parcel and owner, and fills in the miles. An apartment or condo
  address needs a unit number (or **Confirm address**) before a door hanger
  goes there. Every date says what it is: Filed, Judgment or Writ for a
  case that has been read, Hearing for one that hasn't, Opened for a code
  case. The list comes from the server a page (100 leads) at a time, so it
  stays quick however many leads build up, and on a phone each lead is a
  card. Notes,
  quotes and anything typed into a lead are kept while you do other things,
  and are saved with a status change. The tab, filters and open lead are in
  the address bar, so a reload or Back keeps your place.
- **Outreach**: the experiment. "Assign leads" hands out the best
  unassigned leads across three outreach methods, each reaching someone
  different or making a different offer: a door hanger at the property
  (whoever is there), a phone call to the owner about this one job, and a
  standing-rate pitch to the landlord or property manager. So the methods
  can be compared fairly, a round only uses leads that every ticked method
  can work (door hangers need a property address, and a unit number or a
  confirmed address at an apartment or condo complex), all of one landlord's
  leads go to the same method (now and in later rounds), and leads are dealt
  in small random blocks within each kind (address or not, eviction or code
  case) so each method gets the same mix and a similar spread of priority.
  Before you press it, the tab says how many unassigned leads each choice of
  methods can split, ticks only methods the leads can all be worked by, and
  offers to untick the method that blocks a round. Leads whose landlord is
  already being worked by a method follow it; they don't count toward the
  round's number and are reported separately. Each method has its own work
  queue: a driving route for door hangers, a call list with a script, and a
  list of companies to pitch.
- **Results**: per method, how many leads were contacted, responded, were
  quoted and won, what was spent, revenue, cost per job and revenue per
  dollar, and the mix of leads each method got. Lead Desk names a leader
  only when the mixes match; otherwise it says why the comparison isn't
  fair yet. Leads that followed their landlord's method are counted in their
  own column and left out of the mix; "set by hand" counts only methods
  changed on the lead. Money is kept in whole cents; a quote or revenue over
  $100,000 (or a contact cost over $1,000) is refused as a likely typo.
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
   property address or owner name, and each row fills every open lead with
   the same owner and mailing address. An import only fills empty phone and
   email fields; it never changes a number entered by hand, and the result
   says how many rows were skipped for that reason. UTF-8 and Excel
   (Windows-1252) CSV files both work.
3. Type a number into a lead by hand. Hand-entered contacts are never
   overwritten by a lookup or an import.

Check found numbers before calling, and scrub personal cell numbers against
the Do Not Call registry before any cold call.

**Check for new evictions** runs the daily check now (it runs in the
background; the page fills in as cases come in). **Update court cases**
re-reads every open eviction case page, and **Find landlord phones &
emails** runs the lookup; both run in the background with progress in the
header and a Cancel button (online they do one batch per press). **Import
court page / CSV** takes a saved Justice Court case page or calendar page,
or any CSV (UTF-8 or Excel's Windows-1252); imported leads show on the Leads
tab right away, and the result says when nothing in a file was recognised
or a date couldn't be read.

Priority: up to 40 points for what the case says (vacant building, dumping
and trash/debris highest, weeds lowest; evictions 35, +25 more with a writ
of restitution or +15 with a judgment for the landlord), +20 if the owner's
mailing address is elsewhere, +10 for a company/trust owner, +10 if the
owner has several leads, +15 if the latest court or city event (filing,
judgment, writ; opening for a code case) is under a week old (+8 under two
weeks). An upcoming hearing, or any date in the future, earns nothing.
The list order puts the case stage first: every eviction with a writ
(lockout) comes before every one with only a judgment, which comes before
every other lead; priority orders the leads within each stage. The CSV/HTML
export, `leadgen list` and Assign leads use the same order.

A lead becomes Old (stale) 30 days after its latest event, so an eviction
filed weeks ago that has just had a writ stays fresh, and an Old case that
gets a new judgment or writ is New again. The daily summary says how many
phone lookups failed (they are tried again the next day) and points to
Settings when Google refused the key.

## Daily use from the command line

```sh
leadgen daily
```

The same daily check Lead Desk runs: new evictions from the court calendar,
eviction notices from the case pages, owners and landlords from the
assessor, landlord phones, new Tucson code cases. It prints a one-line
summary. `leadgen schedule install` runs it every morning (`--hour 7` for a
different time, `leadgen schedule remove` to stop).

```sh
leadgen run
```

That pulls new City of Tucson code cases, looks up each owner from the county
assessor, geocodes addresses and checks they
are in Pima County, marks leads whose latest event is older than 30 days as stale, and writes
`exports/leads-YYYY-MM-DD.csv` and `exports/leads-YYYY-MM-DD.html`, in Lead Desk's order (writs, then
judgments, then the rest, each highest priority first), with the priority, case stage, notice flag and
latest event (Filed, Judgment, Writ, Opened) in the first columns. Open the
HTML file in a browser to search and filter; open the CSV in Excel or import it
into a CRM.

Search the Justice Court calendar for eviction hearings in a date range, or
read a saved results page:

```sh
leadgen fetch --source pima_jp_calendar --since 2026-10-03 --until 2026-11-02
leadgen fetch --source pima_jp_calendar --file data/inbox/calendar.html
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

In Lead Desk, **Import court page / CSV** does the same, and rows whose case
number matches a court case already in Lead Desk fill in that case's
property address instead of adding a lead (an address you typed or
confirmed is kept). That is the way to get real addresses for evictions: a
Justice Court records request (see docs/DATA_SOURCES.md) lists them.

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

`leadgen schedule install` sets up the daily run (a LaunchAgent on macOS; on
Linux and Windows it prints the cron line or Task Scheduler command to add).
The log goes to `data/daily.log`.

Don't run it as a scheduled GitHub Action that uploads results: this
repository is public and the leads contain names and addresses.

## How de-duplication works

Each upstream record (`source` + case number) is stored once and refreshed on
later runs. Addresses are normalized ("123 North Main Street Apt 4" and
"123 N MAIN ST #4" match), and when a second record lands on an address that's
already listed, it is linked to the first one and hidden from exports
(`--include-duplicates` shows it).

## Environment variables

| Variable | What it does | Default |
|---|---|---|
| `LEADGEN_DATA_DIR` | Folder for the local SQLite database | `data` |
| `LEADGEN_DB` | Path of the local SQLite database | `$LEADGEN_DATA_DIR/leads.db` |
| `DATABASE_URL` | Postgres (Neon) database; when set, used instead of the SQLite file | unset |
| `POSTGRES_URL` | Read when `DATABASE_URL` isn't set (some Vercel integrations name it this) | unset |
| `LEADDESK_PASSWORD` | Password for Lead Desk online (required there) | unset |
| `LEADDESK_PAUSED` | `1` pauses the daily check, case page reads, code case fetches and all lookups | unset (running) |
| `GOOGLE_PLACES_API_KEY` | Google Places key for phone lookups; overrides the key in Settings | unset |
| `LEADDESK_GITHUB_TOKEN` | Online, lets "Check for new evictions" start the GitHub Actions check | unset |
| `LEADDESK_GITHUB_REF` | Branch that check runs from | `main` |
| `VERCEL_GIT_REPO_OWNER`, `VERCEL_GIT_REPO_SLUG` | Set by Vercel; name the repository for that check | set by Vercel |
| `TEST_DATABASE_URL` | Tests only: run the suite against this Postgres database | unset (SQLite) |

## Tests

```sh
pytest                                  # Python tests (SQLite)
pytest --cov                            # with line coverage; fails below the floor in pyproject.toml
TEST_DATABASE_URL=postgresql://... pytest   # the same tests on Postgres
ruff check . && ruff format --check .   # lint and format
mypy                                    # type check
```

The browser tests (`tests/test_browser.py`) drive Lead Desk in headless
Chromium; they run when Playwright is installed
(`pip install -e ".[dev]" && python -m playwright install chromium`) and
are skipped otherwise, with the reason (such as the missing browser file).
In CI (where `CI` is set) a missing browser fails the run instead. CI runs
all of these, with coverage on the SQLite run.
