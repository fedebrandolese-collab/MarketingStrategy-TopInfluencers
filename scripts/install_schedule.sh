#!/usr/bin/env bash
# Install the two launchd agents (macOS), both DISABLED:
#   com.ring-wishlist.wishlist   daily at daily_at (config/schedule.yaml): scripts/refresh.sh -> python -m ring_wishlist.ops run
#   com.ring-wishlist.dashboard  the dashboard on http://localhost:8501, started at login and kept running
# Copies scripts/<label>.plist to ~/Library/LaunchAgents with the absolute repo path and the time filled in, then prints
# the commands to activate and deactivate them. It does NOT load anything: you decide when the schedule starts.
#
#   scripts/install_schedule.sh                              # or: make schedule
#   LAUNCH_AGENTS_DIR=/some/dir scripts/install_schedule.sh  # install somewhere else (e.g. to inspect the result)
# After changing daily_at: run it again, then `make schedule-on` (reloads both agents).
# Linux: deploy/README.md (systemd units), or the cron line at the top of scripts/refresh.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PY="$REPO/.venv/bin/python"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "launchd is macOS only. On Linux see deploy/README.md (systemd), or the cron line at the top of scripts/refresh.sh." >&2
  exit 1
fi
[ -x "$PY" ] || { echo "missing $PY (run: make setup)" >&2; exit 1; }
chmod +x "$REPO/scripts/refresh.sh"
mkdir -p "$REPO/data/logs" "$DEST_DIR"   # launchd will not start a job if its log directory is missing

# The daily time: daily_at in config/schedule.yaml, read (with its default and checks) by src/ring_wishlist/schedule.py.
read -r HOUR MINUTE < <(PYTHONPATH="$REPO/src" "$PY" -c 'import datetime as dt; from ring_wishlist import schedule
t = dt.time.fromisoformat(schedule.load()["daily_at"]); print(t.hour, t.minute)')
AT="$(printf '%02d:%02d' "$HOUR" "$MINUTE")"

# Escape the repo path for XML (&, <, >), then for the sed replacement (\, |, &).
repo_xml="$(printf '%s' "$REPO" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')"
repo_sed="$(printf '%s' "$repo_xml" | sed -e 's/[\\|&]/\\&/g')"
tmp=""
trap 'rm -f "$tmp"' EXIT

install_plist() {   # $1: the label; scripts/$1.plist -> $DEST_DIR/$1.plist
  local label="$1" template="$REPO/scripts/$1.plist" dest="$DEST_DIR/$1.plist"
  [ -f "$template" ] || { echo "missing $template" >&2; exit 1; }
  tmp="$(mktemp "${TMPDIR:-/tmp}/$label.XXXXXX")"
  sed -e "s|__REPO__|$repo_sed|g" -e "s|__HOUR__|$HOUR|g" -e "s|__MINUTE__|$MINUTE|g" "$template" > "$tmp"
  plutil -lint "$tmp" >/dev/null
  if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
    echo "Already installed and up to date: $dest"
  else
    if [ -f "$dest" ]; then
      echo "Replacing $dest (if it is active, \`make schedule-on\` reloads it)."
    fi
    cp "$tmp" "$dest"
    echo "Installed $dest"
  fi
  rm -f "$tmp"
}
install_plist com.ring-wishlist.wishlist
install_plist com.ring-wishlist.dashboard

cat <<EOF

Nothing is running yet: both agents are installed disabled, so nothing starts now or at your next login.
Once active:
  com.ring-wishlist.wishlist   runs $REPO/scripts/refresh.sh every day at $AT (local time; daily_at in
                        config/schedule.yaml): the stages that are due, which SPENDS MONEY (monid, X API, Anthropic) up
                        to the per-run budgets in .env and the monthly caps in config/schedule.yaml.
  com.ring-wishlist.dashboard  the dashboard on http://localhost:8501, started at login and restarted if it stops.
Logs: data/logs/run-*.log (one per run), data/logs/alerts.log, data/logs/launchd.log, data/logs/dashboard.log

Activate both (enable + load):  make schedule-on
Deactivate both:                make schedule-off
State, health and plan (free):  make status
One scheduled run now (spends): make ops-run
EOF
