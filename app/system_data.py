"""Data for the dashboard's System tab: is the daily pipeline alive, what will it do next, what did it spend. Pure
functions over a sqlite3 connection, config/schedule.yaml (wearable_wishlist.schedule) and two folders (data/logs/alerts.log,
data/backups); no Streamlit imports, so tests/test_system_tab.py runs them directly. app/system_tab.py draws them.

  health(conn, sched, now)      -> wearable_wishlist.ops.health when it exists, else fallback_health (the same rule)
  verdict(conn, h, sched, now)  -> (level ok|error|warning, headline, bullets): the tab's first line
  stage_rows(conn, sched, now)  -> one row per stage: every, last ok, next due, provider, paid or free
  spend(conn, sched, now)       -> this month per provider against its cap, today, the month total
  recent_runs(conn, n)          -> the last n runs with a mark per stage (run_stages), or their stage list
  freshness(conn)               -> the newest post, classification, engagement sample and scored run
  read_alerts(path, n)          -> the last n lines of alerts.log, newest first (None: no log)
  newest_backup(folder)         -> the newest data/backups/wishlist-*.db (None: no backup)

A run is scheduled when its notes start with "scheduled" (ops.py writes "scheduled: discover due"); any other run was
started by hand. Times are stored in UTC and shown in the machine's local time.
"""
from __future__ import annotations

import datetime as dt
import importlib
import math
import sqlite3
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable, Mapping

from wearable_wishlist import config, costs, db, schedule

HEALTHY_HOURS = 36                  # the latest scheduled run must have finished within this
ALERTS_LOG = config.DATA_DIR / "logs" / "alerts.log"
BACKUPS_DIR = config.DATA_DIR / "backups"
MARKS = {"ok": "✓", "failed": "✗", "skipped": "–", "running": "…"}
RUNS_SHOWN, ALERTS_SHOWN = 10, 5
SCHEDULED = "notes GLOB 'scheduled*'"   # SQL for is_scheduled
UTC = dt.timezone.utc


# ---------------------------------------------------------------- time and money in words

def parse_ts(s: Any) -> dt.datetime | None:
    """'2026-09-30T06:02:00Z' (or a datetime) -> aware datetime; None or junk -> None."""
    if isinstance(s, dt.datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=UTC)


def _now(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(UTC)).astimezone(UTC)


def _days_apart(t: dt.datetime, now: dt.datetime) -> int:
    """Calendar days from now to t in local time: 0 today, 1 tomorrow, -1 yesterday."""
    return (t.astimezone().date() - now.astimezone().date()).days


def ago(t: Any, now: dt.datetime | None = None) -> str:
    """'just now', '5 min ago', '2 h ago', '1 day ago', '6 days ago'; 'never' when there is no time."""
    t = parse_ts(t)
    if t is None:
        return "never"
    s = (_now(now) - t).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    days = int(s // 86400)
    return f"{days} day{'s' if days > 1 else ''} ago"


def when(t: Any, now: dt.datetime | None = None) -> str:
    """Local time with a day word: 'today 06:02', 'yesterday 23:10', 'tomorrow 06:00', else '28 Sep 06:02'."""
    t, now = parse_ts(t), _now(now)
    if t is None:
        return "unknown"
    lt = t.astimezone()
    day = {0: "today", -1: "yesterday", 1: "tomorrow"}.get(_days_apart(t, now)) or \
        f"{lt.day} {lt:%b}" + ("" if lt.year == now.astimezone().year else f" {lt.year}")
    return f"{day} {lt:%H:%M}"


def in_days(t: Any, now: dt.datetime | None = None) -> str:
    """'today', 'tomorrow', 'in 3 days' (calendar days, local time)."""
    days = _days_apart(parse_ts(t), _now(now))
    return "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"


def duration(seconds: float | None) -> str:
    """45 -> '45 s', 750 -> '12 min', 3900 -> '1 h 05 min'."""
    if seconds is None:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    return f"{s // 60} min" if s < 3600 else f"{s // 3600} h {s % 3600 // 60:02d} min"


def usd(v: float | None) -> str:
    """'$0' for nothing, else the ledger's format ($0.0040 below a cent, else $12.34)."""
    return "$0" if not v else costs.fmt(float(v))


def every_text(days: float) -> str:
    return "by hand" if not days else "every day" if days == 1 else f"every {days:g} days"


def short(text: Any, n: int = 200) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def error_line(text: Any) -> str:
    """The line of an error worth reading: its first, or a traceback's last (the exception)."""
    lines = [line for line in str(text or "").splitlines() if line.strip()] or ["no error recorded"]
    return short(lines[-1] if lines[0].startswith("Traceback") else lines[0])


def is_scheduled(notes: Any) -> bool:
    return str(notes or "").startswith("scheduled")


# ---------------------------------------------------------------- runs and their stages

def stages_of(conn: sqlite3.Connection, run_ids: list[int]) -> dict[int, list[dict]]:
    """run_id -> its run_stages rows, in pipeline order."""
    if not run_ids:
        return {}
    rows = db.rows(conn, f"SELECT * FROM run_stages WHERE run_id IN ({','.join('?' * len(run_ids))})", run_ids)
    order = {s: i for i, s in enumerate(schedule.STAGES)}
    out: dict[int, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (order.get(r["stage"], len(order)), r["started_at"] or "")):
        out.setdefault(r["run_id"], []).append(r)
    return out


def last_scheduled_run(conn: sqlite3.Connection) -> dict | None:
    """The newest scheduled run that finished, with its run_stages rows under "stages"."""
    r = db.one(conn, f"SELECT * FROM runs WHERE {SCHEDULED} AND finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1")
    if r:
        r["stages"] = stages_of(conn, [r["run_id"]]).get(r["run_id"], [])
    return r


def ever_scheduled(conn: sqlite3.Connection) -> bool:
    return db.one(conn, f"SELECT 1 AS yes FROM runs WHERE {SCHEDULED} LIMIT 1") is not None


def recent_runs(conn: sqlite3.Connection, n: int = RUNS_SHOWN) -> list[dict]:
    """The last n runs, newest first: trigger (scheduled or by hand), each stage with its mark (older runs, from before
    run_stages: their stage list, unmarked), duration, cost and the failed stage rows (with their error)."""
    runs = db.rows(conn, "SELECT r.*, (SELECT COALESCE(SUM(c.cost_usd), 0) FROM costs c WHERE c.run_id = r.run_id) "
                         "AS ledger FROM runs r ORDER BY r.run_id DESC LIMIT ?", (n,))
    stages = stages_of(conn, [r["run_id"] for r in runs])
    out = []
    for r in runs:
        rows = stages.get(r["run_id"], [])
        names = ([f"{s['stage']} {MARKS.get(s['status'], '?')}" for s in rows]   # no-break: a mark stays with its stage
                 or [s.strip() for s in str(r["stage"] or "").split(",") if s.strip()])   # before run_stages: unmarked
        start, end = parse_ts(r["started_at"]), parse_ts(r["finished_at"])
        took = duration((end - start).total_seconds()) if start and end else \
            "running" if any(s["status"] == "running" for s in rows) else "not finished"
        out.append({"run_id": r["run_id"], "started_at": r["started_at"], "stages": " · ".join(names),
                    "trigger": "scheduled" if is_scheduled(r["notes"]) else "by hand", "duration": took,
                    "cost_usd": float(r["cost_usd"] or 0.0) or float(r["ledger"] or 0.0),
                    "failed": [s for s in rows if s["status"] == "failed"]})
    return out


# ---------------------------------------------------------------- health and the verdict

def fallback_health(conn: sqlite3.Connection, sched: Mapping[str, Any] | None = None,
                    now: dt.datetime | None = None) -> dict:
    """ops.health's rule, for as long as src/wearable_wishlist/ops.py is not there: healthy when the latest scheduled run finished
    within the last 36 hours and none of its stages failed. A provider at its monthly cap is a warning."""
    sched, now = sched or schedule.load(), _now(now)
    last, problems = last_scheduled_run(conn), []
    if last is None:
        newest = db.one(conn, "SELECT run_id, started_at, notes FROM runs ORDER BY run_id DESC LIMIT 1")
        if newest and ever_scheduled(conn):   # scheduled runs exist, none finished
            problems.append(f"no scheduled run in {HEALTHY_HOURS} h (last: run {newest['run_id']}, not finished)")
        elif newest:
            problems.append(f"never scheduled: every run so far was started by hand (last run {newest['run_id']}, "
                            f"{ago(newest['started_at'], now)})")
        else:
            problems.append("never scheduled: no run yet")
    else:
        if now - parse_ts(last["finished_at"]) > dt.timedelta(hours=HEALTHY_HOURS):
            problems.append(f"no scheduled run in {HEALTHY_HOURS} h (last: run {last['run_id']}, "
                            f"{ago(last['finished_at'], now)})")
        problems += [f"stage {s['stage']} failed in run {last['run_id']}: {error_line(s['error'])}"
                     for s in last["stages"] if s["status"] == "failed"]
    warnings = [f"{why}: its stages are skipped until the 1st" for why in schedule.capped(conn, sched, now).values()]
    return {"ok": not problems, "problems": problems, "warnings": warnings, "last_scheduled_run": last}


def ops_health() -> Callable | None:
    """wearable_wishlist.ops.health, built alongside this tab; None while src/wearable_wishlist/ops.py does not exist."""
    try:
        return getattr(importlib.import_module("wearable_wishlist.ops"), "health", None)
    except ModuleNotFoundError as e:
        if e.name != "wearable_wishlist.ops":   # ops.py is there but broken: health() says so
            raise
        return None


def health(conn: sqlite3.Connection, sched: Mapping[str, Any] | None = None, now: dt.datetime | None = None) -> dict:
    """ops.health when importable (the scheduler's own verdict), else fallback_health; "source" says which."""
    try:
        fn = ops_health()
        if fn is not None:
            return {**fn(conn, sched=sched, now=now), "source": "ops.health"}
    except Exception as e:   # a broken ops.py must not take the page down: the same rule, and say so
        h = fallback_health(conn, sched, now)
        return {**h, "warnings": [*h["warnings"], f"ops.health failed ({short(e)}): this is the dashboard's own check"],
                "source": "fallback"}
    return {**fallback_health(conn, sched, now), "source": "fallback"}


def verdict(conn: sqlite3.Connection, h: Mapping[str, Any], sched: Mapping[str, Any],
            now: dt.datetime | None = None) -> tuple[str, str, list[str]]:
    """The tab's first line: (level, headline, bullets). level 'ok' (green: healthy), 'error' (red: the problems are the
    bullets) or 'warning' (amber: never scheduled, and the command that turns it on). Warnings are bullets with ▲."""
    now = _now(now)
    bullets = list(h.get("problems") or []) + [f"▲ {w}" for w in h.get("warnings") or []]
    nxt = f"next run {when(schedule.next_run_at(sched, now).astimezone(), now)}"
    if not ever_scheduled(conn):
        return "warning", f"● **Not scheduled yet**: turn it on with `make schedule-on` (daily at {sched['daily_at']})", bullets
    if not h.get("ok"):
        return "error", f"● **Needs attention** · {nxt}", bullets
    last = last_scheduled_run(conn)
    if last is None:
        return "ok", f"● **Healthy** · {nxt}", bullets
    n = Counter(s["status"] for s in last["stages"])
    done = (f"{n['ok']} stage{'' if n['ok'] == 1 else 's'} ok" + (f", {n['skipped']} skipped" if n["skipped"] else "")
            if last["stages"] else "nothing was due")
    return "ok", (f"● **Healthy**: last scheduled run {when(last['finished_at'], now)} (run {last['run_id']}), {done} · "
                  f"{nxt}"), bullets


# ---------------------------------------------------------------- stages, spend, freshness

def next_due(row: Mapping[str, Any], next_run: dt.datetime, now: dt.datetime | None = None,
             capped: tuple[str, ...] = ()) -> str:
    """When the schedule runs a stage next (a schedule.due row): 'today', 'tomorrow', 'in 3 days' (the first daily
    trigger at which it is due); 'not scheduled' (every_days 0); 'on hold: monid cap' while its provider is capped."""
    if not row["every_days"]:
        return "not scheduled"
    if capped:
        return f"on hold: {', '.join(capped)} cap"
    at, last = next_run, parse_ts(row["last_ok"])
    if last and not row["due"]:
        due_at = last + dt.timedelta(hours=row["every_days"] * 24 - schedule.SLACK_HOURS)
        if due_at > at:
            at += dt.timedelta(days=math.ceil((due_at - at) / dt.timedelta(days=1)))
    return in_days(at, now)


def stage_rows(conn: sqlite3.Connection, sched: Mapping[str, Any] | None = None,
               now: dt.datetime | None = None) -> list[dict]:
    """One row per stage in pipeline order: every, last ok, next due, provider, paid or free."""
    sched, now = sched or schedule.load(), _now(now)
    nxt, held = schedule.next_run_at(sched, now).astimezone(), schedule.capped(conn, sched, now)
    out = []
    cli = config.llm_backend() != "anthropic"   # classify spends only on the anthropic backend
    for r in schedule.due(conn, sched, now):
        providers = () if r["stage"] == "classify" and cli else schedule.STAGE_PROVIDERS.get(r["stage"], ())
        free = r["stage"] in schedule.FREE_STAGES or (r["stage"] == "classify" and cli)
        out.append({"stage": r["stage"], "every": every_text(r["every_days"]), "last_ok": ago(r["last_ok"], now),
                    "next_due": next_due(r, nxt, now, tuple(p for p in providers if p in held)),
                    "provider": "Claude Code CLI" if r["stage"] == "classify" and cli else (", ".join(providers) or "–"),
                    "paid": "free" if free else "paid"})
    return out


def spend(conn: sqlite3.Connection, sched: Mapping[str, Any] | None = None, now: dt.datetime | None = None) -> dict:
    """This month (UTC, from the 1st) per provider against its cap in config/schedule.yaml ("share" of the cap, None
    without one), today's spend (local day) and the month total. Every ledger row counts: scheduled runs, runs by hand,
    account checks."""
    sched, now = sched or schedule.load(), _now(now)
    month, caps = schedule.month_spend(conn, now), sched["budget"]["month_usd"]
    rows = []
    for p in dict.fromkeys([*caps, *month]):
        used, cap = month.get(p, 0.0), caps.get(p)
        share = None if cap is None else min(used / cap, 1.0) if cap else 1.0
        rows.append({"provider": p, "used": used, "cap": cap, "share": share, "capped": cap is not None and used >= cap})
    midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    today = db.one(conn, "SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM costs WHERE ts >= ?",
                   (midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),))["usd"]
    return {"providers": rows, "today": round(float(today), 4), "month": round(sum(month.values()), 4)}


def freshness(conn: sqlite3.Connection) -> list[dict]:
    """The newest of each kind of data ("what", "at"): posts fetched, classifications, engagement samples, scored run."""
    def newest(sql: str, params: tuple = ()) -> str | None:
        return (db.one(conn, sql, params) or {}).get("t")
    scored = db.latest_run_id(conn, with_scores=True)
    return [
        {"what": "Newest post fetched", "at": newest("SELECT MAX(fetched_at) AS t FROM x_posts")},
        {"what": "Newest classification", "at": newest("SELECT MAX(created_at) AS t FROM classifications")},
        {"what": "Newest engagement sample", "at": newest("SELECT MAX(fetched_at) AS t FROM engagement_samples")},
        {"what": "Newest score run" + ("" if scored is None else f" (run {scored})"),
         "at": newest("SELECT COALESCE(finished_at, started_at) AS t FROM runs WHERE run_id = ?", (scored,))},
    ]


# ---------------------------------------------------------------- files: alerts and backups

def read_alerts(path: Path | str | None = None, n: int = ALERTS_SHOWN) -> list[dict] | None:
    """The last n lines of alerts.log ('<ISO UTC time>\\t<level>\\t<message>'), newest first, as at, level, message.
    None when the file does not exist."""
    p = Path(path or ALERTS_LOG)
    if not p.exists():
        return None
    with p.open(encoding="utf-8", errors="replace") as f:
        tail = deque((line.rstrip("\n") for line in f if line.strip()), maxlen=n)
    out = []
    for line in reversed(tail):
        parts = line.split("\t", 2)
        at, level, message = parts if len(parts) == 3 else (None, "", line)
        out.append({"at": at, "level": level.strip().lower(), "message": message.strip()})
    return out


def newest_backup(folder: Path | str | None = None) -> dict | None:
    """The newest data/backups/wishlist-*.db: name, at (its modification time), how many backups there are."""
    files = [(f.stat().st_mtime, f) for f in Path(folder or BACKUPS_DIR).glob("wishlist-*.db")]
    if not files:
        return None
    mtime, newest = max(files)
    return {"name": newest.name, "at": dt.datetime.fromtimestamp(mtime, UTC), "count": len(files)}
