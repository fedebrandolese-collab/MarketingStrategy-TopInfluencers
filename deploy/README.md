# Running unattended

The runtime is `python -m wearable_wishlist.ops` (`src/wearable_wishlist/ops.py`):

| command | what it does |
|---|---|
| `run` | one scheduled run: the stages due in `config/schedule.yaml`, minus those whose provider reached its monthly cap (spends, up to the caps) |
| `serve` | loops: sleeps until `daily_at`, does `run`, repeats (servers and containers) |
| `plan` | what `run` would do now and why, spend this month against the caps (free) |
| `health` | JSON; exit 0 healthy, 1 not: the latest scheduled run finished within 36 h with no failed stage, and the database passes `PRAGMA quick_check` |
| `backup`, `restore FILE` | SQLite online backup to `data/backups/` (newest 14 kept); restore takes a safety backup first |

A run that has stages to run backs up the database first. Every run logs to `data/logs/run-YYYYMMDD-HHMM.log` (kept 30 days).

## macOS (launchd)

```
make schedule       # installs com.wearable-wishlist.wishlist (daily at daily_at) and com.wearable-wishlist.dashboard (localhost:8501), disabled
make schedule-on    # enables and loads both: the daily run spends, up to the caps
make status         # launchd state, health, plan
make schedule-off   # unloads both
```

After changing `daily_at`, run `make schedule && make schedule-on` again.

## Linux (systemd)

1. Put the repo in `/opt/wearable_wishlist`, create a `wearable_wishlist` user that owns it, run `make setup`, and fill in `.env` as on the Mac.
2. Install the units. Edit the paths, `User` or `TZ` first if yours differ; `daily_at` is the server's local time.
   ```
   sudo cp deploy/systemd/wearable_wishlist-scheduler.service deploy/systemd/wearable_wishlist-dashboard.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now wearable_wishlist-scheduler wearable_wishlist-dashboard
   ```
3. Check it: `systemctl status wearable_wishlist-scheduler`, `journalctl -u wearable_wishlist-scheduler -f`,
   `cd /opt/wearable_wishlist && PYTHONPATH=src .venv/bin/python -m wearable_wishlist.ops health`.

The dashboard listens on localhost only. Reach it with `ssh -L 8501:localhost:8501 <server>`.

To use cron instead of the scheduler service, add one line with `crontab -e`, at `daily_at`:
`0 6 * * * /opt/wearable_wishlist/scripts/refresh.sh >> /opt/wearable_wishlist/data/logs/cron.log 2>&1`

## Alerts

These events are alerted:
- a scheduled run with a failed stage, stopped by a budget, or crashed
- a provider reaching its monthly cap (once per provider and month)
- a scheduled run found abandoned

Each alert is one line in `data/logs/alerts.log`: `<ISO UTC time>\t<error|warning|info>\t<message>`.

| env (.env) | effect |
|---|---|
| `NOTIFY_WEBHOOK_URL` | every alert is also POSTed as `{"text": "wearable_wishlist wishlist <level>: <message>"}` (a Slack incoming webhook works) |
| `WEARABLE_WISHLIST_NO_DESKTOP_NOTIFY=1` | no macOS notification for alerts |
| `BUDGET_IGNORE_MONTH=1` | lifts the monthly caps for a deliberate manual run; scheduled runs ignore it |
