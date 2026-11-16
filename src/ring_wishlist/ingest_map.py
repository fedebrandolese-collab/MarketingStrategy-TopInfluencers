"""Workstream A: mirror Carlo's network map (network.ri.fo) into SQLite.

    fetch_map(passphrase, url, session=None)  log in (POST /login, form field `p`), GET /api/graph?group=capital|press
                                              and GET /api/timeline?id=<id> for every person; returns the payload
    save_raw(payload) / load_raw()            the four data/raw/map_*.json files, same files and shapes as the export
    run_ingest(conn, run_id, from_raw=True)   upsert map_people, map_edges, map_competitor_funds, map_linkedin, map_monthly
    stats(conn)                               vocabularies and counts (pasted in the README)

CLI: `python -m ring_wishlist.ingest_map` (from data/raw) or `python -m ring_wishlist.ingest_map --refresh` (login with MAP_PASSPHRASE,
save data/raw, then ingest). Everything is read-only against the site: one POST to log in, then GETs. Under DRY_RUN=1
fetch_map refuses to open a real HTTP session (tests inject a fake one).
Idempotent: every table is upserted on its primary key, so re-running never duplicates rows.

Known quirk of the export: a person id can appear in both groups (id 167, an angel who also connects to press).
map_people.id is the primary key, so that person is stored once, under the first group seen (capital), with the tags
of both records unioned; the press-group record stays in data/raw/map_press_people.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
from collections import Counter
from pathlib import Path

import requests

from . import config, db

log = logging.getLogger("ring_wishlist.ingest_map")

GROUPS = ("capital", "press")
RAW_FILES = {
    "capital": "map_capital_people.json",
    "press": "map_press_people.json",
    "meta": "map_graph_meta.json",
    "timelines": "map_timelines.json",
}
META_KEYS = ("competitor_funds", "counts", "edges", "monthly")   # what map_graph_meta.json keeps per group
PEOPLE_FIELDS = ("name", "company", "role", "sector", "tags", "warmth", "intros_made", "intros_got", "interactions",
                 "sent", "received", "meetings", "channels", "first_contact", "last_contact")
LINKEDIN_FIELDS = ("company", "position", "connected_on", "profile_url")
TRACKED_TAGS = ("x_following", "x_follower", "competitor-investor", "fresh-fund")


# ---------------------------------------------------------------- live site ----------------------------------------------------------------

def fetch_map(passphrase: str, url: str = config.MAP_URL, session=None, timeout: float = 30.0) -> dict:
    """Log in to the map and pull both groups plus one timeline per person.

    `session` is anything with requests-like `.post(url, data=...)` / `.get(url, params=...)` (tests inject a fake);
    by default a fresh `requests.Session()`, which keeps the HttpOnly session cookie the server sets on login.
    Returns {"capital": {group, people, edges, monthly, counts, competitor_funds}, "press": {...},
             "timelines": {str(id): {enrich, events, linkedin, summary}}, "exported_at": iso}.
    """
    if not passphrase:
        raise RuntimeError("MAP_PASSPHRASE missing: set it in .env (or pass passphrase=) to fetch the map")
    if session is None and config.dry_run():   # tests and dry runs never open a real HTTP session
        raise RuntimeError("DRY_RUN=1: refusing to contact the map (no network in dry runs); inject a session or unset DRY_RUN")
    base = (url or config.MAP_URL).rstrip("/")
    s = session or requests.Session()

    log.info("map: logging in to %s", base)
    r = s.post(f"{base}/login", data={"p": passphrase}, timeout=timeout, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"map login failed: HTTP {r.status_code} from {base}/login (wrong passphrase?)")

    payload: dict = {}
    for group in GROUPS:
        graph = _get_json(s, f"{base}/api/graph", {"group": group}, timeout, what=f"graph group={group}")
        if not isinstance(graph, dict) or "people" not in graph:
            raise RuntimeError(f"map graph group={group}: unexpected response {str(graph)[:120]!r} (login failed?)")
        payload[group] = graph
        log.info("map: group=%s people=%d edges=%d", group, len(graph.get("people") or []), len(graph.get("edges") or []))

    ids = sorted({int(p["id"]) for g in GROUPS for p in payload[g].get("people") or []})
    timelines: dict[str, dict] = {}
    for i, pid in enumerate(ids, 1):
        timelines[str(pid)] = _get_json(s, f"{base}/api/timeline", {"id": pid}, timeout, what=f"timeline id={pid}")
        if i % 50 == 0 or i == len(ids):
            log.info("map: timelines %d/%d", i, len(ids))
    payload["timelines"] = timelines
    payload["exported_at"] = _iso_now_ms()
    return payload


def _get_json(session, url: str, params: dict, timeout: float, what: str):
    r = session.get(url, params=params, timeout=timeout)
    if r.status_code in (401, 403):
        raise RuntimeError(f"map {what}: HTTP {r.status_code}, not logged in (wrong passphrase?)")
    if r.status_code >= 400:
        raise RuntimeError(f"map {what}: HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:   # requests' JSONDecodeError subclasses ValueError; HTML login page -> not logged in
        raise RuntimeError(f"map {what}: response is not JSON (login page served instead? wrong passphrase?)") from e


def _iso_now_ms() -> str:
    """Same style as the browser export: 2026-09-29T09:51:56.449Z."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- raw files ----------------------------------------------------------------

def raw_available(raw_dir: Path | str | None = None) -> bool:
    d = Path(raw_dir or config.RAW_DIR)
    return all((d / f).exists() for f in RAW_FILES.values())


def save_raw(payload: dict, raw_dir: Path | str | None = None) -> list[Path]:
    """Write the four raw files exactly like the existing export (indent=1, ASCII-escaped, sorted keys inside records,
    no trailing newline): people lists as top-level arrays, map_graph_meta.json = {exported_at, capital: {competitor_funds,
    counts, edges, monthly}, press: {...}}, map_timelines.json = {str(id): timeline} in ascending id order.
    Round-trip safe: save_raw(load_raw()) reproduces the files byte for byte."""
    d = Path(raw_dir or config.RAW_DIR)
    d.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    meta: dict = {"exported_at": payload.get("exported_at") or _iso_now_ms()}
    for group in GROUPS:
        g = payload.get(group) or {}
        written.append(_write_json(d / RAW_FILES[group], [_deep_sorted(p) for p in g.get("people") or []]))
        meta[group] = _deep_sorted({k: g.get(k) for k in META_KEYS})
    written.append(_write_json(d / RAW_FILES["meta"], meta))
    tl = payload.get("timelines") or {}
    written.append(_write_json(d / RAW_FILES["timelines"],
                               {str(k): _deep_sorted(tl[k]) for k in sorted(tl, key=lambda x: int(x))}))
    log.info("map: wrote %d raw files to %s", len(written), d)
    return written


def load_raw(raw_dir: Path | str | None = None) -> dict:
    """Read the four raw files back into the fetch_map payload shape."""
    d = Path(raw_dir or config.RAW_DIR)
    missing = [f for f in RAW_FILES.values() if not (d / f).exists()]
    if missing:
        raise FileNotFoundError(f"raw map export incomplete in {d}: missing {', '.join(missing)} "
                                f"(set MAP_PASSPHRASE in .env and run `python -m ring_wishlist.ingest_map --refresh`)")
    meta = _read_json(d / RAW_FILES["meta"])
    payload: dict = {"exported_at": meta.get("exported_at")}
    for group in GROUPS:
        payload[group] = {"group": group, "people": _read_json(d / RAW_FILES[group]), **(meta.get(group) or {})}
    payload["timelines"] = _read_json(d / RAW_FILES["timelines"])
    return payload


def _deep_sorted(obj):
    """Normalise for the export format: dict keys sorted recursively (the site serves them sorted and the export kept
    them) and integral floats written as ints (the API serves warmth 0.0, the browser export printed 0)."""
    if isinstance(obj, dict):
        return {k: _deep_sorted(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_deep_sorted(x) for x in obj]
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    return obj


def _write_json(path: Path, obj) -> Path:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")   # ensure_ascii=True and no trailing newline, as exported
    os.replace(tmp, path)
    return path


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- ingestion ----------------------------------------------------------------

def run_ingest(conn: sqlite3.Connection, run_id: int | None = None, from_raw: bool = True) -> dict:
    """Mirror the map into SQLite. from_raw=True reads data/raw (falls back to a live fetch when the files are missing
    and MAP_PASSPHRASE is set); from_raw=False logs in, refreshes data/raw, then ingests. Returns counts."""
    if from_raw and raw_available():
        payload = load_raw()
        origin = f"raw:{config.RAW_DIR}"
    else:
        passphrase = config.map_passphrase()
        if not passphrase:
            if from_raw:
                raise FileNotFoundError(f"no raw map export in {config.RAW_DIR} and MAP_PASSPHRASE is not set: "
                                        "add it to .env and run `python -m ring_wishlist.ingest_map --refresh`")
            raise RuntimeError("MAP_PASSPHRASE missing: set it in .env to refresh the map")
        payload = fetch_map(passphrase)
        save_raw(payload)
        origin = f"live:{config.MAP_URL}"
    log.info("map: ingesting from %s (exported_at %s)", origin, payload.get("exported_at"))
    counts = ingest_payload(conn, payload, run_id)
    counts["origin"] = origin
    return counts


def ingest_payload(conn: sqlite3.Connection, payload: dict, run_id: int | None = None) -> dict:
    """Upsert one fetch_map/load_raw payload. Pure function of the payload: safe to call any number of times."""
    now = db.utcnow()
    counts: dict = {"run_id": run_id, "exported_at": payload.get("exported_at"),
                    "people_capital": 0, "people_press": 0, "people": 0, "people_in_both_groups": [],
                    "edges": 0, "competitor_funds": 0, "linkedin": 0, "monthly": 0, "stale_people": 0}
    seen: dict[int, str] = {}
    funds: dict[tuple[str, str], str | None] = {}

    for group in GROUPS:
        g = payload.get(group) or {}
        for p in g.get("people") or []:
            pid = int(p["id"])
            counts[f"people_{group}"] += 1
            if pid in seen:   # same id in both graphs: keep the first group's row, union the tags
                counts["people_in_both_groups"].append(pid)
                _union_tags(conn, pid, p.get("tags"))
                log.debug("map: id %s (%s) is in both %s and %s; stored as %s", pid, p.get("name"), seen[pid], group, seen[pid])
                continue
            seen[pid] = group
            row = {"id": pid, "group_name": group, **{f: p.get(f) for f in PEOPLE_FIELDS}, "imported_at": now}
            db.upsert(conn, "map_people", row, keys=("id",))
        for e in g.get("edges") or []:
            db.upsert(conn, "map_edges", {"from_id": int(e["from_id"]), "to_id": int(e["to_id"]),
                                          "source": e.get("source") or "", "group_name": group},
                      keys=("from_id", "to_id", "source", "group_name"))
            counts["edges"] += 1
        for m in g.get("monthly") or []:
            db.upsert(conn, "map_monthly", {"group_name": group, "month": m.get("month") or "", "source": m.get("source") or "",
                                            "n": m.get("n")}, keys=("group_name", "month", "source"))
            counts["monthly"] += 1
        for f in g.get("competitor_funds") or []:   # the same list is served with both groups
            funds.setdefault((f["fund"], f["competitor"]), f.get("round"))

    for (fund, competitor), rnd in funds.items():
        db.upsert(conn, "map_competitor_funds", {"fund": fund, "competitor": competitor, "round": rnd}, keys=("fund", "competitor"))
    counts["competitor_funds"] = len(funds)

    for pid, tl in (payload.get("timelines") or {}).items():
        li = (tl or {}).get("linkedin")
        if not li:   # null linkedin = not connected on LinkedIn; store nothing
            continue
        db.upsert(conn, "map_linkedin", {"person_id": int(pid), **{f: li.get(f) for f in LINKEDIN_FIELDS}}, keys=("person_id",))
        counts["linkedin"] += 1

    counts["people"] = len(seen)
    if seen:   # people in the DB that the map no longer lists: reported, never deleted (candidates may reference them)
        counts["stale_people"] = sum(1 for r in db.rows(conn, "SELECT id FROM map_people") if r["id"] not in seen)
    conn.commit()
    log.info("map: %d capital + %d press people (%d rows), %d edges, %d competitor funds, %d linkedin, %d monthly",
             counts["people_capital"], counts["people_press"], counts["people"], counts["edges"],
             counts["competitor_funds"], counts["linkedin"], counts["monthly"])
    return counts


def _union_tags(conn: sqlite3.Connection, pid: int, extra: str | None) -> None:
    cur = db.one(conn, "SELECT tags FROM map_people WHERE id=?", (pid,)) or {}
    merged: list[str] = []
    for t in _split(cur.get("tags")) + _split(extra):
        if t not in merged:
            merged.append(t)
    conn.execute("UPDATE map_people SET tags=? WHERE id=?", (",".join(merged) or None, pid))


def _split(csv: str | None) -> list[str]:
    return [t.strip() for t in (csv or "").split(",") if t.strip()]


# ---------------------------------------------------------------- stats ----------------------------------------------------------------

def stats(conn: sqlite3.Connection) -> dict:
    """Vocabularies and counts of the mirrored map (for the README and the dashboard)."""
    people = db.rows(conn, "SELECT * FROM map_people")

    def vocab(col: str) -> dict[str, int]:
        c: Counter = Counter()
        for p in people:
            c.update(_split(p.get(col)))
        return dict(c.most_common())

    tags = vocab("tags")
    warmths = [p["warmth"] for p in people if p["warmth"] is not None]
    edges = db.rows(conn, "SELECT source, COUNT(*) AS n FROM map_edges GROUP BY source ORDER BY n DESC, source")
    funds = db.rows(conn, "SELECT competitor, COUNT(*) AS n FROM map_competitor_funds GROUP BY competitor ORDER BY n DESC, competitor")
    months = db.one(conn, "SELECT COUNT(*) AS n, MIN(month) AS lo, MAX(month) AS hi FROM map_monthly") or {}
    return {
        "people": {"total": len(people),
                   **{g: sum(1 for p in people if p["group_name"] == g) for g in GROUPS},
                   "with_last_contact": sum(1 for p in people if p["last_contact"])},
        "sectors": dict(Counter(p["sector"] for p in people if p["sector"]).most_common()),
        "tags": tags,
        "channels": vocab("channels"),
        "edges": {"total": sum(e["n"] for e in edges), "by_source": {e["source"]: e["n"] for e in edges}},
        "competitor_funds": {"total": sum(f["n"] for f in funds), "by_competitor": {f["competitor"]: f["n"] for f in funds}},
        "linkedin_connections": (db.one(conn, "SELECT COUNT(*) AS n FROM map_linkedin") or {}).get("n", 0),
        "monthly": {"rows": months.get("n", 0), "from": months.get("lo"), "to": months.get("hi")},
        "warmth": {"min": min(warmths) if warmths else None, "max": max(warmths) if warmths else None,
                   "people_above_zero": sum(1 for w in warmths if w > 0)},
        "tagged": {t: tags.get(t, 0) for t in TRACKED_TAGS},
        "last_import": max((p["imported_at"] for p in people if p["imported_at"]), default=None),
    }


def summary_line(counts: dict) -> str:
    both = counts.get("people_in_both_groups") or []
    both_s = f", {len(both)} in both groups" if both else ""
    stale_s = f", {counts['stale_people']} stale" if counts.get("stale_people") else ""
    return (f"[ingest_map] run {counts.get('run_id')}: {counts['people_capital']} capital + {counts['people_press']} press people "
            f"({counts['people']} rows{both_s}{stale_s}), {counts['edges']} edges, {counts['competitor_funds']} competitor funds, "
            f"{counts['linkedin']} linkedin, {counts['monthly']} monthly; {counts.get('origin', '')} exported_at {counts.get('exported_at')}")


# ---------------------------------------------------------------- CLI ----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ring_wishlist.ingest_map", description="Mirror Carlo's network map into SQLite.")
    ap.add_argument("--refresh", action="store_true", help="log in with MAP_PASSPHRASE, rewrite data/raw, then ingest")
    ap.add_argument("--db", default=None, help="SQLite path (default: DB_PATH from .env or data/wishlist.db)")
    ap.add_argument("--no-stats", action="store_true", help="print only the one-line summary")
    args = ap.parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    conn = db.connect(args.db)
    run_id = db.new_run(conn, "ingest", "ingest_map --refresh" if args.refresh else "ingest_map")
    counts = run_ingest(conn, run_id, from_raw=not args.refresh)
    db.finish_run(conn, run_id)
    print(summary_line(counts))
    if not args.no_stats:
        print(json.dumps(stats(conn), indent=2, ensure_ascii=False))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
