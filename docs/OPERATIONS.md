# Operations

How to install, run, watch and hand over the wishlist system. For whoever runs it: Federico today, the operator's team later.
Once installed it runs by itself once a day, spends within set caps, and contacts nobody.

Check any install with `make doctor`: offline, read-only, no spend. Every line is ✓ (ok), ! (look at it) or ✗ (broken),
and it exits 1 on any ✗. Run it after every change below. `python -m ...` means the project's Python: `.venv/bin/python`
on a Mac or a systemd install, plain `python` inside the containers.

## What runs when

One trigger a day at `daily_at` in [config/schedule.yaml](../config/schedule.yaml): 06:00, the machine's local time.
Each run does only the stages that are due. A stage is due when its last successful run is at least `every_days` old,
with 3 hours of slack. Free stages run every day, paid ones less often.

| Stage | Every | Spends on |
|---|---|---|
| ingest | day | free: reads the map mirror in data/raw |
| crawl | 7 days | monid: the operator's follow graph |
| resolve | 7 days | X API: handles of new map contacts |
| discover | 7 days | monid: the topic, competitor and author searches |
| expand | 14 days | monid: who the best people follow |
| enrich | day | monid: posts of new candidates |
| classify | day | nothing on `claude_cli`, the Anthropic API on `anthropic` |
| verify | day | X API: new handles near the top |
| paths | day | free |
| sample | day | monid: latest posts of new people on the list |
| score | day | free |
| export | day | free: data/exports/wishlist.json and the CSVs |

Each provider has a monthly cap in `budget.month_usd`: monid $40, X API $10, Anthropic $20. A provider at its cap is
skipped until the 1st, and the run says so. The free stages still run, so the list keeps re-scoring. Each run also has
per-run caps in `.env` (`BUDGET_MONID_USD`, `BUDGET_X_API_USD`, `BUDGET_ANTHROPIC_USD`, $10 each): a paid stage stops
before its projected spend would pass them, and the run stops there.

**Monthly cost, roughly**

| What | Cadence | Per run | Per month |
|---|---|---|---|
| crawl and discover (monid) | weekly | about $3.5 | about $15 |
| expand (monid) | every 14 days | about $1.5 | about $3 |
| enrich, sample (monid) and verify (X API) | daily | $0.3 to $0.5 | $9 to $15 |
| classify | daily | $0 on `claude_cli`; about $0.02 to $0.03 a person on `anthropic` | $0, or $5 to $20 |
| **Total** | | | **about $27 to $33**, up to about $50 on the Anthropic API |

The monthly caps bound every run, scheduled or by hand: at most $70 a month ($40 + $10 + $20). A paid call that would
pass a provider's cap is refused (`costs.guard`), and the scheduler skips that provider's stages until the 1st. Real spend is in the `costs` table, in
[COSTS.md](../COSTS.md) (rewritten after each run) and on the dashboard.

## Install on a Mac

Needs git, make and [uv](https://docs.astral.sh/uv/) (`brew install uv`). With `LLM_BACKEND=claude_cli` (the default,
no bill), Claude Code must be installed and logged in (`claude`, then `/login`) as the user who runs the schedule.

```bash
git clone <repo> wearable_wishlist && cd wearable_wishlist
make setup                                # .venv with Python 3.12 and the dependencies
cp .env.example .env && chmod 600 .env    # fill it in: each line says whether it is required
make doctor                               # no ✗ allowed
make schedule                             # installs the launchd agents com.wearable-wishlist.wishlist and com.wearable-wishlist.dashboard
make schedule-on                          # loads them: the run every day at daily_at, the dashboard always on
make status                               # what ran, what is due, this month's spend against the caps
```

On a new Mac, put the database in place before the first run ([First run](#first-run-on-a-fresh-machine)). The dashboard
is at http://localhost:8501. `make schedule-off` stops both agents.

## Install on a Linux server

Keep `data/` on a local disk, not a network share: SQLite needs real file locks. `daily_at` is the machine's local time:
set the server's timezone (`timedatectl set-timezone Europe/Rome`) or, in Docker, `TZ` in `.env`.

### With Docker compose

Needs Docker with the compose plugin. One image, two containers ([docker-compose.yml](../docker-compose.yml)):
`dashboard` and `scheduler` (`python -m wearable_wishlist.ops serve`). Code and pinned dependencies are in the image; `./data` and
`./config` are mounted; the keys come from `.env`. The containers use the Anthropic API whatever `.env` says: a server has
no Claude Code login.

```bash
git clone <repo> wearable_wishlist && cd wearable_wishlist
cp .env.example .env && chmod 600 .env     # fill it in, with ANTHROPIC_API_KEY
# put the database in place first: First run, below
sudo chown -R 1000:1000 data config        # the container user; or build as yourself:
                                           # WEARABLE_WISHLIST_UID=$(id -u) WEARABLE_WISHLIST_GID=$(id -g) docker compose build
docker compose build                       # make docker-build
docker compose up -d                       # make docker-up; both restart by themselves after a reboot
docker compose ps                          # both "healthy" after a few minutes
docker compose exec dashboard python -m wearable_wishlist.doctor
docker compose logs -f scheduler
```

The dashboard is published on the server's 127.0.0.1:8501 only. From a laptop: `ssh -L 8501:localhost:8501 <server>`,
then http://localhost:8501. To share it on a VPN or the intranet, see [Security](#security-and-privacy).
`docker compose down` (`make docker-down`) stops both; data/ stays. To upgrade: `git pull`, `docker compose build`,
`docker compose up -d`. Every run rewrites data/exports and the dashboard can rewrite config/weights.yaml, both tracked
by git: commit or discard them before a pull. `make lock` regenerates requirements.lock after a change to pyproject.toml.

### With systemd

Needs Python 3.12, make and uv on the service user's PATH. The units are `deploy/systemd/wearable_wishlist-scheduler.service` and
`wearable_wishlist-dashboard.service`. Read them first: they name the user and the checkout path.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin wearable_wishlist
sudo install -d -o wearable_wishlist -g wearable_wishlist /opt/wearable_wishlist                 # the path the units name
sudo -u wearable_wishlist git clone <repo> /opt/wearable_wishlist && cd /opt/wearable_wishlist
sudo -u wearable_wishlist make setup
sudo -u wearable_wishlist cp .env.example .env && sudo chmod 600 .env      # LLM_BACKEND=anthropic and ANTHROPIC_API_KEY
sudo -u wearable_wishlist make doctor
sudo cp deploy/systemd/wearable_wishlist-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wearable_wishlist-scheduler wearable_wishlist-dashboard
journalctl -u wearable_wishlist-scheduler -f
```

## First run on a fresh machine

The database holds work that cannot be bought again: the statuses the operator sets, the 135 hand rejections, the change
history, the cost ledger. Restore a backup; rebuild only when there is none.

**From a backup (the normal way)**

1. On the current machine: `make backup`. The file lands in `data/backups/`.
2. Copy it to the new machine over a secure channel: `scp`, or company storage with access control. Never email.
   Copy `data/raw` too if the map mirror was refreshed after the repository's copy (`make doctor` prints its export
   date). Optional: `data/cache`, so the last week's follow lists and searches are not bought again.
3. On the new machine, before the schedule starts: `python -m wearable_wishlist.ops restore data/backups/<file>`.
   Docker: `docker compose run --rm scheduler python -m wearable_wishlist.ops restore data/backups/<file>`.
4. `make doctor`: the database line shows the latest run.

**From scratch**

`BUDGET_MONID_USD=25 make run` rebuilds everything in one run, for about $15 to $20 of monid and X API. Every paid batch
prints its projected cost first. On the Anthropic API, classifying some 700 people adds about $15 to $20: add
`BUDGET_ANTHROPIC_USD=25` to that command. On a fresh database this month's spend starts at $0, so
the monthly caps allow it. The hand reviews and statuses are lost: they live only in the database.

## What to hand over, and how

| What | Holds | How |
|---|---|---|
| The repository | code, config, the map mirror (data/raw), the latest exports | a **private** repository on the company's GitHub (`git remote add origin <url>`, `git push -u origin main`). CI runs the tests there (.github/workflows/tests.yml) |
| `.env` | keys and the map passphrase | a shared vault in a password manager, or an encrypted file. Never email, chat or the repository |
| A database backup | the list, evidence, statuses, hand reviews, costs | `make backup`, then `scp` or company storage with access control |
| data/raw | the read-only mirror of the operator's map | in the repository; copy the current machine's if it is newer |
| Accounts | monid wallet, X developer app (pay-per-use credits), Anthropic API or a Claude subscription | the company's own accounts, so top-ups and invoices go to the company. Then rotate Federico's keys out ([Rotating keys](#rotating-keys)) |

## Monitoring

| Where | What |
|---|---|
| The dashboard's System tab | the last runs, what is due next, this month's spend against the caps |
| `make status` | the same in the terminal. Docker: `docker compose exec scheduler python -m wearable_wishlist.ops plan` |
| `data/logs/alerts.log` | every alert the scheduler raises, one line each |
| `NOTIFY_WEBHOOK_URL` in `.env` | each alert is also sent there, for example a Slack or Teams channel |
| macOS notifications | alerts show on the Mac too (`WEARABLE_WISHLIST_NO_DESKTOP_NOTIFY=1` turns them off) |
| `python -m wearable_wishlist.ops health` | exits 0 when healthy, 1 when not. The Docker healthcheck uses it; any monitor can |
| `docker compose ps` | Docker: each container's health |
| `make doctor` | the install itself: keys, database, config, disk |

A look once a week is enough: the System tab, alerts.log, COSTS.md.

## Changing cadence or budgets

* Cadence and monthly caps: edit [config/schedule.yaml](../config/schedule.yaml). `daily_at` in quotes (`"06:00"`),
  `stages` in days between runs (0 = by hand only), `budget.month_usd` per provider. Then `make doctor`: it catches an
  unquoted time (YAML reads 17:30 as the number 1050, which runs at 10:50), a misspelled stage, a cap that is not a number.
* After changing `daily_at`, restart the scheduler so the trigger moves. Mac: `make schedule-off`, `make schedule`,
  `make schedule-on`. Docker: `docker compose restart scheduler`. systemd: `sudo systemctl restart wearable_wishlist-scheduler`.
* Per-run caps: `BUDGET_*_USD` in `.env`.
* One run above the monthly cap: by hand only, e.g. `BUDGET_IGNORE_MONTH=1 make discover`. Docker:
  `docker compose run --rm -e BUDGET_IGNORE_MONTH=1 scheduler python -m wearable_wishlist.pipeline run --stage discover`. Scheduled
  runs ignore the switch on purpose (they log a warning), so it can never lift the caps every day. Never leave it in `.env`.
* One stage by hand: `make discover`, `make enrich`... They print the projected cost first. The per-run caps and the
  monthly caps both apply.

## Rotating keys

| Key | New one at |
|---|---|
| `MONID_API_KEY` | app.monid.ai |
| `X_BEARER_TOKEN` | console.x.com, the app's keys and tokens: regenerate the bearer token |
| `ANTHROPIC_API_KEY` | console.anthropic.com, API keys |
| The claude CLI login | `claude`, then `/login` (Mac) |
| `MAP_PASSPHRASE` | the operator, the map's owner |
| `DASHBOARD_PASSWORD`, `NOTIFY_WEBHOOK_URL` | your choice; the chat tool's webhook settings (a webhook URL is a secret too) |

1. Make the new key and put it in `.env`.
2. `make doctor`.
3. Restart what keeps the old value in memory. The dashboard everywhere (Mac:
   `launchctl kickstart -k gui/$(id -u)/com.wearable-wishlist.dashboard`). Docker: `docker compose up -d --force-recreate`.
   systemd: `sudo systemctl restart wearable_wishlist-scheduler wearable_wishlist-dashboard`. The Mac's daily run reads `.env` each time.
4. Revoke the old key.
5. Check the next run in the System tab.

Rotate when someone who had `.env` leaves, and after the handover.

## Backups and restore

* Every scheduled run with work to do takes a backup first (`data/backups/wishlist-YYYYMMDD-HHMM.db`, the 14 newest kept,
  about 240 MB each today). `make backup` (`python -m wearable_wishlist.ops backup`) takes one now. Take one before anything risky:
  a big manual run, a restore, an upgrade.
* A backup on the same disk does not survive the disk. Once a week, copy the newest one to company storage (encrypted,
  access-controlled). Backups take space: `make doctor` fails under 1 GB free.
* Restore: stop the schedule and the dashboard (`make schedule-off`, `docker compose down`, or `systemctl stop`), then
  `python -m wearable_wishlist.ops restore data/backups/<file>`, then `make doctor`, then start again.

## Troubleshooting

**monid day budget exceeded.** The log says `monid blocked the run` with the account's controls: a limit set on the monid
account stopped the call before it ran, and nothing was charged. The stage stays due and runs at the next trigger. To
finish today, raise the limit at app.monid.ai, then `make ops-run`. "wallet balance too low": top up at
app.monid.ai/wallet.

**X API credits used up.** `X API: insufficient credits (402)`. Nothing was charged. resolve and verify fail, the other
stages go on. Top up the pay-per-use balance at console.x.com; the next run verifies what was missed.

**claude CLI logged out.** classify logs `failed, skipped: claude CLI failed` for every person and classifies nobody; new
people wait unscored. On the Mac, run `claude` and `/login` as the schedule's user. Or switch for good:
`LLM_BACKEND=anthropic` and `ANTHROPIC_API_KEY` in `.env` (billed, capped by `budget.month_usd.anthropic`), then
`make doctor`.

**Map passphrase changed.** Nothing breaks at once: ingest reads data/raw every day and never logs in. A refresh
(`python -m wearable_wishlist.ingest_map --refresh`) fails with a login error ending in `wrong passphrase?`. Ask the operator for the new
one, put it in `MAP_PASSPHRASE`, refresh again. The system only reads the map, never writes to it.

**The Mac was asleep at the trigger.** launchd runs a missed job when the Mac wakes (several missed days count as one).
A Mac that was shut down runs at the next trigger. Nothing is lost: due stages stay due. To run on time, keep the Mac on
power at `daily_at`, or schedule a wake: `sudo pmset repeat wakeorpoweron MTWRFSU 05:55:00`. If it must never miss, use
a server.

**A stage fails every day.** `make status` or the System tab shows its last success getting older; alerts.log names it.

1. `make doctor`: a missing or revoked key, a broken config file, the map mirror.
2. Run the stage by hand to see the error: `make <stage>`.
3. `stopped by budget guard`: the per-run cap is too low for that stage, and the run stops there. Raise `BUDGET_*_USD`.
4. A provider at its monthly cap: wait for the 1st, or raise `budget.month_usd`.
5. ingest, classify and score are fatal: when they crash the run stops. ingest needs data/raw or `MAP_PASSPHRASE`.
6. Cannot fix it now: set its `every_days` to 0 in config/schedule.yaml, so it stops failing and alerting, and run it by
   hand later.

## Security and privacy

* **Personal data stays inside the company.** The database, data/raw, data/exports and config/contact_routes hold personal
  data: the operator's contacts (names, companies, meetings, intros), people's X profiles and posts, researched contact routes
  with some email addresses. Keep them on company machines and company storage, with encrypted disks.
* **The repository carries personal data too** (data/raw and data/exports are tracked). Keep it private: never public,
  never a fork outside the company.
* **The dashboard stays on localhost.** Mac: localhost only. Docker: 127.0.0.1 only. To share it, use a VPN or the
  intranet (`DASHBOARD_BIND` in `.env`) and always set `DASHBOARD_PASSWORD`. Never a public address. An SSH tunnel is the
  simplest safe way in.
* **`.env` is never committed** (git-ignored), is `chmod 600`, travels only through a password manager or an encrypted
  file, and its keys are rotated when someone leaves. On 30 Sep 2026 no key, token or passphrase was found in the tracked
  files or the git history; tests hold dummy values only.
* **The map is read-only.** The system logs in and reads; data/raw is its mirror. Nothing ever writes to the map.
* **Nobody is contacted by the system.** X access is read-only (an app-only token and scrapers, no write scopes): no DMs,
  no posts, no emails. The contact routes are for people to act on.
* **Where data goes.** X data comes through monid (Apify, TikHub) and the official X API. Profiles and posts go to Claude
  for classification, which the operator approved.
