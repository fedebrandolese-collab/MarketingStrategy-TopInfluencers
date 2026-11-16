"""Check an account on demand: would this X account make the wishlist, where would it rank, and why?

The dashboard's "Check an account" tab (app/check_tab.py) is the UI; the orchestration lives here and reuses the
pipeline's own functions:

  parse_handle(text)          '@name', 'name' or an x.com / twitter.com link -> 'name'
  estimate(conn, handle)      the itemised projected cost, from the adapters' own estimators; nothing is called
  known(conn, handle)         the stored candidate, and whether a free Show can answer from stored data
  show_stored(conn, handle)   free: no source call, no LLM; affinity, paths and score from what is stored
  run_check(conn, handle)     paid, once the person accepted the estimate:
      1. official X lookup (the verify source). Missing, suspended or protected: stop, only the lookup spent
      2. the candidates row (discovered_via 'manual', note "checked on demand: <date>")
      3. their posts, as enrich fetches them (discover.run_enrich with candidate_ids), then their latest original posts
         for the engagement KPIs (sample.run_sample with candidate_ids)
      4. one classification (classify.run_classify with candidate_ids), then verify.py's handle verdict (no extra call)
      5. topic affinity and paths for this person only (nothing written to `paths`)
      6. score.score_candidate: the current score code against the latest scored run's context, nothing written
  rank_view(table, person)    where they would rank among that run's people under given weights and filters

A paid check is its own `runs` row with stage 'check': every paid call goes through the adapters' budget guards and lands
in the cost ledger under it. It writes no `scores`, `paths` or `changes` rows, so it never becomes the run the dashboard,
the export or the change log compare against (they key on those tables, db.latest_run_id(with_scores=True)). The person
is an ordinary candidate afterwards: the next scheduled run scores them like anyone else. Every check is saved in
`checks` (result_json holds the whole answer), so a past check reopens for free.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pandas as pd

from . import config, contacts, costs, db, weights

log = logging.getLogger("ring_wishlist.check")

STAGE = "check"
POSTS_PER_USER = 30          # run_enrich's per-person budget (the pipeline's --posts-per-user default)
SAMPLE_POSTS = 30            # run_sample's latest original posts per person (the pipeline's --sample-posts default)
FRESH_DAYS = 30              # run_enrich's refresh window: posts fetched within it are recent enough for a free Show
NOTE_KEY = "checked on demand"
KEEP_OFF_NOTE = "manual check: not for the list"
PROB_LABELS = {"p_accept": "Accepts a ring", "p_wear": "Wears it daily", "p_love": "Loves it and says so"}
SUPPORT = {"p_accept": "accept", "p_wear": "wear", "p_love": "love"}
MISSING = {"p_accept": "no sign they take or try new devices", "p_wear": "no wearable evidence",
           "p_love": "no public praise of a device"}
LIFT = {"p_accept": "a post about trying or buying a new device would lift it",
        "p_wear": "a post about wearing a ring, watch or earbuds every day would lift it",
        "p_love": "a public review or praise of a device would lift it"}
MOVED_BY = (("Topic affinity", "topic affinity"), ("spills over", "their wear/love posts"),
            ("Warm relationship", "a warm path"), ("Competitor tie", "a competitor tie"))
_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})(?:[/?#].*)?$", re.I)
_HANDLE_RE = re.compile(r"^[a-z0-9_]{1,15}$")
_NOT_HANDLES = {"home", "i", "search", "explore", "settings", "intent", "share", "hashtag", "messages", "notifications",
                "compose", "login", "signup", "tos", "privacy"}
FILTERS_OFF = {"us_only": False, "hide_competitor": False, "hide_rejected": False, "bar": False}

Say = Callable[[str], None]


def _quiet(msg: str) -> None:
    log.info("check: %s", msg)


# ---------------------------------------------------------------- input

def parse_handle(text: str | None) -> str | None:
    """'@IcoBrando02', 'icobrando02', 'https://x.com/icobrando02', 'twitter.com/icobrando02/status/1?s=20' ->
    'icobrando02'. None when it cannot be an X handle."""
    s = (text or "").strip()
    m = _URL_RE.match(s)
    h = (m.group(1) if m else s.lstrip("@")).strip().lower()
    return h if _HANDLE_RE.match(h) and h not in _NOT_HANDLES else None


# ---------------------------------------------------------------- cost preview (nothing is called)

def source_names(live: bool | None = None) -> tuple[str, str, str]:
    """(bulk, verify, llm backend) a check uses: pipeline._sources' choice; the fixtures under DRY_RUN unless live."""
    if live is None:
        live = not config.dry_run()
    if not live:
        return "fixture", "fixture", "fixture"
    return config.env("SOURCE_BULK", "monid"), config.env("SOURCE_VERIFY", "x_api"), config.env("LLM_BACKEND", "claude_cli")


def enrich_split(posts_per_user: int = POSTS_PER_USER) -> tuple[int, int]:
    """(evidence posts, recent posts): run_enrich's two searches for one person."""
    from .discover import ENRICH_EVIDENCE_SHARE
    n_evidence = max(1, round(posts_per_user * ENRICH_EVIDENCE_SHARE))
    return n_evidence, max(0, int(posts_per_user) - n_evidence)


def _lookup_cached(conn: sqlite3.Connection, handle: str) -> bool:
    """The official adapter serves a profile it bought within x_api.CACHE_DAYS without a paid call."""
    from .sources import x_api
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=x_api.CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bool(db.one(conn, "SELECT 1 AS ok FROM x_users WHERE source='x_api' AND handle=? AND fetched_at>=?",
                       (handle, cutoff)))


def _items(conn: sqlite3.Connection, handle: str, bulk: str, verify: str, backend: str, posts_per_user: int) -> list[dict]:
    from .discover import estimate_cost
    from .sources import x_api
    cached = verify == "x_api" and _lookup_cached(conn, handle)
    if cached or verify == "fixture":
        look = 0.0
    elif verify == "x_api":
        look = x_api.estimate("users/by/username", 1)
    else:
        look = estimate_cost(SimpleNamespace(name=verify), "user_read", 1)
    items = [{"step": "lookup", "provider": verify, "units": 1, "usd": look,
              "what": "Official X lookup: the account exists, its numeric id, badge and age",
              "note": (f"bought in the last {x_api.CACHE_DAYS} days: served from the cache" if cached
                       else "confirms the account before anything else is spent")}]
    n_ev, n_rec = enrich_split(posts_per_user)
    stub = SimpleNamespace(name=bulk)
    for n, what in ((n_ev, "Posts: device and voice evidence search"), (n_rec, "Posts: recent context search")):
        if n:
            items.append({"step": "posts", "provider": bulk, "units": n, "usd": estimate_cost(stub, "post_read", n),
                          "what": f"{what}, up to {n} posts",
                          "note": "upper bound: billed per post returned; the same search within 20 h is free"
                          if bulk == "monid" else "billed per post returned"})
    items.append({"step": "sample", "provider": bulk, "units": SAMPLE_POSTS,
                  "usd": estimate_cost(stub, "timeline_read", SAMPLE_POSTS),
                  "what": f"Posts: latest originals for the engagement KPIs, up to {SAMPLE_POSTS} posts",
                  "note": "no reposts, no replies; upper bound: billed per post returned"})
    from .classify import EST_USD_PER_CALL
    cls_note = {"claude_cli": "no dollar cost: counts toward the Claude account usage",
                "anthropic": "Anthropic API, guarded by BUDGET_ANTHROPIC_USD",
                "fixture": "fixture answer (DRY_RUN)"}.get(backend, "")
    items.append({"step": "classify", "provider": backend, "units": 1,
                  "usd": EST_USD_PER_CALL if backend == "anthropic" else 0.0,
                  "what": "One Claude classification of the profile and posts", "note": cls_note})
    return items


def estimate(conn: sqlite3.Connection, handle: str, *, live: bool | None = None,
             posts_per_user: int = POSTS_PER_USER) -> dict:
    """Itemised projected cost of a paid check, from the adapters' own estimators (x_api.estimate, and monid.estimate
    through discover.estimate_cost); nothing is called. {"items", "total_usd", "live_total_usd", "dry_run"}:
    live_total_usd is what the same check costs with the live sources (the fixtures of DRY_RUN bill nothing)."""
    items = _items(conn, handle, *source_names(live), posts_per_user)
    live_items = _items(conn, handle, *source_names(True), posts_per_user)
    return {"handle": handle, "items": items, "total_usd": round(sum(i["usd"] for i in items), 6),
            "live_total_usd": round(sum(i["usd"] for i in live_items), 6), "live_items": live_items,
            "dry_run": config.dry_run() if live is None else not live, "posts_per_user": posts_per_user}


# ---------------------------------------------------------------- what is stored

def known(conn: sqlite3.Connection, handle: str) -> dict | None:
    """The stored candidate for this handle, or None. fresh=True when a free Show can answer: an X account, a
    classification and posts fetched (or an enrichment) within FRESH_DAYS."""
    from .discover import enriched_on
    c = db.one(conn, "SELECT * FROM candidates WHERE handle=? ORDER BY (x_id IS NULL), candidate_id LIMIT 1", (handle,))
    if not c:
        return None
    cid = int(c["candidate_id"])
    cls = db.one(conn, "SELECT run_id, created_at FROM classifications WHERE candidate_id=? ORDER BY run_id DESC LIMIT 1",
                 (cid,))
    posts = (db.one(conn, "SELECT COUNT(*) AS n, MAX(fetched_at) AS last FROM x_posts WHERE x_id=?", (c["x_id"],))
             if c.get("x_id") else None) or {"n": 0, "last": None}
    dates = [d for d in ((posts.get("last") or "")[:10], enriched_on(c.get("notes"))) if d]
    last = max(dates) if dates else None
    cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=FRESH_DAYS)).isoformat()
    st = db.one(conn, "SELECT status FROM status WHERE candidate_id=?", (cid,)) or {}
    return {"candidate_id": cid, "handle": handle, "x_id": c.get("x_id"), "name": c.get("name"),
            "classified_at": (cls or {}).get("created_at"), "posts": int(posts.get("n") or 0), "last_fetch": last,
            "fresh": bool(c.get("x_id") and cls and last and last >= cutoff),
            "status": st.get("status") or config.STATUSES[0]}


def past_checks(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return db.rows(conn, """SELECT check_id, handle, candidate_id, run_id, compared_run_id, total, rank_current, rank_default,
                                   n, cost_usd, created_at FROM checks ORDER BY check_id DESC LIMIT ?""", (limit,))


def load_check(conn: sqlite3.Connection, check_id: int) -> dict | None:
    row = db.one(conn, "SELECT * FROM checks WHERE check_id=?", (check_id,))
    if not row:
        return None
    ans = db.jloads(row.get("result_json"), {}) or {}
    ans.update({"check_id": row["check_id"], "cost_usd": row.get("cost_usd") or 0.0})
    return ans


# ---------------------------------------------------------------- ranking (pure)

def _reach_fields(contact: Mapping[str, Any]) -> dict:
    return {"contact": contact.get("best"), "contactable": bool(contact.get("contactable")),
            "contact_reason": contact.get("contact_reason") or ""}


def bar_fields(category: Any, p_wear: Mapping[str, Any] | None, contact: Mapping[str, Any] | None = None) -> dict:
    """The ranked_table columns of the brief's bar for one person: wear (the verdict), wear_evidence, bar_reason and,
    with a contacts.contact_for result, contact, contactable and contact_reason (the reach half)."""
    wear = weights.wear_verdict(p_wear or {})
    out = {"wear": wear, "wear_evidence": wear == "for"}
    if contact is not None:
        out.update(_reach_fields(contact))
    reach = True if contact is None or contact.get("contactable") else contact.get("contact_reason")
    return {**out, "bar_reason": weights.bar_reason(category, wear, reach)}


def with_bar(person: Mapping[str, Any], because: list[dict] | None, contact: Mapping[str, Any] | None = None) -> dict:
    """A saved answer's person with the bar's columns; checks saved before the bar get them from the stored p_wear line
    (value and prior rounded to 0.01). contact (contact_now) sets the reach half from today's routes: a check saved
    before reach, or before the research that found a route, reads right."""
    p = dict(person)
    if "wear_evidence" not in p:
        wear = next((b for b in because or [] if b.get("field") == "p_wear"), None) or {}
        p.update(bar_fields(p.get("category"), wear))
    if contact is None:
        return p
    wear = p.get("wear") or ("for" if p.get("wear_evidence") else
                             "against" if weights.BAR_AGAINST in str(p.get("bar_reason") or "") else "none")
    p.update(_reach_fields(contact))
    p["bar_reason"] = weights.bar_reason(p.get("category"), wear, True if p["contactable"] else p["contact_reason"])
    return p


def contact_of(conn: sqlite3.Connection, cid: int, paths: list[dict] | None) -> dict:
    """contacts.contact_for for one person: their paths (the check's own, best first), X DMs and bio as stored now, and
    the research files as they are now."""
    cand = db.one(conn, "SELECT handle, name, x_id FROM candidates WHERE candidate_id=?", (cid,)) or {}
    xu = (db.one(conn, "SELECT can_dm, bio FROM x_users WHERE x_id=?", (cand["x_id"],)) if cand.get("x_id") else None) or {}
    return contacts.contact_for(paths, can_dm=xu.get("can_dm"), bio=xu.get("bio"), handle=cand.get("handle"),
                                researched=contacts.researched_for(contacts.load_routes(), cand.get("handle"), cand.get("name")))


def contact_now(conn: sqlite3.Connection, ans: Mapping[str, Any]) -> dict:
    """A saved answer's contact routes, recomputed from its paths and today's X data and research."""
    return contact_of(conn, int(ans["candidate_id"]), ans.get("paths") or [])


def rank_view(table: pd.DataFrame | None, person: Mapping[str, Any], w: Mapping[str, Any] | None = None,
              filters: Mapping[str, Any] | None = None, baseline: Mapping[str, Any] | None = None) -> dict:
    """Where `person` (a ranked_table-like row) would rank among `table`'s people (data.ranked_table of the compared run,
    precise=True) under weights `w`, and under `baseline` (the default). The others are filtered as the list is; the
    person is always placed, so an account the filters hide still shows where it would rank (hidden_by says which).
    Their own row in `table`, if any, is replaced by `person`."""
    f = {**weights.DEFAULT_FILTERS, **(filters or {})}
    cid = int(person["candidate_id"])
    others = table if table is not None and len(table) else pd.DataFrame()
    if len(others):
        others = weights.filter_rows(others[others["candidate_id"] != cid], f)
    me_row = {**dict(person), "flags": list(person.get("flags") or [])}
    combined = pd.concat([others, pd.DataFrame([me_row])], ignore_index=True) if len(others) else pd.DataFrame([me_row])
    ranked = weights.rerank(combined, w, {**FILTERS_OFF, "n": f["n"]}, baseline=baseline)
    me = ranked[ranked["candidate_id"] == cid].iloc[0]
    rank = int(me["rank"])

    def neighbour(k: int) -> dict | None:
        rows = ranked[ranked["rank"] == k]
        if not len(rows):
            return None
        r = rows.iloc[0]
        handle = r.get("handle") if isinstance(r.get("handle"), str) and r.get("handle") else None
        return {"rank": k, "candidate_id": int(r["candidate_id"]), "handle": handle,
                "name": r.get("name") if isinstance(r.get("name"), str) else None, "total": float(r["total"])}

    hidden = weights.hidden_by(me_row, f)   # the list's own filter source, the brief's bar included
    return {"rank": rank, "rank_default": int(me["rank_default"]), "n": int(len(ranked)), "total": float(me["total"]),
            "total_default": float(me["total_default"]), "above": neighbour(rank - 1), "below": neighbour(rank + 1),
            "hidden_by": hidden, "list_size": int(f["n"]), "in_list": rank <= int(f["n"]) and not hidden,
            "filters": {k: f[k] for k in weights.DEFAULT_FILTERS},
            "weights_differ": weights.normalize(w) != weights.normalize(baseline)}


# ---------------------------------------------------------------- the answer (pure over the score result)

def _moved_by(reasons: list[str]) -> list[str]:
    return [label for needle, label in MOVED_BY if any(needle in str(r) for r in reasons or [])]


def _quote(e: dict) -> dict:
    return {"text": " ".join(str(e.get("text") or "").split()), "url": e.get("url"), "date": e.get("date"),
            "kind": e.get("kind"), "weight": e.get("weight")}


def because(field: str, f: Mapping[str, Any], evidence: Mapping[int, dict], events: Mapping[int, dict]) -> dict:
    """One probability in plain words: the value, and the strongest quote behind it (highest weight, then newest) with
    its link, or the event, or 'no evidence, prior only'."""
    key = SUPPORT[field]
    rows = [evidence[i] for i in f.get("evidence_ids") or [] if i in evidence]
    pos = [e for e in rows if e.get("supports") == key and (e.get("polarity") or 1) > 0]
    neg = [e for e in rows if (e.get("supports") == key and (e.get("polarity") or 1) < 0) or e.get("supports") == "competitor"]
    for group in (pos, neg):
        group.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
        group.sort(key=lambda e: -float(e.get("weight") or 0.0))
    evs = [events[i] for i in f.get("event_ids") or [] if i in events]
    value, prior = float(f.get("value") or 0.0), f.get("prior")
    backed = bool(f.get("backed"))
    moved = _moved_by(f.get("reasons") or [])
    if pos:
        basis = "quote"
        note = f"{len(pos)} supporting quote{'s' if len(pos) != 1 else ''}" if len(pos) > 1 else "one supporting quote"
    elif evs:
        basis, note = "event", (evs[-1].get("text") or evs[-1].get("kind") or "an event").strip()
    elif backed:
        basis = "other"
        note = ("backed by their topic affinity" if "topic affinity" in moved else "backed by other signals")
    elif prior is not None and abs(value - float(prior)) >= 0.005:
        basis = "moved"
        note = f"no direct evidence: the prior {float(prior):.2f}, moved by {' and '.join(moved) or 'other signals'}"
    else:
        basis, note = "prior", "no evidence, prior only"
    return {"field": field, "label": PROB_LABELS[field], "value": round(value, 2),
            "prior": None if prior is None else round(float(prior), 2), "backed": backed, "basis": basis, "note": note,
            "quote": _quote(pos[0]) if pos else None, "against": _quote(neg[0]) if neg else None,
            "event": ({"text": evs[-1].get("text"), "url": evs[-1].get("url"), "date": evs[-1].get("date"),
                       "kind": evs[-1].get("kind")} if evs and not pos else None)}


def what_moves(result: Mapping[str, Any], followers: int | None = None) -> str:
    """One line on what would move them up: the lowest probability without evidence (in a product the smallest factor
    is the biggest lever), else a small audience."""
    fs = result.get("factors") or {}
    unbacked = [(k, fs[k]) for k in PROB_LABELS if k in fs and not fs[k].get("backed")]
    if unbacked:
        k, f = min(unbacked, key=lambda kv: (float(kv[1].get("value") or 0.0), list(PROB_LABELS).index(kv[0])))
        word, value, prior = SUPPORT[k], float(f.get("value") or 0.0), f.get("prior")
        if prior is None or abs(value - float(prior)) < 0.005:
            s = f"{MISSING[k]}: {word} stays at its prior {value:.2f}"
        else:
            s = f"{MISSING[k]}: {word} is {value:.2f}, its prior {float(prior):.2f} moved only by " + \
                (" and ".join(_moved_by(f.get("reasons") or [])) or "other signals")
        rest = [SUPPORT[o] for o, _ in unbacked if o != k]
        s += f" ({' and '.join(rest)} too); {LIFT[k]}." if rest else f"; {LIFT[k]}."
        return s[0].upper() + s[1:]
    impact = float(result.get("impact") or 0.0)
    if impact < 0.3:
        who = f" ({followers:,} followers)" if followers is not None else ""
        return f"All three chances rest on evidence; what holds them back is reach: impact {impact:.2f}{who}."
    return "All three chances rest on evidence; only more of it, or a bigger audience, would lift them."


def build_answer(conn: sqlite3.Connection, result: dict, paths: list[dict], compared_run_id: int | None, *,
                 mode: str, lookup: dict | None = None, steps: dict | None = None) -> dict:
    """The JSON-safe answer the tab renders and `checks.result_json` stores (the rank is added by save())."""
    cid = int(result["candidate_id"])
    cand = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (cid,)) or {}
    xu = db.one(conn, "SELECT * FROM x_users WHERE x_id=?", (cand["x_id"],)) if cand.get("x_id") else None
    xu = xu or {}
    status = db.one(conn, "SELECT status, note FROM status WHERE candidate_id=?", (cid,)) or {}
    evidence = {e["evidence_id"]: e for e in db.rows(conn, "SELECT * FROM evidence WHERE candidate_id=?", (cid,))}
    events = {e["event_id"]: e for e in db.rows(conn, "SELECT * FROM events WHERE candidate_id=?", (cid,))}
    posts = db.one(conn, "SELECT COUNT(*) AS n, AVG(likes+reposts+replies+quotes) AS eng FROM x_posts WHERE x_id=?",
                   (cand.get("x_id"),)) if cand.get("x_id") else None
    fs = result["factors"]
    contact = contact_of(conn, cid, paths)
    person = {"candidate_id": cid, "name": cand.get("name") or (f"@{cand['handle']}" if cand.get("handle") else f"candidate {cid}"),
              "handle": cand.get("handle") or "", "handle_verified": cand.get("handle_verified"),
              "category": result.get("category"), "status": status.get("status") or config.STATUSES[0],
              **{k: result.get(k) for k in ("p_accept", "p_wear", "p_love", "impact", "reach", "affinity", "engagement",
                                            "confidence", "total")},
              "flags": list(result.get("flags") or []), **bar_fields(result.get("category"), fs.get("p_wear"), contact)}
    aff = fs.get("affinity") or {}
    followers = xu.get("followers")
    eng = (fs.get("impact") or {}).get("engagement") or {}
    return {
        "ok": True, "mode": mode, "handle": person["handle"], "candidate_id": cid, "name": person["name"],
        "x_url": f"https://x.com/{person['handle']}" if person["handle"] else None,
        "checked_at": db.utcnow(), "compared_run_id": compared_run_id,
        "compared_run_date": ((db.one(conn, "SELECT started_at FROM runs WHERE run_id=?", (compared_run_id,)) or {})
                              .get("started_at") or "")[:10] if compared_run_id is not None else None,
        "person": person, "summary": result.get("summary") or "", "section": result.get("section"),
        "because": [because(k, fs.get(k) or {}, evidence, events) for k in PROB_LABELS],
        "impact": {"value": result.get("impact"), "followers": followers, "posts": int((posts or {}).get("n") or 0),
                   "engagement": eng.get("avg") if eng.get("avg") is not None else
                   (round(float(posts["eng"]), 1) if posts and posts.get("eng") is not None else None),
                   "reasons": (fs.get("impact") or {}).get("reasons") or []},
        "affinity": {"value": result.get("affinity"), "reason": aff.get("reason"), "top_posts": aff.get("top_posts") or []},
        "engagement": fs.get("engagement"),   # the community engagement factor: value, short, reasons, kpis
        "flags": person["flags"], "flag_reasons": (fs.get("flags") or {}).get("reasons") or [],
        "candidate": {k: cand.get(k) for k in ("competitor_detail", "handle_verified", "discovered_via", "notes")},
        "x_user": {k: xu.get(k) for k in ("location", "followers", "created_at", "verified", "verified_type", "bio")},
        "paths": paths, "contact_routes": contact["routes"], "unknowns": result.get("unknowns") or [],
        "moves": what_moves(result, followers), "lookup": lookup or {}, "steps": steps or {},
        "status_note": status.get("note"),
    }


# ---------------------------------------------------------------- persistence

def save(conn: sqlite3.Connection, ans: dict, table: pd.DataFrame | None = None, w: Mapping[str, Any] | None = None,
         filters: Mapping[str, Any] | None = None, baseline: Mapping[str, Any] | None = None,
         weights_label: str | None = None) -> dict:
    """Rank the answer (when there is a table to rank in) and write its `checks` row. Returns the answer with check_id."""
    rv = rank_view(table, ans["person"], w, filters, baseline) if ans.get("ok") and table is not None else None
    ans["rank_at_check"] = rv
    ans["weights_label"] = weights_label
    cur = conn.execute(
        "INSERT INTO checks (handle, candidate_id, run_id, compared_run_id, total, rank_current, rank_default, n, cost_usd, "
        "result_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ans["handle"], ans.get("candidate_id"), ans.get("run_id"), ans.get("compared_run_id"),
         (ans.get("person") or {}).get("total"), rv["rank"] if rv else None, rv["rank_default"] if rv else None,
         rv["n"] if rv else None, float(ans.get("cost_usd") or 0.0), db.jdumps(ans), db.utcnow()))
    conn.commit()
    ans["check_id"] = int(cur.lastrowid)
    return ans


# ---------------------------------------------------------------- the check itself

def _paths_for(conn: sqlite3.Connection, cid: int, now: str) -> list[dict]:
    """paths.candidate_paths for one person (best first), NOT written to `paths`: a check run must hold no paths rows,
    or an export-only run would take its one row for everyone's (export._pick_run)."""
    from . import paths as paths_mod
    cand = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (cid,))
    ctx = paths_mod.Context(conn, config.carlo_handle(), paths_mod.parse_ts(now))
    return paths_mod.candidate_paths(ctx, cand)


def assess(conn: sqlite3.Connection, cid: int, compared_run_id: int | None, say: Say = _quiet) -> tuple[dict, list[dict]]:
    """Topic affinity and paths for this person only, then the score with the current code: nothing is written."""
    from . import affinity as aff_mod
    from . import score
    now = db.utcnow()
    say("Topic affinity and paths for this person")
    out = aff_mod.topic_affinity(conn, [cid], now=now, window_days=score.AFFINITY_WINDOW_DAYS) or {}
    aff = out.get(cid) or out.get(str(cid))
    ps = _paths_for(conn, cid, now)
    best = ps[0] if ps else None
    path = {"how": best["how"], "strength": best["strength"], "via_name": best.get("via")} if best else None
    say(f"Scoring with the current code against run {compared_run_id} (nothing written)")
    return score.score_candidate(conn, cid, compared_run_id, affinity=aff, path=path), ps


def show_stored(conn: sqlite3.Connection, handle: str, *, table: pd.DataFrame | None = None,
                w: Mapping[str, Any] | None = None, filters: Mapping[str, Any] | None = None,
                baseline: Mapping[str, Any] | None = None, weights_label: str | None = None, say: Say = _quiet) -> dict:
    """The free answer from stored posts and classification: no source call, no LLM, no runs row."""
    k = known(conn, handle)
    if not k or not k["fresh"]:
        raise ValueError(f"@{handle} has no recent stored posts and classification: run a check instead")
    compared = db.latest_run_id(conn, with_scores=True)
    say(f"Stored data: {k['posts']} posts (last fetched {k['last_fetch']}), classification of "
        f"{(k['classified_at'] or '')[:10]}. No source call, no LLM, no cost.")
    result, ps = assess(conn, k["candidate_id"], compared, say)
    ans = build_answer(conn, result, ps, compared, mode="stored")
    ans.update({"run_id": None, "cost_usd": 0.0})
    return save(conn, ans, table, w, filters, baseline, weights_label)


def _age_years(created_at: str | None) -> float | None:
    from .verify import _age_years as age
    return age(created_at)


def handle_verdict(conn: sqlite3.Connection, cid: int, u: Any, source_name: str) -> tuple[str, str]:
    """verify.py's verdict for this one account from the lookup already paid for (no extra call): the same rule, the
    same columns and the same 'handle check' evidence row. Runs after classify, whose verdict it takes into account."""
    from .verify import PARODY
    c = db.one(conn, "SELECT x_id, handle, name, handle_verified FROM candidates WHERE candidate_id=?", (cid,)) or {}
    llm_verdict = c.get("handle_verified")
    text = f"{u.name or ''} {u.bio or ''}"
    if c.get("x_id") and str(u.x_id) != str(c["x_id"]):
        verdict, why = "suspicious", f"handle now belongs to a different account (id {u.x_id}, stored {c['x_id']})"
    elif PARODY.search(text) or llm_verdict == "suspicious":
        verdict, why = "suspicious", "parody or fan signals in name/bio, or the classifier flagged it"
    else:
        age = _age_years(u.created_at)
        badge = bool(u.verified) and (u.verified_type or "none") != "none"
        verdict = "verified" if badge or ((age or 0) >= 2 and (u.followers or 0) >= 2000) else "likely_real"
        why = (f"official X lookup confirms id {u.x_id}; badge={u.verified_type or 'none'}; account age {age:.1f}y; "
               f"{u.followers or 0:,} followers" if age is not None else
               f"official X lookup confirms id {u.x_id}; badge={u.verified_type or 'none'}")
    now = db.utcnow()
    conn.execute("UPDATE candidates SET handle_verified=?, handle_verification_method=? WHERE candidate_id=?",
                 (verdict, f"{source_name} id check + signals{' + llm' if llm_verdict else ''} (manual check)", cid))
    conn.execute("""INSERT OR IGNORE INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (cid, "profile_signal", "authenticity", -1 if verdict == "suspicious" else 1,
                  0.8 if verdict == "verified" else 0.5, f"https://x.com/{u.handle}", now[:10],
                  f"handle check: {verdict}. {why}", source_name, now))
    conn.commit()
    return verdict, why


def _run_cost(conn: sqlite3.Connection, run_id: int) -> float:
    return float(db.one(conn, "SELECT COALESCE(SUM(cost_usd),0) AS c FROM costs WHERE run_id=?", (run_id,))["c"])


def run_check(conn: sqlite3.Connection, handle: str, *, table: pd.DataFrame | None = None,
              w: Mapping[str, Any] | None = None, filters: Mapping[str, Any] | None = None,
              baseline: Mapping[str, Any] | None = None, weights_label: str | None = None, say: Say = _quiet,
              sources: tuple | None = None, posts_per_user: int = POSTS_PER_USER) -> dict:
    """The paid check, once the person accepted estimate() (see the module docstring). Returns the saved answer
    (check_id set; ok False with a reason when it stopped). `sources` = (bulk, verify) overrides pipeline._sources."""
    from .classify import run_classify
    from .discover import run_enrich, upsert_candidate
    from .sample import run_sample
    from .sources.base import persist_users
    h = parse_handle(handle)
    if not h:
        raise ValueError(f"not an X handle: {handle!r}")
    compared = db.latest_run_id(conn, with_scores=True)
    run_id = db.new_run(conn, STAGE, notes=f"check @{h}")
    today = db.utcnow()[:10]
    steps: dict[str, Any] = {}
    ans: dict[str, Any] = {"ok": False, "mode": "paid", "handle": h, "candidate_id": None, "run_id": run_id,
                           "compared_run_id": compared, "checked_at": db.utcnow()}
    try:
        if sources is None:
            from .pipeline import _sources
            sources = _sources(conn, run_id)
        bulk, verify = sources
        say(f"Looking up @{h} on X ({verify.name})")
        try:
            u = verify.get_user_by_handle(h)
        except costs.BudgetExceeded as e:
            ans["reason"] = f"Stopped by the budget guard before the lookup: {e}"
            u = None
        except Exception as e:  # noqa: BLE001 - token missing, network: say so, nothing else is spent
            ans["reason"] = f"The X lookup failed: {e}"
            u = None
        if u is None and not ans.get("reason"):
            ans["reason"] = (f"X has no active account @{h}: it does not exist, was renamed or is suspended. "
                             "Only the lookup was spent.")
        elif u is not None and (u.raw or {}).get("protected"):
            ans["reason"] = f"@{u.handle} is protected: its posts are private, so there is nothing to read. Only the lookup was spent."
            u = None
        if u is None:
            say(ans["reason"])
            return ans
        if getattr(verify, "conn", None) is None:     # adapters with a connection persist what they fetch themselves
            persist_users(conn, [u], verify.name)
        age = _age_years(u.created_at)
        steps["lookup"] = {"x_id": u.x_id, "followers": u.followers, "verified_type": u.verified_type or "none",
                           "created_at": u.created_at, "age_years": None if age is None else round(age, 1),
                           "location": u.location, "source": verify.name}
        say(f"Found @{u.handle}: id {u.x_id}, {u.followers or 0:,} followers, badge {u.verified_type or 'none'}"
            + (f", account {age:.1f} years old" if age is not None else ""))
        cid, created = upsert_candidate(conn, u.x_id, u.handle, u.name, "manual", note=f"{NOTE_KEY}: {today}")
        conn.commit()
        ans["candidate_id"] = cid
        say(("New candidate" if created else "Already a candidate") + f" #{cid}; fetching up to {posts_per_user} posts ({bulk.name})")
        enr = run_enrich(conn, run_id, bulk, top=1, posts_per_user=posts_per_user, candidate_ids=[cid],
                         seed_handle=config.carlo_handle())
        steps["posts"] = {k: enr.get(k) for k in ("posts", "posts_new", "evidence_posts", "errors", "budget_stop")}
        say(f"{enr.get('posts', 0)} posts of their own ({enr.get('posts_new', 0)} new)"
            + (", the fetch failed" if enr.get("errors") else "") + (", stopped by the budget guard" if enr.get("budget_stop") else ""))
        say(f"Fetching their latest {SAMPLE_POSTS} original posts for the engagement KPIs ({bulk.name})")
        smp = run_sample(conn, run_id, bulk, candidate_ids=[cid], posts=SAMPLE_POSTS)
        steps["sample"] = {k: smp.get(k) for k in ("posts", "errors", "budget_stop")}
        say(f"{smp.get('posts', 0)} latest original posts sampled"
            + (", the fetch failed" if smp.get("errors") else "") + (", stopped by the budget guard" if smp.get("budget_stop") else ""))
        say("Classifying with Claude (one call)")
        cls = run_classify(conn, run_id, candidate_ids=[cid], workers=1, seed_handle=config.carlo_handle())
        steps["classify"] = {k: cls.get(k) for k in ("classified", "errors", "skipped_no_data", "budget_stop")}
        say("Classified" if cls.get("classified") else
            "Not classified (no bio and no posts)" if cls.get("skipped_no_data") else
            "Classification failed: the score uses the category priors")
        verdict, why = handle_verdict(conn, cid, u, verify.name)
        steps["lookup"].update({"verdict": verdict, "why": why})
        say(f"Handle check: {verdict}")
        result, ps = assess(conn, cid, compared, say)
        ans = {**build_answer(conn, result, ps, compared, mode="paid", lookup=steps["lookup"], steps=steps),
               "run_id": run_id}
    except costs.BudgetExceeded as e:
        ans["ok"], ans["reason"] = False, f"Stopped by the budget guard: {e}"
        say(ans["reason"])
    except Exception as e:  # noqa: BLE001 - the tab shows the reason; what was spent stays in the ledger under run_id
        log.exception("check @%s failed", h)
        ans["ok"], ans["reason"] = False, f"The check failed: {e}"
        say(ans["reason"])
    finally:
        db.finish_run(conn, run_id)
        ans["cost_usd"] = round(_run_cost(conn, run_id), 6)
        if "check_id" not in ans:
            ans = save(conn, ans, table, w, filters, baseline, weights_label)
    return ans
