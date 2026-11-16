"""Runtime: the scheduled run and its safety rails, so the system keeps running without anyone at the keyboard.

  python -m ring_wishlist.ops run            one scheduled run now: the stages due in config/schedule.yaml (spends, up to the caps)
  python -m ring_wishlist.ops plan [--json]  what `run` would do now and why, spend this month against the caps (free, no network)
  python -m ring_wishlist.ops serve          servers and containers: sleep until daily_at, run, repeat; SIGTERM stops it cleanly
  python -m ring_wishlist.ops health         JSON; exit 0 when healthy, 1 when not (the dashboard's System tab calls health())
  python -m ring_wishlist.ops backup         SQLite online backup to data/backups/wishlist-YYYYMMDD-HHMM.db (keeps the newest 14)
  python -m ring_wishlist.ops restore FILE   copy a backup over the database, after a safety backup of the current one

A scheduled run (`run`, and each cycle of `serve`): take the lock data/logs/.ops.lock (a second run exits 75) -> close the
work a dead process left open (close_abandoned) -> plan: schedule.due minus the stages whose provider reached its monthly
cap (the free stages still run) -> back up the database -> pipeline.run(notes="scheduled: ...", skipped=...), where the
stages it does not run are recorded as skipped with why. The whole log (logging and prints) goes to
data/logs/run-YYYYMMDD-HHMM.log, kept 30 days. Nothing due: no backup and no stage, but a run with notes
"scheduled: nothing due" tells health that the scheduler is alive. A scheduled run keeps the monthly caps even when
BUDGET_IGNORE_MONTH=1 is set (that switch is for a deliberate manual run).
Exit codes: 0 ok · 3 a stage failed or a budget stopped one · 75 another run holds the lock · 2 crashed.

Health (the one rule, here and in the System tab): healthy when the latest finished scheduled run (runs.notes starting with
"scheduled") finished within the last 36 hours, none of its run_stages rows failed, and PRAGMA quick_check passes. A
provider at its monthly cap is a warning, not a problem.

Alerts: a scheduled run with a failed stage, stopped by a budget, or crashed; a provider reaching its monthly cap (once per
provider and month); scheduled runs found abandoned. Each is one line in data/logs/alerts.log (the dashboard reads it):
    <ISO UTC time>\\t<error|warning|info>\\t<message>
plus a POST {"text": "ring_wishlist wishlist <level>: <message>"} to NOTIFY_WEBHOOK_URL when set (Slack-compatible, 10 s timeout,
never raises) and a macOS notification (off with RING_WISHLIST_NO_DESKTOP_NOTIFY=1).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import logging
import os
import platform
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from . import config, costs, db, pipeline, schedule

log = logging.getLogger("ring_wishlist.ops")

LOGS_DIR = config.DATA_DIR / "logs"
BACKUPS_DIR = config.DATA_DIR / "backups"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
KEEP_BACKUPS = 14
KEEP_LOG_DAYS = 30
ABANDON_HOURS = 6                 # an unfinished run this old is dead (a scheduled one is dead as soon as we hold the lock)
HEALTH_HOURS = 36                 # a daily trigger plus slack
ABANDONED = "abandoned: the process ended before the stage finished"
NOTHING_DUE = "scheduled: nothing due"
EXIT_OK, EXIT_CRASH, EXIT_FAILED, EXIT_LOCKED = 0, 2, 3, 75
RUN_LOG = re.compile(r"(run|refresh)-\d{8}-\d{4}.*\.log$")   # ours, and the older refresh.sh logs; never alerts.log
TS = "%Y-%m-%dT%H:%M:%SZ"


class Locked(RuntimeError):
    """Another scheduled run (or a restore) holds data/logs/.ops.lock."""


class Stop(BaseException):
    """SIGTERM: raised in the main thread, so `serve` stops between runs at once and a run in progress stops at its next
    Python instruction (pipeline.run records the interrupted stage as failed and closes the run)."""


def _on_sigterm(signum, frame):
    raise Stop(signal.Signals(signum).name)


def _utc(now: dt.datetime | None = None) -> dt.datetime:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)


def _parse(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(config.ROOT))
    except ValueError:
        return str(p)


# ---------------- the lock ----------------

@contextlib.contextmanager
def lock(path: Path | None = None):
    """Hold data/logs/.ops.lock for the block, or raise Locked. An flock: the kernel drops it when its process dies, so a
    lock file a dead run left behind never blocks; the file holds the owner's pid for the message."""
    path = path or LOGS_DIR / ".ops.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
            raise Locked(f"another scheduled run (pid {owner}) holds {_rel(path)}") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------- logs and alerts ----------------

class _Tee:
    """print() during a run goes to the terminal (or launchd's log) and to the run's log file."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()

    def __getattr__(self, name):                     # isatty, encoding, fileno...: the terminal's
        return getattr(self.streams[0], name)


@contextlib.contextmanager
def _run_log(path: Path):
    """Everything logged (INFO and up) or printed inside the block also goes to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    level = root.level
    with open(path, "a", buffering=1, encoding="utf-8") as fh:
        handler = logging.StreamHandler(fh)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
        if root.getEffectiveLevel() > logging.INFO:
            root.setLevel(logging.INFO)
        try:
            with contextlib.redirect_stdout(_Tee(sys.stdout, fh)), contextlib.redirect_stderr(_Tee(sys.stderr, fh)):
                yield
        finally:
            root.removeHandler(handler)
            root.setLevel(level)


def prune_logs(days: int = KEEP_LOG_DAYS) -> list[Path]:
    """Delete the run logs (run-YYYYMMDD-HHMM.log, refresh.sh's refresh-*.log) older than `days`."""
    cutoff, gone = time.time() - days * 86400, []
    for p in LOGS_DIR.glob("*.log"):
        try:
            if RUN_LOG.match(p.name) and p.stat().st_mtime < cutoff:
                p.unlink()
                gone.append(p)
        except OSError as e:
            log.warning("log %s not pruned: %s", p.name, e)
    return gone


def alert(level: str, message: str) -> str:
    """Record an alert ('error' | 'warning' | 'info'): a line in data/logs/alerts.log, a POST to NOTIFY_WEBHOOK_URL when
    set, a macOS notification. Never raises; returns the line."""
    message = " ".join(str(message).split())            # one line, no tabs: the file format is fixed
    line = f"{db.utcnow()}\t{level}\t{message}"
    log.log({"error": logging.ERROR, "warning": logging.WARNING}.get(level, logging.INFO), "alert: %s", message)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "alerts.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("alerts.log not written: %s", e)
    url = config.env("NOTIFY_WEBHOOK_URL")
    if url:
        try:
            r = requests.post(url, json={"text": f"ring_wishlist wishlist {level}: {message}"}, timeout=10)
            if r.status_code >= 400:
                log.warning("alert webhook answered HTTP %s", r.status_code)
        except Exception as e:  # noqa: BLE001 (an alert never breaks the run)
            log.warning("alert webhook failed: %s", e)
    if platform.system() == "Darwin" and config.env("RING_WISHLIST_NO_DESKTOP_NOTIFY") != "1":
        try:
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run",
                            message[:250], f"ring_wishlist wishlist: {level}"], capture_output=True, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.debug("desktop notification failed: %s", e)
    return line


def read_alerts(since: str | None = None) -> list[dict]:
    """alerts.log as [{"ts", "level", "message"}], oldest first; `since`: an ISO UTC time."""
    p = LOGS_DIR / "alerts.log"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and (since is None or parts[0] >= since):
            out.append({"ts": parts[0], "level": parts[1], "message": parts[2]})
    return out


def _alert_caps(conn: sqlite3.Connection, sched: dict, now: dt.datetime | None = None) -> None:
    """A warning when a provider reaches its monthly cap: once per provider and month (alerts.log remembers)."""
    seen = [a["message"] for a in read_alerts(since=schedule.month_start(now))]
    for provider, why in schedule.capped(conn, sched, now).items():
        if not any(m.startswith(f"monthly cap reached: {provider} ") for m in seen):
            alert("warning", f"monthly cap reached: {why}; its stages are skipped until the 1st")


# ---------------- abandoned work, backups ----------------

def close_abandoned(conn: sqlite3.Connection, now: dt.datetime | None = None, why: str = ABANDONED) -> list[dict]:
    """Close what a dead process left open. Called holding the lock, so an unfinished scheduled run is dead; any other run
    still unfinished after ABANDON_HOURS is too (a manual run in progress is younger). Their stages stuck at 'running'
    become 'failed' with `why`; a run with no stage rows at all (older than run_stages, or dead before its first stage)
    gets a failed row per pipeline stage it named, so schedule.last_ok never takes it for a success. The run gets
    finished_at = its last sign of life and ' [abandoned]' in its notes. Stages stuck at 'running' in a finished run fail
    too. Returns the closed runs (run_id, stage, notes)."""
    now = _utc(now)
    cutoff = (now - dt.timedelta(hours=ABANDON_HOURS)).strftime(TS)
    dead = db.rows(conn, "SELECT run_id, stage, notes FROM runs WHERE finished_at IS NULL "
                         "AND (notes LIKE 'scheduled%' OR started_at < ?) ORDER BY run_id", (cutoff,))
    for r in dead:
        rid = r["run_id"]
        conn.execute("UPDATE run_stages SET status='failed', error=? WHERE run_id=? AND status='running'", (why, rid))
        if not conn.execute("SELECT 1 FROM run_stages WHERE run_id=?", (rid,)).fetchone():
            named = schedule.STAGES if r["stage"] == "all" else [s.strip() for s in str(r["stage"] or "").split(",")]
            for s in dict.fromkeys(s for s in named if s in schedule.STAGES):
                conn.execute("INSERT INTO run_stages (run_id, stage, status, error) VALUES (?,?,'failed',?)", (rid, s, why))
        last = conn.execute("SELECT MAX(t) FROM (SELECT started_at AS t FROM runs WHERE run_id=:r "
                            "UNION ALL SELECT started_at FROM run_stages WHERE run_id=:r "
                            "UNION ALL SELECT finished_at FROM run_stages WHERE run_id=:r "
                            "UNION ALL SELECT ts FROM costs WHERE run_id=:r)", {"r": rid}).fetchone()[0]
        cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE run_id=?", (rid,)).fetchone()[0]
        conn.execute("UPDATE runs SET finished_at=?, cost_usd=?, notes=TRIM(COALESCE(notes,'') || ' [abandoned]') "
                     "WHERE run_id=?", (last or now.strftime(TS), cost, rid))
        log.warning("run %s (%s, %s) was abandoned: closed, its unfinished stages marked failed", rid, r["stage"], r["notes"])
    stuck = conn.execute("UPDATE run_stages SET status='failed', error=? WHERE status='running' "
                         "AND run_id IN (SELECT run_id FROM runs WHERE finished_at IS NOT NULL)", (why,)).rowcount
    if stuck:
        log.warning("%d stage rows stuck at 'running' in finished runs marked failed", stuck)
    conn.commit()
    return dead


def backup(conn: sqlite3.Connection | None = None, tag: str = "", keep: int | None = KEEP_BACKUPS) -> Path:
    """SQLite online backup of the database (consistent while other processes write) to
    data/backups/wishlist-YYYYMMDD-HHMM[-tag].db, local time, never overwriting a backup; then keep the newest `keep`
    (None: no rotation). Returns the file."""
    src = conn
    if src is None:
        if not config.db_path().exists():
            raise FileNotFoundError(f"no database at {config.db_path()}")
        src = sqlite3.connect(str(config.db_path()), timeout=60)
    try:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        base = f"wishlist-{dt.datetime.now():%Y%m%d-%H%M}" + (f"-{tag}" if tag else "")
        out, n = BACKUPS_DIR / f"{base}.db", 1
        while out.exists():
            n += 1
            out = BACKUPS_DIR / f"{base}-{n}.db"
        part = out.with_name(out.name + ".part")      # a crash mid-copy never leaves a file that looks complete
        dst = sqlite3.connect(str(part))
        try:
            src.backup(dst)
        finally:
            dst.close()
        part.replace(out)
    finally:
        if conn is None:
            src.close()
    log.info("backup: %s (%.1f MB)", _rel(out), out.stat().st_size / 1e6)
    if keep:
        rotate_backups(keep)
    return out


def rotate_backups(keep: int = KEEP_BACKUPS) -> list[Path]:
    """Keep the newest `keep` backups (by the time they were written), delete the others. Returns the deleted ones."""
    files = sorted(BACKUPS_DIR.glob("wishlist-*.db"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for p in files[keep:]:
        p.unlink(missing_ok=True)
        log.info("backup: rotated out %s", p.name)
    return files[keep:]


def restore(file: str | Path) -> Path | None:
    """Copy backup `file` over the database through SQLite's backup API (a live WAL database is never overwritten as a
    file), after a safety backup of the current one (wishlist-...-before-restore.db, not rotated). Refuses a file that is
    not a healthy wishlist database, and raises Locked while a scheduled run holds the lock. Returns the safety backup."""
    src_path = Path(file).expanduser().resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"no such backup: {src_path}")
    if config.db_path().exists() and src_path == config.db_path().resolve():
        raise ValueError(f"{src_path} is the database itself")
    src = sqlite3.connect(str(src_path))
    try:
        try:
            check = [r[0] for r in src.execute("PRAGMA quick_check")]
            has_runs = src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
        except sqlite3.DatabaseError as e:
            check, has_runs = [str(e)], None
        if check != ["ok"] or not has_runs:
            raise ValueError(f"{src_path.name} is not a healthy wishlist database (quick_check: {'; '.join(check)[:200]}"
                             f"{'' if has_runs else ', no runs table'})")
        with lock():
            safety = backup(tag="before-restore", keep=None) if config.db_path().exists() else None
            dst = sqlite3.connect(str(config.db_path()), timeout=60)
            try:
                src.backup(dst)
            finally:
                dst.close()
    finally:
        src.close()
    alert("info", f"database restored from {src_path.name}" + (f"; the previous one is saved as {safety.name}" if safety else ""))
    return safety


# ---------------- plan, health ----------------

def stage_providers(stage: str) -> tuple[str, ...]:
    """The providers a stage spends on (schedule.STAGE_PROVIDERS); classify spends only on the anthropic backend."""
    if stage == "classify" and config.llm_backend() != "anthropic":
        return ()
    return tuple(schedule.STAGE_PROVIDERS.get(stage, ()))


def plan(conn: sqlite3.Connection, sched: dict | None = None, now: dt.datetime | None = None) -> dict:
    """What a scheduled run does now (reads only: free). stages: schedule.due's rows plus providers and action ('run',
    'capped' or 'not due'); run: the stages to run, in pipeline order; skipped: {stage: why} for the others; capped:
    {provider: why}; spend and caps: USD this month and the monthly caps per provider; next_run_at: the next daily trigger
    (local time)."""
    sched = sched or schedule.load()
    caps = schedule.capped(conn, sched, now)
    rows, run, skipped = schedule.due(conn, sched, now), [], {}
    for r in rows:
        r["providers"] = list(stage_providers(r["stage"]))
        hit = [p for p in r["providers"] if p in caps]
        if not r["due"]:
            r["action"], skipped[r["stage"]] = "not due", f"not due: {r['reason']}"
        elif hit:
            r["action"], skipped[r["stage"]] = "capped", f"monthly cap: {caps[hit[0]]}"
        else:
            r["action"] = "run"
            run.append(r["stage"])
    return {"at": _utc(now).strftime(TS), "daily_at": sched["daily_at"], "llm_backend": config.llm_backend(),
            "stages": rows, "run": run, "skipped": skipped, "capped": caps, "spend": schedule.month_spend(conn, now),
            "caps": dict(sched["budget"]["month_usd"]), "month_start": schedule.month_start(now),
            "next_run_at": schedule.next_run_at(sched, now).strftime("%Y-%m-%d %H:%M")}


def health(conn: sqlite3.Connection, sched: dict | None = None, now: dt.datetime | None = None) -> dict:
    """Healthy when the latest finished scheduled run (runs.notes starting with 'scheduled'; one still running does not
    count yet) finished within HEALTH_HOURS and none of its run_stages rows failed, and PRAGMA quick_check passes (it
    reads the whole file: seconds on a large database). A provider at its monthly cap is a warning.
    Returns {ok, problems: [str], warnings: [str], last_scheduled_run: {run_id, stage, notes, started_at, finished_at,
    cost_usd, stages: [...]} or None, checked_at}."""
    sched = sched or schedule.load()
    now = _utc(now)
    problems: list[str] = []
    last = db.one(conn, "SELECT run_id, stage, notes, started_at, finished_at, cost_usd FROM runs "
                        "WHERE notes LIKE 'scheduled%' AND finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1")
    finished = _parse(last["finished_at"]) if last else None
    if finished is None or now - finished > dt.timedelta(hours=HEALTH_HOURS):
        problems.append(f"no scheduled run in {HEALTH_HOURS} h (last: {last['finished_at'] if last else 'never'})")
    if last:
        order = {s: i for i, s in enumerate(schedule.STAGES)}
        last["stages"] = sorted(db.rows(conn, "SELECT stage, status, started_at, finished_at, seconds, cost_usd, error "
                                              "FROM run_stages WHERE run_id=?", (last["run_id"],)),
                                key=lambda s: order.get(s["stage"], len(order)))
        problems += [f"stage {s['stage']} failed in run {last['run_id']}: {s['error']}"
                     for s in last["stages"] if s["status"] == "failed"]
    warnings = [f"monthly cap reached: {why}" for why in schedule.capped(conn, sched, now).values()]
    try:
        check = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.DatabaseError as e:
        check = [str(e)]
    if check != ["ok"]:
        problems.append(f"database quick_check failed: {'; '.join(map(str, check))[:300]}")
    return {"ok": not problems, "problems": problems, "warnings": warnings, "last_scheduled_run": last,
            "checked_at": now.strftime(TS)}


# ---------------- the scheduled run ----------------

def run(now: dt.datetime | None = None) -> int:
    """One scheduled run (`ops run`, each cycle of `serve`). Returns the exit code; Stop (SIGTERM) and Ctrl-C propagate
    after the run is recorded."""
    try:
        with lock():
            path = LOGS_DIR / f"run-{dt.datetime.now():%Y%m%d-%H%M}.log"
            with _run_log(path):
                try:
                    return _scheduled(path, now)
                except (Stop, KeyboardInterrupt):
                    raise
                except Exception as e:
                    log.exception("scheduled run crashed: %s", e)
                    alert("error", f"scheduled run crashed: {type(e).__name__}: {e} (log: {_rel(path)})")
                    with contextlib.suppress(Exception):      # our own run, if it got that far (we still hold the lock)
                        c = db.connect()
                        close_abandoned(c, why=f"crashed: {type(e).__name__}: {e}")
                        c.close()
                    return EXIT_CRASH
                finally:
                    prune_logs()
    except Locked as e:
        log.warning("scheduled run skipped: %s", e)
        return EXIT_LOCKED
    except Exception as e:                                    # the lock or the log file could not be set up
        log.exception("scheduled run crashed before it started: %s", e)
        alert("error", f"scheduled run crashed before it started: {type(e).__name__}: {e}")
        return EXIT_CRASH


def _scheduled(log_path: Path, now: dt.datetime | None = None) -> int:
    if config.env("BUDGET_IGNORE_MONTH") == "1":
        log.warning("BUDGET_IGNORE_MONTH=1 is for a deliberate manual run: this scheduled run keeps the monthly caps")
        os.environ.pop("BUDGET_IGNORE_MONTH", None)
    sched = schedule.load()
    conn = db.connect()
    try:
        log.info("scheduled run: database %s, log %s", config.db_path(), _rel(log_path))
        dead = [r["run_id"] for r in close_abandoned(conn, now) if str(r["notes"] or "").startswith("scheduled")]
        if dead:
            alert("warning", f"scheduled run(s) {', '.join(map(str, dead))} abandoned (the process ended before they "
                             f"finished): closed, their unfinished stages marked failed")
        pl = plan(conn, sched, now)
        for r in pl["stages"]:
            log.info("plan %-8s %-7s %s", r["stage"], r["action"], _why(pl, r))
        _alert_caps(conn, sched, now)
        if not pl["run"]:
            run_id = db.new_run(conn, "", NOTHING_DUE)
            for st, why in pl["skipped"].items():
                pipeline.stage_row(conn, run_id, st, "skipped", error=why)
            db.finish_run(conn, run_id)
            log.info("scheduled run %s: nothing due", run_id)
            return EXIT_OK
        backup(conn)                                          # before anything writes
        capped = sorted({c for r in pl["stages"] if r["action"] == "capped" for c in r["providers"] if c in pl["capped"]})
        notes = f"scheduled: {len(pl['run'])} due" + (f", capped: {', '.join(capped)}" if capped else "")
        before = db.latest_run_id(conn) or 0
        try:
            res = pipeline.run(stage=",".join(pl["run"]), notes=notes, skipped=pl["skipped"])
        except (Stop, KeyboardInterrupt):
            with contextlib.suppress(Exception):              # pipeline.run recorded the interrupted stage: say so
                _report(conn, before, {}, log_path)
            raise
        code = _report(conn, before, res, log_path)
        _alert_caps(conn, sched)                              # a cap reached during this run
        return code
    finally:
        conn.close()


def _report(conn: sqlite3.Connection, before: int, res: dict, log_path: Path) -> int:
    """Log the scheduled run pipeline.run just recorded (the one after run `before`), alert on failed stages and budget
    stops, and return the exit code."""
    row = db.one(conn, "SELECT run_id, cost_usd FROM runs WHERE run_id > ? AND notes LIKE 'scheduled%' "
                       "ORDER BY run_id DESC LIMIT 1", (before,))
    if not row:
        raise RuntimeError("pipeline.run left no scheduled run behind")
    stages = db.rows(conn, "SELECT stage, status, error FROM run_stages WHERE run_id=?", (row["run_id"],))
    failed = [s for s in stages if s["status"] == "failed"]
    budget = [st for st, v in res.items() if isinstance(v, dict) and v.get("budget_stop")
              and st not in {s["stage"] for s in failed}]
    counts = Counter(s["status"] for s in stages)
    log.info("scheduled run %s done: %s, cost %s", row["run_id"], ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
             costs.fmt(row["cost_usd"] or 0))
    parts = [f"stage {s['stage']} failed: {str(s['error'] or '')[:300]}" for s in failed]
    if budget:
        parts.append(f"a budget stopped {', '.join(budget)} before it finished")
    if parts:
        alert("error" if failed else "warning", f"scheduled run {row['run_id']}: " + "; ".join(parts) + f" (log: {_rel(log_path)})")
    return EXIT_FAILED if parts else EXIT_OK


def serve() -> int:
    """Servers and containers: sleep until the next daily_at, do a scheduled run, repeat. config/schedule.yaml is re-read
    every cycle and every minute while sleeping, so a new daily_at applies without a restart. SIGTERM (or Ctrl-C) stops it:
    at once while sleeping; during a run, pipeline.run records the interrupted stage and closes the run first."""
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    log.info("serve: pid %s, database %s", os.getpid(), config.db_path())
    try:
        while True:
            sched = schedule.load()
            target = schedule.next_run_at(sched)
            log.info("serve: next scheduled run at %s (local time)", f"{target:%Y-%m-%d %H:%M}")
            while (left := (target - dt.datetime.now()).total_seconds()) > 0:
                time.sleep(min(left, 60))            # wall-clock checks: a machine that slept past the time runs on wake
                fresh = schedule.load()
                if fresh["daily_at"] != sched["daily_at"]:
                    sched, target = fresh, schedule.next_run_at(fresh)
                    log.info("serve: daily_at is now %s, next run at %s", fresh["daily_at"], f"{target:%Y-%m-%d %H:%M}")
            log.info("serve: scheduled run exited %s", run())
    except (Stop, KeyboardInterrupt) as e:
        log.info("serve: stopped (%s)", str(e) or type(e).__name__)
        return EXIT_OK
    finally:
        signal.signal(signal.SIGTERM, previous)


# ---------------- CLI ----------------

def _why(pl: dict, r: dict) -> str:
    return pl["capped"][next(p for p in r["providers"] if p in pl["capped"])] if r["action"] == "capped" else r["reason"]


def print_plan(pl: dict) -> None:
    classify = "paid (anthropic)" if pl["llm_backend"] == "anthropic" else f"free ({pl['llm_backend']})"
    print(f"Plan at {pl['at']}: config/schedule.yaml, daily at {pl['daily_at']} local time; classify is {classify}\n")
    print(f"  {'stage':9s} {'every':>5s}  {'last ok':17s} {'action':8s} why")
    for r in pl["stages"]:
        last = (r["last_ok"] or "never").replace("T", " ")[:16]
        print(f"  {r['stage']:9s} {r['every_days']:>5g}  {last:17s} {r['action']:8s} {_why(pl, r)}")
    print(f"\nWould run: {', '.join(pl['run']) or 'nothing (a run is still recorded: scheduled: nothing due)'}")
    spend = " · ".join(f"{p} ${pl['spend'].get(p, 0):.2f} of ${cap:.2f}" for p, cap in pl["caps"].items())
    print(f"Spend this month (since {pl['month_start'][:10]}): {spend}")
    print(f"At the monthly cap: {'; '.join(pl['capped'].values()) or 'none'}")
    print(f"Next trigger: {pl['next_run_at']} (local time)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ring_wishlist.ops", description="The scheduled run, its safety rails and health.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="one scheduled run now: the due stages (SPENDS, up to the caps)")
    p = sub.add_parser("plan", help="what `run` would do now, and why (free)")
    p.add_argument("--json", action="store_true")
    sub.add_parser("serve", help="loop: sleep until daily_at, run, repeat (servers and containers)")
    sub.add_parser("health", help="JSON; exit 0 healthy, 1 not")
    sub.add_parser("backup", help="back up the database now (data/backups, keeps the newest 14)")
    r = sub.add_parser("restore", help="copy a backup over the database (after a safety backup)")
    r.add_argument("file")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                        stream=sys.stdout if a.cmd in ("run", "serve", "backup", "restore") else sys.stderr)
    if a.cmd == "run":
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
        try:
            return run()
        except (Stop, KeyboardInterrupt):
            return EXIT_FAILED                       # the interrupted stage is recorded as failed
        finally:
            signal.signal(signal.SIGTERM, previous)
    if a.cmd == "serve":
        return serve()
    if a.cmd == "health":
        try:
            conn = db.connect()
            h = health(conn)
            conn.close()
        except Exception as e:  # noqa: BLE001 (health always answers)
            h = {"ok": False, "problems": [f"health check failed: {type(e).__name__}: {e}"], "warnings": [],
                 "last_scheduled_run": None, "checked_at": db.utcnow()}
        if h["last_scheduled_run"]:                   # the terminal gets {stage: status}; health() keeps the rows
            h["last_scheduled_run"]["stages"] = {s["stage"]: s["status"] for s in h["last_scheduled_run"]["stages"]}
        print(json.dumps(h, indent=2, default=str))
        return 0 if h["ok"] else 1
    if a.cmd == "plan":
        conn = db.connect()
        pl = plan(conn)
        conn.close()
        if a.json:
            print(json.dumps(pl, indent=2, default=str))
        else:
            print_plan(pl)
        return 0
    if a.cmd == "backup":
        print(backup())
        return 0
    try:
        safety = restore(a.file)
    except Locked as e:
        print(f"restore refused: {e}; try again when it finishes", file=sys.stderr)
        return EXIT_LOCKED
    except (FileNotFoundError, ValueError) as e:
        print(f"restore refused: {e}", file=sys.stderr)
        return 1
    print(f"restored {a.file} into {config.db_path()}" + (f" (previous database saved as {safety})" if safety else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
