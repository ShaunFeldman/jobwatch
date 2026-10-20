# Jobwatch

A free job watcher for four Discord channels. It collects company postings,
checks your role/location preferences, and sends important matches quickly.

| Channel | Contents |
|---|---|
| Important internships | Watchlist matches in the internship category; notifications enabled |
| Important full-time | Watchlist matches in configured full-time categories; notifications enabled |
| All internships | Every newly discovered matching internship; silent messages |
| All full-time | Every newly discovered matching full-time job; silent messages |

Shaun's alerts use a personal interest policy in `subscribers.json` under
`notification_fit`. Software/backend, ML/data, and quant roles lead; investing
and selected technical/business roles qualify when company and location make
them compelling. NYC/SF receive the strongest location preference, followed by
other U.S. locations and Canada. A bare "remote" does not make a foreign-only
role eligible. Unknown locations are explicitly labeled in the match reasons.

Priority tech/trading/investment firms get a company bonus. Routine finance at
Canadian banks stays in the quiet feeds; relevant U.S. technical internships
at those banks can still qualify. Generic technical titles no longer require
"intern" or "new grad" to reach important full-time alerts. Clearly senior,
unrelated, or PhD-specific titles are excluded. Each important card explains
why it matched. This is interest matching from titles and locations, not a
claim that work authorization, graduation windows, or experience requirements
have been verified.

The policy overrides the older title/location regex gates for Shaun; subscribers
without it retain their existing filters/watchlists/categories. Employer agency
exclusions and freshness rules still apply. Changing policy rechecks known
**official postings within 24 hours** once per source; old and undated records
are not replayed. First-source baselines remain silent. The resume itself and
contact details are not stored in this repository.

## Freshness and completeness

- Official postings published within 24 hours can ping. Edit dates do not count.
- Undated jobs absent from a recent complete company-board snapshot say
  **New on company board · original posting date unavailable**.
- Other newly discovered undated jobs and recent discovery-feed entries can
  ping with **Age unverified — may be older**. LinkedIn repost dates never
  certify original publication.
- Known postings older than 24 hours appear only in full feeds, labeled
  **Older posting — newly found** with their age at discovery.
- First baselines, including incomplete first reads, are silent. Collection
  configuration changes establish another silent baseline. Changing cadence
  alone does not reset coverage. Existing seen IDs survive upgrades.
- Every returned posting is indexed before notification filters. Full feeds
  mean all **matching newly discovered** jobs, not every occupation worldwide.
- Distinct requisitions are kept even when their company and title match.
  Canonical URL deduplication is per subscriber and destination channel. A
  full-feed entry never suppresses an important-channel entry.

See [the source audit](docs/source-audit.md) for all 206 configured sources and
known limits. Disabled sources have no direct monitoring; discovery feeds may
surface their jobs, but cannot guarantee their coverage.

## Speed and delivery

Direct boards have eight worker slots; discovery feeds have two separate
slots. A shared host is polled by only one worker at a time. Results are
processed as they finish, without waiting for slower boards. In continuous
mode, each source is rescheduled independently. Default direct cadence is
three minutes after a fetch completes; explicit per-source intervals and
provider backoff still apply. Netflix is set to three minutes. Discovery feeds
default to 30 minutes unless configured otherwise.

Important jobs are individually queued with no eight-job cutoff. Each drain
sends up to eight messages, paces requests to each webhook, and uses a ten-second
budget with persistent provider
cooldowns; remaining jobs stay queued. Full feeds batch for about one minute.
An important job expires at its own deadline; full-feed discoveries stay queued
until delivered. Discord network requests can temporarily block the processing
thread for their timeout, but slow collection does not block ready delivery.

The operating target is a roughly three-minute check cadence for healthy
priority boards and important delivery within a minute of processing. These
are targets, not guarantees: GitHub queue delays, large sources, provider limits,
and outages can extend them. Successful sends log queue age. Source health
uses last successful check time as well as failures. Persistent failures or
stale checks generate one grouped coverage notice, followed by recovery.

Delivery is **at least once**. If Discord accepts a message but its response is
lost, or a runner stops before a checkpoint reaches GitHub, retry may duplicate
it. Normal acknowledged messages are removed individually; a failed channel
does not replay another channel's successful messages.

## GitHub automation

`.github/workflows/jobwatch.yml` validates configuration, restores state, and
runs `python watcher.py --run-for 1200`. Collection and delivery continue during
that window; active fetches finish afterward. It saves atomic local snapshots
every 15 seconds and pushes the latest snapshot every minute, then chains the
next run. A ten-minute cron schedule restarts broken chains. No extra hosting
account is required.

Failed downloads never masquerade as a fresh installation. State push failures
stop the run visibly, with a seven-day recovery artifact. The `state` branch
contains seen IDs, per-channel deduplication, queues, and health. Schema v5 reads
older state versions; v4 full-feed queues lose their freshness expiry, while
legacy queues with no expiry metadata are retired. Existing job/application
records are retained. Tests run separately in CI on Python 3.11–3.13.

Configure these GitHub Actions secrets:

- `DISCORD_WEBHOOK_FULLTIME` (required; also the fallback destination)
- `DISCORD_WEBHOOK_APPLY_INTERN`
- `DISCORD_WEBHOOK_APPLY_FULLTIME`
- `DISCORD_WEBHOOK_INTERN`
- `HEALTHCHECK_URL` (optional external heartbeat)
- `TELEGRAM_BOT_TOKEN` (optional existing quiet-feed integration)

Webhook URLs are resolved from environment variables and never exported in
portable state. Important channels use normal Discord notifications; explicit
user/role mentions require `discord_mention` in subscriber configuration.
Quiet feeds suppress notifications and disallow mentions.

## Commands

```bash
pip install -r requirements.txt
python watcher.py --check           # validate configuration; no live sends
python watcher.py --verify          # live read-only source audit
python watcher.py --list pinterest  # inspect a company's current postings
python watcher.py --once            # one pass through due boards
python watcher.py --run-for 1200    # independently scheduled checks for 20 min
python watcher.py                  # continuous monitoring
python -m unittest -v               # regression and acceptance tests
```

Paginated adapters traverse reported totals or the end of results. Repeated
pages, count mismatches, missing IDs, malformed rows, time budgets, and failed
later pages are visible as partial results; usable recovered postings survive.
The default fetch budget is 180 seconds, with a 1,000-page safety limit.
`min_interval`, `fetch_budget_seconds`, `max_pages`, and optional
`page_delay_seconds` control source-specific behavior.

## Existing local dashboard

`python watcher.py --serve` opens the optional local dashboard with search,
ranking, saved jobs, application notes, and source health. It uses its own
SQLite data; GitHub's notification state is not a hosted dashboard sync.
Application notes never enter the public state branch. The server binds to
loopback and has no authentication; keep it local.
