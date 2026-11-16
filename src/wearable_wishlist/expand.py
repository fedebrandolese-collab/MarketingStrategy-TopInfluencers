"""Stage expand: people our best people follow.
Discovery had three doors (the operator's follow graph, 23 narrow topic and competitor searches, the map), so recall rested on a
tiny sample: @simonw (227k followers, writes daily about LLMs and agents) came through none of them, and the crawl's bio
filter (crawl_verdict 'no_signal') would have dropped him anyway. This stage asks the network instead of the bio:
  1. seeds: the top N (default 12) of the latest scored run under the dashboard's default filters (US or unknown, status
     not rejected, no competitor flag), with confidence >= 0.67 and >= 5,000 followers. Deterministic (stored rank, then
     total, then candidate_id) and printed.
  2. who each seed follows, up to max_following (default 1000), through source.get_following (monid: the kaito follow
     scraper, $0.000225 per row, at least 200 rows billed per list; the 7-day follow-list cache serves a fresh list for
     $0), persisted to x_users / x_follows.
  3. co-follows: how many seeds follow each account. A new candidate needs >= max(3, 20% of the seeds) co-follows,
     >= 5,000 followers, no parody / org / spam / brand signs (the discover helpers) and no candidates row yet: the
     network's vouching replaces the bio-keyword requirement. discovered_via='cofollow:<k>/<n>', note
     'cofollow:<k>/<n> seeds:@a,@b,...' (PRIORITY_WEIGHTS['cofollow'] reads it, so enrichment ranks them like strong
     topical evidence). Known candidates at the threshold get the same note, and their counts are reported.
The projected cost is printed before any paid call; the stage budget (_Budget) and the adapter's guard both apply, and
the adapter writes the cost ledger. `python -m wearable_wishlist.expand` projects the whole paid pass (expand + the `authors`
queries + enrichment of the new people) without spending anything."""
from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from collections import Counter, defaultdict

from . import config, costs, db
from .discover import (_Budget, bulk_upsert_candidates, competitor_brand_account, estimate_cost, load_queries,
                       looks_brand_account, looks_org, looks_parody, looks_spam, persist_follows_batch, seed_x_id)
from .sources.base import XUser, norm_handle

log = logging.getLogger("wearable_wishlist.expand")

SEEDS = 12
MAX_FOLLOWING = 1000
SEED_MIN_CONFIDENCE = 0.67
SEED_MIN_FOLLOWERS = 5000
MIN_COFOLLOWS = 3
MIN_COFOLLOW_SHARE = 0.20
MIN_FOLLOWERS = 5000
WATCH = ("simonw",)          # always reported: the recall miss that motivated this stage
_USER_FIELDS = ("x_id", "handle", "name", "bio", "location", "followers", "following", "verified", "verified_type",
                "created_at", "can_dm")


def cofollow_threshold(n_seeds: int, min_k: int = MIN_COFOLLOWS, min_share: float = MIN_COFOLLOW_SHARE) -> int:
    """Co-follows a new candidate needs: max(min_k, min_share of the seeds) rounded up (12 seeds -> 3, 16 -> 4)."""
    return max(int(min_k), math.ceil(round(min_share * n_seeds, 9)))


def select_seeds(conn: sqlite3.Connection, n: int = SEEDS, run_id: int | None = None,
                 min_confidence: float = SEED_MIN_CONFIDENCE, min_followers: int = SEED_MIN_FOLLOWERS) -> list[dict]:
    """The top n of a scored run (default: the latest) that pass the dashboard's default filters (no 'not_us' flag,
    status not rejected) with no competitor flag, confidence >= min_confidence, >= min_followers and a known X account."""
    rid = run_id if run_id is not None else db.latest_run_id(conn, with_scores=True)
    if rid is None:
        return []
    rows = db.rows(conn, """
        SELECT s.rank, s.total, s.confidence, s.flags, c.candidate_id, c.x_id, c.competitor_flag,
               COALESCE(c.handle, u.handle) AS handle, COALESCE(u.name, c.name) AS name, u.followers, u.following,
               st.status
        FROM scores s JOIN candidates c ON c.candidate_id = s.candidate_id
        LEFT JOIN x_users u ON u.x_id = c.x_id LEFT JOIN status st ON st.candidate_id = s.candidate_id
        WHERE s.run_id = ? ORDER BY COALESCE(s.rank, 999999), s.total DESC, c.candidate_id""", (rid,))
    out = []
    for r in rows:
        flags = [str(f) for f in (db.jloads(r["flags"], []) or [])]
        if ("not_us" in flags or any(f.startswith("competitor") for f in flags) or r["competitor_flag"]
                or r["status"] == "rejected" or not r["x_id"] or not r["handle"]
                or (r["confidence"] or 0) < min_confidence - 1e-9 or (r["followers"] or 0) < min_followers):
            continue
        out.append({**r, "run_id": rid})
        if len(out) >= n:
            break
    return out


def plan_follow_reads(source, seeds: list[dict], max_following: int = MAX_FOLLOWING) -> list[dict]:
    """Per seed, from local state only: rows expected (their following count, capped at max_following), whether the
    source's follow-list cache would serve the list, and the projected USD (0 when cached; monid bills >= 200 rows)."""
    cached_fn = getattr(source, "_cached_follow_list", None)
    plan = []
    for s in seeds:
        cached = cached_fn(s["handle"], "following", max_following) is not None if cached_fn else False
        rows = min(int(max_following), int(s["following"])) if s.get("following") else int(max_following)
        plan.append({"handle": s["handle"], "rows": rows, "cached": cached,
                     "cost_usd": 0.0 if cached else estimate_cost(source, "follow_read", rows)})
    return plan


def cofollow_verdict(u: XUser, min_followers: int = MIN_FOLLOWERS) -> str | None:
    """Why a co-followed account is not made a candidate, or None when it is."""
    if looks_parody(u.name, u.bio):
        return "parody"
    if looks_org(u.name, u.bio, u.verified_type):
        return "org"
    if looks_spam(u.name, u.bio):
        return "spam"
    if competitor_brand_account(u.handle, u.name) or looks_brand_account(u.name, u.handle, u.bio, u.verified_type):
        return "brand"
    if (u.followers or 0) < min_followers:
        return "small_audience"
    return None


def _user_row(conn, handle: str) -> XUser | None:
    r = db.one(conn, f"SELECT {','.join(_USER_FIELDS)} FROM x_users WHERE handle=?", (norm_handle(handle),))
    return XUser(**{k: r[k] for k in _USER_FIELDS}) if r else None


def run_expand(conn: sqlite3.Connection, run_id: int, source, seeds: int = SEEDS, max_following: int = MAX_FOLLOWING,
               budget_usd: float | None = None, seed_handle: str | None = None, min_cofollows: int = MIN_COFOLLOWS,
               min_share: float = MIN_COFOLLOW_SHARE, min_followers: int = MIN_FOLLOWERS,
               watch: tuple[str, ...] = WATCH) -> dict:
    """Seeds -> their following lists -> co-follow counts -> new candidates (discovered_via='cofollow:<k>/<n>')."""
    now = db.utcnow()
    chosen = select_seeds(conn, seeds)
    stats: dict = {"seeds": [s["handle"] for s in chosen], "lists": 0, "lists_cached": 0, "seeds_failed": 0,
                   "seeds_skipped_budget": 0, "accounts": 0, "threshold": None, "at_threshold": 0, "candidates_new": 0,
                   "known_stamped": 0, "skipped": {}, "new": [], "known": [], "watch": {}, "estimated_cost_usd": 0.0,
                   "cost_usd": 0.0}
    if not chosen:
        log.warning("expand: no seeds (nothing scored yet, or nobody passes the seed filters)")
        return stats
    print(f"[expand] {len(chosen)} seeds from scored run {chosen[0]['run_id']}: "
          + ", ".join(f"#{s['rank']} @{s['handle']}" for s in chosen))
    plan = plan_follow_reads(source, chosen, max_following)
    paid = [p for p in plan if not p["cached"]]
    print(f"[cost] {source.name} expand: ~{costs.fmt(sum(p['cost_usd'] for p in plan))} for {len(paid)} following lists "
          f"(~{sum(p['rows'] for p in paid)} rows, at most {max_following} each); {len(plan) - len(paid)} served from cache")
    budget = _Budget(conn, source, run_id, budget_usd)
    lists: dict[str, list[XUser]] = {}
    for s, p in zip(chosen, plan):
        if not budget.reserve(p["cost_usd"]):
            stats["seeds_skipped_budget"] += 1
            continue
        try:
            following = list(source.get_following(s["handle"], max_users=max_following))
        except costs.BudgetExceeded as e:
            log.warning("expand: adapter budget guard stopped at @%s: %s", s["handle"], e)
            budget.settle(p["cost_usd"], 0.0)
            budget.stopped = True
            stats["seeds_skipped_budget"] += 1
            continue
        except Exception as e:   # one failing list must not lose the others
            log.warning("expand: following of @%s failed, skipped: %s", s["handle"], e)
            stats["seeds_failed"] += 1
            continue
        if p["cached"]:
            stats["lists_cached"] += 1
        else:   # a cached list is already in x_follows; re-writing it would only move fetched_at
            budget.settle(p["cost_usd"], estimate_cost(source, "follow_read", len(following)))
            persist_follows_batch(conn, s["x_id"], following, "following", source.name, now)
            conn.commit()
        lists[s["handle"]] = following
        log.info("expand: @%s follows %d accounts%s", s["handle"], len(following), " (cache)" if p["cached"] else "")
    stats["lists"] = n = len(lists)
    k_min = stats["threshold"] = cofollow_threshold(n, min_cofollows, min_share)

    followed_by: dict[str, list[str]] = defaultdict(list)       # x_id -> seed handles, in seed rank order
    users: dict[str, XUser] = {}
    for h, following in lists.items():
        for u in {u.x_id: u for u in following if u.x_id}.values():
            followed_by[u.x_id].append(h)
            users.setdefault(u.x_id, u)
    stats["accounts"] = len(followed_by)
    known = {r["x_id"]: r["candidate_id"] for r in db.rows(conn, "SELECT candidate_id, x_id FROM candidates WHERE x_id IS NOT NULL")}
    known_h = {r["handle"]: r["candidate_id"] for r in db.rows(
        conn, "SELECT candidate_id, handle FROM candidates WHERE x_id IS NULL AND handle IS NOT NULL")}
    operator_user = seed_x_id(conn, seed_handle)
    entries, skipped = [], Counter()
    for x_id, by in sorted(followed_by.items(), key=lambda kv: (-len(kv[1]), users[kv[0]].handle or "")):
        k = len(by)
        if k < k_min:
            break
        u = users[x_id]
        if x_id == operator_user or not u.handle:
            continue
        stats["at_threshold"] += 1
        note = f"cofollow:{k}/{n} seeds:" + ",".join(f"@{h}" for h in by)
        cid = known.get(x_id) or known_h.get(norm_handle(u.handle))
        if cid:
            stats["known"].append(f"@{u.handle} {k}/{n}")
        else:
            why = cofollow_verdict(u, min_followers)
            if why:
                skipped[why] += 1
                continue
            stats["new"].append(f"@{u.handle} {k}/{n}")
        entries.append((x_id, u.handle, u.name, f"cofollow:{k}/{n}", note))
    stats["candidates_new"], stats["known_stamped"] = bulk_upsert_candidates(conn, entries, now)
    conn.commit()
    stats["skipped"] = dict(skipped)
    stats["new"], stats["known"] = stats["new"][:25], stats["known"][:25]     # the strongest; all of them are in notes

    for h in watch:   # would co-follow alone have found them? (their own list, if they are a seed, does not count)
        u = _user_row(conn, h)
        by = [s for s in followed_by.get(u.x_id, []) if s != norm_handle(h)] if u else []
        others = n - (1 if norm_handle(h) in lists else 0)
        why = cofollow_verdict(u, min_followers) if u else "not_seen"
        stats["watch"][h] = {"cofollows": len(by), "of_seeds": others, "threshold": k_min, "seeds": by, "filter": why,
                             "would_be_found": len(by) >= k_min and why is None}
        print(f"[expand] @{h}: followed by {len(by)} of the other {others} seeds ({', '.join('@' + s for s in by) or 'none'}); "
              f"threshold {k_min}; co-follow alone would have found them: {'yes' if stats['watch'][h]['would_be_found'] else 'no'}"
              + (f" ({why})" if why else ""))
    stats["estimated_cost_usd"] = round(budget.estimate, 4)
    stats["cost_usd"] = costs.spent(conn, getattr(source, "name", ""), run_id)
    log.info("expand done: %d lists (%d cached, %d failed, %d skipped for budget), %d accounts, threshold %d/%d: %d at it, "
             "%d new candidates, %d known stamped; skipped %s", n, stats["lists_cached"], stats["seeds_failed"],
             stats["seeds_skipped_budget"], stats["accounts"], k_min, n, stats["at_threshold"], stats["candidates_new"],
             stats["known_stamped"], stats["skipped"])
    return stats


def project_pass(conn: sqlite3.Connection, source, seeds: int = SEEDS, max_following: int = MAX_FOLLOWING,
                 groups: tuple[str, ...] = ("authors",), enrich_top: int = 50, posts_per_user: int = 30) -> dict:
    """What the people-centric pass would cost, from local state only (no paid call): the seeds' following lists, the
    discovery queries of `groups` not served by the search cache, and enrichment of `enrich_top` new people."""
    chosen = select_seeds(conn, seeds)
    plan = plan_follow_reads(source, chosen, max_following)
    cfg = load_queries()
    per_query = int(cfg["limits"]["posts_per_query"])
    queries = [q for g in groups for q in cfg.get(g) or []]
    cached_fn = getattr(source, "_cached_search", None)
    paid_q = [q for q in queries if not (cached_fn and cached_fn(q, per_query) is not None)]
    out = {"seeds": [{"rank": s["rank"], "handle": s["handle"], "followers": s["followers"], "following": s["following"]}
                     for s in chosen],
           "follow_plan": plan,
           "follow_rows": sum(p["rows"] for p in plan if not p["cached"]),
           "follow_usd": round(sum(p["cost_usd"] for p in plan), 4),
           "queries": len(queries), "queries_paid": len(paid_q), "query_posts": per_query * len(paid_q),
           "query_usd": estimate_cost(source, "post_read", per_query, len(paid_q)),
           "enrich_people": enrich_top, "enrich_posts": enrich_top * posts_per_user,
           "enrich_usd": estimate_cost(source, "post_read", posts_per_user, enrich_top)}
    out["total_usd"] = round(out["follow_usd"] + out["query_usd"] + out["enrich_usd"], 4)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wearable_wishlist.expand", description="Project the people-centric pass (no paid call). "
                                 "Run it for real with: python -m wearable_wishlist.pipeline run --stage expand")
    ap.add_argument("--seeds", type=int, default=SEEDS)
    ap.add_argument("--max-following", type=int, default=MAX_FOLLOWING)
    ap.add_argument("--groups", default="authors", help="queries.yaml groups the pass would search (comma list)")
    ap.add_argument("--enrich-top", type=int, default=50)
    ap.add_argument("--posts-per-user", type=int, default=30)
    a = ap.parse_args(argv)
    from .sources import get_source
    conn = db.connect()
    source = get_source("fixture") if config.dry_run() else get_source(config.env("SOURCE_BULK", "monid"), conn=conn)
    p = project_pass(conn, source, a.seeds, a.max_following, tuple(g.strip() for g in a.groups.split(",") if g.strip()),
                     a.enrich_top, a.posts_per_user)
    print(f"Seeds ({len(p['seeds'])}):")
    for s, f in zip(p["seeds"], p["follow_plan"]):
        print(f"  #{s['rank']:<3} @{s['handle']:<18} {s['followers'] or 0:>9,} followers  follows {s['following'] or 0:>6,}"
              f"  -> {f['rows']:>5} rows {'cached $0' if f['cached'] else costs.fmt(f['cost_usd'])}")
    print(f"Following lists: {p['follow_rows']:,} rows -> {costs.fmt(p['follow_usd'])}")
    print(f"New queries ({a.groups}): {p['queries_paid']} of {p['queries']} not cached x {p['query_posts'] // max(1, p['queries_paid'])}"
          f" posts = {p['query_posts']:,} posts max -> {costs.fmt(p['query_usd'])}")
    print(f"Enrichment: {p['enrich_people']} people x {a.posts_per_user} posts = {p['enrich_posts']:,} posts -> "
          f"{costs.fmt(p['enrich_usd'])}")
    print(f"Total {source.name}: {costs.fmt(p['total_usd'])} (upper bound: lists and searches bill the rows returned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
