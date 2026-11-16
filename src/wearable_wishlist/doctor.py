"""Check-up of an install: `python -m wearable_wishlist.doctor` (make doctor). docs/OPERATIONS.md says what to do on each line.

Offline and read-only: no network, no paid call, nothing written. The database is opened read-only, so the doctor is safe
next to a running pipeline or dashboard. One line per finding: ✓ ok, ! works but look at it, ✗ broken. Exit 1 on any ✗.

  python     Python 3.12 (3.10+ runs); every module of the package imports, with wearable_wishlist.ops, anthropic and streamlit
  env        .env present and private; the keys the configured sources and LLM backend need; SEED_X_HANDLE; budgets
  database   opens read-only, db.init has nothing left to do (schema migrated), PRAGMA quick_check
  map        data/raw, the read-only mirror of the operator's map (without it, ingest logs in with MAP_PASSPHRASE)
  config     schedule, weights, queries, competitor ties, handle overrides and contact routes parse
  disk       data/ writable, more than 1 GB free
  launchd    macOS only: the agents com.wearable-wishlist.wishlist and com.wearable-wishlist.dashboard, installed and loaded (launchctl list)
  dashboard  something answers on localhost:8501

Each check is a function returning [(status, text)], with its inputs as parameters so tests can point it elsewhere.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import pkgutil
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import config, db, llm

OK, WARN, FAIL = "✓", "!", "✗"
Line = tuple[str, str]

REQUIRED_MODULES = ("wearable_wishlist.ops", "anthropic", "streamlit")   # the scheduler; imported lazily by llm.py and the dashboard
SOURCES = ("monid", "x_api", "fixture")
SOURCE_KEYS = {"monid": "MONID_API_KEY", "x_api": "X_BEARER_TOKEN"}
BUDGET_VARS = ("BUDGET_MONID_USD", "BUDGET_X_API_USD", "BUDGET_X_USD", "BUDGET_ANTHROPIC_USD")
CONFIG_FILES = {  # file in config/ -> what happens without it
    "schedule.yaml": "the built-in cadence and caps apply",
    "weights.yaml": "the built-in weights apply",
    "queries.yaml": None,                                      # None: discover cannot run
    "competitor_ties.yaml": "no researched competitor ties: tied people can reach the list",
    "handle_overrides.yaml": "no curated handles for map contacts",
}
ROUTES_DIR = "contact_routes"
MIN_FREE_GB = 1.0
LAUNCHD_LABELS = ("com.wearable-wishlist.wishlist", "com.wearable-wishlist.dashboard")
DASHBOARD_PORT = 8501


def _rel(p: Path | str) -> str:
    try:
        return str(Path(p).resolve().relative_to(config.ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- python and imports

def check_python(version: tuple[int, ...] | None = None) -> list[Line]:
    v = tuple(version or sys.version_info[:3])
    ver = ".".join(str(x) for x in v)
    if v < (3, 10):
        out = [(FAIL, f"Python {ver}: needs 3.10 or newer, 3.12 tested (make setup)")]
    elif v < (3, 12):
        out = [(WARN, f"Python {ver}: runs, but 3.12 is the tested version")]
    else:
        out = [(OK, f"Python {ver}")]
    import wearable_wishlist
    names = [f"wearable_wishlist.{m.name}" for m in pkgutil.iter_modules(wearable_wishlist.__path__) if m.name != "doctor"]
    names += [m for m in REQUIRED_MODULES if m not in names]
    failed = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - any import error is the finding
            failed.append(f"{name} ({type(e).__name__}: {e})")
    if failed:
        out.append((FAIL, f"cannot import {'; '.join(failed)}. Run make setup (Docker: docker compose build)"))
    else:
        out.append((OK, f"the package imports: {len(names)} modules, {', '.join(REQUIRED_MODULES)} among them"))
    return out


# ---------------------------------------------------------------- .env and keys

def check_env(root: Path | str | None = None) -> list[Line]:
    """.env present and private, and the keys the configured sources and backend need. Values are never printed."""
    f = Path(root or config.ROOT) / ".env"
    if not f.exists():
        out = [(WARN, "no .env: only the process environment is read. Fine in Docker (env_file); "
                      "otherwise cp .env.example .env")]
    elif f.stat().st_mode & 0o077:
        out = [(WARN, ".env is readable by other users of this machine: chmod 600 .env")]
    else:
        out = [(OK, ".env present, readable by its owner only")]
    if config.dry_run():
        out.append((WARN, "DRY_RUN=1: every run uses the fixtures, nothing real is fetched or spent"))
    need: dict[str, list[str]] = {"SEED_X_HANDLE": ["crawl and paths"]}
    for var, default in (("SOURCE_BULK", "monid"), ("SOURCE_VERIFY", "x_api")):
        name = config.env(var, default)
        if name not in SOURCES:
            out.append((FAIL, f"{var}={name}: unknown source, use monid | x_api | fixture"))
        elif name == "fixture":
            out.append((WARN, f"{var}=fixture: canned test data, nothing real is fetched"))
        else:
            need.setdefault(SOURCE_KEYS[name], []).append(f"{var}={name}")
    backend = config.env("LLM_BACKEND", "claude_cli")
    if backend == "anthropic":
        need.setdefault("ANTHROPIC_API_KEY", []).append("LLM_BACKEND=anthropic")
    elif backend == "claude_cli":
        path = llm.cli_path()
        if os.path.isfile(path) and os.access(path, os.X_OK):
            out.append((OK, f"LLM_BACKEND=claude_cli: claude CLI at {path} (its login is not checked offline)"))
        else:
            out.append((FAIL, f"LLM_BACKEND=claude_cli but no claude CLI at {path}: install Claude Code, set "
                              "CLAUDE_CLI_PATH, or use LLM_BACKEND=anthropic with ANTHROPIC_API_KEY"))
    elif backend == "fixture":
        out.append((WARN, "LLM_BACKEND=fixture: canned classifications, nothing real is read"))
    else:
        out.append((FAIL, f"LLM_BACKEND={backend}: unknown backend, use claude_cli | anthropic"))
    for key, why in need.items():
        if config.env(key):
            out.append((OK, f"{key} set ({', '.join(why)})"))
        else:
            out.append((FAIL, f"{key} missing, needed by {', '.join(why)}: add it to .env"))
    for key in BUDGET_VARS:
        v = config.env(key)
        if v is not None and not _number(v):
            out.append((FAIL, f"{key}={v!r} is not a number: every paid stage stops on it"))
    if config.env("BUDGET_IGNORE_MONTH") == "1":
        out.append((WARN, "BUDGET_IGNORE_MONTH=1: the monthly caps of config/schedule.yaml are off; "
                          "set it for one run only, not in .env"))
    return out


# ---------------------------------------------------------------- database

def _schema(conn: sqlite3.Connection) -> set[str]:
    out = set()
    for kind, name in conn.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index') "
                                   "AND name NOT LIKE 'sqlite_%'").fetchall():
        out.add(f"{kind} {name}")
        if kind == "table":
            out |= {f"column {name}.{r[1]}" for r in conn.execute(f'PRAGMA table_info("{name}")').fetchall()}
    return out


def _pending(conn: sqlite3.Connection) -> list[str]:
    """What db.init would still add: tables, columns and indexes of a fresh database this one lacks."""
    ref = sqlite3.connect(":memory:")
    try:
        db.init(ref)
        missing = _schema(ref) - _schema(conn)
    finally:
        ref.close()
    tables = {m.split()[1] for m in missing if m.startswith("table ")}
    return sorted(m for m in missing if not (m.startswith("column ") and m.split()[1].split(".")[0] in tables))


def check_db(path: Path | str | None = None) -> list[Line]:
    """Opens the database read-only (never writes to a database in use): db.init must have nothing left to do, and
    PRAGMA quick_check must pass. A missing database is a fresh install."""
    p = Path(path or config.db_path())
    if not p.exists():
        ref = sqlite3.connect(":memory:")
        try:
            db.init(ref)
        except sqlite3.Error as e:
            return [(FAIL, f"db/schema.sql does not build a database: {e}")]
        finally:
            ref.close()
        return [(WARN, f"no database at {_rel(p)}: a fresh install. Restore a backup from the current machine or "
                       "build it with make run (docs/OPERATIONS.md, First run)")]
    out: list[Line] = []
    try:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            try:
                db.init(conn)                    # every statement is IF NOT EXISTS: a migrated database needs no write
            except sqlite3.OperationalError as e:
                if "readonly" not in str(e):
                    raise
                todo = _pending(conn)
                pending = (", ".join(todo[:6]) + (f" and {len(todo) - 6} more" if len(todo) > 6 else "")) or "a change"
                out.append((WARN, f"database schema behind ({pending}): the next pipeline run or dashboard start "
                                  "migrates it"))
            problems = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
            has_runs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
            last = conn.execute("SELECT MAX(run_id), MAX(finished_at) FROM runs").fetchone() if has_runs else None
        finally:
            conn.close()
    except sqlite3.Error as e:
        return [(FAIL, f"database {_rel(p)} does not open: {e}. Restore the latest backup (docs/OPERATIONS.md)")]
    if problems != ["ok"]:
        out.append((FAIL, f"database {_rel(p)}: quick_check found {len(problems)} problems ({problems[0]}). "
                          "Restore the latest backup (docs/OPERATIONS.md)"))
        return out
    runs = f"latest run #{last[0]}, finished {last[1] or 'not yet'}" if last and last[0] else "no runs yet"
    out.insert(0, (OK, f"database {_rel(p)}: {p.stat().st_size / 1e6:.0f} MB, quick_check ok, {runs}"
                       + ("" if out else ", schema up to date")))
    return out


# ---------------------------------------------------------------- map mirror

def check_map(raw_dir: Path | str | None = None) -> list[Line]:
    from .ingest_map import RAW_FILES
    d = Path(raw_dir or config.RAW_DIR)
    missing = [f for f in RAW_FILES.values() if not (d / f).exists()]
    if not missing:
        try:
            parsed = {k: json.loads((d / f).read_text(encoding="utf-8")) for k, f in RAW_FILES.items()}
        except (OSError, ValueError) as e:
            return [(FAIL, f"map mirror {_rel(d)} does not parse ({e}): copy data/raw again from the current machine")]
        exported = parsed["meta"].get("exported_at") if isinstance(parsed["meta"], dict) else None
        return [(OK, f"map mirror {_rel(d)}: {len(RAW_FILES)} files, exported {exported or 'at an unknown date'}")]
    what = f"map mirror {_rel(d)} is missing {', '.join(missing)}"
    if config.map_passphrase():
        return [(WARN, f"{what}: the next ingest logs in to {config.MAP_URL} with MAP_PASSPHRASE to rebuild it")]
    return [(FAIL, f"{what} and MAP_PASSPHRASE is not set: ingest fails and stops every run. Copy data/raw from the "
                   "current machine, or set MAP_PASSPHRASE")]


# ---------------------------------------------------------------- config files

def _number(v: Any) -> bool:
    try:
        float(v)
        return not isinstance(v, bool)
    except (TypeError, ValueError):
        return False


def _schedule_problems(raw: Mapping) -> list[str]:
    """What schedule.load would silently ignore, replace with a default, or misread."""
    from . import schedule
    out = []
    at = raw.get("daily_at")
    if isinstance(at, int) and not isinstance(at, bool):
        # YAML reads an unquoted 17:30 as the number 1050 (base 60), and time.fromisoformat("1050") is 10:50
        try:
            runs_at = dt.time.fromisoformat(str(at)).strftime("%H:%M")
        except ValueError:
            runs_at = schedule.DEFAULT_DAILY_AT
        meant = f"{at // 60:02d}:{at % 60:02d}" if 0 <= at < 1440 else "HH:MM"
        out.append(f'daily_at is the number {at}, so the run starts at {runs_at}: YAML reads an unquoted {meant} '
                   f'as a number. Quote it: daily_at: "{meant}"')
    elif at is not None:
        try:
            dt.time.fromisoformat(str(at))
        except ValueError:
            out.append(f"daily_at {at!r} is not HH:MM, so the run starts at {schedule.DEFAULT_DAILY_AT}")
    stages = raw.get("stages") or {}
    for k, v in stages.items() if isinstance(stages, Mapping) else ():
        if k not in schedule.STAGES:
            out.append(f"unknown stage {k!r} (ignored)")
        elif not _number(v):
            out.append(f"stages.{k} {v!r} is not a number (the default applies)")
    budget = raw.get("budget") if isinstance(raw.get("budget"), Mapping) else {}
    month = budget.get("month_usd") or {}
    for k, v in month.items() if isinstance(month, Mapping) else ():
        if k not in schedule.DEFAULT_MONTH_USD:
            out.append(f"unknown provider {k!r} in budget.month_usd (caps no provider)")
        elif not _number(v):
            out.append(f"budget.month_usd.{k} {v!r} is not a number (the default applies)")
    return out


def check_config(config_dir: Path | str | None = None) -> list[Line]:
    """Each file parses as YAML with a mapping at the top (the loaders fall back to defaults without a word when one
    does not, so a broken file changes the list silently)."""
    d = Path(config_dir or config.CONFIG_DIR)
    out: list[Line] = []
    good, good_routes = [], 0
    for name, without in CONFIG_FILES.items():
        p = d / name
        if not p.exists():
            out.append((WARN, f"config/{name} missing: {without}") if without else
                       (FAIL, f"config/{name} missing: discover cannot run"))
            continue
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            out.append((FAIL, f"config/{name} does not parse: {' '.join(str(e).split())}"))
            continue
        if not isinstance(raw, Mapping):
            out.append((FAIL, f"config/{name} is not a YAML mapping (key: value at the top)"))
            continue
        if name == "schedule.yaml":
            from . import schedule
            problems = _schedule_problems(raw)
            if problems:
                out.append((WARN, f"config/schedule.yaml: {'; '.join(problems)}"))
            s = schedule.load(p)
            caps = ", ".join(f"{k} ${v:g}" for k, v in s["budget"]["month_usd"].items())
            at = dt.time.fromisoformat(s["daily_at"]).strftime("%H:%M")
            out.append((OK, f"schedule: daily at {at} (machine time), monthly caps {caps}"))
        good.append(name)
    routes = d / ROUTES_DIR
    files = sorted([*routes.glob("*.yaml"), *routes.glob("*.yml")]) if routes.is_dir() else []
    if not files:
        out.append((WARN, f"config/{ROUTES_DIR} has no files: no researched contact routes"))
    for p in files:
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            out.append((FAIL, f"config/{ROUTES_DIR}/{p.name} does not parse: {' '.join(str(e).split())}. "
                              "Its people lose their researched routes"))
            continue
        items = raw.get("people") if isinstance(raw, Mapping) else raw
        if not isinstance(items, list):
            out.append((FAIL, f"config/{ROUTES_DIR}/{p.name} has no `people:` list: it is skipped"))
            continue
        good_routes += 1
    if good_routes:
        good.append(f"{ROUTES_DIR} ({good_routes} files)")
    if good:
        out.append((OK, f"config parses: {', '.join(good)}"))
    return out


# ---------------------------------------------------------------- disk

def check_disk(data_dir: Path | str | None = None, min_free_gb: float = MIN_FREE_GB) -> list[Line]:
    d = Path(data_dir or config.DATA_DIR)
    if not d.is_dir():
        return [(FAIL, f"{_rel(d)}/ is missing: the database, logs and backups live there (mkdir data)")]
    who = f"uid {os.getuid()}" if hasattr(os, "getuid") else "this user"
    targets = [d, *(d / s for s in ("exports", "logs", "backups", "cache", "raw"))]
    if data_dir is None:
        targets += [config.db_path(), config.db_path().parent]
    blocked = [_rel(p) for p in targets if p.exists() and not os.access(p, os.W_OK)]
    out: list[Line] = []
    if blocked:
        out.append((FAIL, f"not writable by {who}: {', '.join(dict.fromkeys(blocked))}"))
    else:
        out.append((OK, f"{_rel(d)}/ writable"))
    if data_dir is None and config.CONFIG_DIR.is_dir() and not os.access(config.CONFIG_DIR, os.W_OK):
        out.append((WARN, f"config/ not writable by {who}: the dashboard cannot save weights"))
    free = shutil.disk_usage(d).free / 1e9
    if free < min_free_gb:
        out.append((FAIL, f"disk: {free:.1f} GB free, under {min_free_gb:g} GB: runs and backups will fail"))
    else:
        out.append((OK, f"disk: {free:.0f} GB free" if free >= 10 else f"disk: {free:.1f} GB free"))
    return out


# ---------------------------------------------------------------- launchd (macOS)

def check_launchd(listing: str | None = None, agents_dir: Path | str | None = None,
                  system: str | None = None) -> list[Line]:
    """Informational, macOS only: which agents are installed (~/Library/LaunchAgents) and loaded (launchctl list)."""
    if (system or platform.system()) != "Darwin":
        return []
    if listing is None:
        try:
            listing = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as e:
            return [(WARN, f"launchctl list failed ({e}): cannot tell whether the schedule is on")]
    loaded = {}
    for row in listing.splitlines():
        parts = row.split()
        if len(parts) == 3:
            loaded[parts[2]] = (parts[0], parts[1])        # pid ('-' when not running), last exit status
    d = Path(agents_dir) if agents_dir else Path.home() / "Library" / "LaunchAgents"
    out: list[Line] = []
    for label in LAUNCHD_LABELS:
        if label in loaded:
            pid, status = loaded[label]
            if status not in ("0", "-"):
                out.append((WARN, f"launchd {label}: loaded, last exit {status} (see data/logs)"))
            else:
                out.append((OK, f"launchd {label}: loaded" + (f", running (pid {pid})" if pid != "-" else "")))
        elif (d / f"{label}.plist").exists():
            out.append((WARN, f"launchd {label}: installed, not loaded (make schedule-on)"))
        else:
            out.append((WARN, f"launchd {label}: not installed (make schedule, then make schedule-on)"))
    return out


# ---------------------------------------------------------------- dashboard

def check_dashboard(port: int = DASHBOARD_PORT, host: str = "localhost", timeout: float = 0.5) -> list[Line]:
    """Informational: a socket connect on this machine, no HTTP request."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return [(OK, f"something answers on {host}:{port} (the dashboard)")]
    except OSError:
        return [(WARN, f"nothing answers on {host}:{port}: the dashboard is not running here "
                       "(make dashboard, the com.wearable-wishlist.dashboard agent, or docker compose up -d)")]


# ---------------------------------------------------------------- main

CHECKS = (check_python, check_env, check_db, check_map, check_config, check_disk, check_launchd, check_dashboard)


def run_checks() -> list[Line]:
    out: list[Line] = []
    for check in CHECKS:
        try:
            out += check()
        except Exception as e:  # noqa: BLE001 - a check that crashes is a finding, not a traceback
            out.append((FAIL, f"{check.__name__} crashed: {type(e).__name__}: {e}"))
    return out


def main(argv: list[str] | None = None) -> int:
    lines = run_checks()
    print(f"wearable_wishlist doctor: {config.ROOT}")
    for status, text in lines:
        print(f"{status} {text}")
    n = {s: sum(1 for x, _ in lines if x == s) for s in (OK, WARN, FAIL)}
    print(f"{n[OK]} ok, {n[WARN]} to look at, {n[FAIL]} failing. Fixes: docs/OPERATIONS.md")
    return 1 if n[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
