# Running Lead Desk online (Vercel)

Lead Desk online is the same app as `leadgen serve`, reachable from any
browser. Three pieces work together:

| Piece | What it does | Cost |
|---|---|---|
| **Vercel** | Shows Lead Desk at a web address, behind a password | Hobby plan is free but for non-commercial use only; Pro is $20 per person per month |
| **Turso** | Stores the leads (Vercel keeps no files between requests) | Free plan is enough for this |
| **GitHub Actions** | Runs the daily eviction check every morning at 6:00 Tucson time and saves the results in Turso | Free for this public repository |

The daily check runs on GitHub, not Vercel, because it takes 10 to 20 minutes
and Vercel stops a request after 5. Its log on GitHub shows step names and
counts only, never names or addresses, because this repository is public.

## 1. Create the Turso database

1. Sign up at https://turso.tech.
2. Create a database. Pick the AWS US East (Virginia) location, close to
   Vercel's default region.
3. On the database page, copy its URL. It starts with `libsql://`.
4. Create a token for the database with read and write access and no
   expiration. Copy it; it is shown once.

## 2. Set up Vercel

In the Vercel project (Settings, Environment Variables), add:

| Name | Value |
|---|---|
| `TURSO_DATABASE_URL` | the `libsql://...` URL |
| `TURSO_AUTH_TOKEN` | the token |
| `LEADDESK_PASSWORD` | the password Steve will type to open Lead Desk |
| `GOOGLE_PLACES_API_KEY` | optional; the key can also be pasted in Lead Desk Settings |
| `LEADDESK_GITHUB_TOKEN` | optional; lets the "Check for new evictions" button start the check (step 4) |

Under Settings, Git, make sure the production branch is `main`, then redeploy.
The browser asks for the password once (any user name works). Until all three
required variables are set, the site shows a setup page and no data.

## 3. Turn on the daily check

In GitHub, open the repository's Settings, Secrets and variables, Actions, and
add these repository secrets:

- `TURSO_DATABASE_URL`
- `TURSO_AUTH_TOKEN`
- `GOOGLE_PLACES_API_KEY` (optional, same as above)

The check is `.github/workflows/daily.yml`. It runs every day at 13:00 UTC
(6:00 in Tucson) from the `main` branch. To run it right away, open the
Actions tab, pick "Daily eviction check" and press "Run workflow". The first
run fills the database with the next 30 days of eviction hearings.

GitHub pauses scheduled workflows in a public repository after 60 days
without any commits; the Actions tab shows a button to turn it back on.

## 4. Optional: start the check from Lead Desk

Online, "Check for new evictions" asks GitHub to start the same workflow. That
needs a GitHub fine-grained personal access token
(https://github.com/settings/personal-access-tokens/new) with access to this
repository only and the "Actions" permission set to "Read and write". Save it
in Vercel as `LEADDESK_GITHUB_TOKEN`. Without it, the button explains that the
check runs every morning.

## Running locally still works

`leadgen serve` with no Turso variables uses the SQLite file in `data/` as
before. To work on the online data from your computer, set
`TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in the terminal first; every
`leadgen` command then uses Turso.
