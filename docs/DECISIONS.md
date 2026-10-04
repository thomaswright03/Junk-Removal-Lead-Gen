# Decisions

Choices the owner has made about what Lead Desk does and doesn't do. A review
or a new contributor may suggest otherwise. Read this first, and change it
only with the owner's say-so.

## Decided by the owner, 2026-10-03

| Decision | What it means in the code |
|---|---|
| **The default view stays "evictions with a filed notice".** | Leads → Show defaults to evictions with an eviction notice (or a judgment or writ). City code cases appear under "All leads" only. |
| **Clean-out leads cover the City of Tucson only.** | Code-enforcement leads come from the City of Tucson's code cases alone. Unincorporated Pima County, Marana, Oro Valley, Sahuarita and South Tucson, foreclosures and probate or estate clean-outs are not collected. Adding a source for any of them is a new data source and needs the owner's sign-off (below). The README and the Leads tab's "What Lead Desk covers" say so. |
| **Spanish is deferred until the client asks for it.** | The page, scripts and door-hanger text are English only; there is no translation layer yet. When the client asks, the page's text and the message templates get a Spanish version and a language switch that is remembered. |
| **No new data sources or paid services without the owner's sign-off.** | The free OpenStreetMap lookup and the capped, opt-in Google Places lookup may be improved. Adding another provider, a paid API or a scraper needs sign-off first. |
| **Postcards are dropped for good.** | There is no postcard outreach method. `outreach.RETIRED_CHANNELS` / `retire_channels` stay on purpose: they move any old "postcard" lead in a database from before the change back to unassigned. |
| **No scanning of Justice Court case ID ranges.** | Cases come from the court calendar, a records-request file, or a case link pasted into **Add cases**. Nothing walks case numbers. |
| **Never commit lead data.** | `data/`, `exports/`, `*.db` and `*.csv` stay git-ignored. The repository is public: workflows print counts only, and test fixtures use made-up names. |

## Waiting on the owner

- **Is the outreach experiment wanted scope?** The Outreach and Results tabs
  (three outreach methods dealt in random blocks, compared on cost per job)
  weren't in the original brief. Until the owner confirms, they stay, but
  locked until a lead can be contacted, so day one shows the lead list
  only. While they stay, **Assign leads** never deals nothing without
  saying why: it names the leads it leaves out and the reason, and offers
  to deal leads that only some ticked methods can work (a phone but no
  confirmed address) to a method they can use, outside the balanced split
  (Results keeps those apart). If the owner says no, the tabs come out and
  the lead list stays as it is.
- **More clean-out coverage.** A county-wide or town code-case source,
  foreclosures or probate would each be a new data source: the owner
  decides whether one is wanted (and whether any cost is acceptable).
  Until then the City-only coverage above stands.
- **Require CI before merging to `main`.** This needs a repository admin, in
  GitHub's settings. Code can't do it. Steps:
  1. Open the repository's **Settings → Rules → Rulesets → New ruleset →
     New branch ruleset**.
  2. Name it `main`, set **Enforcement status** to Active, and under
     **Target branches** choose **Add target → Include default branch**.
  3. Tick **Require a pull request before merging**.
  4. Tick **Require status checks to pass**, then **Add checks**, and pick
     **test** (the job in `.github/workflows/ci.yml`; it appears once CI has
     run on a pull request).
  5. Tick **Block force pushes**, then **Create**.

  To check it: open a pull request with a failing test. The merge button
  stays disabled until CI passes.
