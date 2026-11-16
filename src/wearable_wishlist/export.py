"""Export the wishlist for a run: wishlist.json (the map's wishlist tab shape), wishlist.csv (flat), changes.csv.

Row shape (docs/ARCHITECTURE.md, section F): n, name, role, section, handle, score, p_accept, p_wear, p_love,
impact, reach, confidence, flags, status, summary, reasons, evidence[{kind,supports,url,date,text}],
paths[{how,you,via,warmth,target,strength}]; extra keys (candidate_id, category, x_url, best_path...) are ignored
by the renderer. The renderer's path keys are always present and typed: how/you/via/target are strings ("" when a kind
has no connector or target, never null), warmth is an int 0..3. People with status 'rejected' are left out. Rows are ranked by scores.rank for the run (n = 1..N);
candidates without a score come last with score null.

wishlist.json holds only the wishlist: people who meet the brief's bar (a person category, net positive evidence that
they wear a device or use a voice tool, and a real way to contact them: weights.hidden_by, the dashboard's own filter
source), not flagged not_us and not rejected, renumbered n = 1..k. Each item has contact: {best, routes: [{type, detail,
source_url}]} (contacts.py, best first). wishlist.csv keeps every row, with contact (the best route), contactable,
meets_bar and bar_reason ('' when met; it names the missing contact route too).
Community engagement (scores.engagement and the reasons_json factor "engagement", engagement.py): each item has
engagement: {value, short, followers, per_post, rates, vs_size, index, n, first, last}, null when unknown (no sample of
their latest posts, too few recent ones, or a run scored before it existed); wishlist.csv flattens it into engagement,
er, er_vs_size (the index), <count>_per_post and <count>_rate for views, likes, replies and reposts.
When the run has no scores/paths of its own (e.g. `--stage export` alone) the latest run that has them is used.

CLI: python -m wearable_wishlist.export [--run N] [--out DIR]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import config, contacts, db, weights

log = logging.getLogger("wearable_wishlist.export")

CSV_COLUMNS = ["n", "rank", "name", "handle", "section", "category", "score", "p_accept", "p_wear", "p_love", "impact",
               "engagement", "er", "er_vs_size", "views_per_post", "likes_per_post", "replies_per_post", "reposts_per_post",
               "view_rate", "like_rate", "reply_rate", "repost_rate",
               "reach", "confidence", "flags", "status", "best_path_how", "best_path", "best_path_strength", "summary",
               "competitor_detail", "us_based", "map_person_id", "x_url", "linkedin_url", "evidence_count", "reasons",
               "contact", "contactable", "meets_bar", "bar_reason"]
# wishlist.csv column -> (key of the engagement object, count): er is the average post's rate per follower
ENGAGEMENT_CSV = {"er": ("rates", "engagement"), "views_per_post": ("per_post", "views"),
                  "likes_per_post": ("per_post", "likes"), "replies_per_post": ("per_post", "replies"),
                  "reposts_per_post": ("per_post", "reposts"), "view_rate": ("rates", "views"),
                  "like_rate": ("rates", "likes"), "reply_rate": ("rates", "replies"), "repost_rate": ("rates", "reposts")}
WISHLIST_FILTERS = {"us_only": True, "hide_competitor": False, "hide_rejected": True, "bar": True}   # wishlist.json
CHANGES_COLUMNS = ["change_id", "run_id", "candidate_id", "name", "handle", "field", "old_value", "new_value", "reason"]


def fmt_count(n: int | None) -> str | None:
    if n is None:
        return None
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}k"
    if n >= 1_000:
        return f"{n / 1e3:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def first_clause(text: str | None, limit: int = 60) -> str | None:
    if not text:
        return None
    first = next((c.strip() for c in re.split(r"[.|·•\n]|\s[-–—]\s", text) if c.strip()), "")
    if len(first) <= limit:
        return first or None
    cut = first[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def role_text(cand: dict, mp: dict | None, xu: dict | None) -> str:
    parts = []
    if mp:
        parts.append(" · ".join(x for x in (mp.get("role"), mp.get("company")) if x)[:80])
    elif xu and xu.get("bio"):
        parts.append(first_clause(xu["bio"]))
    if xu and xu.get("followers") is not None:
        parts.append(f"{fmt_count(xu['followers'])} on X")
    return " · ".join(p for p in parts if p) or config.SECTIONS.get(cand.get("category") or "", "Other")


def parse_reasons(s: str | None) -> list[str]:
    obj = db.jloads(s, None)
    if isinstance(obj, list):
        return [str(x) for x in obj]
    if isinstance(obj, dict):
        if isinstance(obj.get("reasons"), list):
            return [str(x) for x in obj["reasons"]]
        out = []
        for k, v in obj.items():
            if k in ("evidence_ids", "factors"):
                continue
            if isinstance(v, str):
                out.append(f"{k}: {v}")
            elif isinstance(v, dict) and isinstance(v.get("reason"), str):
                out.append(f"{k}: {v['reason']}")
            elif isinstance(v, list):
                out.extend(str(x) for x in v if isinstance(x, str))
        return out
    return []


def engagement_factor(stored: Any) -> dict:
    """The community engagement factor of a reasons_json, parsed or not: {"value", "short", "reasons", "kpis"}.
    {} for runs scored before it existed."""
    obj = db.jloads(stored, None) if isinstance(stored, str) else stored
    factors = obj.get("factors") if isinstance(obj, dict) and isinstance(obj.get("factors"), dict) else {}
    fac = factors.get("engagement")
    return fac if isinstance(fac, dict) else {}


def engagement_json(value: Any, fac: dict | None) -> dict | None:
    """wishlist.json's engagement object: value (scores.engagement, else the factor's), short, and from the KPIs followers,
    per_post, rates, vs_size, index (x peers), n, first, last. None when the value is unknown."""
    fac = fac or {}
    try:
        v = float(value if value is not None else fac.get("value"))
    except (TypeError, ValueError):
        return None
    if v != v:   # NaN
        return None
    k = fac.get("kpis") if isinstance(fac.get("kpis"), dict) else {}
    return {"value": v, "short": fac.get("short") or k.get("short") or "unknown",
            **{key: k.get(key) for key in ("followers", "per_post", "rates", "vs_size", "index", "n", "first", "last")}}


def best_path_words(p: dict | None) -> str:
    if not p:
        return "none"
    how = p.get("how")
    if how == "direct":
        return f"direct: {p.get('you') or ''}".strip()
    if how == "via":
        return f"via {p.get('via')}: {p.get('target') or ''} (you: {p.get('you') or ''})"
    if how == "x_direct":
        return f"X: {p.get('you') or ''}"
    if how == "open_dm":
        return "DMs open"
    return "none"


def render_path(obj: dict) -> dict:
    """A stored path object with the map renderer's keys guaranteed: strings for how/you/via/target, warmth 0..3."""
    out = dict(obj)
    for k in ("how", "you", "via", "target"):
        out[k] = "" if obj.get(k) is None else str(obj[k])
    try:
        out["warmth"] = max(0, min(3, int(obj.get("warmth") or 0)))
    except (TypeError, ValueError):
        out["warmth"] = 0
    out["strength"] = None if obj.get("strength") is None else round(float(obj["strength"]), 3)
    return out


def _pick_run(conn: sqlite3.Connection, table: str, run_id: int) -> int | None:
    if db.one(conn, f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1", (run_id,)):
        return run_id
    r = db.one(conn, f"SELECT MAX(run_id) AS r FROM {table} WHERE run_id<=?", (run_id,))
    return int(r["r"]) if r and r["r"] is not None else None


def build_rows(conn: sqlite3.Connection, score_run: int | None, path_run: int | None) -> list[dict]:
    cands = db.rows(conn,
                    "SELECT c.*, s.p_accept, s.p_wear, s.p_love, s.impact, s.engagement, s.reach, s.confidence, s.total, "
                    "s.rank, s.flags AS flags_json, s.reasons_json FROM candidates c "
                    "LEFT JOIN scores s ON s.candidate_id=c.candidate_id AND s.run_id=? "
                    "ORDER BY (s.rank IS NULL), s.rank, s.total DESC, c.name", (score_run if score_run is not None else -1,))
    people = {p["id"]: p for p in db.rows(conn, "SELECT * FROM map_people")}
    x_users = {u["x_id"]: u for u in db.rows(conn, "SELECT x_id, handle, bio, followers, can_dm FROM x_users")}
    status = {s["candidate_id"]: s for s in db.rows(conn, "SELECT * FROM status")}
    summaries: dict[int, dict] = {}
    for c in db.rows(conn, "SELECT candidate_id, json FROM classifications WHERE run_id<=? ORDER BY run_id",
                     (score_run if score_run is not None else 10**9,)):
        summaries[c["candidate_id"]] = db.jloads(c["json"], {}) or {}
    evidence: dict[int, list] = defaultdict(list)
    for e in db.rows(conn, "SELECT candidate_id, kind, supports, polarity, weight, url, date, text FROM evidence "
                           "ORDER BY weight DESC, date DESC"):
        if len(evidence[e["candidate_id"]]) < 25:
            evidence[e["candidate_id"]].append({"kind": e["kind"], "supports": e["supports"], "polarity": e["polarity"],
                                                "weight": e["weight"], "url": e["url"], "date": e["date"], "text": e["text"]})
    paths: dict[int, list] = defaultdict(list)
    if path_run is not None:
        for p in db.rows(conn, "SELECT candidate_id, json, how, strength, via_name, you_rel, target_rel FROM paths "
                               "WHERE run_id=? ORDER BY candidate_id, rank", (path_run,)):
            obj = db.jloads(p["json"], None) or {"how": p["how"], "you": p["you_rel"], "via": p["via_name"], "warmth": 0,
                                                 "target": p["target_rel"], "strength": p["strength"]}
            paths[p["candidate_id"]].append(render_path(obj))

    research = contacts.load_routes()
    rows = []
    cands = [c for c in cands if (status.get(c["candidate_id"]) or {}).get("status") != "rejected"]   # rejected by hand
    cands.sort(key=lambda c: (c.get("rank") is None, c.get("rank") if c.get("rank") is not None else 0,
                              -(c.get("total") or 0), (c.get("name") or c.get("handle") or "").lower()))
    for i, c in enumerate(cands, 1):
        cid = c["candidate_id"]
        mp = people.get(c["map_person_id"]) if c.get("map_person_id") else None
        xu = x_users.get(c["x_id"]) if c.get("x_id") else None
        cls = summaries.get(cid) or {}
        summary = contacts.strip_reach(cls.get("summary")) or None   # how to contact is the `contact` field
        if not summary and mp:
            summary = f"In the operator's map: {mp.get('role') or mp.get('sector') or '?'} at {mp.get('company') or '?'}; not classified yet."
        cpaths = paths.get(cid) or []
        name = c.get("name") or c.get("handle") or f"#{cid}"
        ct = contacts.contact_for(cpaths, can_dm=(xu or {}).get("can_dm"), bio=(xu or {}).get("bio"), handle=c.get("handle"),
                                  researched=contacts.researched_for(research, c.get("handle"), name))
        wear = weights.wear_verdict(c.get("reasons_json"))
        bar_reason = weights.bar_reason(c.get("category"), wear, True if ct["contactable"] else ct["contact_reason"])
        rows.append({
            "n": i, "name": name, "role": role_text(c, mp, xu),
            "section": config.SECTIONS.get(c.get("category") or "", "Other"), "handle": c.get("handle"),
            "score": None if c.get("total") is None else round(float(c["total"]), 1),
            "p_accept": c.get("p_accept"), "p_wear": c.get("p_wear"), "p_love": c.get("p_love"),
            "impact": c.get("impact"), "reach": c.get("reach"), "confidence": c.get("confidence"),
            "flags": db.jloads(c.get("flags_json"), []) or [],
            "status": (status.get(cid) or {}).get("status") or "candidate",
            "summary": summary, "reasons": parse_reasons(c.get("reasons_json")),
            "evidence": evidence.get(cid, []), "paths": cpaths,
            # extras (ignored by the map renderer, used by the csv and the dashboard)
            "candidate_id": cid, "rank": c.get("rank"), "category": c.get("category"), "x_id": c.get("x_id"),
            "x_url": f"https://x.com/{c['handle']}" if c.get("handle") else None,
            "map_person_id": c.get("map_person_id"), "linkedin_url": c.get("linkedin_url"),
            "us_based": c.get("us_based"), "competitor_flag": c.get("competitor_flag"),
            "competitor_detail": c.get("competitor_detail"), "handle_verified": c.get("handle_verified"),
            "discovered_via": c.get("discovered_via"), "status_note": (status.get(cid) or {}).get("note"),
            "best_path": best_path_words(cpaths[0] if cpaths else None),
            "contact": {"best": ct["best"], "routes": contacts.export_routes(ct)}, "contactable": ct["contactable"],
            "contact_reason": ct["contact_reason"],
            "wear_evidence": wear == "for", "meets_bar": not bar_reason, "bar_reason": bar_reason,
            "engagement": engagement_json(c.get("engagement"), engagement_factor(c.get("reasons_json"))),
        })
    return rows


def wishlist_rows(rows: list[dict], bar: bool = True) -> list[dict]:
    """The rows wishlist.json holds: those no WISHLIST_FILTERS filter leaves out (bar=False drops the brief's bar),
    renumbered n = 1..k in their order."""
    f = {**WISHLIST_FILTERS, "bar": bool(bar)}
    keep = [r for r in rows if not weights.hidden_by(r, f)]
    return [dict(r, n=i) for i, r in enumerate(keep, 1)]


def rows_for_run(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    """build_rows for a run, with the same fallback to the latest scores/paths as run_export."""
    return build_rows(conn, _pick_run(conn, "scores", run_id), _pick_run(conn, "paths", run_id))


def reorder(rows: list[dict], ranking: list[dict]) -> list[dict]:
    """The export rows of `ranking` ([{candidate_id, rank, total, rank_default?}], best first), renumbered n = 1..N with
    rank and score from the ranking (the dashboard's re-ranked list under other weights). Same shape as wishlist.json."""
    by_id = {r["candidate_id"]: r for r in rows}
    out = []
    for x in ranking:
        base = by_id.get(int(x["candidate_id"]))
        if base is None:
            continue
        r = dict(base, n=len(out) + 1, rank=int(x["rank"]), score=round(float(x["total"]), 1))
        if x.get("rank_default") is not None:
            r["rank_default"] = int(x["rank_default"])
        out.append(r)
    return out


def csv_text(rows: list[dict]) -> str:
    buf = io.StringIO(newline="")
    _write_csv(rows, buf)
    return buf.getvalue()


def write_wishlist_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        _write_csv(rows, f)


def _write_csv(rows: list[dict], f) -> None:
    w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for r in rows:
        best = r["paths"][0] if r["paths"] else None
        e = r.get("engagement") or {}
        w.writerow({
            "n": r["n"], "rank": r["rank"], "name": r["name"], "handle": r["handle"], "section": r["section"],
            "category": r["category"], "score": r["score"], "p_accept": r["p_accept"], "p_wear": r["p_wear"],
            "p_love": r["p_love"], "impact": r["impact"], "engagement": e.get("value"), "er_vs_size": e.get("index"),
            **{col: (e.get(group) or {}).get(key) for col, (group, key) in ENGAGEMENT_CSV.items()},
            "reach": r["reach"], "confidence": r["confidence"],
            "flags": "|".join(str(x) for x in r["flags"]), "status": r["status"],
            "best_path_how": best["how"] if best else "none", "best_path": r["best_path"],
            "best_path_strength": best.get("strength") if best else None, "summary": r["summary"],
            "competitor_detail": r["competitor_detail"], "us_based": r["us_based"], "map_person_id": r["map_person_id"],
            "x_url": r["x_url"], "linkedin_url": r["linkedin_url"], "evidence_count": len(r["evidence"]),
            "reasons": " | ".join(r["reasons"]),
            "contact": (r.get("contact") or {}).get("best"), "contactable": r.get("contactable"),
            "meets_bar": r.get("meets_bar"), "bar_reason": r.get("bar_reason"),
        })


def write_changes_csv(conn: sqlite3.Connection, run_id: int | None, path: Path) -> int:
    rows = [] if run_id is None else db.rows(
        conn, "SELECT ch.change_id, ch.run_id, ch.candidate_id, c.name, c.handle, ch.field, ch.old_value, ch.new_value, ch.reason "
              "FROM changes ch LEFT JOIN candidates c ON c.candidate_id=ch.candidate_id WHERE ch.run_id=? ORDER BY ch.change_id", (run_id,))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CHANGES_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in CHANGES_COLUMNS})
    return len(rows)


def run_export(conn: sqlite3.Connection, run_id: int, out_dir=config.EXPORTS_DIR, bar: bool = True) -> dict:
    """wishlist.json (wishlist_rows: the bar, US only, not rejected; bar=False drops the bar), wishlist.csv (every row,
    with meets_bar and bar_reason) and changes.csv."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    score_run = _pick_run(conn, "scores", run_id)
    path_run = _pick_run(conn, "paths", run_id)
    change_run = _pick_run(conn, "changes", run_id)
    if score_run != run_id or path_run != run_id:
        log.info("export: run %s has no scores/paths of its own; using scores from run %s, paths from run %s", run_id, score_run, path_run)
    rows = build_rows(conn, score_run, path_run)
    wishlist = wishlist_rows(rows, bar)
    json_path, csv_path, changes_path = out / "wishlist.json", out / "wishlist.csv", out / "changes.csv"
    json_path.write_text(json.dumps(wishlist, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_wishlist_csv(rows, csv_path)
    n_changes = write_changes_csv(conn, change_run, changes_path)
    scored = sum(1 for r in rows if r["score"] is not None)
    result = {"run_id": run_id, "score_run": score_run, "path_run": path_run, "change_run": change_run, "rows": len(rows),
              "scored": scored, "unscored": len(rows) - scored, "wishlist": len(wishlist), "bar": bool(bar),
              "meets_bar": sum(1 for r in rows if r["meets_bar"]), "changes": n_changes,
              "files": {"wishlist_json": str(json_path), "wishlist_csv": str(csv_path), "changes_csv": str(changes_path)}}
    log.info("export done: %s", result)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export wishlist.json / wishlist.csv / changes.csv")
    ap.add_argument("--run", type=int, default=None, help="run id (default: latest scored run, else latest run)")
    ap.add_argument("--out", default=str(config.EXPORTS_DIR))
    ap.add_argument("--no-bar", action="store_true", help="wishlist.json without the brief's bar (US only, not rejected)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    run_id = a.run or db.latest_run_id(conn, with_scores=True) or db.latest_run_id(conn)
    if run_id is None:
        raise SystemExit("no runs yet: run the pipeline first")
    print(run_export(conn, run_id, a.out, bar=not a.no_bar))


if __name__ == "__main__":
    main()
