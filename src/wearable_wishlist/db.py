"""SQLite access. One connection per process; schema in db/schema.sql (idempotent)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA_PATH = config.ROOT / "db" / "schema.sql"


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=60)   # stages may run side by side (e.g. discover while expand runs)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    init(conn)
    return conn


# Columns added after the first release: CREATE TABLE IF NOT EXISTS does not add them to an existing database.
MIGRATIONS = [("scores", "affinity", "REAL"), ("scores", "engagement", "REAL")]


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def upsert(conn: sqlite3.Connection, table: str, row: dict, keys: tuple[str, ...]) -> None:
    """INSERT ... ON CONFLICT(keys) DO UPDATE for the non-key columns present in `row`."""
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = [c for c in cols if c not in keys]
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    if updates:
        sql += f" ON CONFLICT({','.join(keys)}) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in updates)
    else:
        sql += f" ON CONFLICT({','.join(keys)}) DO NOTHING"
    conn.execute(sql, tuple(row[c] for c in cols))


def new_run(conn: sqlite3.Connection, stage: str = "all", notes: str = "") -> int:
    cur = conn.execute("INSERT INTO runs (started_at, stage, notes) VALUES (?,?,?)", (utcnow(), stage, notes))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int) -> None:
    cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE run_id=?", (run_id,)).fetchone()[0]
    conn.execute("UPDATE runs SET finished_at=?, cost_usd=? WHERE run_id=?", (utcnow(), cost, run_id))
    conn.commit()


def latest_run_id(conn: sqlite3.Connection, with_scores: bool = False) -> int | None:
    if with_scores:
        r = conn.execute("SELECT MAX(run_id) FROM scores").fetchone()
    else:
        r = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()
    return int(r[0]) if r and r[0] is not None else None


def previous_scored_run_id(conn: sqlite3.Connection, run_id: int) -> int | None:
    r = conn.execute("SELECT MAX(run_id) FROM scores WHERE run_id<?", (run_id,)).fetchone()
    return int(r[0]) if r and r[0] is not None else None


def jdumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def jloads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default
