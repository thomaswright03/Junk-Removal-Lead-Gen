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

**Check for new evictions** runs the same check now. A first check takes
about 15 minutes, because it reads up to 400 court case pages with a pause
between them; while it runs the header counts them ("Reading court cases:
120 of 400"). The header shows one short line: open leads, when the last
check finished, and a warning sign if a lookup failed. **Details** shows the
full summary, including how many landlords the automatic phone lookup has
found a number for.

## The Leads tab

The list comes first. Above it are the filters, one status line and the
one next step; **Check for new evictions** is in the header:

- **Filters:** Show (the view), search, kind of lead, status, outreach
  method and order, then **More tools**.
- **Status line:** "N of M open eviction leads can be reached now", then the
  next step: **Find phones for the top 10** while any of the top ten can't be
  reached (it opens the first-phones pass, below), and **How this works**
  (the explanations, the coverage note and a glossary of the court and
  property words). To list the leads still to work on, pick **Can't be
  reached yet** under the kind of lead.
- **Top 10 line:** how many of the top ten can be reached, and what Lead
  Desk's free lookup found for them ("found numbers for 1 of the 3 it looked
  up"). When that's little and no Google key is set, it says so and offers
  the Google option with its cost.
- **More tools** holds the occasional tools, each with a line saying what it
  does: Add cases (paste case links), Update court cases, Find landlord
  phones now, Import a court file or saved page, Import phones / emails,
  Download the phone-lookup list, and Set up phone lookups and court
  addresses (a chip says how many setup steps are left).
- **First run:** until the business name and base address are saved, a card
  above the list asks for them. Messages use the name; miles and routes start
  from the address, and stay blank until it's set.

Each eviction row shows its case number and the tenant's surname, so two
cases at one complex never look the same. When several open cases share an
address, **N cases at this complex** lists them together: call the landlord
about all of them at once. Hover a priority number for why it ranks there,
in words ("Eviction notice, filed 3 days ago; company landlord"), and a court
stage chip (notice filed, judgment, writ) for what it means for a clean-out.

**Show** picks the view:

- **Default:** evictions with a notice filed, or further along (judgment or
  writ). Cases you imported whose page hasn't been read yet also show,
  marked "case not checked".
- **All evictions.**
- **All leads:** adds the City of Tucson code cases. They cover the City only,
  not unincorporated Pima County, Marana, Oro Valley, Sahuarita or South
  Tucson.

**Order:**

- Every recent writ (lockout) comes first, then every recent judgment, then
  everything else. Recent means 45 days or less; an older one is ranked
  like any other lead.
- Within each group, leads are ordered by priority (see below).

Click a lead, or Tab to it and press Enter, for its details, the owner's
other properties, and to log outreach and results. The tab, filters and open
lead are kept in the address bar, so a reload or Back keeps your place.

### The first phones: Find phones for the top 10

On the first day no eviction lead can be called: the court gives no phone
number and no property address. What Lead Desk fills in by itself, and what
it can't:

- **By itself:** each morning it looks up company landlords on OpenStreetMap
  (and Google Places, if you add a key). The pass and **Details** say how
  many landlords it found a number for out of how many it looked up.
  Expect only some on OpenStreetMap.
- **Never by itself:** a private landlord (a person, not a company), and any
  paid phone service.

**Find phones for the top 10** on the status line lists the landlords of
your best open eviction leads, best first, ten at a time. Each has the web,
Google Maps and Corporation Commission searches and a box for the number.
**Save** puts the number on every open lead of that landlord, so ten numbers
reach at least your top ten leads; about a minute each, 15 minutes for ten.
The pass shows "Top 10 leads: N can be reached now" as you go.

- **Can't find one** skips a landlord so the next one moves up. **Bring back
  the skipped** puts them back.
- **Next 10 landlords** goes on down the list.
- A number found on one lead of a landlord is filled in for you: check it
  and save it on the rest.

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

**Find landlord phones now** (under More tools), which also runs in the
daily check, looks up office numbers for businesses only, starting at the
top of the list, so the best leads are looked up first. Owners who are people are never
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
  - **More tools → Set up phone lookups and court addresses → How to get a
    key** lists the exact clicks.
  - Saving a key looks up again the leads the free lookup found nothing for.
  - Google gives about 1,000 searches a month free, then charges about $35
    per 1,000.
  - Lead Desk stops at 30 a day and 1,000 a month unless you change the
    limits in Settings. A limit of 0 allows none.

Other ways to get numbers:

- **Skip-trace file:**
  1. **Download the phone-lookup list** (More tools) writes the owners still
     missing a phone.
  2. Send it to a skip-tracing service (paid per record).
  3. **Import phones / emails** (More tools) brings back the file the service sends.
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

The records card sits above the address queue (below), and **More tools →
Set up phone lookups and court addresses** links to it:

- **What to ask for:**
  - The first request covers the last month. Later ones cover the dates since
    your last import.
  - Include addresses, judgment dates, writ dates and the disposition.
- **Copy the request**, then open the court's form from the link.
- **I've sent the request** records the date. The next one is due two weeks
  later.
- **Import the court's file** (or **More tools → Import a court file or saved
  page**) takes the file
  the court sends:
  - Rows for cases already in Lead Desk fill in their address. An address
    you typed or confirmed is kept.
  - New judgment and writ cases join the default view, at the top of the
    list while the judgment or writ is 45 days old or less.

The address queue (**Work through the ones that need one**) lists evictions
with no address or only a guess:

- **Confirm** accepts a guess.
- **Landlord's properties** lists the landlord's parcels, with a **Use**
  button on each.

An address on a parcel with more than one home (an apartment complex, a
mobile home park) needs the tenant's unit number before a door hanger goes
there. Without it the whole complex is not counted as an address a door
hanger can go to, nor as "can be reached": the row says **complex — unit
needed**, the lead page asks for the unit, and the lead is worked through the
landlord (ask for the unit when you call). Confirming a guessed complex
records that the tenant lived there; it doesn't replace the unit. A guessed
single home only needs **Confirm address**.

### Priority

Up to 40 points for what the case says:

- Vacant building, dumping and trash/debris score highest; weeds score
  lowest.
- Evictions score 35, with 25 more for a writ of restitution or 15 more for
  a judgment, while that writ or judgment is recent (45 days or less).

The list puts recent writ cases first, then recent judgments, then
everything else by priority. A judgment or writ older than 45 days gets no
stage points and no place at the top: that unit was cleared long ago, so it
ranks like any other lead.

Then:

- **+20** on a code case if the owner's mailing address is elsewhere.
- **+10** for a company or trust owner.
- **+10** if the owner has several leads.
- **+15** if the latest court or city event was 7 days ago or less.
- **+8** if it was 8 to 14 days ago.

A lead becomes Old (stale) 30 days after its latest event. A case with a
court date today or later is not marked Old, unless it already has a
judgment or writ: a hearing after the judgment doesn't keep an old case
fresh.

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

- Business name, phone and base address. A new install starts with these
  blank and asks for the name and address on the Leads tab; miles stay
  blank until the base address is saved. (An install from before this kept
  the name and address it ran with.)
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

English only, for now: every page, script and door-hanger message is in
English, and there is no language switch. Spanish waits until the client asks
for it (see [DECISIONS.md](DECISIONS.md)).

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
