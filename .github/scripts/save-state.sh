#!/usr/bin/env bash
set -euo pipefail

# Preserve queues even when polling or delivery exits early. Never claim that
# state is durable when the push failed; the workflow retains an artifact too.
[ -f state.json ] || exit 0
git config user.name "jobwatch"
git config user.email "jobwatch@users.noreply.github.com"
snapshot_dir=$(mktemp -d)
trap 'rm -rf "$snapshot_dir"' EXIT
# Read one atomic snapshot, validate it, and compress without changing the
# collector's live file or the source checkout/index.
python3 - "$snapshot_dir/state.json.gz" <<'PY'
import gzip, json, pathlib, sys
raw = pathlib.Path('state.json').read_bytes()
json.loads(raw)
packed = gzip.compress(raw, compresslevel=6, mtime=0)
if len(packed) >= 90 * 1024 * 1024:
    raise SystemExit('Compressed checkpoint exceeds 90 MiB; recovery artifact retained')
pathlib.Path(sys.argv[1]).write_bytes(packed)
print(f'Checkpoint: {len(raw)/1024/1024:.1f} MiB -> {len(packed)/1024/1024:.1f} MiB compressed')
PY
blob=$(git hash-object -w "$snapshot_dir/state.json.gz")
tree=$(printf '100644 blob %s\tstate.json.gz\n' "$blob" | git mktree)
# A single parentless checkpoint avoids retaining every historical snapshot
# and prevents a rejected oversized blob from poisoning subsequent pushes.
commit=$(git commit-tree "$tree" -m "state $(date -u +%FT%TZ)")
for attempt in 1 2 3; do
  if git push -f origin "$commit:refs/heads/state"; then
    exit 0
  fi
  sleep "$((attempt * 3))"
done
echo "::error::Could not persist jobwatch state after three attempts"
exit 1
