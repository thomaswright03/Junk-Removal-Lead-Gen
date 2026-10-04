# Lead Desk: the operator guide

For Steve, or whoever works the leads day to day. It covers what each tab is
for and what to do on it. For commands see [COMMANDS.md](COMMANDS.md); for how
the pieces fit together see [ARCHITECTURE.md](ARCHITECTURE.md).

## Starting Lead Desk

- **On your computer:** `leadgen serve` opens http://127.0.0.1:8765. It runs
  only on your computer and uses the same database as the commands.
- **Online:** Lead Desk runs on Vercel behind a password, and the daily check
  runs on GitHub Actions. Setup is in [VERCEL.md](VERCEL.md).
- **Every morning by itself (on your computer):** run `leadgen schedule
  install` once. On macOS it runs the daily check at 6:00.

## What happens every morning

The daily check:

1. Searches the Justice Court calendar for eviction hearings in the next 30
   days.
2. Reads each new case page for the eviction notice, judgment and writ.
3. Looks up the landlord in the county assessor's records. When the landlord
   owns just one complex, it uses that property (marked "confirm").
4. Looks up the landlord's office phone, email and website.
5. Adds new City of Tucson code cases.

**Check for new evictions** runs the same check now. The header shows one
short line: open leads, when the last check finished, and a warning sign if a
lookup failed. **Details** shows the full summary.

## The Leads tab

The list comes first. Above it are one status line and one toolbar:

- **Status line:** "N of M open eviction leads can be reached now".
  - **Show the N that can't** filters to the leads still to work on.
  - **Get phones and addresses** opens the setup steps. A chip says how many
    steps are left.
  - **How this works** opens the explanations, the area coverage note and a
    glossary (notice, judgment, writ, parcel).
- **Toolbar:** search, filters and the buttons (Check for new evictions,
  Update court cases, Find landlord phones, Add cases, Import).

**Show** picks the view:

- **Default:** evictions with a notice filed, or further along (judgment or
  writ). Cases you imported whose page hasn't been read yet also show,
  marked "case not checked".
- **All evictions.**
- **All leads:** adds the City of Tucson code cases. They cover the City only,
  not unincorporated Pima County, Marana, Oro Valley, Sahuarita or South
  Tucson.

**Order:**

- Every writ (lockout) comes first, then every judgment, then everything
  else.
- Within each group, leads are ordered by priority (see below).

Click a lead, or Tab to it and press Enter, for its details, the owner's
other properties, and to log outreach and results. The tab, filters and open
lead are kept in the address bar, so a reload or Back keeps your place.

### Getting a phone number: Find phone

Court cases name the landlord but carry no phone number. Every lead with a
company landlord and no number has a **Find phone** button on its row. It
opens a panel under the row with:

- **Search the web** and **Search Google Maps**: searches for the landlord in Tucson.
- **AZ Corporation Commission**: copies the company name for the
  Commission's search box. The entry lists the company's statutory agent.
- **Company website**, when one is known.
- **A box to paste the number into.** Press Enter or **Save number**.

**Also on this landlord's other leads** is ticked by default. It fills the
same number into every other open lead of that landlord that has no number
yet. After saving, the list says how many leads can be reached now, and the
cursor moves to the next lead's Find phone. Most numbers take about half a
minute.

### Phone lookups that run by themselves

**Find landlord phones & emails**, which also runs in the daily check, looks
up office numbers for businesses only. Owners who are people are never
looked up this way. It uses:

- **OpenStreetMap (free):**
  - A business mapped at the property, when the lead has coordinates.
  - A business of the same name in the Tucson area.
  - Few Tucson landlord companies are on OpenStreetMap, so expect it to find
    only some. The daily "Phone lookup check" on GitHub reports how many it
    finds.
- **The company's website**, for a phone and email.
- **Google Places**, only if you add a key:
  - It finds most apartment complexes and property managers.
  - **Get phones and addresses → How to get a key** lists the exact clicks.
  - Google gives about 1,000 searches a month free, then charges about $35
    per 1,000.
  - Lead Desk stops at 30 a day and 1,000 a month unless you change the
    limits in Settings. A limit of 0 allows none.

Other ways to get numbers:

- **Skip-trace file:**
  1. **Download skip-trace list** writes the owners still missing a phone.
  2. Send it to a skip-tracing service (paid per record).
  3. **Import phones / emails** brings back the file the service sends.
     Imports only fill empty fields.
- **Type a number into the lead.** Hand-entered numbers are never
  overwritten.

Check numbers before calling. Scrub personal cell numbers against the Do Not
Call registry before any cold call.

### Getting addresses, judgments and writs: the records request

Court case pages have no property address, and the court calendar only lists
upcoming hearings. Cases already past their hearing, which are the judgments
and writs that need clearing now, come in only through a Justice Court
records request.

The records card sits above the address queue (below), and **Get phones and
addresses** links to it:

- **What to ask for:**
  - The first request covers the last month. Later ones cover the dates since
    your last import.
  - Include addresses, judgment dates, writ dates and the disposition.
- **Copy the request**, then open the court's form from the link.
- **I've sent the request** records the date. The next one is due two weeks
  later.
- **Import the court's file** (or **Import court page / CSV**) takes the file
  the court sends:
  - Rows for cases already in Lead Desk fill in their address. An address
    you typed or confirmed is kept.
  - New judgment and writ cases join the default view at the top of the
    list.

The address queue (**Work through the ones that need one**) lists evictions
with no address or only a guess:

- **Confirm** accepts a guess.
- **Landlord's properties** lists the landlord's parcels, with a **Use**
  button on each.

An address on a parcel with more than one home needs a unit number, or
**Confirm address**, before a door hanger goes there.

### Priority

Up to 40 points for what the case says:

- Vacant building, dumping and trash/debris score highest; weeds score
  lowest.
- Evictions score 35, with 25 more for a writ of restitution or 15 more for
  a judgment.

Then:

- **+20** on a code case if the owner's mailing address is elsewhere.
- **+10** for a company or trust owner.
- **+10** if the owner has several leads.
- **+15** if the latest court or city event was 7 days ago or less.
- **+8** if it was 8 to 14 days ago.

A lead becomes Old (stale) 30 days after its latest event. A case with a
court date today or later is never marked Old.

## Outreach and Results

These tabs stay locked until at least one open lead can be contacted. The
first phone number you save opens them. Whether this experiment is wanted
is waiting on the owner (see [DECISIONS.md](DECISIONS.md)).

**Outreach:**

- **Assign leads** deals the best unassigned leads across three outreach
  methods:
  - a door hanger at the property;
  - a phone call to the owner about this one job;
  - a standing-rate pitch to the landlord or property manager.
- Leads are dealt in small random blocks, so each method gets a similar mix.
- All of one landlord's leads go to the same method.
- Each method has its own work queue: a driving route, a call list with a
  script, or a list of companies to pitch.

**Results:** for each outreach method:

- Contacted, responded, quoted and won.
- What was spent, revenue, cost per job and revenue per dollar.

It compares one kind of lead at a time, and names a leader only when the
mixes match.

Saving job revenue marks a lead Won. For a lead marked Lost or Skip, Lead
Desk asks first (**Mark won** or **Keep it lost**). The server applies the
same rule to the command line and to any other caller. Removing a logged
contact offers **Undo**, which puts back the same entry with its time and
cost.

## Settings

- Business name and phone.
- A tracking number and cost per contact for each outreach method.
- Message templates. Each one is previewed with one of your leads of the
  kind it is for, from any view.
- Phone lookups (Google key and limits).
- Theme.

## Stopping everything (kill switch)

Tick **Pause Lead Desk** in Settings. Online and on GitHub Actions, you can
also set `LEADDESK_PAUSED=1`.

While paused, nothing makes a request: the daily check, case pages, code
cases, the county assessor, maps, and every phone lookup, Google included.
Pausing also stops a job that is already running. Turning the pause off
finishes a daily check that was stopped.

To stop only Google, untick **Use Google lookups**, or set its limits to 0.

## Language

English only, for now. Spanish waits until it is asked for (see
[DECISIONS.md](DECISIONS.md)).

## How Lead Desk reads a court case

Each document and calendar event on a Justice Court case page is read as one
kind of paper:

| Paper | Means |
|---|---|
| Judgment for the plaintiff (the landlord), default judgment, "Judgment for Plaintiff" as a hearing's result, or the parties table's "Judgment For: Plaintiff" | stage **judgment** (the latest such judgment's date counts) |
| Writ of restitution (issued or served) | stage **writ** (the lockout) |
| Judgment set aside or vacated by the court | the judgment no longer counts |
| Writ quashed or recalled | the writ no longer counts |
| Dismissal (stipulated, voluntary or by order), judgment for the defendant | case ended: **dismissed** |
| Satisfaction of judgment (the tenant paid; a partial one doesn't count) | case ended: **satisfied** |

How the papers are read:

- A motion counts only once the court grants it. Anything denied or
  withdrawn counts for nothing.
- A writ of garnishment or execution is about collecting money, not a
  lockout.
- Papers are read in date order, and the latest one that decides something
  wins. A writ after a dismissal brings the case back.
- An ended case leaves the default view. While it is under 60 days old, it
  is still read once a week in case a writ follows.
