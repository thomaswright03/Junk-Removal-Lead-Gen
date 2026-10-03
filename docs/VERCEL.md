# Running Lead Desk online (Vercel)

Lead Desk online is the same app as `leadgen serve`, reachable from any
browser. Three pieces work together:

| Piece | What it does | Cost |
|---|---|---|
| **Vercel** | Shows Lead Desk at a web address, behind a password | Hobby plan is free but for non-commercial use only; Pro is $20 per person per month |
| **Neon** (through Vercel) | Stores the leads in Postgres (Vercel keeps no files between requests) | Free plan is enough for this |
| **GitHub Actions** | Runs the daily eviction check every morning at 6:00 Tucson time and saves the results in Neon | Free for this public repository |

The daily check runs on GitHub, not Vercel, because it takes 10 to 20 minutes
and Vercel stops a request after 5. Its log on GitHub shows step names and
counts only, never names or addresses, because this repository is public.
For the same reason, online the "Update court cases" and "Find landlord
phones" buttons do one batch per press (about 40 seconds of work) instead of
running in the background.

## 1. Add the Neon database in Vercel

1. In the Vercel project, open **Storage**, choose **Create Database**, then
   **Neon** (Serverless Postgres). Pick the Washington, D.C. (US East) region,
   close to where Vercel runs Lead Desk, and the free plan.
2. Connect it to this project (Production and Preview). On the connect
   screen:
   - set **Custom Prefix** to `DATABASE`, so the variable is `DATABASE_URL`
     (the name Lead Desk reads);
   - uncheck **Create database branch for deployment** for both Production
     and Preview. Otherwise each deployment gets its own copy of the
     database, and the daily check would save leads to a different copy
     than the one the site shows.
   Use a database no other app uses. Lead Desk creates its tables on first use.

## 2. Set the password

In the Vercel project, open **Settings**, **Environment Variables**, and add:

| Name | Value |
|---|---|
| `LEADDESK_PASSWORD` | the password Steve will type to open Lead Desk |
| `GOOGLE_PLACES_API_KEY` | optional; the key can also be pasted in Lead Desk Settings |
| `LEADDESK_GITHUB_TOKEN` | optional; lets the "Check for new evictions" button start the check (step 4) |
| `LEADDESK_GITHUB_REF` | optional; the branch that check runs from (default `main`) |
| `LEADDESK_PAUSED` | optional; `1` stops all checking and lookups (see "Stopping everything") |

Under **Settings**, **Git**, make sure the production branch is `main`, then
redeploy. The browser asks for the password once (any user name works). Until
the database and password are set, the site shows a setup page and no data.

## 3. Turn on the daily check

1. In Vercel, open **Settings**, **Environment Variables**, find
   `DATABASE_URL` and copy its value (the eye icon shows it).
2. In GitHub, open the repository's **Settings**, **Secrets and variables**,
   **Actions**, and add a repository secret named `DATABASE_URL` with that
   value. Add `GOOGLE_PLACES_API_KEY` too if you use one.

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

## Stopping everything

If the court asks for the checks to stop, or the Google bill rises, either:

- open Lead Desk, **Settings**, and tick **Pause Lead Desk** (anyone with the
  password can do this; it takes effect on the next request and on the next
  daily run), or
- set `LEADDESK_PAUSED` to `1`: in Vercel under **Settings**, **Environment
  Variables** (then redeploy), and in GitHub under **Settings**, **Secrets
  and variables**, **Actions**, **Variables** as a repository variable, which
  the daily workflow reads. This holds even if someone unticks the setting.

While paused, the daily check exits without contacting the court, the
county or any lookup service, and the buttons say Lead Desk is paused. To
stop only Google lookups, untick **Use Google lookups** in Settings (this
also covers a key set as `GOOGLE_PLACES_API_KEY`) or set the Google limits
to 0; deleting the `GOOGLE_PLACES_API_KEY` secret in GitHub and Vercel
removes the key for good.

## Running locally still works

`leadgen serve` without `DATABASE_URL` uses the SQLite file in `data/` as
before. To work on the online data from your computer, set `DATABASE_URL` in
the terminal first; every `leadgen` command then uses Neon.
