#!/usr/bin/env bash
# One scheduled run of the ring_wishlist wishlist: `python -m ring_wishlist.ops run` (src/ring_wishlist/ops.py). It runs the stages that are
# due (config/schedule.yaml), skips those whose provider reached its monthly cap, backs up the database before running
# any, takes its own lock (data/logs/.ops.lock: a second trigger exits 75) and logs to data/logs/run-YYYYMMDD-HHMM.log.
#
# Linux (cron), daily at 06:00 (daily_at in config/schedule.yaml). Run `crontab -e` and add this line with your repo path:
#   0 6 * * * /absolute/path/to/ring_wishlist/scripts/refresh.sh >> /absolute/path/to/ring_wishlist/data/logs/cron.log 2>&1
# macOS: `make schedule` installs a launchd agent that runs this script daily at daily_at (`make schedule-on` starts it).
# Servers and containers can run `python -m ring_wishlist.ops serve` instead (deploy/README.md).
#
# The run spends money (monid, X API, Anthropic) up to the per-run budgets in .env and the monthly caps in
# config/schedule.yaml. `make status` (or `python -m ring_wishlist.ops plan`) shows for free what it would do.
#
# Exit codes: 0 ok · 1 setup problem (no .venv) · 2 crashed · 3 a stage failed or a budget stopped one ·
#             75 skipped because another run holds the lock.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

# launchd and cron start with a minimal PATH; add the usual places for make, uv and the claude CLI.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"   # this checkout's code, as the Makefile does
export PYTHONUNBUFFERED=1

if [ ! -x .venv/bin/python ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) refresh failed: $REPO/.venv is missing (run: make setup)" >&2
  exit 1
fi
exec .venv/bin/python -m ring_wishlist.ops run
