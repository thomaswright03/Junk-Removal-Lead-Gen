"""Command line entry point: ``python -m leadgen <command>``.

Typical daily run::

    leadgen serve                    # open the lead desk in your browser
    leadgen run                      # fetch automatic sources, owners, geocode, age, export
    leadgen fetch --source pima_jp_calendar --file data/inbox/*.html
    leadgen export --format html --out exports/leads.html
"""

import argparse
import logging
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

import requests

from . import config, db, export
from .enrich import enrich
from .geocode import CensusGeocoder
from .sources import AUTOMATIC, SOURCES
from .util import PAUSED_MESSAGE, Conn, az_today, decode_text, env_flag, is_paused

log_ = logging.getLogger(__name__)


def _leads(n: int) -> str:
    return f"{n} lead{'' if n == 1 else 's'}"


def _connect(args: argparse.Namespace) -> Conn:
    return db.connect(args.db)


def _exit_if_paused(conn: Conn) -> None:
    """The kill switch: a command that would contact another website stops
    here, before any request, while Lead Desk is paused."""
    from . import outreach

    if is_paused(outreach.merged_settings(db.get_settings(conn))):
        sys.exit(PAUSED_MESSAGE)


def _uses_network(name: str, args: argparse.Namespace) -> bool:
    """Whether fetching from this source contacts a website (saved files don't)."""
    if name == "csv_import":
        return False
    if name in ("pima_jp_calendar", "pima_jp_case"):
        return not args.file
    return True


_CALENDAR_LIMIT = (
    "The court calendar only lists upcoming hearings, so it can't show "
    "evictions from past dates. For older cases, send the Justice Court "
    "records request (see docs/DATA_SOURCES.md) and import its file, or "
    "paste the case links into Add cases."
)
_PAST_CALENDAR = f"pima_jp_calendar: nothing to fetch for past dates. {_CALENDAR_LIMIT}"
_PART_PAST_CALENDAR = "pima_jp_calendar: hearings before {today} aren't listed. " + _CALENDAR_LIMIT


def cmd_fetch(args: argparse.Namespace, conn: Conn = None) -> dict:
    conn = conn or _connect(args)
    until = args.until or az_today().isoformat()
    since = args.since or (az_today() - timedelta(days=args.days)).isoformat()
    names = args.source or list(AUTOMATIC)
    if any(_uses_network(n, args) for n in names):
        _exit_if_paused(conn)
    total = {"new": 0, "updated": 0}
    today = az_today().isoformat()
    for name in names:
        if name == "pima_jp_calendar" and not args.file:
            # The calendar has no past hearings; say so rather than "0 new".
            if until < today:
                print(_PAST_CALENDAR)
                continue
            if args.since and since < today:
                print(_PART_PAST_CALENDAR.format(today=today))
        source = SOURCES[name]()
        counts = {"new": 0, "updated": 0}
        options = {"all_cases": args.all_cases, "assume_eviction": args.assume_eviction}
        if args.lead_type:
            options["lead_type"] = args.lead_type
        for lead in source.fetch(since, until, paths=args.file, **options):
            counts[db.upsert(conn, lead)] += 1
        conn.commit()
        print(f"{name}: {counts['new']} new, {counts['updated']} updated")
        for k in total:
            total[k] += counts[k]
    return total


def cmd_geocode(args: argparse.Namespace, conn: Conn = None) -> None:
    conn = conn or _connect(args)
    _exit_if_paused(conn)
    geocoder = CensusGeocoder()
    rows = db.needs_geocode(conn, limit=args.limit)
    ok = outside = failed = errors = 0
    last_error: Optional[BaseException] = None
    for row in rows:
        try:
            result = geocoder.geocode(row["address"], row["city"], row["zip"])
        except Exception as e:  # network trouble: try again next run
            errors += 1
            last_error = e
            log_.debug("map lookup failed for lead %s", row["id"], exc_info=True)
            continue
        db.save_geocode(conn, row["id"], result)
        if result is None:
            failed += 1
        elif result.in_pima:
            ok += 1
        else:
            outside += 1
        conn.commit()
    print(f"geocoded {ok} in Pima County, {outside} outside, {failed} not found")
    if errors:
        print(f"the map service couldn't be reached for {_leads(errors)}; tried again on the next check")
    _exit_if_all_failed(errors, ok + outside + failed, last_error, "the US Census map service", "map lookups")


def cmd_enrich(args: argparse.Namespace, conn: Conn = None) -> None:
    conn = conn or _connect(args)
    _exit_if_paused(conn)
    counts = enrich(conn, limit=getattr(args, "enrich_limit", None), refresh=getattr(args, "refresh", False))
    print(f"owners: {counts['found']} found, {counts['not_found']} not found")
    _exit_if_all_failed(
        counts.get("errors", 0),
        counts["found"] + counts["not_found"],
        None,
        "the county parcel (owner) records on the City of Tucson map server",
        "owner lookups",
    )


def cmd_contacts(args: argparse.Namespace) -> None:
    from . import contacts, outreach
    from .lookup import find_contacts, providers_from
    from .web import App

    conn = _connect(args)
    if args.action == "find":
        _exit_if_paused(conn)
        settings = outreach.merged_settings(db.get_settings(conn))
        providers = providers_from(settings, google_key=args.google_key, conn=conn)
        names = ", ".join(p.name for p in providers)
        print(f"looking up business contacts with: {names} (+ company websites)")
        counts = find_contacts(conn, providers, limit=args.limit, refresh=args.refresh)
        print(
            f"companies checked {counts['checked']}; leads with a contact found "
            f"{counts['found']}, not found {counts['not_found']}; individuals skipped "
            f"{counts['skipped_people']}; errors {counts['errors']}"
        )
        if counts.get("error_cause") == "google_key":
            sys.exit("Google refused the Google Places key. Check it in Lead Desk's Settings (or --google-key).")
        _exit_if_all_failed(
            counts["errors"], counts["found"] + counts["not_found"], None, names_of(providers), "phone lookups"
        )
    elif args.action == "import":
        if not args.file:
            sys.exit("contacts import needs --file")
        text = decode_text(Path(args.file).read_bytes())
        print(contacts.import_contacts(conn, text))
    else:
        out = Path(args.file or f"exports/skiptrace-{az_today().isoformat()}.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        leads = [l for l in App(args.db).leads(conn) if l["status"] not in ("stale", "skip", "lost", "won")]
        out.write_text(contacts.skiptrace_csv(leads), encoding="utf-8")
        print(f"wrote owners missing a phone to {out}")


def cmd_cases(args: argparse.Namespace) -> None:
    from .sources.pima_jp_case import add_cases, update_cases

    conn = _connect(args)
    _exit_if_paused(conn)
    if args.action == "add":
        if not args.links:
            sys.exit("cases add needs one or more case links or IDs")
        counts = add_cases(conn, " ".join(args.links))
        read = counts["new"] + counts["updated"]
        print(
            f"cases: {counts['new']} new, {counts['updated']} updated, {counts['with_notice']} with an eviction "
            f"notice, {counts['failed']} couldn't be read"
            + (f"; skipped (not case links): {', '.join(counts['skipped'])}" if counts["skipped"] else "")
        )
    else:
        counts = update_cases(conn, limit=args.limit)
        read = counts.get("checked", 0)
        print(
            f"cases: {read} re-read, {counts.get('with_notice', 0)} with an eviction notice, "
            f"{counts.get('failed', 0)} couldn't be read"
        )
    _exit_if_all_failed(counts.get("failed", 0), read, None, "the Pima County Justice Court website", "case pages")


def cmd_daily(args: argparse.Namespace) -> None:
    from .daily import due, failed_steps, main_log, run_daily

    conn = _connect(args)
    if args.if_due and not due(conn, hour=0):
        # The scheduled runs after the first: only a same-day retry (or a day
        # whose check hasn't run yet) does anything.
        print("nothing to do: today's check has run (or its retry isn't due yet)")
        return
    if args.counts_only:  # public logs (GitHub Actions): step names and counts, nothing else
        log = lambda m: print("  " + m) if m.startswith("checking ") else None  # noqa: E731
    else:
        log = lambda m: print("  " + m)  # noqa: E731
    summary = run_daily(conn, stale_days=args.stale_days, log=log)
    main_log(summary, public=args.counts_only, debug=args.debug or env_flag("LEADGEN_DEBUG"))
    if failed_steps(summary):
        # A whole source failed: schedulers and CI see a failed run (the
        # same-day retry is still set, and the line above says when).
        sys.exit(1)


def cmd_schedule(args: argparse.Namespace) -> None:
    from . import schedule

    if args.action == "install":
        print(schedule.install(args.db, hour=args.hour, minute=args.minute))
    elif args.action == "remove":
        print(schedule.remove())
    else:
        print(schedule.status())


def cmd_serve(args: argparse.Namespace) -> None:
    from .web import serve

    serve(args.db, host=args.host, port=args.port, stale_days=args.stale_days, open_browser=not args.no_browser)


def cmd_age(args: argparse.Namespace, conn: Conn = None) -> None:
    conn = conn or _connect(args)
    n = db.mark_stale(conn, args.stale_days)
    conn.commit()
    print(f"marked {n} leads stale (older than {args.stale_days} days)")


def _rows_for_export(args: argparse.Namespace, conn: Conn) -> list:
    since = None
    if not args.include_stale:
        since = (az_today() - timedelta(days=args.stale_days)).isoformat()
    statuses = args.status or (
        [s for s in db.STATUSES if s not in ("stale", "skip", "lost")] if not args.include_stale else None
    )
    return db.query(
        conn,
        since=since,
        statuses=statuses,
        lead_types=args.type,
        include_duplicates=args.include_duplicates,
        only_pima=True,
    )


def _ranked_rows(args: argparse.Namespace, conn: Conn) -> list[dict]:
    """The rows to export or list, in Lead Desk's order with its priority."""
    return export.ranked(conn, _rows_for_export(args, conn), db.get_settings(conn))


def cmd_export(args: argparse.Namespace, conn: Conn = None) -> Path:
    conn = conn or _connect(args)
    rows = _ranked_rows(args, conn)
    out = Path(args.out or f"exports/leads-{az_today().isoformat()}.{args.format}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        export.write_csv(rows, out)
    else:
        export.write_html(rows, out)
    print(f"wrote {len(rows)} leads to {out}")
    return out


def cmd_run(args: argparse.Namespace) -> None:
    conn = _connect(args)
    cmd_fetch(args, conn)
    cmd_enrich(args, conn)
    if not args.no_geocode:
        cmd_geocode(args, conn)
    cmd_age(args, conn)
    for fmt in ("csv", "html"):
        args.format, args.out = fmt, None
        cmd_export(args, conn)


def cmd_status(args: argparse.Namespace) -> None:
    conn = _connect(args)
    if not db.set_status(conn, args.id, args.status, args.notes):
        sys.exit(f"no lead with id {args.id}")
    conn.commit()
    print(f"lead {args.id} -> {args.status}")


def cmd_list(args: argparse.Namespace) -> None:
    conn = _connect(args)
    rows = _ranked_rows(args, conn)
    for r in rows[: args.limit]:
        who = r["plaintiff"] or r["owner_name"] or ""
        stage = r["case_stage"] or ("notice" if r["eviction_notice"] else "")
        print(
            f"{r['priority']:>3}  {r['id']:>5}  {r['latest_event_date'] or r['event_date'] or '':10}  "
            f"{r['lead_type']:<14} {stage:<9} {(r['address'] or '(address needed)')[:40]:<40}  {who[:30]}"
        )
    print(f"{len(rows)} leads")


def cmd_check_court(args: argparse.Namespace) -> None:
    """Smoke test of the eviction source: the court calendar still answers
    and still parses. Exits with an error when it finds no eviction hearing
    in the next ``--days`` days (there are always some), so a change in the
    court's page shows up as a failed job instead of a quiet day with no
    new evictions. Prints counts only. ``--file`` checks saved pages."""
    from .sources.pima_jp_calendar import CalendarClient, PimaJpCalendar, parse_calendar_html

    today = az_today()
    if args.file:  # saved pages: whatever dates they hold
        leads = [
            lead
            for path in args.file
            for lead in parse_calendar_html(
                Path(path).read_text(encoding="utf-8", errors="replace"), assume_eviction=True
            )
        ]
        pages = len(args.file)
    else:
        _exit_if_paused(_connect(args))
        client = CalendarClient()
        until = (today + timedelta(days=args.days)).isoformat()
        leads = list(PimaJpCalendar().fetch(today.isoformat(), until, client=client))
        pages = client.pages
    with_case = sum(1 for l in leads if l.source_id)
    print(f"court calendar: {len(leads)} eviction hearings in the next {args.days} days on {pages} page(s)")
    if not leads or not with_case:
        sys.exit(
            "The Justice Court calendar returned no eviction hearings. The court's page has probably "
            "changed (or the search failed): the daily check would find no new evictions."
        )


def cmd_sources(args: argparse.Namespace) -> None:
    for name, cls in SOURCES.items():
        auto = " (automatic)" if name in AUTOMATIC else ""
        print(f"{name}{auto}: {cls.description}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leadgen", description=__doc__.splitlines()[0])
    p.add_argument(
        "--db",
        default=config.DATABASE_URL or str(config.DB_PATH),
        help=f"SQLite file or postgres:// URL (default: DATABASE_URL if set, else {config.DB_PATH})",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="show the full error (traceback) when a website or the database can't be reached",
    )
    p.add_argument(
        "--stale-days",
        type=int,
        default=config.STALE_AFTER_DAYS,
        help="leads older than this are stale (default %(default)s)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def fetch_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--source",
            action="append",
            choices=sorted(SOURCES),
            help="source to run; repeatable (default: all automatic sources)",
        )
        sp.add_argument("--file", nargs="+", help="input files for file-based sources")
        sp.add_argument("--since", help="start date YYYY-MM-DD")
        sp.add_argument("--until", help="end date YYYY-MM-DD (default today)")
        sp.add_argument("--days", type=int, default=30, help="look-back if --since is not given")
        sp.add_argument(
            "--all-cases", action="store_true", help="tucson_code_cases: keep every case, not only junk/debris/vacant"
        )
        sp.add_argument(
            "--assume-eviction",
            action="store_true",
            help="pima_jp_calendar: page is already filtered to eviction hearings",
        )
        sp.add_argument("--lead-type", help="csv_import: lead type for rows without one")

    def export_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--type", action="append", help="only this lead type; repeatable")
        sp.add_argument("--status", action="append", choices=db.STATUSES)
        sp.add_argument("--include-stale", action="store_true")
        sp.add_argument("--include-duplicates", action="store_true")

    sp = sub.add_parser("fetch", help="pull leads from sources into the database")
    fetch_args(sp)
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("geocode", help="add coordinates and check the county")
    sp.add_argument("--limit", type=int)
    sp.set_defaults(func=cmd_geocode)

    sp = sub.add_parser("enrich", help="look up each lead's owner from the county assessor")
    sp.add_argument("--limit", dest="enrich_limit", type=int)
    sp.add_argument("--refresh", action="store_true", help="look up leads already done too")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser(
        "contacts", help="find business phone/email for landlords and owners; import or export contacts"
    )
    sp.add_argument("action", choices=("find", "import", "export"))
    sp.add_argument("--file", help="import: CSV with phone/email; export: output path")
    sp.add_argument("--limit", type=int, help="find: max companies to look up")
    sp.add_argument("--refresh", action="store_true", help="find: re-check leads already done")
    sp.add_argument("--google-key", help="find: Google Places API key (or set GOOGLE_PLACES_API_KEY)")
    sp.set_defaults(func=cmd_contacts)

    sp = sub.add_parser("cases", help="read Justice Court case pages: add cases by link, or update eviction cases")
    sp.add_argument("action", choices=("add", "update"))
    sp.add_argument("links", nargs="*", help="add: case page links (jcDisplayCase.aspx?ID=...) or IDs")
    sp.add_argument("--limit", type=int, help="update: max cases to re-read")
    sp.set_defaults(func=cmd_cases)

    sp = sub.add_parser(
        "daily", help="the daily run: new evictions, eviction notices, owners, landlord phones, code cases"
    )
    sp.add_argument(
        "--counts-only",
        action="store_true",
        help="print step names and counts only, no names or error details (public logs)",
    )
    sp.add_argument(
        "--if-due",
        action="store_true",
        help="run only if today's check hasn't run yet, or failed and its retry is due (for schedules)",
    )
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("schedule", help="run `leadgen daily` automatically every morning")
    sp.add_argument("action", choices=("install", "remove", "status"))
    sp.add_argument("--hour", type=int, default=6, help="hour of day, 0-23 (default 6)")
    sp.add_argument("--minute", type=int, default=0)
    sp.set_defaults(func=cmd_schedule)

    sp = sub.add_parser("serve", help="open the lead desk web app")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("age", help="mark old leads stale")
    sp.set_defaults(func=cmd_age)

    sp = sub.add_parser("export", help="write leads to CSV or HTML")
    sp.add_argument("--format", choices=("csv", "html"), default="csv")
    sp.add_argument("--out")
    export_args(sp)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("run", help="fetch + geocode + age + export CSV and HTML")
    fetch_args(sp)
    export_args(sp)
    sp.add_argument("--no-geocode", action="store_true")
    sp.add_argument("--limit", type=int, help="max addresses to geocode")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("list", help="print current leads")
    sp.add_argument("--limit", type=int, default=50)
    export_args(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("status", help="set a lead's status (contacted, won, ...)")
    sp.add_argument("id", type=int)
    sp.add_argument("status", choices=db.STATUSES)
    sp.add_argument("--notes")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("check-court", help="fail if the court calendar shows no eviction hearings (the page changed?)")
    sp.add_argument("--days", type=int, default=30, help="hearings this many days ahead (default 30)")
    sp.add_argument("--file", nargs="+", help="check saved calendar pages instead of the live site")
    sp.set_defaults(func=cmd_check_court)

    sp = sub.add_parser("sources", help="list available sources")
    sp.set_defaults(func=cmd_sources)
    return p


def names_of(providers: list) -> str:
    from .providers import PROVIDER_LABELS

    return " and ".join(PROVIDER_LABELS.get(p.name, p.name) for p in providers) or "the lookup services"


def _exit_if_all_failed(failed: int, done: int, error: Optional[BaseException], site: str, what: str) -> None:
    """A command that carries on past single failures (one case page, one
    lookup) still fails, with a plain sentence, when every one failed: the
    site is down or this computer is offline."""
    if failed and not done:
        if error is not None and isinstance(error, requests.exceptions.RequestException):
            site = site_name(error)
        sys.exit(
            f"Lead Desk stopped: no {what} got through ({failed} failed), so {site} probably couldn't be "
            "reached. Check this computer's internet connection, or try again later if the site is down."
        )


# The websites the commands talk to, by a piece of their address, in words.
SITES = (
    ("jp.pima.gov", "the Pima County Justice Court website"),
    ("PermitsCode", "the City of Tucson code case service"),
    ("PropertyHousing", "the county parcel (owner) records on the City of Tucson map server"),
    ("tucsonaz.gov", "the City of Tucson map server"),
    ("census.gov", "the US Census map service"),
    ("googleapis.com", "Google Places"),
    ("overpass", "OpenStreetMap"),
    ("api.github.com", "GitHub"),
)


def site_name(error: BaseException) -> str:
    """The website a network error was about, in words ("the Pima County
    Justice Court website"), from the request's address or the message."""
    request = getattr(error, "request", None)
    url = str(getattr(request, "url", "") or "") or str(error)
    for part, name in SITES:
        if part.lower() in url.lower():
            return name
    host = re.search(r"https?://([^/:\s'\"]+)", url) or re.search(r"host='([^']+)'", url)
    return host.group(1) if host else "a website it needs"


def network_message(error: BaseException) -> str:
    """One or two plain sentences for a network failure."""
    if isinstance(error, requests.exceptions.Timeout):
        what = "didn't answer in time"
    elif isinstance(error, requests.exceptions.HTTPError) and getattr(error, "response", None) is not None:
        what = f"answered with an error ({error.response.status_code})"
    else:
        what = "couldn't be reached"
    return (
        f"Lead Desk stopped: {site_name(error)} {what}. Check this computer's internet connection, "
        "or try again later if the site is down. (Add --debug for the full error.)"
    )


# Commands that bring leads in or run Lead Desk, and may start a new database.
# The others work on leads already there: a mistyped --db path must say so,
# not show an empty new database as "0 leads".
CREATES_DATABASE = {"serve", "daily", "run", "fetch", "cases", "check-court", "schedule", "sources"}


def missing_database(args: argparse.Namespace) -> Optional[str]:
    """The message for a command that needs an existing SQLite file that
    isn't there, else None."""
    if args.command in CREATES_DATABASE or db.pg.is_url(args.db) or str(args.db) == ":memory:":
        return None
    if Path(args.db).is_file():
        return None
    return (
        f"No lead database at {args.db}. Check the path (--db, or DATABASE_URL), "
        "or start one with `leadgen daily` or `leadgen serve`."
    )


def main(argv: Optional[list] = None) -> None:
    args = build_parser().parse_args(argv)
    missing = missing_database(args)
    if missing:
        sys.exit(missing)
    debug = args.debug or env_flag("LEADGEN_DEBUG")
    if debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    try:
        args.func(args)
    except requests.exceptions.RequestException as e:
        if debug:
            raise
        sys.exit(network_message(e))
    except Exception as e:
        # The database (Neon) unreachable: say so in a sentence too.
        if debug or type(e).__module__.split(".")[0] != "psycopg" or type(e).__name__ != "OperationalError":
            raise
        sys.exit(
            "Lead Desk stopped: it couldn't connect to the database (DATABASE_URL). Check the connection "
            "and the address, or try again later. (Add --debug for the full error.)"
        )


if __name__ == "__main__":
    main()
