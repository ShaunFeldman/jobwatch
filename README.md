# Jobwatch

Your private career command center: broad job-market coverage, focused daily
ranking, fast alerts, and a lightweight application CRM.

Jobwatch continuously checks **195 enabled sources** across company ATSs,
startup boards, quant firms, banks, big tech, LinkedIn, and community listing
repos. It indexes every role it sees, ranks each one against a transparent
profile, and only interrupts you for the matches that meet your notification
rules.

The included profile is tuned for a 2027 candidate with strong backend,
distributed-systems, quant, and ML experience. It prioritizes the U.S., then
Canada, and gives extra weight to startups and trading firms. The product is
not limited to those categories: software, quant, AI/data, hardware, product,
design, finance, sales, operations, people/legal, and uncategorized roles are
all searchable.

## Start using it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Populate the local database. The first run seeds alerts silently.
python watcher.py --once

# Open the daily dashboard at http://127.0.0.1:8787
python watcher.py --serve
```

The dashboard opens automatically and provides:

- a morning briefing with fresh, high-fit roles;
- explainable 0–100 fit scores and score reasons;
- full-text search and filters for role family, stage, location, startup,
  freshness, and minimum fit;
- a private application pipeline for saved, applied, interview, offer,
  rejected, and archived roles;
- notes, due dates, and next actions;
- source health and the last polling-run status;
- one-click CSV export.

Application notes live only in `jobwatch.db`. They are never sent to Discord,
included in `state.json`, or committed by the included workflow. The web server
binds to loopback by default and has no authentication, so do not expose it to
the public internet.

## The daily workflow

1. Open `python watcher.py --serve` and review **Worth your attention**.
2. Use **Discover** to narrow the full market to SWE, quant, startups, or any
   other family. A score is a sorting aid; every reason is visible.
3. Save promising roles to **Pipeline**, record the next action, and move them
   through the process.
4. Let Discord or Telegram handle time-sensitive postings between reviews.
5. Check **Sources** if coverage looks quiet; failures are visible instead of
   silently shrinking the search.

## How ranking works

Collection and ranking are intentionally separate:

- **Collection is broad.** Every returned posting is stored before subscriber
  notification filters are applied.
- **Ranking is personal.** `profile` in `config.json` assigns weights for role
  family, career stage, geography, company type, and skills.
- **Alerts stay scarce.** The existing filters in `subscribers.json` decide
  what reaches quiet feeds and which watchlist matches create a loud ping.

The default profile boosts internships/new-grad roles, U.S. and Canadian
locations, software/quant/AI work, startup and quant companies, and skills such
as Rust, distributed systems, databases, backend, cloud, Python, and C++.
Senior roles and out-of-market locations rank lower but remain searchable.

Edit the `profile` object to retune the dashboard without changing collection
or alert behavior:

```json
{
  "profile": {
    "headline": "SWE, quant & startup roles · U.S. first, Canada too",
    "track_weights": {"software": 18, "quant": 20, "data_ml": 15},
    "location_weights": {"us": 12, "canada": 8, "remote": 9},
    "skills": {"rust": 8, "distributed": 7, "python": 4}
  }
}
```

## Sources

| Kind | Coverage |
|---|---|
| Greenhouse, Lever, Ashby, SmartRecruiters | Direct company boards, including a large startup/AI/fintech set. Ashby includes compensation where published. |
| Workday, Phenom, Eightfold | Banks, NVIDIA, Salesforce, Adobe, Capital One, Intel, PayPal, Mastercard, Disney, Netflix, and others. |
| Bespoke adapters | Amazon, Microsoft, and Jane Street. |
| LinkedIn guest search | Fresh U.S. and Canadian searches without a login, with repost filtering. |
| GitHub listing repos | Simplify, New Grad, internship, off-season, Canada, and salary-aware lists covering bespoke ATSs and long-tail companies. |

Cross-source deduplication prefers direct apply links and remembers alerts for
90 days. Staffing-agency patterns are excluded before alert fanout.

## Notifications

Jobwatch uses a two-speed model:

- **Apply now:** a ranked, capped, loud alert for matching categories at
  watchlist companies.
- **Complete feeds:** quiet digests containing every subscriber match,
  including anything omitted from the loud cap.

Recommended Discord channels are `#apply-now-intern`,
`#apply-now-full-time`, `#internships`, and `#full-time`. Unset optional hooks
fall back to `DISCORD_WEBHOOK_FULLTIME`, so a single channel also works.

Configure secrets in GitHub Actions:

- `DISCORD_WEBHOOK_FULLTIME` (required by the included workflow)
- `DISCORD_WEBHOOK_APPLY_INTERN`
- `DISCORD_WEBHOOK_APPLY_FULLTIME`
- `DISCORD_WEBHOOK_INTERN`
- `TELEGRAM_BOT_TOKEN` (optional)
- `HEALTHCHECK_URL` (optional)

Webhook values in `subscribers.json` use `env:SECRET_NAME`; URLs do not belong
in the repository.

## Commands

```bash
python watcher.py --serve                 # daily dashboard
python watcher.py --serve --port 9000     # choose a local port
python watcher.py --once                  # one polling cycle + state export
python watcher.py                         # continuous local/VPS polling
python watcher.py --check                 # validate configuration and regexes
python watcher.py --verify                # test every enabled source
python watcher.py --list stripe           # inspect one source
python watcher.py --test shaun             # send notification examples
python -m unittest -v                     # run the full test suite
```

## Add coverage

For a direct board, copy its token from its careers URL and add it to
`config.json`:

- `job-boards.greenhouse.io/TOKEN`
- `jobs.lever.co/TOKEN`
- `jobs.ashbyhq.com/TOKEN`
- `jobs.smartrecruiters.com/TOKEN`

For Workday, inspect the careers-page request to
`/wday/cxs/TENANT/SITE/jobs`. Run `python watcher.py --verify` after any source
change. To add a person, copy the example entry in `subscribers.json`, choose
their source set and filters, and configure their own delivery secret.

## Automation and storage

`.github/workflows/jobwatch.yml` runs tests and configuration validation,
restores the portable state, polls repeatedly for roughly 25 minutes, and
chains a successor run. `state.json` on the force-pushed `state` branch keeps
deduplication and alert queues durable across ephemeral runners.

Local SQLite stores richer searchable job metadata, run history, and the
application pipeline. Portable state deliberately excludes private pipeline
notes. The project has one runtime dependency (`requests`); the dashboard uses
Python's standard-library HTTP server and plain HTML/CSS/JavaScript.
