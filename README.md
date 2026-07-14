# jobwatch

Personal job-posting watcher for 2026 internships / new-grad SWE + quant roles.
Polls ~115 sources on a GitHub Actions cron and pings Discord (and/or
Telegram) when something new appears — deduped across sources, grouped by
company, staffing-agency spam filtered, no link-preview spam.

> GitHub's `schedule` trigger is best-effort: on a private repo expect runs
> every 10-60 min, not a guaranteed 10. For true near-real-time, hit the
> `workflow_dispatch` endpoint from an external cron (cron-job.org +
> fine-grained PAT), or run `python watcher.py` in loop mode on any
> always-on box.

## Sources

| Kind | What it covers |
|---|---|
| Greenhouse / Lever / Ashby / SmartRecruiters | ~80 company boards (quant firms, fintech, AI labs, big startups). Ashby boards include 💰 salary ranges. |
| Workday | NVIDIA, Salesforce, Adobe, Capital One, Intel, PayPal, Mastercard, Disney — `*_early` boards use tenant facet ids to see **every** Intern / University / New College Grad req server-side |
| Eightfold | Netflix |
| amazon.jobs | Amazon intern + SDE searches |
| LinkedIn guest search | last-24h postings for SWE intern / new grad / quant queries (US + Canada) — no login needed |
| GitHub listing repos | SimplifyJobs, vanshb03, cvrve (JSON) + markdown-table repos: speedyapply (with salaries), Canadian-Tech-Internships-2026, off-season/Fall-2026 and Summer-2027 lists — covers Jane Street, Google, Meta, Apple, banks and hundreds more, all with direct apply links |

## How it works

- `watcher.py --once` runs one poll cycle; `.github/workflows/jobwatch.yml`
  runs it on a `*/10` cron and commits `state.json` (known job ids, alert
  history, poll timestamps) back to the repo — no server needed.
- New jobs are matched per subscriber (`subscribers.json`): title
  include/exclude regexes and a location regex, falling back to the global
  filters in `config.json`.
- Cross-source dedupe: the same job seen via an ATS, LinkedIn, and a listing
  repo alerts once (canonical-URL + company|title keys, 90-day memory).
- New boards seed silently — you're only alerted for jobs posted after the
  board was added.

## Discord output

- 🆕 one embed per batch, jobs grouped under bold company names, each line
  `🛠️/🎓/💼 [title](link) · location · 💰 salary`.
- 🔥 watchlist matches (per-subscriber company/title regex) arrive first in a
  gold embed, optionally pinging `discord_mention`.
- 📊 daily digest at `digest_hour_utc` — last-24h counts by company, plus any
  failing boards (subscribers with `digest: true`).
- ⚠️ ops alert when a board fails 10 polls in a row (subscribers with
  `ops: true`).

## Commands

```
python watcher.py --check          # validate config + regexes
python watcher.py --verify        # hit every board once, report ok/broken
python watcher.py --list stripe   # dump one board (token debugging)
python watcher.py --test shaun    # send a sample message to a subscriber
python watcher.py --once          # one cycle (Actions mode)
python watcher.py                 # loop forever (VPS mode)
```

## Adding things

- **A company**: find its ATS from the careers-page URL
  (`job-boards.greenhouse.io/TOKEN`, `jobs.lever.co/TOKEN`,
  `jobs.ashbyhq.com/TOKEN`, …), add one line to `config.json`, run
  `--verify`. Workday: grab TENANT/SITE from the devtools POST to
  `/wday/cxs/TENANT/SITE/jobs`; the response's `facets` array has the ids for
  early-career filtering.
- **A friend**: copy the `friend_example` block in `subscribers.json`, give
  them their own Discord webhook, pick boards + filters. `mute: true` to
  pause.

Secrets (repo → Settings → Secrets → Actions): `TELEGRAM_BOT_TOKEN` (only for
Telegram delivery), `HEALTHCHECK_URL` (optional dead-man ping).
