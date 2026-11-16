"""Data access for the dashboard: pure functions over a sqlite3 connection, no Streamlit imports.

Reads runs, scores, changes, paths, evidence, events, classifications, costs and status.
Topic affinity (scores.affinity, reasons_json factor "affinity") is optional: runs scored before it existed read as None.
So is community engagement (scores.engagement, reasons_json factor "engagement" with its KPIs): "unknown" before it existed.
Writes only the `status` table (set_status). Importable and tested in tests/test_dashboard_data.py.

Everything is tolerant of missing pieces: no paths yet -> "no path yet", no classification -> empty summary,
no previous run -> "first scored run". Unknown stays unknown (None), never guessed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote
from typing import Any, Mapping

import pandas as pd

from ring_wishlist import config, contacts, costs, db, export, weights

RANKED_COLUMNS = [
    "rank", "candidate_id", "name", "handle", "section", "total",
    "p_accept", "p_wear", "p_love", "affinity", "impact", "engagement", "engagement_short", "er", "er_vs_size", "reach",
    "confidence",
    "flags", "best_path", "status", "summary", "category", "x_url", "flags_text", "note", "owner",
    "handle_verified", "why", "wear_evidence", "bar_reason", "contact", "contactable", "contact_reason", "contact_routes",
]
PROB_FIELDS = ("p_accept", "p_wear", "p_love")
FACTOR_FIELDS = ("p_accept", "p_wear", "p_love", "impact", "reach")
FACTOR_LABELS = {
    "p_accept": "accepts a ring",
    "p_wear": "wears it every day",
    "p_love": "loves it and says so",
    "impact": "impact if they love it",
    "reach": "path strength",
}
COLD = "cold: no warm intro"   # no path is how to approach someone, not a reason to drop them
SCORE_FIELDS = ("p_accept", "p_wear", "p_love", "impact")   # the story; the path is shown beside it, not in it
UNCLASSIFIED_SECTION = "Unclassified"
NO_FLAG = "(no flags)"   # filter option: rows without any flag
FIELD_SHORT = {"p_accept": "accept", "p_wear": "wear", "p_love": "love", "affinity": "topic", "engagement": "engagement",
               "new_candidate": "new"}
# The ranked table as Carlo reads it: who, what kind, what to watch (a competitor tie: flagged, never removed), why in
# one line, the three probabilities, impact and how their community answers (engagement_short), who reaches them, where
# they stand. Score, reach, topic affinity and confidence live in the person panel.
# "change" is the move against the default weights (shown only when the weights in use differ from the default).
TABLE_COLUMNS = [
    "rank", "change", "person", "person_link", "category_label", "flags_short", "why", "p_accept", "p_wear", "p_love",
    "impact", "engagement_short", "contact", "status_label", "candidate_id",
]
CATEGORY_LABELS = {"x_voice": "X voice", "media": "Media", "vc": "VC", "founder_angel": "Founder / angel",
                   "other": "Other"}
NOT_RESEARCHED = "Not researched yet: no posts read, so the numbers are starting guesses for the category."
HANDLE_MARKS = {"verified": "✓", "likely_real": "(likely real)", "suspicious": "(suspicious)"}
TABLE_HIDDEN_FLAGS = ("no_path",)   # not a problem: the path column already says "cold: no warm intro"
SUPPORT_LABELS = {
    "accept": "would accept a ring", "wear": "would wear it daily", "love": "would love it and say so",
    "impact": "impact", "reach": "reach", "competitor": "competitor tie", "us": "US base",
    "authenticity": "the handle is really them", "affinity": "talks about the topic",
}
FIELD_LABELS = {"p_accept": "Accepts", "p_wear": "Wears daily", "p_love": "Loves + says so", "total": "Score",
                "rank": "Rank", "impact": "Impact", "reach": "Path strength", "affinity": "Topic affinity", "flags": "Flags",
                "confidence": "Confidence", "new_candidate": "New on the list", "engagement": "Community engagement"}
TOPIC_LABELS = {"personal_agents": "personal agents", "voice": "voice interfaces", "wearables": "AI wearables and rings",
                "screen_free": "screen-free life"}
AFFINITY_MAX_POSTS = 3
ENGAGEMENT_UNKNOWN = "no sample of their latest posts yet"
# The person panel's engagement table: one column per count of the average post (engagement = likes + replies + reposts)
ENGAGEMENT_COUNTS = {"views": "Views", "likes": "Likes", "replies": "Replies", "reposts": "Reposts", "engagement": "Engagement"}
NO_VALUE = "–"
# The hard filters (weights.filter_rows) decide who is ranked at all; the word says who each one leaves out.
HARD_FILTER_WORDS = {"us_only": "not US", "hide_competitor": "competitor-tied", "hide_rejected": "rejected",
                     "bar": "below the bar"}
NO_CONTACT_WORD = "with no way to contact"   # the bar's reach half: people who pass the device half but cannot be contacted
FILTER_NAMES = {"us_only": "US only", "hide_competitor": "Hide competitor-tied", "hide_rejected": "Hide rejected",
                "bar": "the bar"}          # the Filters toggles, as the count line's help names them
PEOPLE = {"not US": "non-US {}", "competitor-tied": "competitor-tied {}", "rejected": "rejected {}",
          "below the bar": "{} below the bar", NO_CONTACT_WORD: "{} with no way to contact"}
COMPETITOR_MARK = "⚑"   # a competitor tie in the Flags cell, the cards and the person panel: flagged, never removed
STATUS_LABELS = {
    "candidate": "candidate", "approved": "approved", "intro_asked": "intro asked", "ring_sent": "ring sent",
    "wearing": "wearing", "posted": "posted", "rejected": "rejected",
}


# ---------------------------------------------------------------- connections and runs

def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (and initialise) the SQLite file. None -> DB_PATH env or data/wishlist.db."""
    return db.connect(path)


def scored_runs(conn: sqlite3.Connection) -> list[dict]:
    """All runs that have scores, newest first."""
    return db.rows(conn, """
        SELECT r.run_id, r.started_at, r.finished_at, r.stage, r.notes, COALESCE(r.cost_usd, 0) AS cost_usd,
               COUNT(s.candidate_id) AS n_candidates
        FROM runs r JOIN scores s ON s.run_id = r.run_id
        GROUP BY r.run_id ORDER BY r.run_id DESC""")


def run_info(conn: sqlite3.Connection, run_id: int) -> dict:
    r = db.one(conn, "SELECT * FROM runs WHERE run_id=?", (run_id,)) or {
        "run_id": run_id, "started_at": None, "finished_at": None, "stage": None, "notes": None, "cost_usd": 0.0}
    r["n_candidates"] = int(db.one(conn, "SELECT COUNT(*) AS n FROM scores WHERE run_id=?", (run_id,))["n"])
    r["previous_run_id"] = db.previous_scored_run_id(conn, run_id)
    ledger = float(db.one(conn, "SELECT COALESCE(SUM(cost_usd),0) AS c FROM costs WHERE run_id=?", (run_id,))["c"])
    r["cost_usd"] = float(r.get("cost_usd") or 0.0) or ledger  # runs.cost_usd is set by finish_run; fall back to the ledger
    r["date"] = (r.get("started_at") or "")[:10]
    return r


def last_run(conn: sqlite3.Connection) -> dict | None:
    """The newest run of any stage (scored or not), for the empty state."""
    return db.one(conn, "SELECT run_id, started_at, finished_at, stage, notes FROM runs ORDER BY run_id DESC LIMIT 1")


def latest_scored_run(conn: sqlite3.Connection) -> dict | None:
    """The newest run that wrote scores, with candidate count, cost and previous run id. None when nothing is scored."""
    run_id = db.latest_run_id(conn, with_scores=True)
    return run_info(conn, run_id) if run_id is not None else None


# ---------------------------------------------------------------- helpers

def _flags_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(f) for f in raw]
    parsed = db.jloads(raw, None)
    if isinstance(parsed, list):
        return [str(f) for f in parsed]
    if isinstance(parsed, str):
        return [parsed]
    return [f.strip() for f in str(raw).split(",") if f.strip()]


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _section(category: str | None) -> str:
    return config.SECTIONS.get(category, UNCLASSIFIED_SECTION) if category else UNCLASSIFIED_SECTION


def _x_url(handle: str | None) -> str | None:
    return f"https://x.com/{handle}" if handle else None


def _round(v: Any, nd: int = 2) -> float | None:
    n = _num(v)
    return None if n is None else round(n, nd)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _pick_latest_per_candidate(conn: sqlite3.Connection, table: str, run_id: int, candidate_id: int | None = None) -> list[dict]:
    """Rows of `table` for this run; per candidate, fall back to the latest run that has rows (stage may have run separately)."""
    # One grouped pass picks the run per candidate (this run if it has rows, else the latest); a correlated subquery
    # per row took ~15 s on 8k paths without an index.
    where = "" if candidate_id is None else " WHERE candidate_id = ?"
    params: list[Any] = [run_id] + ([candidate_id] if candidate_id is not None else [])
    return db.rows(conn, f"""
        WITH pick AS (
            SELECT candidate_id, COALESCE(MAX(CASE WHEN run_id = ? THEN run_id END), MAX(run_id)) AS rid
            FROM {table}{where} GROUP BY candidate_id)
        SELECT p.* FROM {table} p JOIN pick ON pick.candidate_id = p.candidate_id AND p.run_id = pick.rid
        ORDER BY p.candidate_id""", params)


# ---------------------------------------------------------------- paths

def paths_for(conn: sqlite3.Connection, run_id: int, candidate_id: int | None = None) -> list[dict]:
    """Paths (best first) for this run, falling back per candidate to the latest run that computed paths."""
    rows = _pick_latest_per_candidate(conn, "paths", run_id, candidate_id)
    for r in rows:
        r["json_obj"] = db.jloads(r.get("json"), {}) or {}
        r["strength"] = _num(r.get("strength"))
    rows.sort(key=lambda r: (r["candidate_id"], r.get("rank") if r.get("rank") is not None else 99, -(r["strength"] or 0)))
    return rows


def best_paths(conn: sqlite3.Connection, run_id: int) -> dict[int, dict]:
    best: dict[int, dict] = {}
    for p in paths_for(conn, run_id):
        best.setdefault(int(p["candidate_id"]), p)
    return best


_REL_SUBS = (
    (re.compile(r"\blast (\d+)d ago\b"), lambda m: f"last contact {m.group(1)} day{'' if m.group(1) == '1' else 's'} ago"),
    (re.compile(r"\b(\d+) touches\b"), lambda m: f"{m.group(1)} interactions"),
    (re.compile(r"\bfaded\b"), lambda m: "gone quiet"),
    (re.compile(r",? per their bio\)"), lambda m: ")"),
    (re.compile(r"\s*\(map(?:, [^)]*)?\)"), lambda m: ""),
    (re.compile(r"\s+on X\b"), lambda m: ""),
)


def plain_rel(text: str | None, keep_touches: bool = True) -> str:
    """Relationship label in plain words: 'last 6d ago' -> 'last contact 6 days ago', 'faded' -> 'gone quiet',
    provenance tags such as '(map, transcript)' dropped. keep_touches=False drops the interaction count (table)."""
    t = str(text or "").strip()
    if not keep_touches:
        t = re.sub(r"\s*·\s*\d+ touches\b", "", t)
    for rx, fn in _REL_SUBS:
        t = rx.sub(fn, t)
    return " ".join(t.split())


def path_short(p: dict | None) -> str:
    """How to approach them, for the table: 'via Jane Doe, introduced them', 'you know them · met · …',
    'on X · follow each other', 'cold: DMs open', 'cold: no warm intro'. Information, never a requirement."""
    if not p:
        return COLD
    how, via, you, tgt = p.get("how"), p.get("via_name"), p.get("you_rel"), p.get("target_rel")
    if how == "direct":
        rel = plain_rel(you, keep_touches=False)
        return "you know them" + (f" · {rel}" if rel else "")
    if how == "via":
        rel = plain_rel(tgt)
        return f"via {via or 'a contact'}" + (f", {rel}" if rel else "")
    if how == "x_direct":
        return "on X · " + (plain_rel(tgt or you) or "follow each other")
    if how == "open_dm":
        return "cold: DMs open"
    return COLD


def strength_word(strength: float | None) -> str:
    """0..1 path strength as a word Carlo can act on."""
    if strength is None:
        return "unknown strength"
    return "strong" if strength >= 0.7 else "good" if strength >= 0.45 else "weak" if strength >= 0.2 else "very weak"


def path_line(p: dict | None, target_name: str | None = None) -> str:
    """Full 'you -> via -> target' line for the detail panel, with strength and both relationship labels."""
    target = target_name or "them"
    if not p:
        return f"you → {target} · {COLD}"
    how, via, you, tgt = p.get("how"), p.get("via_name"), p.get("you_rel"), p.get("target_rel")
    strength = p.get("strength")
    s = f"{strength:.2f}" if isinstance(strength, (int, float)) else "?"
    if how == "direct":
        line = f"you → {target} · direct · strength {s}"
        if you:
            line += f" · {you}"
    elif how == "via":
        line = f"you → {via or 'a contact'} → {target} · strength {s}"
        if you:
            line += f" · you→{via or 'them'}: {you}"
        if tgt:
            line += f" · {via or 'them'}→{target}: {tgt}"
    elif how == "x_direct":
        line = f"you ↔ {target} on X · strength {s}" + (f" · {tgt or you}" if (tgt or you) else " · follow each other")
    elif how == "open_dm":
        line = f"you → {target} · DMs open · strength {s} · no relationship yet"
    else:
        line = f"you → ✕ → {target} · no path · strength {s}"
    return line


def path_view(p: dict | None, target_name: str | None = None) -> dict:
    """Parts to draw one path in the detail panel: head ("you → via → target"), kind, strength, one label per hop,
    the connector's warmth bucket (0..3, via paths) and why we believe the relationship is real."""
    target = target_name or "them"
    if not p:
        return {"how": "none", "kind": "cold", "head": f"you → {target}", "strength": None,
                "labels": ["no warm intro found: approach cold"], "warmth": None, "evidence": None, "strength_word": "cold"}
    how, via, you, tgt = p.get("how"), p.get("via_name") or "a contact", p.get("you_rel"), p.get("target_rel")
    strength = _num(p.get("strength"))
    warmth = (p.get("json_obj") or {}).get("warmth") if isinstance(p.get("json_obj"), dict) else None
    if how == "direct":
        head, kind = f"you → {target}", "you know them"
        labels = [f"you → {target}: {plain_rel(you)}"] if you else ["you know them"]
    elif how == "via":
        head, kind = f"you → {via} → {target}", "intro"
        labels = ([f"you → {via}: {plain_rel(you)}"] if you else []) + ([f"{via} → {target}: {plain_rel(tgt)}"] if tgt else [])
    elif how == "x_direct":
        head, kind, labels = f"you ↔ {target}", "on X", [plain_rel(tgt or you) or "follow each other"]
    elif how == "open_dm":
        head, kind, labels = f"you → {target}", "cold", ["DMs open, no relationship yet"]
    else:
        head, kind, labels = f"you → {target}", "cold", ["no warm intro found: approach cold"]
    return {"how": how or "none", "kind": kind, "head": head, "strength": strength, "labels": labels,
            "warmth": warmth if how == "via" else None, "evidence": p.get("evidence"),
            "strength_word": strength_word(strength) if how not in (None, "none", "open_dm") else "cold"}


# ---------------------------------------------------------------- classifications, evidence, events

def classifications_for_run(conn: sqlite3.Connection, run_id: int) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in _pick_latest_per_candidate(conn, "classifications", run_id):
        j = db.jloads(r.get("json"), {})
        out[int(r["candidate_id"])] = j if isinstance(j, dict) else {}
    return out


def classification_for(conn: sqlite3.Connection, run_id: int, candidate_id: int) -> dict | None:
    rows = _pick_latest_per_candidate(conn, "classifications", run_id, candidate_id)
    if not rows:
        return None
    j = db.jloads(rows[0].get("json"), {})
    return j if isinstance(j, dict) else {}


def evidence_for(conn: sqlite3.Connection, candidate_id: int) -> list[dict]:
    order = {s: i for i, s in enumerate(config.SUPPORTS)}
    rows = db.rows(conn, "SELECT * FROM evidence WHERE candidate_id=? ORDER BY date DESC, evidence_id", (candidate_id,))
    rows.sort(key=lambda r: (order.get(r.get("supports"), 99), -(int(r.get("polarity") or 1)), r.get("date") or ""))
    return rows


def events_for(conn: sqlite3.Connection, candidate_id: int) -> list[dict]:
    return db.rows(conn, "SELECT * FROM events WHERE candidate_id=? ORDER BY COALESCE(date,'') DESC, event_id DESC", (candidate_id,))


# ---------------------------------------------------------------- score reasons

def parse_reasons(raw: Any) -> dict:
    """Tolerant reader of scores.reasons_json.
    Accepts the contract shape {"factors": {f: {"reasons": [...], "evidence_ids": [...]}}, "reasons": [...], "evidence_ids": [...]}
    and looser variants (strings, lists, per-factor strings, factors at top level).
    Returns {"overall": [str], "factors": {field: {"value": float|None, "reasons": [str], "evidence_ids": [int]}}, "evidence_ids": [int]}."""
    out: dict[str, Any] = {"overall": [], "factors": {}, "evidence_ids": []}
    obj = db.jloads(raw, None) if isinstance(raw, str) else raw
    if obj is None:
        return out

    def as_texts(v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, (int, float)):
            return [str(v)]
        if isinstance(v, dict):
            for k in ("text", "reason", "why", "summary"):
                if isinstance(v.get(k), str):
                    return [v[k]]
            return [json.dumps(v, ensure_ascii=False)]
        if isinstance(v, (list, tuple)):
            return [t for item in v for t in as_texts(item)]
        return [str(v)]

    def as_ids(v: Any) -> list[int]:
        ids = []
        for item in (v if isinstance(v, (list, tuple)) else [v]) if v is not None else []:
            if isinstance(item, dict):
                item = item.get("evidence_id") or item.get("id")
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        return ids

    if isinstance(obj, (list, str)):
        out["overall"] = as_texts(obj)
        return out
    if not isinstance(obj, dict):
        return out

    factors = obj.get("factors") if isinstance(obj.get("factors"), dict) else {}
    for f in FACTOR_FIELDS + ("confidence", "total"):
        if f in obj and f not in factors:
            factors[f] = obj[f]
    for name, v in factors.items():
        if isinstance(v, dict):
            reasons = as_texts(v.get("reasons") if v.get("reasons") is not None else v.get("reason", v.get("why")))
            out["factors"][name] = {"value": _num(v.get("value")), "reasons": reasons,
                                    "evidence_ids": as_ids(v.get("evidence_ids", v.get("evidence")))}
        else:
            out["factors"][name] = {"value": None, "reasons": as_texts(v), "evidence_ids": []}
    out["overall"] = as_texts(obj.get("reasons"))
    out["evidence_ids"] = as_ids(obj.get("evidence_ids"))
    if not out["overall"]:
        out["overall"] = [t for f in FACTOR_FIELDS for t in out["factors"].get(f, {}).get("reasons", [])][:5]
    return out


# ---------------------------------------------------------------- topic affinity

def _affinity_factor(stored: Any) -> dict | None:
    """The "affinity" factor of a parsed reasons_json (under "factors" or at top level), as a dict; None when absent."""
    if not isinstance(stored, dict):
        return None
    factors = stored.get("factors") if isinstance(stored.get("factors"), dict) else {}
    raw = factors.get("affinity", stored.get("affinity"))
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {"value": raw}
    if isinstance(raw, str):
        return {"reason": raw}
    return None


def affinity_posts(raw: Any, limit: int = AFFINITY_MAX_POSTS) -> list[dict]:
    """top_posts as [{"url", "date", "text", "engagement", "replies"}]: accepts url strings or objects; drops empties."""
    out: list[dict] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            continue
        url = item.get("url") or None
        text = item.get("text") or item.get("quote") or None
        if not url and not text:
            continue
        date = item.get("date") or item.get("created_at")
        out.append({"url": url, "date": str(date)[:10] if date else None, "text": text,
                    "engagement": _num(item.get("engagement")), "replies": _num(item.get("replies"))})
        if len(out) >= limit:
            break
    return out


def affinity_source_label(source: Any) -> str | None:
    """'computed' / 'posts' -> 'own posts', 'classifier' -> 'classifier verdict', both -> 'own posts + classifier'."""
    if source is None or str(source).strip() == "":
        return None
    s = str(source).lower()
    posts = any(k in s for k in ("comput", "post", "affinity"))
    cls = "classif" in s or "topic_voice" in s or "llm" in s
    if posts and cls:
        return "own posts + classifier"
    if cls:
        return "classifier verdict"
    if posts:
        return "own posts"
    return str(source).replace("_", " ")


def affinity_view(column_value: Any, stored: Any, cls: dict | None = None, evidence: list[dict] | None = None) -> dict:
    """Everything the detail panel shows about topic affinity. Tolerant of a NULL column and of reasons_json without
    the factor (runs scored before affinity existed): value None, measured False, reason None, no posts.
    Best posts come from the factor's top_posts; without them, from evidence rows with supports='affinity'."""
    fac = _affinity_factor(stored) or {}
    value = _num(column_value)
    if value is None:
        value = _num(fac.get("value"))
    reason = fac.get("reason") if isinstance(fac.get("reason"), str) else None
    if not reason:
        texts = fac.get("reasons")
        texts = [texts] if isinstance(texts, str) else texts if isinstance(texts, list) else []
        reason = next((t for t in texts if isinstance(t, str) and t.strip()), None)
    posts = affinity_posts(fac.get("top_posts"))
    from_evidence = False
    if not posts and evidence:
        cand = [e for e in evidence if e.get("supports") == "affinity" and (e.get("polarity") or 1) > 0 and e.get("url")]
        cand.sort(key=lambda e: (-(_num(e.get("weight")) or 0), e.get("kind") != "post", str(e.get("date") or "")))
        posts = affinity_posts([{"url": e.get("url"), "date": e.get("date"), "text": e.get("text")} for e in cand])
        from_evidence = bool(posts)
    verdict = (cls or {}).get("topic_voice") if isinstance((cls or {}).get("topic_voice"), dict) else {}
    topics = [TOPIC_LABELS.get(str(t), str(t).replace("_", " ")) for t in (verdict.get("topics") or []) if t]
    ids = fac.get("evidence_ids", fac.get("evidence"))
    return {
        "value": None if value is None else round(value, 2),
        "measured": value is not None,
        "source": fac.get("source"),
        "source_label": affinity_source_label(fac.get("source")),
        "reason": reason.strip() if reason else None,
        "top_posts": posts,
        "posts_from_evidence": from_evidence,
        "classifier": verdict.get("value") if verdict else None,
        "topics": topics,
        "evidence_ids": [int(i) for i in (ids if isinstance(ids, list) else []) if str(i).lstrip("-").isdigit()],
    }


# ---------------------------------------------------------------- community engagement

def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def rate_pct(v: Any) -> str | None:
    """A rate per follower as a percent, the decimals by size: 14.6%, 0.44%, 0.049%."""
    n = _num(v)
    if n is None:
        return None
    p = n * 100
    return f"{p:.1f}%" if abs(p) >= 1 else f"{p:.2f}%" if abs(p) >= 0.1 else f"{p:.3f}%"


def count_text(v: Any) -> str | None:
    """An average count with thousands separators, one decimal under 10: 12,400 · 42 · 4.5."""
    n = _num(v)
    if n is None:
        return None
    return f"{n:,.0f}" if abs(n) >= 10 else f"{n:.1f}".removesuffix(".0")


def times_text(v: Any) -> str | None:
    n = _num(v)
    return None if n is None else f"{n:.1f}×"


def engagement_short(value: float | None, fac: Mapping[str, Any]) -> str:
    """The table's cell: the factor's one-cell summary ('ER 0.44% · 2.0× peers'), 'unknown' when the value is."""
    return (fac.get("short") or _dict(fac.get("kpis")).get("short") or "unknown") if value is not None else "unknown"


def engagement_table(kpis: Mapping[str, Any]) -> pd.DataFrame | None:
    """The average post (raw counts), the same per follower, the rate typical for an account this size, and the ratio,
    one column per ENGAGEMENT_COUNTS count; a column no row knows (views, often) is left out. None without KPIs."""
    per, rates, typical, vs = (_dict(kpis.get(g)) for g in ("per_post", "rates", "typical", "vs_size"))
    n, followers = _num(kpis.get("n")), _num(kpis.get("followers"))
    rows = {
        f"Average post ({int(n)} latest post{'' if n == 1 else 's'})" if n else "Average post":
            {c: count_text(per.get("interactions" if c == "engagement" else c)) for c in ENGAGEMENT_COUNTS},
        f"÷ {int(followers):,} followers" if followers is not None else "÷ followers":
            {c: rate_pct(rates.get(c)) for c in ENGAGEMENT_COUNTS},
        "Typical for this size": {c: rate_pct(typical.get(c)) for c in ENGAGEMENT_COUNTS},
        "vs size": {c: times_text(vs.get(c)) for c in ENGAGEMENT_COUNTS},
    }
    cols = [c for c in ENGAGEMENT_COUNTS if any(r[c] is not None for r in rows.values())]
    if not cols:
        return None
    return pd.DataFrame([{ENGAGEMENT_COUNTS[c]: r[c] or NO_VALUE for c in cols} for r in rows.values()], index=list(rows))


def engagement_view(value: Any, fac: Any) -> dict:
    """Everything the person panel and the check tab show about community engagement, from scores.engagement (value; None
    falls back to the factor's) and the stored factor (export.engagement_factor; a bare number counts as the value):
    value (0..1, 0.5 = typical for their size), known, short, headline, table (engagement_table), reasons (the
    arithmetic), pace (posts a week, interactions per 1,000 views) and, when unknown, the one line saying why."""
    fac = fac if isinstance(fac, dict) else {"value": fac}
    kpis = _dict(fac.get("kpis"))
    v = _num(value)
    v = _num(fac.get("value")) if v is None else v
    reasons = [str(t) for t in (fac.get("reasons") if isinstance(fac.get("reasons"), list) else []) if str(t).strip()]
    view = {"value": None if v is None else round(v, 2), "known": v is not None, "short": engagement_short(v, fac),
            "headline": None, "table": None, "reasons": reasons, "pace": None, "unknown": None, "kpis": kpis or None}
    if v is None:
        why = kpis.get("why") if isinstance(kpis.get("why"), str) and kpis["why"].strip() else ENGAGEMENT_UNKNOWN
        return {**view, "unknown": f"Community engagement unknown: {why}"}
    ppw, per_view = _num(kpis.get("posts_per_week")), _num(kpis.get("per_view"))
    pace = [f"{count_text(ppw)} posts a week" if ppw is not None else None,
            f"{count_text(per_view * 1000)} interactions per 1,000 views" if per_view is not None else None]
    head = [view["short"]] if view["short"] != "unknown" else []
    return {**view, "headline": " · ".join(head + [f"score factor {v:.2f} (0.50 = typical for their size)"]),
            "table": engagement_table(kpis), "pace": " · ".join(t for t in pace if t) or None}


# ---------------------------------------------------------------- flags

def flag_explanation(flag: str, cand: dict | None = None, x_user: dict | None = None) -> str:
    cand, x_user = cand or {}, x_user or {}
    if flag.startswith("competitor"):
        which = flag.split(":", 1)[1].strip() if ":" in flag else None
        detail = (cand.get("competitor_detail") or "").strip()
        if which and detail.lower().startswith(which.lower()):   # "Oura: product lead ..." -> "product lead ..."
            detail = detail[len(which):].lstrip(" :-·")
        head = f"Tied to competitor {which}" if which else "Tied to a direct competitor"
        return head + (f": {detail}" if detail else "") + ". The score stands; Carlo decides."
    if flag == "not_us":
        loc = x_user.get("location")
        return "Not US-based" + (f" ({loc})" if loc else "") + ": shipping and launch focus are US-first."
    if flag == "unverified_handle":
        hv = cand.get("handle_verified") or "unknown"
        return f"Handle not verified ({hv}): could be a parody or a namesake. Check before anything goes out."
    if flag == "no_path":
        return "No warm intro in your network: approach them cold. Not a reason to drop them."
    return flag


def competitor_name(flag: str, full: bool = False) -> str:
    """Who a competitor flag ties them to: 'competitor:Wispr Flow rival (VoiceOS, not in the brief's list)' -> 'Wispr
    Flow rival' (full=True keeps the note in brackets, for the person panel and the filter); a bare 'competitor' ->
    'competitor'."""
    which = flag.split(":", 1)[1].strip() if ":" in flag else ""
    return (which if full else which.split("(", 1)[0].strip() or which) or "competitor"


def flag_short(flag: str, full: bool = False) -> str:
    """Short text for the table, the cards and the person panel: competitor:Oura -> '⚑ Oura' (full=True keeps a tie's
    note in brackets: the Flag filter tells two 'Wispr Flow rival' ties apart), not_us -> 'not US'."""
    if flag.startswith("competitor"):
        return f"{COMPETITOR_MARK} {competitor_name(flag, full)}"
    return {"not_us": "not US", "unverified_handle": "unverified handle", "no_path": "cold (no warm intro)",
            NO_FLAG: "no flags"}.get(flag, flag.replace("_", " "))


def flags_short(flags: list[str] | None) -> str:
    """The table's Flags cell: every competitor tie behind one mark, first ('⚑ Sesame, Limitless'), then the other
    flags ('⚑ Ultrahuman · not US'); no_path (TABLE_HIDDEN_FLAGS) left out, '' when nothing is left."""
    fl = [str(f) for f in (flags or []) if str(f) not in TABLE_HIDDEN_FLAGS]
    ties = list(dict.fromkeys(competitor_name(f) for f in fl if f.startswith("competitor")))
    return " · ".join(([f"{COMPETITOR_MARK} {', '.join(ties)}"] if ties else [])
                      + [flag_short(f) for f in fl if not f.startswith("competitor")])


def handle_label(handle: str | None, verified: str | None) -> str:
    """'@srcasm ✓' when the account is confirmed; otherwise '(likely real)', '(suspicious)' or '(unchecked)'."""
    if not handle:
        return "no X handle"
    return f"@{handle} {HANDLE_MARKS.get(verified or '', '(unchecked)')}"


def person_label(name: str | None, handle: str | None, verified: str | None) -> str:
    """'Jesse Middleton · @srcasm ✓' (just the handle when the name is the handle)."""
    if not handle:
        return f"{name or 'unnamed'} · no X handle"
    if not name or str(name).lstrip("@") == handle:
        return handle_label(handle, verified)
    return f"{name} · {handle_label(handle, verified)}"


# The table's Person cell is a link whose text is the label: LinkColumn shows the capture group of PERSON_LINK_TEXT.
PERSON_LINK_TEXT = r"https://x\.com/[^#]*#(.*)"


def x_url(handle: str | None, name: str | None = None) -> str | None:
    """Their X profile; with no known handle, an X people search for their name (None without a name either)."""
    if handle:
        return f"https://x.com/{str(handle).lstrip('@')}"
    return f"https://x.com/search?q={quote(str(name))}&f=user" if name else None


def person_link(name: str | None, handle: str | None, verified: str | None) -> str | None:
    """'https://x.com/srcasm#Jesse Middleton · @srcasm ✓': the X link, with the person label after '#' for the table."""
    url = x_url(handle, name)
    return f"{url}#{person_label(name, handle, verified)}" if url else None


def category_label(category: str | None) -> str:
    return CATEGORY_LABELS.get(category or "", "Not classified")


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", (status or config.STATUSES[0]).replace("_", " "))


# ---------------------------------------------------------------- ranked table

def ranked_table(conn: sqlite3.Connection, run_id: int | None, precise: bool = False) -> pd.DataFrame:
    """One row per scored candidate for the run, ranked. Always returns RANKED_COLUMNS (empty when nothing is scored).
    precise=True keeps the stored factors unrounded (the dashboard re-ranks from them in rank_list: weights.weighted_total
    with default weights then reproduces the stored total exactly). wear_evidence (net positive evidence that they wear a
    device or use a voice tool, weights.wear_verdict of the stored p_wear factor) and bar_reason ('' when the row meets
    the brief's bar) come from reasons_json, already parsed here. contact (the best route's label), contactable,
    contact_reason and contact_routes (every route, best first) come from contacts.contact_for: the reach half of the
    bar. engagement (the community engagement factor, None when unknown, which weights.weighted_total counts as typical),
    engagement_short (its one-cell summary, 'unknown'), er (the average post's likes + replies + reposts per follower) and
    er_vs_size (the x peers index) come from scores.engagement and its stored factor."""
    rnd = _round if not precise else (lambda v, nd=2: _num(v))
    if run_id is None:
        return pd.DataFrame(columns=RANKED_COLUMNS)
    affinity_col = "s.affinity" if _has_column(conn, "scores", "affinity") else "NULL"   # older DB opened without db.init
    engagement_col = "s.engagement" if _has_column(conn, "scores", "engagement") else "NULL"
    rows = db.rows(conn, f"""
        SELECT s.rank, s.candidate_id, s.total, s.p_accept, s.p_wear, s.p_love, s.impact, s.reach, s.confidence,
               {affinity_col} AS affinity, {engagement_col} AS engagement, s.reasons_json, s.flags AS flags_json,
               c.name, c.handle, c.category, c.notes, c.handle_verified,
               st.status, st.note, st.owner
        FROM scores s JOIN candidates c ON c.candidate_id = s.candidate_id
        LEFT JOIN status st ON st.candidate_id = s.candidate_id
        WHERE s.run_id = ?
        ORDER BY COALESCE(s.rank, 999999) ASC, s.total DESC, s.candidate_id ASC""", (run_id,))
    by_cid: dict[int, list[dict]] = {}
    for p in paths_for(conn, run_id):   # one pass: the best path for the column, every path for the contact routes
        by_cid.setdefault(int(p["candidate_id"]), []).append(p)
    best = {cid: ps[0] for cid, ps in by_cid.items()}
    reach = contacts.contacts_for(conn, paths_by_cid=by_cid)
    cls = classifications_for_run(conn, run_id)
    out = []
    for i, r in enumerate(rows, start=1):
        cid = int(r["candidate_id"])
        flags = _flags_list(r["flags_json"])
        stored = db.jloads(r["reasons_json"], None) if r.get("reasons_json") else None
        stored = stored if isinstance(stored, dict) else {}
        summary = (cls.get(cid) or {}).get("summary") or ""
        why = contacts.strip_reach(summary or stored.get("summary") or "") or NOT_RESEARCHED
        wear = weights.wear_verdict(stored)
        affinity = _num(r.get("affinity"))
        ct = reach.get(cid) or contacts.contact_for(by_cid.get(cid))
        if affinity is None and stored:   # column NULL: the stored factor may still carry the value
            affinity = _num((_affinity_factor(stored) or {}).get("value"))
        eng = export.engagement_factor(stored)
        kpis = _dict(eng.get("kpis"))
        engagement = _num(r.get("engagement"))
        engagement = _num(eng.get("value")) if engagement is None else engagement
        out.append({
            "rank": int(r["rank"]) if r.get("rank") is not None else i,
            "candidate_id": cid,
            "name": r.get("name") or (f"@{r['handle']}" if r.get("handle") else f"candidate {cid}"),
            "handle": r.get("handle") or "",
            "section": _section(r.get("category")),
            "total": rnd(r.get("total"), 1),
            "p_accept": rnd(r.get("p_accept")), "p_wear": rnd(r.get("p_wear")), "p_love": rnd(r.get("p_love")),
            "affinity": rnd(affinity),
            "impact": rnd(r.get("impact")), "reach": rnd(r.get("reach")), "confidence": rnd(r.get("confidence")),
            "engagement": rnd(engagement), "engagement_short": engagement_short(engagement, eng),
            "er": _num(_dict(kpis.get("rates")).get("engagement")), "er_vs_size": _num(kpis.get("index")),
            "flags": flags,
            "best_path": path_short(best.get(cid)),
            "status": r.get("status") or config.STATUSES[0],
            "summary": summary,
            "category": r.get("category"),
            "x_url": _x_url(r.get("handle")),
            "flags_text": ", ".join(flags),
            "note": r.get("note") or "",
            "owner": r.get("owner") or "",
            "handle_verified": r.get("handle_verified"),
            "why": why,
            "wear_evidence": wear == "for",
            "bar_reason": weights.bar_reason(r.get("category"), wear, True if ct["contactable"] else ct["contact_reason"]),
            "contact": ct["best"], "contactable": bool(ct["contactable"]), "contact_reason": ct["contact_reason"],
            "contact_routes": ct["routes"],
        })
    return pd.DataFrame(out, columns=RANKED_COLUMNS)


def filter_options(df: pd.DataFrame) -> dict[str, list[str]]:
    """Sections, flags and statuses present in the table. Flags end with NO_FLAG when some rows have none."""
    sections = [s for s in list(config.SECTIONS.values()) + [UNCLASSIFIED_SECTION] if s in set(df["section"])] if len(df) else []
    flags = sorted({f for fl in df["flags"] for f in (fl or [])}) if len(df) else []
    if len(df) and any(not fl for fl in df["flags"]):
        flags.append(NO_FLAG)
    statuses = [s for s in config.STATUSES if s in set(df["status"])] if len(df) else []
    return {"sections": sections, "flags": flags, "statuses": statuses}


def apply_filters(df: pd.DataFrame, sections: list[str] | None = None, flags: list[str] | None = None,
                  statuses: list[str] | None = None, min_confidence: float = 0.0, query: str = "",
                  min_affinity: float = 0.0) -> pd.DataFrame:
    """Pure view filter over a ranked frame (empty selections mean 'all'): it only hides rows, ranks stay. Flags match
    any of the chosen ones; NO_FLAG matches rows without flags. A minimum topic affinity above 0 drops rows whose
    affinity is unknown. Who is ranked at all (US only, competitor-tied, rejected, the bar) is rank_list's hard
    filters."""
    if df is None or len(df) == 0:
        return df
    mask = pd.Series(True, index=df.index)
    if sections:
        mask &= df["section"].isin(sections)
    if flags:
        want = set(flags)
        mask &= df["flags"].apply(lambda fl: bool(want & set(fl or [])) or (NO_FLAG in want and not fl))
    if statuses:
        mask &= df["status"].isin(statuses)
    if min_confidence and min_confidence > 0:
        mask &= df["confidence"].fillna(0.0) >= float(min_confidence)
    if min_affinity and min_affinity > 0 and "affinity" in df:
        mask &= pd.to_numeric(df["affinity"], errors="coerce").fillna(-1.0) >= float(min_affinity)
    q = (query or "").strip().lower()
    if q:
        hay = (df["name"].fillna("") + " " + df["handle"].fillna("") + " " + df["why"].fillna("") + " "
               + df["best_path"].fillna("") + " " + df["contact"].fillna("") + " " + df["flags_text"].fillna("") + " "
               + df["note"].fillna("")).str.lower()
        mask &= hay.str.contains(re.escape(q), regex=True)
    return df[mask]


def table_view(df: pd.DataFrame | None) -> pd.DataFrame:
    """Display frame for the ranked table: TABLE_COLUMNS, positional index (row selection maps back via candidate_id).
    rank and change are the frame's: for a rank_list frame the weighted rank and the move against the default
    ('↑12', '↓3', '=', 'new'); blank change otherwise. p_accept/p_wear/p_love become whole percents (0..100);
    everything else keeps the ranked_table scale."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=TABLE_COLUMNS)
    out = df.copy()
    if "change" not in out:
        out["change"] = ""
    if "engagement_short" not in out:   # a frame read before community engagement existed
        out["engagement_short"] = "unknown"
    for f in PROB_FIELDS:   # probabilities read faster as whole percents; impact/reach/affinity stay 0..1 indices
        out[f] = (pd.to_numeric(df[f], errors="coerce") * 100).round()
    out["person"] = [person_label(n, h, v) for n, h, v in zip(df["name"], df["handle"], df["handle_verified"])]
    out["person_link"] = [person_link(n, h, v) for n, h, v in zip(df["name"], df["handle"], df["handle_verified"])]
    out["category_label"] = [category_label(c) for c in df["category"]]
    out["flags_short"] = [flags_short(fl) for fl in df["flags"]]
    out["status_label"] = [status_label(s) for s in df["status"]]
    return out[TABLE_COLUMNS].reset_index(drop=True)


def list_count(shown: int, k: int, n: int, bar: bool) -> str:
    """The count line's lead: 'Top N of K people who meet the bar' when more people are ranked than the list shows,
    else 'K people who meet the bar' ('people' alone with the bar off)."""
    people = "people who meet the bar" if bar else "people"
    return f"Top {shown} of {k} {people}" if k > n else f"{shown} {people}"


def filter_effects(df: pd.DataFrame | None, filters: Mapping[str, Any] | None = None) -> dict[str, dict]:
    """What each hard filter does on this run, by the count line's word (HARD_FILTER_WORDS, the bar's two halves apart:
    'below the bar' fails the device-or-voice half, NO_CONTACT_WORD only the reach half), in the Filters order. Per
    word: key (the filter, session key tf_<key>), on, matches (the people it leaves out when on), alone (those no other
    filter that is on leaves out: what switching it off adds to the list, or switching it on takes away) and also ({word
    of another filter that is on: how many of the matches it leaves out too}, most first). Words matching nobody are
    left out."""
    f = {**weights.DEFAULT_FILTERS, **(filters or {})}
    order = list(HARD_FILTER_WORDS.values()) + [NO_CONTACT_WORD]
    out: dict[str, dict] = {}
    if df is None or len(df) == 0:
        return out
    cols = [c for c in ("flags", "status", "category", "wear_evidence", "contactable") if c in df]
    for r in df[cols].to_dict("records"):
        by = weights.hidden_by(r, {**f, **dict.fromkeys(HARD_FILTER_WORDS, True)})   # every filter that matches them
        for k in by:
            word = NO_CONTACT_WORD if k == "bar" and weights.meets_device_bar(r) else HARD_FILTER_WORDS[k]
            e = out.setdefault(word, {"key": k, "on": bool(f.get(k)), "matches": 0, "alone": 0, "also": {}})
            others = [HARD_FILTER_WORDS[o] for o in by if o != k and f.get(o)]
            e["matches"] += 1
            e["alone"] += not others
            for o in others:
                e["also"][o] = e["also"].get(o, 0) + 1
    return {w: {**out[w], "also": dict(sorted(out[w]["also"].items(), key=lambda kv: (-kv[1], order.index(kv[0]))))}
            for w in sorted(out, key=order.index)}


def _people(word: str, n: int) -> str:
    """'71 non-US people', '1 person below the bar'."""
    return f"{n} " + PEOPLE.get(word, "{}").format("person" if n == 1 else "people")


def _or(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def hidden_text(effects: Mapping[str, dict]) -> str:
    """The count line's 'hidden: …' (filter_effects): per filter that is on, the people only it leaves out, so each
    number is what switching that filter off adds; one that adds nobody although it matches people says who else
    leaves them out: 'hidden: 0 not US (71 non-US people are also below the bar or rejected), 31 rejected, 457 below
    the bar'. '' when the filters that are on leave nobody out."""
    items = []
    for word, e in effects.items():
        if e["on"]:
            verb = "is" if e["matches"] == 1 else "are"
            also = (f" ({_people(word, e['matches'])} {verb} also {_or(list(e['also']))})"
                    if not e["alone"] and e["also"] else "")
            items.append(f"{e['alone']} {word}{also}")
    return "hidden: " + ", ".join(items) if items else ""


def hidden_help(effects: Mapping[str, dict]) -> str:
    """The count line's help: per filter that is on, everyone it matches, how many it alone hides and who else leaves
    out the rest ('US only matches 71 non-US people: 0 hidden by it alone, 71 also below the bar or rejected')."""
    lines = [f"- {FILTER_NAMES[e['key']][:1].upper()}{FILTER_NAMES[e['key']][1:]} matches "
             f"{_people(word, e['matches'])}: {e['alone']} hidden by it alone"
             + (f", {e['matches'] - e['alone']} also {_or(list(e['also']))}" if e["also"] else "")
             for word, e in effects.items() if e["on"]]
    return ("Each number is who that filter alone hides: switching it off in Filters adds exactly them. Someone two "
            "filters hide comes back only when both are off.\n\n" + "\n".join(lines)) if lines else ""


def rank_list(df: pd.DataFrame, state: Mapping[str, Any] | None = None,
              weights_path: Path | str | None = None) -> dict:
    """The dashboard's list, one code path whether the 'Manual weights' switch is on or off. df: ranked_table(precise=True)
    of a run; state: st.session_state (manual_weights, sliders tw_<name>, hard filters tf_<name>; missing keys take
    the defaults). Ranks with weights.effective(state) against the default, weights.effective({}): with the switch off
    both are the scheduled default, so the order is the stored one for a run scored by today's code.
    Returns ranked (weights.rerank: every row that passes the hard filters, rank 1..k counted after them, rank_default,
    change), top (rank <= n), n, filters, weights, label, default, default_label, manual, differs (weights != default:
    show the move against the default), effects (filter_effects) and hidden ({word: the people only that filter leaves
    out, i.e. what switching it off adds}, for every filter that is on and matches someone, 0 included). With the bar
    on, "below the bar" is its device-or-voice half and NO_CONTACT_WORD its reach half: people who pass the device half
    and have no way to be contacted."""
    state = state if state is not None else {}
    default_w, default_label = weights.effective({}, weights_path)
    manual = bool(state.get("manual_weights"))
    w, label = weights.effective(state, weights_path) if manual else (default_w, default_label)
    filters = {k: state.get(f"tf_{k}", v) for k, v in weights.DEFAULT_FILTERS.items()}
    ranked = weights.rerank(df, w, filters, baseline=default_w)
    n = int(filters["n"])
    effects = filter_effects(df, filters)
    return {"ranked": ranked, "top": ranked[ranked["rank"] <= n], "n": n, "filters": filters, "weights": w,
            "label": label, "default": default_w, "default_label": default_label, "manual": manual,
            "differs": w != default_w, "effects": effects,
            "hidden": {word: e["alone"] for word, e in effects.items() if e["on"]}}


# ---------------------------------------------------------------- changes

def _direction(fields: dict[str, tuple[Any, Any]]) -> str:
    if "new_candidate" in fields:
        return "new"
    if "total" in fields:
        old, new = _num(fields["total"][0]), _num(fields["total"][1])
        if old is not None and new is not None and abs(new - old) > 1e-9:
            return "up" if new > old else "down"
    if "rank" in fields:
        old, new = _num(fields["rank"][0]), _num(fields["rank"][1])
        if old is not None and new is not None and old != new:
            return "up" if new < old else "down"   # rank 1 is best
    for f in PROB_FIELDS + ("impact", "reach", "affinity", "engagement"):
        if f in fields:
            old, new = _num(fields[f][0]), _num(fields[f][1])
            if old is not None and new is not None and old != new:
                return "up" if new > old else "down"
    return "other"


def changes_since_previous(conn: sqlite3.Connection, run_id: int | None) -> dict:
    """Changes written for this run grouped per candidate into up / down / new (+ other), each with reasons.
    Returns {"previous_run_id", "up", "down", "new", "other", "counts", "summary"}."""
    empty = {"previous_run_id": None, "up": [], "down": [], "new": [], "other": [],
             "counts": {"up": 0, "down": 0, "new": 0, "other": 0}, "summary": "no scored run yet"}
    if run_id is None:
        return empty
    prev = db.previous_scored_run_id(conn, run_id)
    rows = db.rows(conn, """
        SELECT ch.change_id, ch.candidate_id, ch.field, ch.old_value, ch.new_value, ch.reason, c.name, c.handle
        FROM changes ch LEFT JOIN candidates c ON c.candidate_id = ch.candidate_id
        WHERE ch.run_id = ? ORDER BY ch.change_id""", (run_id,))
    ranks = {int(r["candidate_id"]): r["rank"] for r in db.rows(conn, "SELECT candidate_id, rank FROM scores WHERE run_id=?", (run_id,))}
    per: dict[int, dict] = {}
    for r in rows:
        cid = int(r["candidate_id"])
        e = per.setdefault(cid, {"candidate_id": cid, "name": r.get("name") or f"candidate {cid}", "handle": r.get("handle") or "",
                                 "rank": ranks.get(cid), "fields": {}, "reasons": [], "delta_total": None,
                                 "old_rank": None, "new_rank": None})
        e["fields"][r["field"]] = (r.get("old_value"), r.get("new_value"))
        reason = (r.get("reason") or "").strip()
        if reason and reason not in e["reasons"]:
            e["reasons"].append(reason)
    groups: dict[str, list[dict]] = {"up": [], "down": [], "new": [], "other": []}
    for e in per.values():
        e["direction"] = _direction(e["fields"])
        if "total" in e["fields"]:
            o, n = _num(e["fields"]["total"][0]), _num(e["fields"]["total"][1])
            e["delta_total"] = None if o is None or n is None else round(n - o, 1)
        if "rank" in e["fields"]:
            e["old_rank"], e["new_rank"] = _num(e["fields"]["rank"][0]), _num(e["fields"]["rank"][1])
        e["changed_fields"] = sorted(e["fields"].keys())
        groups[e["direction"]].append(e)
    groups["up"].sort(key=lambda e: -(e["delta_total"] or 0))
    groups["down"].sort(key=lambda e: (e["delta_total"] or 0))
    groups["new"].sort(key=lambda e: e["rank"] if e["rank"] is not None else 10**6)
    groups["other"].sort(key=lambda e: e["rank"] if e["rank"] is not None else 10**6)
    counts = {k: len(v) for k, v in groups.items()}
    if prev is None and not rows:
        summary = "first scored run"
    else:
        summary = f"since last run: {counts['up']} up · {counts['down']} down · {counts['new']} new"
        if counts["other"]:
            summary += f" · {counts['other']} other"
    return {"previous_run_id": prev, **groups, "counts": counts, "summary": summary}


def change_headline(e: dict) -> str:
    """One line per moved person for the change strip: who, score and rank movement (reasons are listed under it)."""
    who = e.get("name") or f"candidate {e.get('candidate_id')}"
    if e.get("handle") and not str(who).startswith("@"):
        who += f" (@{e['handle']})"
    bits: list[str] = []
    if e.get("direction") == "new":
        bits.append(f"new, enters at #{int(e['rank'])}" if e.get("rank") is not None else "new")
    else:
        old, new = (_num(v) for v in (e.get("fields", {}).get("total") or (None, None)))
        if old is not None and new is not None:
            bits.append(f"score {old:.1f} → {new:.1f} ({new - old:+.1f})")
        o, n = e.get("old_rank"), e.get("new_rank")
        if o is not None and n is not None and o != n:
            bits.append(f"rank #{int(o)} → #{int(n)}")
        other = [FIELD_SHORT.get(f, f) for f in e.get("changed_fields", []) if f not in ("total", "rank")]
        if other:
            bits.append("changed: " + ", ".join(other))
    return who + (" · " + " · ".join(bits) if bits else "")


def change_row_text(ch: dict) -> str:
    """One change row (field, old, new, reason) as a sentence; score.py reasons already name the field."""
    field, reason = ch.get("field") or "", (ch.get("reason") or "").strip()
    if field == "new_candidate":
        return reason or "new on the list"
    if reason.lower().startswith(field.replace("_", " ").lower()) or reason.lower().startswith(field.lower()):
        return reason
    if field == "affinity" and reason.lower().startswith("topic affinity"):
        return reason
    old, new = ch.get("old_value"), ch.get("new_value")
    return f"{field} {old if old is not None else '?'} → {new if new is not None else '?'}" + (f": {reason}" if reason else "")


# ---------------------------------------------------------------- plain-language history ("why it moved")

_ID_REFS = re.compile(r"(?:\b(?:event|evidence)\s+)?#\d+(?:\s*,\s*#\d+)*")
_FIELD_MOVE = re.compile(r"^(p_accept|p_wear|p_love|impact|reach|total|rank|confidence|topic affinity|affinity|"
                         r"community engagement|engagement)\s+"
                         r"(\S+)\s*->\s*(\S+?)\s*(?::\s*|$)", re.I)
_WORDS = ((re.compile(r"\bp_accept\b"), "accepts"), (re.compile(r"\bp_wear\b"), "wears daily"),
          (re.compile(r"\bp_love\b"), "loves + says so"), (re.compile(r"\btotal (?=\d)"), "score "))
HISTORY_FIELD_ORDER = ("new_candidate", "total", "rank", "p_accept", "p_wear", "p_love", "impact", "reach", "affinity",
                       "engagement", "flags", "confidence")


def plain_reason(text: Any) -> str:
    """A stored reason without internal ids and arrows: '(wear, #2340)' -> '(wear)', 'p_love' -> 'loves + says so'."""
    t = " ".join(str(text or "").split())
    t = _ID_REFS.sub("", t)
    t = re.sub(r"\(\s*[,;]?\s*\)", "", t)
    t = re.sub(r"\s*,\s*\)", ")", t)
    t = re.sub(r"\(\s*,\s*", "(", t)
    t = re.sub(r"\s+([.,;:)])", r"\1", t)
    t = t.replace("->", "→")
    for rx, word in _WORDS:
        t = rx.sub(word, t)
    return " ".join(t.split())


def _fmt_value(field: str, v: Any) -> str:
    n = _num(v)
    if n is None:
        return "unknown" if v in (None, "") else str(v)
    if field in PROB_FIELDS:
        return f"{n * 100:.0f}%"
    if field == "rank":
        return f"#{int(n)}"
    if field == "total":
        return f"{n:.1f}"
    return f"{n:.2f}"


def history_text(ch: dict, brief: bool = False) -> str:
    """One change row as Carlo reads it: 'Wears daily 70% → 85%: new evidence: post ...'.
    brief=True keeps only the move ('Score 33.7 → 38.9'), for a total whose reasons the factor lines already give."""
    field = ch.get("field") or ""
    reason = " ".join(str(ch.get("reason") or "").split())
    if field == "new_candidate":
        return plain_reason(reason) or "New on the list."
    if field == "flags":
        return plain_reason(reason) or "Flags changed."
    m = _FIELD_MOVE.match(reason)
    rest = "" if brief else plain_reason(reason[m.end():] if m else reason)
    label = FIELD_LABELS.get(field, field.replace("_", " "))
    move = f"{label} {_fmt_value(field, ch.get('old_value'))} → {_fmt_value(field, ch.get('new_value'))}"
    return move + (f": {rest}" if rest else "")


def person_history(conn: sqlite3.Connection, candidate_id: int, max_runs: int = 8) -> list[dict]:
    """Every scored change for one person, newest run first: [{"run_id", "date", "when", "lines": [str]}].
    The score line stays short when factor lines in the same run explain it."""
    rows = db.rows(conn, """
        SELECT ch.change_id, ch.run_id, ch.field, ch.old_value, ch.new_value, ch.reason, r.started_at
        FROM changes ch LEFT JOIN runs r ON r.run_id = ch.run_id
        WHERE ch.candidate_id = ? ORDER BY ch.run_id DESC, ch.change_id""", (candidate_id,))
    order = {f: i for i, f in enumerate(HISTORY_FIELD_ORDER)}
    runs: dict[int, dict] = {}
    for r in rows:
        started = r.get("started_at") or ""
        e = runs.setdefault(int(r["run_id"]), {"run_id": int(r["run_id"]), "date": started[:10] or None,
                                               "when": (started[:16].replace("T", " ") + " UTC") if started else None,
                                               "rows": []})
        e["rows"].append(r)
    out = []
    for e in list(runs.values())[:max_runs]:
        e["rows"].sort(key=lambda r: (order.get(r["field"], 99), r["change_id"]))
        explained = any(r["field"] not in ("total", "rank", "new_candidate") for r in e["rows"])
        lines = [history_text(r, brief=explained and r["field"] == "total") for r in e["rows"]]
        out.append({"run_id": e["run_id"], "date": e["date"], "when": e["when"], "lines": lines})
    return out


def evidence_groups(evidence: list[dict]) -> dict[str, list[dict]]:
    """Evidence by what it supports (config.SUPPORTS order), duplicates of the same quote and link dropped."""
    out: dict[str, list[dict]] = {}
    seen: set[tuple] = set()
    for e in evidence:
        key = (e.get("supports"), " ".join(str(e.get("text") or "").split()).lower(), e.get("url"), (e.get("polarity") or 1) < 0)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(e.get("supports") or "other", []).append(e)
    return out


# ---------------------------------------------------------------- person detail

def person_detail(conn: sqlite3.Connection, run_id: int | None, candidate_id: int) -> dict | None:
    cand = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,))
    if not cand:
        return None
    x_user = db.one(conn, "SELECT * FROM x_users WHERE x_id=?", (cand["x_id"],)) if cand.get("x_id") else None
    map_person = db.one(conn, "SELECT * FROM map_people WHERE id=?", (cand["map_person_id"],)) if cand.get("map_person_id") else None
    score = db.one(conn, "SELECT * FROM scores WHERE run_id=? AND candidate_id=?", (run_id, candidate_id)) if run_id is not None else None
    if score is None and run_id is not None:  # scored in an earlier run only
        score = db.one(conn, "SELECT * FROM scores WHERE candidate_id=? ORDER BY run_id DESC LIMIT 1", (candidate_id,))
    flags = _flags_list(score.get("flags")) if score else []
    reasons = parse_reasons(score.get("reasons_json")) if score else parse_reasons(None)
    cls = classification_for(conn, run_id, candidate_id) if run_id is not None else None
    if cls is None:
        row = db.one(conn, "SELECT json FROM classifications WHERE candidate_id=? ORDER BY run_id DESC LIMIT 1", (candidate_id,))
        cls = db.jloads(row["json"], {}) if row else None
    paths = paths_for(conn, run_id, candidate_id) if run_id is not None else []
    if not paths:
        paths = db.rows(conn, "SELECT * FROM paths WHERE candidate_id=? AND run_id=(SELECT MAX(run_id) FROM paths WHERE candidate_id=?) "
                              "ORDER BY COALESCE(rank,99), strength DESC", (candidate_id, candidate_id))
        for p in paths:
            p["json_obj"] = db.jloads(p.get("json"), {}) or {}
            p["strength"] = _num(p.get("strength"))
    name = cand.get("name") or (f"@{cand['handle']}" if cand.get("handle") else f"candidate {candidate_id}")
    contact = contacts.contact_for(paths, can_dm=(x_user or {}).get("can_dm"), bio=(x_user or {}).get("bio"),
                                   handle=cand.get("handle"),
                                   researched=contacts.researched_for(contacts.load_routes(), cand.get("handle"), name))
    for p in paths:
        p["line"] = path_line(p, name)
        p["short"] = path_short(p)
    changes = db.rows(conn, "SELECT field, old_value, new_value, reason FROM changes WHERE run_id=? AND candidate_id=? ORDER BY change_id",
                      (run_id, candidate_id)) if run_id is not None else []
    stored = db.jloads(score.get("reasons_json"), {}) if score else {}
    stored = stored if isinstance(stored, dict) else {}
    unknowns: list[str] = []
    for u in list((cls or {}).get("unknowns") or []) + list(stored.get("unknowns") or []):   # classification, then scoring
        if str(u).strip() and str(u) not in unknowns:
            unknowns.append(str(u))
    summary = (cls or {}).get("summary") or stored.get("summary") or ""
    evidence = evidence_for(conn, candidate_id)
    affinity = affinity_view(score.get("affinity") if score else None, stored, cls, evidence)
    engagement = engagement_view(score.get("engagement") if score else None, export.engagement_factor(stored))
    flag_reasons = reasons["factors"].get("flags", {}).get("reasons", [])
    factors = {}
    for f in FACTOR_FIELDS + ("confidence", "total"):
        info = reasons["factors"].get(f, {})
        factors[f] = {"value": _num(score.get(f)) if score else None, "label": FACTOR_LABELS.get(f, f),
                      "reasons": info.get("reasons", []), "evidence_ids": info.get("evidence_ids", [])}
    aff_info = reasons["factors"].get("affinity", {})
    factors["affinity"] = {"value": affinity["value"], "label": "topic affinity",
                           "reasons": [affinity["reason"]] if affinity["reason"] else aff_info.get("reasons", []),
                           "evidence_ids": affinity["evidence_ids"] or aff_info.get("evidence_ids", [])}
    factors["engagement"] = {"value": engagement["value"], "label": "community engagement",
                             "reasons": engagement["reasons"], "evidence_ids": []}
    return {
        "candidate": cand,
        "name": name,
        "handle": cand.get("handle") or "",
        "handle_label": handle_label(cand.get("handle"), cand.get("handle_verified")),
        "x_url": _x_url(cand.get("handle")),
        "section": _section(cand.get("category")),
        "category_label": category_label(cand.get("category")),
        "x_user": x_user,
        "map_person": map_person,
        "score": score,
        "rank": score.get("rank") if score else None,
        "total": _round(score.get("total"), 1) if score else None,
        "factors": factors,
        "affinity": affinity,
        "engagement": engagement,
        "reasons": reasons["overall"],
        "flags": [{"flag": f, "short": flag_short(f), "explanation": flag_explanation(f, cand, x_user),
                   "scoring": [r for r in flag_reasons if r.startswith(f)]} for f in flags],
        "summary": summary,
        "classification": cls,
        "unknowns": unknowns,
        "evidence": evidence,
        "paths": paths,
        "best_path": path_short(paths[0] if paths else None),
        "contact": contact,
        "events": events_for(conn, candidate_id),
        "changes": changes,
        "history": person_history(conn, candidate_id),
        "status": get_status(conn, candidate_id),
    }


# ---------------------------------------------------------------- costs

def run_cost_summary(conn: sqlite3.Connection, run_id: int | None) -> dict:
    """Cost of one run from the ledger: total, per provider, per provider+endpoint. Never raises on an empty ledger.
    Also `since_previous_usd` (every run after the previous scored run up to this one: stage-only runs count too)
    and `to_date_usd` (the whole ledger)."""
    to_date = float(db.one(conn, "SELECT COALESCE(SUM(cost_usd),0) AS c FROM costs")["c"])
    if run_id is None:
        return {"run_id": None, "total_usd": 0.0, "label": costs.fmt(0.0), "by_provider": {}, "rows": [],
                "since_previous_usd": 0.0, "to_date_usd": round(to_date, 6)}
    prev = db.previous_scored_run_id(conn, run_id)
    since = float(db.one(conn, "SELECT COALESCE(SUM(cost_usd),0) AS c FROM costs WHERE run_id > ? AND run_id <= ?",
                         (prev if prev is not None else -1, run_id))["c"])
    rows = db.rows(conn, """
        SELECT provider, endpoint, SUM(units) AS units, SUM(cost_usd) AS cost_usd, COUNT(*) AS calls
        FROM costs WHERE run_id=? GROUP BY provider, endpoint ORDER BY cost_usd DESC""", (run_id,))
    by_provider: dict[str, float] = {}
    for r in rows:
        r["cost_usd"] = float(r.get("cost_usd") or 0.0)
        by_provider[r["provider"]] = round(by_provider.get(r["provider"], 0.0) + r["cost_usd"], 6)
    total = round(sum(by_provider.values()), 6)
    run = db.one(conn, "SELECT cost_usd FROM runs WHERE run_id=?", (run_id,))
    if run and run.get("cost_usd") and total == 0.0:
        total = float(run["cost_usd"])
    return {"run_id": run_id, "total_usd": total, "label": costs.fmt(total), "by_provider": by_provider, "rows": rows,
            "since_previous_usd": round(max(since, total), 6), "to_date_usd": round(max(to_date, total), 6)}


# ---------------------------------------------------------------- status (the only write)

def get_status(conn: sqlite3.Connection, candidate_id: int) -> dict:
    r = db.one(conn, "SELECT * FROM status WHERE candidate_id=?", (candidate_id,))
    return r or {"candidate_id": candidate_id, "status": config.STATUSES[0], "owner": None, "note": None, "updated_at": None}


def set_status(conn: sqlite3.Connection, candidate_id: int, status: str, note: str | None = None,
               owner: str | None = None) -> dict:
    """Upsert the status row for a candidate with updated_at = now. note/owner None keep the stored value; "" clears it."""
    if status not in config.STATUSES:
        raise ValueError(f"status must be one of {config.STATUSES}, got {status!r}")
    if not db.one(conn, "SELECT 1 AS ok FROM candidates WHERE candidate_id=?", (candidate_id,)):
        raise ValueError(f"unknown candidate_id {candidate_id}")
    current = get_status(conn, candidate_id)
    row = {
        "candidate_id": int(candidate_id),
        "status": status,
        "note": current.get("note") if note is None else (note.strip() or None),
        "owner": current.get("owner") if owner is None else (owner.strip() or None),
        "updated_at": db.utcnow(),
    }
    db.upsert(conn, "status", row, keys=("candidate_id",))
    conn.commit()
    return get_status(conn, candidate_id)
