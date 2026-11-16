"""The schedule: which pipeline stages are due, and how much each provider has spent this month (config/schedule.yaml).

Pure reads of the database and the file, no network, no spend: ops.py uses them to decide what a scheduled run does,
the dashboard's System tab to show what will happen next.

  load(path)                  -> {"daily_at": "06:00", "stages": {stage: every_days}, "budget": {"month_usd": {...}}}
  last_ok(conn)               -> {stage: finished_at of its latest successful run}
  due(conn, sched, now)       -> one dict per stage in pipeline order: stage, every_days, last_ok, age_days, due, reason
  month_spend(conn, now)      -> {provider: USD spent since the 1st of this month (UTC)}
  capped(conn, sched, now)    -> {provider: reason} for providers at or over their monthly cap
  next_run_at(sched, now)     -> the next daily trigger (local time)

A stage counts as successful from run_stages (status 'ok'). Runs from before run_stages existed count when they finished
and their `runs.stage` list names the stage (or 'all').
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import config, db

SCHEDULE_FILE = config.CONFIG_DIR / "schedule.yaml"
STAGES = ["ingest", "crawl", "resolve", "discover", "expand", "enrich", "classify", "verify", "paths", "sample", "score",
          "export"]                       # pipeline.STAGES (not imported: the pipeline imports every stage module)
DEFAULT_DAILY_AT = "06:00"
DEFAULT_EVERY = {"ingest": 1, "crawl": 7, "resolve": 7, "discover": 7, "expand": 14, "enrich": 1, "classify": 1,
                 "verify": 1, "paths": 1, "sample": 1, "score": 1, "export": 1}
DEFAULT_MONTH_USD = {"monid": 40.0, "x_api": 10.0, "anthropic": 20.0}
SLACK_HOURS = 3                           # a stage due every N days is due again after N days minus this
# The provider each stage spends on (classify spends only on the anthropic backend; the others are free).
STAGE_PROVIDERS = {"crawl": ("monid",), "discover": ("monid",), "expand": ("monid",), "enrich": ("monid",),
                   "sample": ("monid",), "resolve": ("x_api",), "verify": ("x_api",), "classify": ("anthropic",)}
FREE_STAGES = {"ingest", "paths", "score", "export"}


def load(path: Path | str | None = None) -> dict:
    """The schedule with defaults filled in; a missing or unreadable file gives the defaults. Unknown stages are dropped,
    every_days is a number >= 0 (0 = never by schedule)."""
    p = Path(path) if path else SCHEDULE_FILE
    raw: Any = {}
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text()) or {}
        except yaml.YAMLError:
            raw = {}
    raw = raw if isinstance(raw, Mapping) else {}
    stages = dict(DEFAULT_EVERY)
    for k, v in (raw.get("stages") or {}).items() if isinstance(raw.get("stages"), Mapping) else ():
        if k in stages:
            try:
                stages[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    month = dict(DEFAULT_MONTH_USD)
    b = raw.get("budget") if isinstance(raw.get("budget"), Mapping) else {}
    for k, v in (b.get("month_usd") or {}).items() if isinstance(b.get("month_usd"), Mapping) else ():
        try:
            month[str(k)] = max(0.0, float(v))
        except (TypeError, ValueError):
            pass
    at = raw.get("daily_at") or DEFAULT_DAILY_AT
    if isinstance(at, int) and not isinstance(at, bool) and 0 <= at < 24 * 60:
        at = f"{at // 60:02d}:{at % 60:02d}"   # YAML reads an unquoted 17:30 as 1050, minutes after midnight
    at = str(at)
    try:
        dt.time.fromisoformat(at if ":" in at else "invalid")   # '1050' would parse as 10:50: only HH:MM counts
    except ValueError:
        at = DEFAULT_DAILY_AT
    return {"daily_at": at, "stages": stages, "budget": {"month_usd": month}}


def _ts(s: Any) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _now(now: dt.datetime | None) -> dt.datetime:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)


def last_ok(conn: sqlite3.Connection) -> dict[str, str]:
    """stage -> finished_at of its latest successful run."""
    out: dict[str, str] = {}
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "run_stages" in have:
        for r in db.rows(conn, "SELECT stage, MAX(finished_at) AS t FROM run_stages WHERE status='ok' GROUP BY stage"):
            if r["t"]:
                out[r["stage"]] = r["t"]
    # history from before run_stages: a finished run counts for every stage its stage list names
    for r in db.rows(conn, "SELECT stage, finished_at FROM runs WHERE finished_at IS NOT NULL AND run_id NOT IN "
                           "(SELECT DISTINCT run_id FROM run_stages)" if "run_stages" in have else
                     "SELECT stage, finished_at FROM runs WHERE finished_at IS NOT NULL"):
        names = STAGES if (r["stage"] or "") == "all" else [s.strip() for s in str(r["stage"] or "").split(",")]
        for s in names:
            if s in STAGES and (s not in out or r["finished_at"] > out[s]):
                out[s] = r["finished_at"]
    return out


def due(conn: sqlite3.Connection, sched: Mapping[str, Any] | None = None, now: dt.datetime | None = None) -> list[dict]:
    """One row per stage, in pipeline order: stage, every_days, last_ok, age_days (None: never), due, reason."""
    sched = sched or load()
    now = _now(now)
    ok = last_ok(conn)
    out = []
    for s in STAGES:
        every = float(sched["stages"].get(s, 0) or 0)
        last = ok.get(s)
        t = _ts(last)
        age = (now - t).total_seconds() / 86400.0 if t else None
        if every <= 0:
            is_due, reason = False, "not scheduled (every_days 0): run it by hand"
        elif t is None:
            is_due, reason = True, "never ran successfully"
        elif age * 24 >= every * 24 - SLACK_HOURS:
            is_due, reason = True, f"last ok {age:.1f} days ago, every {every:g}"
        else:
            is_due, reason = False, f"last ok {age:.1f} days ago, next in {every - age:.1f} days"
        out.append({"stage": s, "every_days": every, "last_ok": last, "age_days": None if age is None else round(age, 2),
                    "due": is_due, "reason": reason})
    return out


def month_start(now: dt.datetime | None = None) -> str:
    n = _now(now)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_spend(conn: sqlite3.Connection, now: dt.datetime | None = None) -> dict[str, float]:
    """provider -> USD in the costs ledger since the 1st of this month (UTC)."""
    rows = db.rows(conn, "SELECT provider, COALESCE(SUM(cost_usd), 0) AS usd FROM costs WHERE ts >= ? GROUP BY provider",
                   (month_start(now),))
    return {r["provider"]: round(float(r["usd"] or 0), 4) for r in rows}


def capped(conn: sqlite3.Connection, sched: Mapping[str, Any] | None = None, now: dt.datetime | None = None) -> dict[str, str]:
    """provider -> why, for each provider whose spend this month reached its cap."""
    sched = sched or load()
    spend = month_spend(conn, now)
    out = {}
    for provider, cap in sched["budget"]["month_usd"].items():
        used = spend.get(provider, 0.0)
        if used >= cap:
            out[provider] = f"{provider} spent ${used:.2f} this month, cap ${cap:.2f} (config/schedule.yaml)"
    return out


def next_run_at(sched: Mapping[str, Any] | None = None, now: dt.datetime | None = None) -> dt.datetime:
    """The next daily trigger in local time (naive datetime in the machine's timezone)."""
    sched = sched or load()
    local_now = (now.astimezone() if now else dt.datetime.now().astimezone()).replace(tzinfo=None)
    at = dt.time.fromisoformat(sched["daily_at"])
    nxt = dt.datetime.combine(local_now.date(), at)
    return nxt if nxt > local_now else nxt + dt.timedelta(days=1)
