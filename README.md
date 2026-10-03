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

**Making eviction leads reachable.** Court cases name the landlord and the
tenant but carry no phone number and no property address, so on a fresh
install most evictions can't be called or visited yet. Until both are set
up, the Leads tab shows a guide, **Get phone numbers and addresses for
eviction leads**, with two steps: adding a Google Places key (how to get one,
what it costs at your limits, and a box to paste it that saves it and starts
**Find landlord phones** at once), and sending the court records request
(**I've sent the request** records the date; the next one is due two weeks
after the latest request or import). **Hide this guide** puts it away; **How
to reach more** brings it back. The Leads tab says how many open eviction
leads can be reached now (a phone or email, or an address a door hanger can
go to), and each step says how many leads it would reach (Google only finds
companies, so it counts leads with a company landlord not looked up yet; the
records request counts leads with no usable address) and marks the one to
start with,
each eviction with neither is marked "can't reach yet" (the kind filter has
**Can't be reached yet** and **Can be reached**), and the lead says how it
can be reached. The Outreach call list and landlord list put leads with a
phone number (then an email) first, and say how many have no number yet,
why, and what to do (set up phone lookups, or find phones now).

**Language.** Lead Desk, its scripts and its door-hanger text are in English
only, as agreed; Spanish is left until Steve asks for it.

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
  Cases that ended drop out: dismissed (or decided for the tenant), judgment
  satisfied (the tenant paid), and closed or disposed with no judgment for
  the landlord and no writ. See "How Lead Desk reads a court case" below.
  The "Show" menu switches to all evictions, or all leads including City
  code cases (it says how many code cases there are; code cases cover the
  City of Tucson only, not unincorporated Pima County, Marana, Oro Valley,
  Sahuarita or South Tucson). The header is one short line (open leads, when
  the last check finished, a warning sign if a lookup failed); **Details**
  shows the full summary, how many court cases are still waiting to be
  checked and when the next check runs. The Leads tab also says how many
  open eviction leads have an address a door hanger can go to (typed,
  confirmed, imported or from the court, with a unit number where the parcel
  has more than one home; never a guess from the landlord's parcels until
  you confirm it), and an eviction
  with no address shows a "Find the address" checklist. The share of open
  evictions with an address is shown next to the share a week ago (from a
  snapshot the daily check keeps). **Work through the ones that need one**
  (or Address work queue under the kind filter) lists the evictions with no
  address or only a guess, with the case, tenant and landlord side by side:
  **Confirm** accepts a guess in one click, **Landlord's properties** lists
  the landlord's parcels with a **Use** button each. Above it, the
  records-request card says what to ask the court for (the dates since the
  last file you imported), links the court's request form, copies the
  request, imports the file the court sends, and says when the next one is
  due (every two weeks). Paste Justice Court case links
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
  the parcel and owner, and fills in the miles. An address on a parcel with
  more than one home (apartments, condos, townhouses, duplexes, a mobile or
  manufactured home park, "multiple residence", a house with an additional
  residence) needs a unit number (or **Confirm address**) before a door
  hanger goes there. Every date says what it is: Filed, Judgment or Writ for a
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
  can work (door hangers need a property address that isn't an unconfirmed
  guess, with a unit number or a confirmed address where the parcel has
  more than one home; the phone call needs a phone number and the landlord
  pitch a phone or email; **Include leads with no phone or email** deals
  those too, when you will look the numbers up yourself), all of one landlord's
  leads go to the same method (now and in later rounds), and leads are dealt
  in small random blocks within each kind (address or not, eviction or code
  case) so each method gets the same mix and a similar spread of priority.
  A round is evictions only by default (or City code cases only, or both:
  **Which leads**), since Results compares methods within one kind of lead
  and door hangers can't go to most evictions (no address), which would
  otherwise fill a mixed round with code cases. For a round of both kinds
  the methods ticked first are the ones that take in the most evictions.
  Before you press it, the tab says how many unassigned leads each choice of
  methods can split (and, for both kinds, how many are evictions), ticks
  only methods the leads can all be worked by, and offers to untick the
  method that blocks a round. The confirm question says exactly how many
  evictions and code cases the round takes. Afterwards a summary of the
  round stays on the tab until the next round or **Dismiss**: how many went
  to each method, and how many leads were left out and why, with links to
  them. Leads whose landlord is
  already being worked by a method follow it; they don't count toward the
  round's number and are reported separately. Each method has its own work
  queue: a driving route for door hangers, a call list with a script, and a
  list of companies to pitch.
- **Results**: compared within one kind of lead at a time (evictions or
  City code cases; "all leads together" is offered but says it mixes them),
  because the methods reach different people on each. On an eviction the
  phone call and the landlord pitch both reach the landlord, so they make
  different offers (one clean-out of this unit vs. a standing rate for every
  turnover); the lead shows who its method reaches and what it offers, and
  the Results tab says what it is comparing. Per method, how many leads were contacted, responded, were
  quoted and won, what was spent, revenue, cost per job and revenue per
  dollar, and the mix of leads each method got. Lead Desk names a leader
  only when the mixes match; otherwise it says why the comparison isn't
  fair yet. Leads that followed their landlord's method are counted in their
  own column and left out of the mix; "set by hand" counts only methods
  changed on the lead. Money is kept in whole cents; a quote or revenue over
  $100,000 (or a contact cost over $1,000) is refused as a likely typo.
  Saving job revenue marks a lead Won, except that a lead marked Lost or
  Skip asks first ("Mark won" or "Keep it lost"), so Results counts don't
  change without you deciding.
  A value that can't be saved (a negative or too-large amount, a phone
  number without 10 digits, an email without an @, a unit with no street
  address, notes over the limit, a blank business name) is caught in the
  browser before anything is sent and shown under its box, which is marked
  invalid and focused; the message stays until the value is corrected. A
  refusal from the server names its field and is shown the same way.
- **Settings**: business name and phone, a tracking phone number and cost
  per contact for each channel, and the message templates. Template fields
  (owner name, first name, address...) go in from buttons, and each
  template shows a live preview for one of your leads.

Each tab says what it is for in one line; the longer explanations (how the
daily check works, how Assign leads splits) are behind a "How..." link, and
the Leads tab has a short glossary of the court and property words (notice,
judgment, writ, parcel, phone-lookup service).

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
of restitution or +15 with a judgment for the landlord), +20 on a code case
if the owner's mailing address is elsewhere (not on an eviction, where the
owner is the landlord and nearly always has an office elsewhere, so it would
add the same to every eviction), +10 for a company/trust owner, +10 if the
owner has several leads, +15 if the latest court or city event (filing,
judgment, writ; opening for a code case) was 7 days ago or less (today
counts as 0 days), +8 if 8 to 14 days ago. An upcoming hearing, or any date
in the future, earns nothing.
The list order puts the case stage first: every eviction with a writ
(lockout) comes before every one with only a judgment, which comes before
every other lead; priority orders the leads within each stage. The CSV/HTML
export, `leadgen list` and Assign leads use the same order.

A lead becomes Old (stale) 30 days after its latest event, so an eviction
filed weeks ago that has just had a writ stays fresh, and an Old case that
gets a new judgment or writ is New again. A case with a court date today or
later is never marked Old. A phone lookup that fails for a
passing reason (connection dropped, timeout, busy server) is tried twice
more within the run, a few seconds apart; once a service has failed every
try for two companies it is treated as down for the rest of that run. The
daily summary says how many
phone lookups failed (they are tried again the next day) and points to
Settings when Google refused the key.

## Daily use from the command line

```sh
leadgen daily
```

The same daily check Lead Desk runs: new evictions from the court calendar,
eviction notices from the case pages, owners and landlords from the
assessor, landlord phones, new Tucson code cases. It prints one line, in
Arizona time ("Oct 3, 2026 6:12 AM 4 new evictions, ..."), naming any site
that couldn't be reached; `leadgen --debug daily` adds the full summary with
the error details. It exits with code 1 when a whole source failed (the
court calendar or the City's site), so a scheduler or GitHub Actions shows
the run as failed; the same-day retry is still set. `leadgen schedule install` runs it every morning (`--hour 7` for a
different time, `leadgen schedule remove` to stop).

When a whole source fails (the court calendar or the City's site can't be
reached, not just one lookup), the run doesn't count as the day's check: it
is tried again the same day 30 minutes later, then 1 and 2 hours after each
further failure (at most three retries, never past midnight), and the Lead
Desk header says "Today's check failed (...) · trying again today at ...".
Once a retry gets through, the day counts as checked. Lead Desk retries
while it is open; the job `leadgen schedule install` sets up also starts 1,
2, 4 and 6 hours after the first run, with `leadgen daily --if-due`, which
does nothing unless a retry is due (run `leadgen schedule install` again to
add these to a schedule set up before).

`leadgen check-court` searches the court calendar for the next 30 days and
fails when it finds no eviction hearings (there are always some), which
means the court's page has changed. GitHub runs it every day
(`.github/workflows/live-court.yml`); `--file` checks saved pages instead.

When a website can't be reached (no internet, or the court, City or county
site down), every command stops with a sentence naming the site, such as
"Lead Desk stopped: the Pima County Justice Court website couldn't be
reached. Check this computer's internet connection, or try again later if
the site is down.", and a non-zero exit code. Commands that carry on past
one failed case page or lookup do the same when every one failed. Add
`--debug` (`leadgen --debug check-court`, or `LEADGEN_DEBUG=1`) for the full
error.

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

The calendar only lists upcoming hearings. Asked for past dates, the command
says so and points to the records request or case links instead of printing
"0 new".

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

Statuses: `new, contacted, responded, quoted, won, lost, skip, stale`
(Lead Desk sets `responded` when a lead answers; `stale` is shown as Old).
Re-running a fetch never overwrites a status or notes.

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

## How Lead Desk reads a court case

Each document and calendar event on a Justice Court case page is read as
one kind of paper:

| Paper | Means |
|---|---|
| Judgment for the plaintiff (the landlord), default judgment, "Judgment for Plaintiff" as a hearing's result, or the parties table's "Judgment For: Plaintiff" | stage **judgment** |
| Writ of restitution (issued or served) | stage **writ** (the lockout) |
| Judgment set aside or vacated by the court | the judgment no longer counts |
| Writ quashed or recalled | the writ no longer counts |
| Dismissal (stipulated, voluntary or by order), judgment for the defendant | case ended: **dismissed** |
| Satisfaction of judgment (the tenant paid; a partial one doesn't count) | case ended: **satisfied** |

A motion, application, request or petition counts only once the court
grants it ("Order Granting Motion to Set Aside Judgment"); anything denied
or withdrawn counts for nothing. A writ of garnishment or execution, or a
judgment debtor exam, is about collecting money, not a lockout. A paper or
event dated after today (an upcoming hearing) never sets a stage.

The papers are read in date order and the latest one that decides
something wins: a dismissal or satisfaction after a judgment ends the case,
and a writ after a dismissal (a payment plan the tenant missed) brings it
back. Then the court's status: Dismissed ends a case with no writ; Closed or
Disposed ends a case with no judgment for the landlord and no writ (a case
the court disposed of by deciding for the landlord stays a lead, as the
tenant has to move out). An ended case leaves the default view; while it
is under 60 days old it is still read once a week in case a writ follows.

When these rules change, Lead Desk works out every stored case's stage again
from the papers it saved, the first time it starts (SQLite or Postgres),
and reads those cases again first on the next check.

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
