# Junk Removal Lead Gen

Builds a list of properties in Pima County, Arizona, that are likely to need a
clean-out:

- recent evictions from the Justice Court, with the eviction notice, judgment
  and writ (lockout) read from each case page;
- City of Tucson code-enforcement cases for junk, debris, yard waste and
  vacant buildings.

Each lead is ranked and given the owner of record, and, where one can be
found, the landlord's phone number. Steve works the list in **Lead Desk**,
a small web app that runs on his computer or online.

Made for Steve's junk-removal business in Tucson. Phase 1 (this) is the lead
list. Phase 2 will add price estimates.

## Quick start

Python 3.9 or newer.

```sh
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
leadgen serve                    # opens Lead Desk at http://127.0.0.1:8765
```

Press **Check for new evictions**. A minute later the list has this month's
evictions. Each lead with no number has **Find phone** on its row: search
for the landlord, paste the number, and it is saved on every lead of that
landlord.

To have the check run every morning, use `leadgen schedule install`. To run
Lead Desk online, see [docs/VERCEL.md](docs/VERCEL.md).

## Documentation

| Read this | For |
|---|---|
| [docs/OPERATOR.md](docs/OPERATOR.md) | Working the leads: each tab, Find phone, the records request (addresses, judgments, writs), priority, outreach methods, the kill switch |
| [docs/COMMANDS.md](docs/COMMANDS.md) | The `leadgen` commands: daily check, imports, exports, schedules |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it fits together: data-flow diagram, each module's role, workflows, environment variables, tests |
| [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) | Where the data comes from, what's automatic, and the legal limits on using it |
| [docs/VERCEL.md](docs/VERCEL.md) | Putting Lead Desk online (Vercel and Neon, with the daily check on GitHub Actions) |
| [docs/DECISIONS.md](docs/DECISIONS.md) | What the owner has decided: the default view, English only, no new paid sources, no postcards, no case-number scanning, no lead data in the repo |

## Tests

```sh
pytest --cov                                  # SQLite, with the coverage floor
TEST_DATABASE_URL=postgresql://... pytest     # the same tests on Postgres
ruff check . && ruff format --check . && mypy
```

The full list is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#tests).

Lead data is never committed (`data/`, `exports/`, `*.db` and `*.csv` are
git-ignored). This repository is public.
