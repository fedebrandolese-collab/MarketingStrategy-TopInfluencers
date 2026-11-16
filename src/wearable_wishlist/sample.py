"""Engagement sample: each person's latest original posts (newest first, no reposts, no replies), the posts the community
engagement KPIs average (engagement.py reads the newest engagement_samples row per x_id and joins x_posts on post_ids).

Why a sample of its own: run_enrich's searches are engagement-ranked ("Top") over 365 days, so the posts it stores are
each person's best ones, and an average over them is inflated (the more someone posts, the more). The source's
get_latest_originals reads the latest ones instead (monid: the tweets actor, sort Latest, 'from:<h> -filter:retweets
-filter:replies'; x_api: the user timeline with exclude=retweets,replies).

  run_sample(conn, run_id, source)   everyone who meets the brief's bar and is not rejected, US or not (the wishlist with
                                     US only off: the dashboard shows non-US people when that filter is off), plus the
                                     `top` best-scored people of a person category who are not rejected, both from the
                                     latest scored run; people without an X account and those sampled within
                                     `refresh_days` are skipped. `candidate_ids` (a manual check, wearable_wishlist.check): only
                                     these, fetched now whatever the refresh window says.

Posts are stored with persist_posts, fresh counts included; a post already stored keeps the query that first found it
(classify and discover read it), a new one gets SAMPLE_QUERY (discover.query_kind: 'recent', not a topic hit). One
engagement_samples row per fetched account, with 0 posts too (an empty account is not re-bought daily); a failed fetch
writes none, so the next run retries it.
Cost: as run_enrich (discover.estimate_cost per person, discover._Budget). The projection is printed first (monid tweets
$0.0006 per post x posts x people), the adapters guard and record every paid call in the ledger, and the loop stops before
the running estimate would exceed `budget_usd` (None: the provider's per-run cap from .env, minus this run's spend)."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3

from . import costs, db, weights
from .discover import _Budget, estimate_cost, is_retweet
from .export import rows_for_run
from .sources.base import persist_posts

log = logging.getLogger("wearable_wishlist.sample")

TOP = 0                    # best-scored people sampled besides those who meet the bar (the pipeline's --sample-top
                           # default): the list only (Federico, 30 Sep: sample the people on the list, $0.018 each)
POSTS = 30                 # latest original posts per person (the pipeline's --sample-posts default)
REFRESH_DAYS = 14          # a sample younger than this is not bought again
SAMPLE_QUERY = "from:{handle} -filter:retweets -filter:replies"   # x_posts.query of a post a sample stored first
NOT_REJECTED = {"us_only": False, "hide_competitor": False, "hide_rejected": True, "bar": False}   # for weights.hidden_by
MEETS_BAR = {**NOT_REJECTED, "bar": True}   # the wishlist with US only off (export's wishlist.json keeps US only on)


def select(conn: sqlite3.Connection, run_id: int, top: int = TOP) -> tuple[list[int], dict]:
    """(candidate ids, counts): everyone who meets the brief's bar and is not rejected, US or not, first, in rank order
    (Federico, 30 Sep: the US only filter must be usable, so the non-US people it shows when off get engagement numbers
    too), then the `top` best-scored people of a person category who are not rejected, US or not. Both from export's rows
    of the latest scored run (rows_for_run's fallback, as run_export), which come in scores.rank order."""
    rows = rows_for_run(conn, run_id)
    bar = [r["candidate_id"] for r in rows if not weights.hidden_by(r, MEETS_BAR)]
    best = [r["candidate_id"] for r in rows if r["score"] is not None and r["category"] in weights.PERSON_CATEGORIES
            and not weights.hidden_by(r, NOT_REJECTED)][:max(0, int(top))]
    return list(dict.fromkeys(bar + best)), {"bar": len(bar), "top": len(best)}


def _accounts(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    """candidate_id, x_id and the handle to fetch (the newest X showed us: x_users, else candidates), in `ids` order."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids:
        return []
    got = {r["candidate_id"]: r for r in db.rows(
        conn, f"""SELECT c.candidate_id, c.x_id, COALESCE(u.handle, c.handle) AS handle FROM candidates c
                  LEFT JOIN x_users u ON u.x_id = c.x_id WHERE c.candidate_id IN ({','.join('?' * len(ids))})""", ids)}
    return [got[i] for i in ids if i in got]


def run_sample(conn: sqlite3.Connection, run_id: int, source, *, candidate_ids: list[int] | None = None, top: int = TOP,
               posts: int = POSTS, refresh_days: int = REFRESH_DAYS, budget_usd: float | None = None) -> dict:
    """Fetch the latest `posts` original posts of each selected person (see the module docstring) and write their
    engagement_samples rows. Returns stats: bar and top (people each selection gave; None for candidate_ids), selected
    (to fetch), accounts (sampled), posts, empty, skipped_recent, skipped_no_x (no X id or handle), skipped_budget,
    budget_stop, errors, cost (USD this stage added to the ledger)."""
    posts = int(posts)
    if candidate_ids is None:
        ids, counts = select(conn, run_id, top)
    else:
        ids, counts = [int(i) for i in candidate_ids], {"bar": None, "top": None}
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=refresh_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = {r["x_id"] for r in db.rows(conn, "SELECT x_id FROM engagement_samples WHERE fetched_at >= ?", (cutoff,))}
    stats: dict = {**counts, "selected": 0, "accounts": 0, "posts": 0, "empty": 0, "skipped_recent": 0, "skipped_no_x": 0,
                   "skipped_budget": 0, "budget_stop": False, "errors": 0, "cost": 0.0}
    chosen = []
    for a in _accounts(conn, ids):
        if not (a["x_id"] and a["handle"]):
            stats["skipped_no_x"] += 1
        elif candidate_ids is None and a["x_id"] in recent:
            stats["skipped_recent"] += 1
        else:
            chosen.append(a)
    per_user = estimate_cost(source, "timeline_read", posts)
    budget = _Budget(conn, source, run_id, budget_usd)
    if budget.cap is not None and per_user > 0:      # never prefetch (buy) more people than the budget allows
        fit = max(0, int((budget.cap - budget.estimate + 1e-9) // per_user))
        if fit < len(chosen):
            log.warning("sample: budget %s fits %d of %d people", budget.label(), fit, len(chosen))
            stats["skipped_budget"], stats["budget_stop"] = len(chosen) - fit, True
            chosen = chosen[:fit]
    stats["selected"] = len(chosen)
    log.info("sample: %s: %d to fetch; %d sampled within %d days, %d without an X account",
             f"{stats['bar']} meeting the bar, top {stats['top']} by score" if candidate_ids is None else f"{len(ids)} asked for",
             len(chosen), stats["skipped_recent"], refresh_days, stats["skipped_no_x"])
    print(f"[cost] {source.name} sample: ~{costs.fmt(per_user * len(chosen))} for {len(chosen)} people x {posts} latest "
          f"original posts (~{costs.fmt(per_user)} each, cap {budget.label()})")
    spent_before = costs.spent(conn, source.name, run_id)
    if len(chosen) > 1 and hasattr(source, "prefetch_latest_originals"):
        # the adapter runs the fetches concurrently and keeps them; the loop below then reads them for $0
        try:
            stats["prefetch"] = source.prefetch_latest_originals([a["handle"] for a in chosen], posts)
        except costs.BudgetExceeded as e:
            log.warning("sample: prefetch refused by the budget guard (%s); fetching one by one", e)
            stats["prefetch"] = {"error": str(e)}
        except Exception as e:   # the loop below still works, only slower
            log.warning("sample: prefetch failed (%s); fetching one by one", e)
            stats["prefetch"] = {"error": str(e)}
    for i, a in enumerate(chosen):
        if not budget.reserve(per_user):
            stats["skipped_budget"] += len(chosen) - i
            stats["budget_stop"] = True
            break
        x_id, handle = a["x_id"], a["handle"]
        known = {r["post_id"]: r["query"] for r in db.rows(conn, "SELECT post_id, query FROM x_posts WHERE x_id=?", (x_id,))}
        try:
            got = list(source.get_latest_originals(handle, max_results=posts))
        except costs.BudgetExceeded as e:
            log.warning("sample: adapter budget guard stopped the sample at @%s: %s", handle, e)
            stats["skipped_budget"] += len(chosen) - i
            stats["budget_stop"] = True
            budget.settle(per_user, 0.0)
            break
        except Exception as e:   # one bad account must not stop the batch; no row, so the next run retries it
            log.warning("sample: @%s failed, skipped: %s", handle, e)
            stats["errors"] += 1
            continue
        budget.settle(per_user, min(per_user, estimate_cost(source, "timeline_read", len(got))))
        own = [p for p in got if (p.x_id == x_id or (p.handle or "").lower() == handle.lower())
               and not p.in_reply_to_x_id and not is_retweet(p)]
        for p in own:
            p.x_id = x_id                          # the candidate's id, whatever representation the source used
        own = list({p.post_id: p for p in sorted(own, key=lambda p: p.created_at or "", reverse=True)}.values())
        for p in own:   # a post already stored keeps the query that first found it
            p.query = known[p.post_id] if p.post_id in known else SAMPLE_QUERY.format(handle=handle)
        persist_posts(conn, own, source.name)
        # adapters that persist on their own (x_api) wrote their query over the stored one: put it back
        conn.executemany("UPDATE x_posts SET query=? WHERE post_id=?",
                         [(known[p.post_id], p.post_id) for p in got if p.post_id in known])
        db.upsert(conn, "engagement_samples", {"x_id": x_id, "fetched_at": db.utcnow(), "source": source.name,
                                               "requested": posts, "post_ids": db.jdumps([p.post_id for p in own]),
                                               "run_id": run_id}, keys=("x_id", "fetched_at"))
        conn.commit()
        stats["accounts"] += 1
        stats["posts"] += len(own)
        stats["empty"] += not own
        log.info("sample: @%s: %d latest original posts, %d new", handle, len(own), sum(p.post_id not in known for p in own))
    stats["cost"] = round(costs.spent(conn, source.name, run_id) - spent_before, 6)
    log.info("sample done: %d/%d people sampled, %d posts (%d accounts empty), %d errors, %d skipped for budget, cost %s",
             stats["accounts"], stats["selected"], stats["posts"], stats["empty"], stats["errors"], stats["skipped_budget"],
             costs.fmt(stats["cost"]))
    return stats
