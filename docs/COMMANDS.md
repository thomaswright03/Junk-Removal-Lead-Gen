# Lead Desk from the command line

Everything Lead Desk does can also be run as a command. Set up first as in the
[README](../README.md). The operator guide ([OPERATOR.md](OPERATOR.md)) covers
the web app; [ARCHITECTURE.md](ARCHITECTURE.md) says which module does what.

## The daily check

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
Justice Court records request (see [DATA_SOURCES.md](DATA_SOURCES.md)) lists them.

Columns are matched by name: `address`, `case number`, `date filed`,
`landlord`/`plaintiff`, `tenant`/`defendant`, `notes`, and a few variants.
For evictions, `judgment date`/`judgment`, `writ date`/`writ` (a date or
"yes") and `disposition` set how far the case got (judgment, writ, dismissed
or satisfied), so a records request of recent cases brings in judgment and
writ leads the court calendar never shows.

Track follow-up:

```sh
leadgen list                         # current leads with their ids
leadgen status 42 contacted --notes "left voicemail with property manager"
leadgen status 42 won
```

Statuses: `new, contacted, responded, quoted, won, lost, skip, stale`
(Lead Desk sets `responded` when a lead answers; `stale` is shown as Old).
Re-running a fetch never overwrites a status or notes.

## Other commands

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

## Running it on a schedule

`leadgen schedule install` sets up the daily run (a LaunchAgent on macOS; on
Linux and Windows it prints the cron line or Task Scheduler command to add).
The log goes to `data/daily.log`.

Don't run it as a scheduled GitHub Action that uploads results: this
repository is public and the leads contain names and addresses.
