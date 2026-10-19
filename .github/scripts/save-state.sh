#!/usr/bin/env bash
set -euo pipefail

# Preserve queues even when polling or delivery exits early. Never claim that
# state is durable when the push failed; the workflow retains an artifact too.
[ -f state.json ] || exit 0
python3 -c 'import json; json.load(open("state.json"))'
git config user.name "jobwatch"
git config user.email "jobwatch@users.noreply.github.com"
git add -f state.json
if ! git diff --cached --quiet; then
  git commit -m "state $(date -u +%FT%TZ)"
fi
for attempt in 1 2 3; do
  if git push -f origin HEAD:state; then
    exit 0
  fi
  sleep "$((attempt * 3))"
done
echo "::error::Could not persist jobwatch state after three attempts"
exit 1
