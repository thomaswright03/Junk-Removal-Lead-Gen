"""Command line entry point: ``python -m leadgen <command>``.

Typical daily run::

    leadgen serve                    # open the lead desk in your browser
    leadgen run                      # fetch automatic sources, owners, geocode, age, export
    leadgen fetch --source pima_jp_calendar --file data/inbox/*.html
    leadgen export --format html --out exports/leads.html
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from . import config, db, export
from .enrich import enrich
from .geocode import CensusGeocoder
from .sources import AUTOMATIC, SOURCES
from .util import PAUSED_MESSAGE, az_today, decode_text, is_paused


def _connect(args):
    return db.connect(args.db)


def _exit_if_paused(conn):
    """The kill switch: a command that would contact another website stops
    here, before any request, while Lead Desk is paused."""
    from . import outreach

    if is_paused(outreach.merged_settings(db.get_settings(conn))):
        sys.exit(PAUSED_MESSAGE)


def _uses_network(name, args):
    """Whether fetching from this source contacts a website (saved files don't)."""
    if name == "csv_import":
        return False
    if name in ("pima_jp_calendar", "pima_jp_case"):
        return not args.file
    return True


def cmd_fetch(args, conn=None):
    conn = conn or _connect(args)
    until = args.until or az_today().isoformat()
    since = args.since or (az_today() - timedelta(days=args.days)).isoformat()
    names = args.source or list(AUTOMATIC)
    if any(_uses_network(n, args) for n in names):
        _exit_if_paused(conn)
    total = {"new": 0, "updated": 0}
    for name in names:
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


def cmd_geocode(args, conn=None):
    conn = conn or _connect(args)
    _exit_if_paused(conn)
    geocoder = CensusGeocoder()
    rows = db.needs_geocode(conn, limit=args.limit)
    ok = outside = failed = 0
    for row in rows:
        try:
            result = geocoder.geocode(row["address"], row["city"], row["zip"])
        except Exception as e:  # network trouble: try again next run
            print(f"  geocode error for lead {row['id']}: {e}", file=sys.stderr)
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


def cmd_enrich(args, conn=None):
    conn = conn or _connect(args)
    _exit_if_paused(conn)
    counts = enrich(conn, limit=getattr(args, "enrich_limit", None), refresh=getattr(args, "refresh", False))
    print(f"owners: {counts['found']} found, {counts['not_found']} not found")


def cmd_contacts(args):
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


def cmd_cases(args):
    from .sources.pima_jp_case import add_cases, update_cases

    conn = _connect(args)
    _exit_if_paused(conn)
    if args.action == "add":
        if not args.links:
            sys.exit("cases add needs one or more case links or IDs")
        counts = add_cases(conn, " ".join(args.links))
    else:
        counts = update_cases(conn, limit=args.limit)
    print(counts)


def cmd_daily(args):
    from .daily import main_log, run_daily

    conn = _connect(args)
    if args.counts_only:  # public logs (GitHub Actions): step names and counts, nothing else
        log = lambda m: print("  " + m) if m.startswith("checking ") else None  # noqa: E731
    else:
        log = lambda m: print("  " + m)  # noqa: E731
    summary = run_daily(conn, stale_days=args.stale_days, log=log)
    main_log(summary, public=args.counts_only)


def cmd_schedule(args):
    from . import schedule

    if args.action == "install":
        print(schedule.install(args.db, hour=args.hour, minute=args.minute))
    elif args.action == "remove":
        print(schedule.remove())
    else:
        print(schedule.status())


def cmd_serve(args):
    from .web import serve

    serve(args.db, host=args.host, port=args.port, stale_days=args.stale_days, open_browser=not args.no_browser)


def cmd_age(args, conn=None):
    conn = conn or _connect(args)
    n = db.mark_stale(conn, args.stale_days)
    conn.commit()
    print(f"marked {n} leads stale (older than {args.stale_days} days)")


def _rows_for_export(args, conn):
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


def cmd_export(args, conn=None):
    conn = conn or _connect(args)
    rows = _rows_for_export(args, conn)
    out = Path(args.out or f"exports/leads-{az_today().isoformat()}.{args.format}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        export.write_csv(rows, out)
    else:
        export.write_html(rows, out)
    print(f"wrote {len(rows)} leads to {out}")
    return out


def cmd_run(args):
    conn = _connect(args)
    cmd_fetch(args, conn)
    cmd_enrich(args, conn)
    if not args.no_geocode:
        cmd_geocode(args, conn)
    cmd_age(args, conn)
    for fmt in ("csv", "html"):
        args.format, args.out = fmt, None
        cmd_export(args, conn)


def cmd_status(args):
    conn = _connect(args)
    if not db.set_status(conn, args.id, args.status, args.notes):
        sys.exit(f"no lead with id {args.id}")
    conn.commit()
    print(f"lead {args.id} -> {args.status}")


def cmd_list(args):
    conn = _connect(args)
    rows = _rows_for_export(args, conn)
    for r in rows[: args.limit]:
        who = r["plaintiff"] or ""
        print(
            f"{r['id']:>5}  {r['event_date'] or '':10}  {r['lead_type']:<14} "
            f"{(r['address'] or '(address needed)')[:40]:<40}  {who[:30]}"
        )
    print(f"{len(rows)} leads")


def cmd_sources(args):
    for name, cls in SOURCES.items():
        auto = " (automatic)" if name in AUTOMATIC else ""
        print(f"{name}{auto}: {cls.description}")


def build_parser():
    p = argparse.ArgumentParser(prog="leadgen", description=__doc__.splitlines()[0])
    p.add_argument(
        "--db",
        default=config.DATABASE_URL or str(config.DB_PATH),
        help=f"SQLite file or postgres:// URL (default: DATABASE_URL if set, else {config.DB_PATH})",
    )
    p.add_argument(
        "--stale-days",
        type=int,
        default=config.STALE_AFTER_DAYS,
        help="leads older than this are stale (default %(default)s)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def fetch_args(sp):
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

    def export_args(sp):
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

    sp = sub.add_parser("sources", help="list available sources")
    sp.set_defaults(func=cmd_sources)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
