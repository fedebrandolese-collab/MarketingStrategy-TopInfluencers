"""Community engagement KPIs (SCORING.md "Community engagement"): how many views, likes, replies and reposts a person's
average post receives per follower, against accounts of the same size. Raw numbers and plain arithmetic, from stored rows.

The sample: the person's latest original posts (no reposts, no replies), newest first, fetched by sample.run_sample into
engagement_samples. Not the posts enrich stores: its searches rank by engagement, so those are the person's best posts and
would inflate the average (more so for people who post a lot). Posts younger than SETTLE_HOURS when fetched are left out
(their counts are still growing), and so are posts older than WINDOW_DAYS.

KPIs (kpis_for, one dict per x_id):
  per_post  the average post, a trimmed mean per count: the TRIM share of posts with the highest and the lowest value is
            left out (at least one each side), so one viral post cannot carry an account.
            views; likes; replies; reposts (reposts + quotes); interactions (likes + replies + reposts + quotes)
  rates     per_post / followers: view rate, like rate, reply rate (conversation), repost rate (amplification) and the
            engagement rate (interactions / followers, the usual ER)
  typical   the rate an account with as many followers usually gets: log10(rate) = a + b x log10(followers), a least
            squares fit on every stored sample (PRIOR until MIN_FIT_ACCOUNTS are sampled). Rates fall as followers grow,
            so a raw rate would favour small accounts: each person is compared with their own size.
  vs_size   rate / typical (1.0 = typical for their size)
  index     weighted geometric mean of vs_size (WEIGHTS): replies and reposts 0.3 each, likes and views 0.2 each. A ring
            post that gets answered and reposted travels; a like is cheap.
  value     0..1 = 0.5 + 0.25 x log2(index): typical 0.5, twice typical 0.75, four times 1.0, half 0.25, a quarter 0
Unknown (no sample, fewer than MIN_POSTS usable posts, or followers unknown): value None, and the total counts it as
typical (weights.ENGAGEMENT_UNKNOWN). No network, no LLM.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
from typing import Any, Iterable, Mapping

from . import db

METRICS = ("views", "likes", "replies", "reposts")
WEIGHTS = {"views": 0.2, "likes": 0.2, "replies": 0.3, "reposts": 0.3}
MIN_POSTS = 5                   # usable posts needed for a value (and for a count's average: views can be missing)
WINDOW_DAYS = 180               # posts older than this when fetched say little about today's audience
SETTLE_HOURS = 24               # posts younger than this when fetched are still gathering views and likes
TRIM = 0.10                     # share of posts left out at each end of each count (at least one)
MIN_FIT_ACCOUNTS = 20           # sampled accounts needed to fit `typical`; fewer: PRIOR
RATIO_MIN, RATIO_MAX = 1 / 16, 16.0   # vs_size bounds inside the index (a count of 0 would be log 0)
MIN_FOLLOWERS_FIT = 100
# Until enough accounts are sampled: rough X norms for the average post, log10(rate) = a + b x log10(followers).
# 10k followers: ~10% views, 0.3% likes, 0.03% replies, 0.04% reposts; 1M: ~3%, 0.05%, 0.004%, 0.006%.
PRIOR: dict[str, tuple[float, float]] = {
    "views": (0.0, -0.25), "likes": (-0.96, -0.39), "replies": (-1.76, -0.44), "reposts": (-1.76, -0.41),
    "engagement": (-0.85, -0.395),
}
RATE_KEYS = METRICS + ("engagement",)
UNKNOWN_SHORT = "unknown"
NO_SAMPLE = "no sample of their latest posts yet"


# ------------------------------------------------------------------ small helpers

def _ts(s: Any) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def pct(x: float | None) -> str:
    """0.00444 -> '0.44%'; adaptive decimals so small rates stay readable."""
    if x is None:
        return "n/a"
    p = 100.0 * x
    if p >= 100:
        return f"{p:,.0f}%"
    if p >= 1:
        return f"{p:.1f}%"
    if p >= 0.1:
        return f"{p:.2f}%"
    return f"{p:.3f}%"


def num(x: float | None) -> str:
    """A per-post count: 12400 -> '12,400', 42.3 -> '42', 2.46 -> '2.5'."""
    if x is None:
        return "n/a"
    if x >= 100:
        return f"{x:,.0f}"
    if x >= 10:
        return f"{x:.0f}"
    return f"{x:.1f}"


def trim_count(n: int) -> int:
    """Posts left out at each end: TRIM of n, at least one (n >= 3), so a 5-post sample averages its middle 3."""
    return max(1, math.floor(TRIM * n + 0.5)) if n >= 3 else 0


def trimmed_mean(values: Iterable[float | None]) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    k = trim_count(len(vals))
    kept = vals[k:len(vals) - k] or vals
    return sum(kept) / len(kept)


def _count(p: Mapping[str, Any], metric: str) -> float | None:
    g = lambda k: float(p.get(k) or 0)   # noqa: E731
    if metric == "views":
        v = p.get("views")
        return float(v) if v is not None and float(v) > 0 else None   # 0 or missing: X gave no view count
    if metric == "reposts":
        return g("reposts") + g("quotes")
    if metric == "interactions":
        return g("likes") + g("replies") + g("reposts") + g("quotes")
    return g(metric)


def usable(posts: Iterable[Mapping[str, Any]], fetched_at: Any) -> list[Mapping[str, Any]]:
    """Original posts between SETTLE_HOURS and WINDOW_DAYS old when the sample was fetched."""
    f = _ts(fetched_at)
    out = []
    for p in posts:
        if p.get("in_reply_to_x_id") or str(p.get("text") or "").startswith("RT @"):
            continue
        c = _ts(p.get("created_at"))
        if f is None or c is None:
            continue
        age_h = (f - c).total_seconds() / 3600.0
        if SETTLE_HOURS <= age_h <= WINDOW_DAYS * 24:
            out.append(p)
    return out


# ------------------------------------------------------------------ the KPIs

def measure(posts: list[Mapping[str, Any]], followers: Any, fetched_at: Any, source: str | None = None) -> dict:
    """Raw KPIs of one sample: per_post, rates, n, dates. No comparison with other accounts yet (finalize does that)."""
    use = usable(posts, fetched_at)
    n = len(use)
    fol = int(followers) if isinstance(followers, (int, float)) and followers == followers and followers > 0 else None
    per_post: dict[str, float | None] = {}
    for m in METRICS + ("interactions",):
        vals = [c for c in (_count(p, m) for p in use) if c is not None]
        per_post[m] = trimmed_mean(vals) if len(vals) >= MIN_POSTS else None
    rates: dict[str, float | None] = {m: None for m in RATE_KEYS}
    if fol:
        for m in METRICS:
            rates[m] = per_post[m] / fol if per_post[m] is not None else None
        rates["engagement"] = per_post["interactions"] / fol if per_post["interactions"] is not None else None
    dates = sorted(str(p.get("created_at"))[:10] for p in use)
    originals = [p for p in posts if not p.get("in_reply_to_x_id") and not str(p.get("text") or "").startswith("RT @")]
    created = [c for c in (_ts(p.get("created_at")) for p in originals) if c is not None]
    f = _ts(fetched_at)
    ppw = None
    if created and f is not None:
        span_days = max(1.0, (f - min(created)).total_seconds() / 86400.0)
        ppw = round(len(created) / span_days * 7.0, 1)
    views, inter = per_post.get("views"), per_post.get("interactions")
    why = None
    if not fol:
        why = "follower count unknown"
    elif n < MIN_POSTS:
        why = (f"only {n} of their latest {len(posts)} posts are original and between 1 and {WINDOW_DAYS} days old "
               f"({MIN_POSTS} needed)")
    return {
        "n": n, "trimmed": trim_count(n), "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
        "fetched_at": fetched_at, "source": source, "followers": fol, "sampled": len(posts),
        "per_post": {k: (round(v, 2) if v is not None else None) for k, v in per_post.items()},
        "rates": rates, "posts_per_week": ppw,
        "per_view": (inter / views) if views and inter is not None else None, "why": why,
    }


def fit(raws: Iterable[Mapping[str, Any]]) -> tuple[dict[str, tuple[float, float]], int, bool]:
    """(benchmarks, accounts, fitted): log10(rate) = a + b x log10(followers) per rate, least squares over the sampled
    accounts with a value. Fewer than MIN_FIT_ACCOUNTS accounts (or points for one rate): PRIOR."""
    pts: dict[str, list[tuple[float, float]]] = {m: [] for m in RATE_KEYS}
    accounts = 0
    for r in raws:
        fol = r.get("followers")
        if r.get("why") or not fol or fol < MIN_FOLLOWERS_FIT:
            continue
        accounts += 1
        for m in RATE_KEYS:
            rate = (r.get("rates") or {}).get(m)
            if rate and rate > 0:
                pts[m].append((math.log10(fol), math.log10(rate)))
    if accounts < MIN_FIT_ACCOUNTS:
        return dict(PRIOR), accounts, False
    out: dict[str, tuple[float, float]] = {}
    for m, xy in pts.items():
        if len(xy) < MIN_FIT_ACCOUNTS:
            out[m] = PRIOR[m]
            continue
        mx = sum(x for x, _ in xy) / len(xy)
        my = sum(y for _, y in xy) / len(xy)
        sxx = sum((x - mx) ** 2 for x, _ in xy)
        b = sum((x - mx) * (y - my) for x, y in xy) / sxx if sxx > 0 else PRIOR[m][1]
        out[m] = (round(my - b * mx, 4), round(b, 4))
    return out, accounts, True


def typical_rate(metric: str, followers: float, bench: Mapping[str, tuple[float, float]]) -> float:
    a, b = bench.get(metric) or PRIOR[metric]
    return 10 ** (a + b * math.log10(max(float(followers), 1.0)))


def finalize(raw: Mapping[str, Any], bench: Mapping[str, tuple[float, float]], accounts: int, fitted: bool) -> dict:
    """raw (measure) + the comparison with accounts of the same size: typical, vs_size, index, value, short."""
    k = dict(raw)
    k["benchmark"] = {"accounts": accounts, "fitted": fitted}
    fol, rates = raw.get("followers"), raw.get("rates") or {}
    k["typical"], k["vs_size"], k["index"], k["value"] = {}, {}, None, None
    if raw.get("why") or not fol:
        k["short"] = UNKNOWN_SHORT
        return k
    logs, wsum = 0.0, 0.0
    for m in RATE_KEYS:
        t = typical_rate(m, fol, bench)
        k["typical"][m] = t
        r = rates.get(m)
        k["vs_size"][m] = round(r / t, 3) if r is not None and t > 0 else None
        if m in WEIGHTS and r is not None:
            ratio = min(RATIO_MAX, max(RATIO_MIN, r / t))
            logs += WEIGHTS[m] * math.log(ratio)
            wsum += WEIGHTS[m]
    if wsum == 0:
        k["why"], k["short"] = "no counts in the sample", UNKNOWN_SHORT
        return k
    index = math.exp(logs / wsum)
    k["index"] = round(index, 3)
    k["value"] = round(min(1.0, max(0.0, 0.5 + 0.25 * math.log2(index))), 3)
    k["short"] = f"ER {pct(rates.get('engagement'))} · {index:.1f}× peers"
    return k


def kpis_for(conn: sqlite3.Connection, x_ids: Iterable[str] | None = None) -> dict[str, dict]:
    """{x_id: kpis} from each account's newest engagement_samples row; x_ids None: every sampled account. The benchmark
    is fitted on every sampled account, whatever x_ids asks for."""
    samples = db.rows(conn, """SELECT s.x_id, s.fetched_at, s.source, s.post_ids FROM engagement_samples s
        WHERE s.fetched_at = (SELECT MAX(fetched_at) FROM engagement_samples WHERE x_id = s.x_id)""")
    if not samples:
        return {}
    ids: dict[str, list[str]] = {}
    for s in samples:
        try:
            ids[s["x_id"]] = [str(i) for i in json.loads(s["post_ids"] or "[]")]
        except ValueError:
            ids[s["x_id"]] = []
    wanted = [i for lst in ids.values() for i in lst]
    posts: dict[str, dict] = {}
    for i in range(0, len(wanted), 900):
        chunk = wanted[i:i + 900]
        for p in db.rows(conn, f"""SELECT post_id, created_at, text, likes, reposts, replies, quotes, views, in_reply_to_x_id
                FROM x_posts WHERE post_id IN ({','.join('?' for _ in chunk)})""", chunk):
            posts[p["post_id"]] = p
    followers = {u["x_id"]: u["followers"] for u in db.rows(conn, "SELECT x_id, followers FROM x_users")}
    raws = {s["x_id"]: measure([posts[i] for i in ids[s["x_id"]] if i in posts], followers.get(s["x_id"]), s["fetched_at"],
                               s.get("source"))
            for s in samples}
    bench, accounts, fitted = fit(raws.values())
    keep = set(raws) if x_ids is None else {x for x in x_ids if x in raws}
    return {x: finalize(raws[x], bench, accounts, fitted) for x in keep}


# ------------------------------------------------------------------ plain-language reasons

def short(k: Mapping[str, Any] | None) -> str:
    return (k or {}).get("short") or UNKNOWN_SHORT


def explain(k: Mapping[str, Any] | None) -> list[str]:
    """The arithmetic behind the value, one line per step."""
    if not k:
        return [f"Community engagement unknown: {NO_SAMPLE} (the total counts it as typical for their size)."]
    if k.get("value") is None:
        return [f"Community engagement unknown: {k.get('why') or NO_SAMPLE} (the total counts it as typical for their size)."]
    pp, r, t, v = k["per_post"], k["rates"], k["typical"], k["vs_size"]
    fol, n, trim = k["followers"], k["n"], k["trimmed"]
    b = k.get("benchmark") or {}
    bench = (f"fit on {b.get('accounts')} sampled accounts" if b.get("fitted")
             else f"rough X norms until {MIN_FIT_ACCOUNTS} accounts are sampled")
    x = lambda m: f"x{v[m]:.1f}" if v.get(m) is not None else "n/a"   # noqa: E731
    lines = [
        f"Average post over their {n} latest original posts ({k['first']} to {k['last']}; the {trim} highest and {trim} "
        f"lowest of each count left out): {num(pp['views'])} views, {num(pp['likes'])} likes, {num(pp['replies'])} "
        f"replies, {num(pp['reposts'])} reposts (quotes included).",
        f"÷ {fol:,} followers: views {pct(r['views'])}, likes {pct(r['likes'])}, replies {pct(r['replies'])}, "
        f"reposts {pct(r['reposts'])}; engagement rate {pct(r['engagement'])} (likes + replies + reposts per follower).",
        f"Typical for {fol:,} followers ({bench}): views {pct(t['views'])}, likes {pct(t['likes'])}, replies "
        f"{pct(t['replies'])}, reposts {pct(t['reposts'])}. This account: views {x('views')}, likes {x('likes')}, "
        f"replies {x('replies')}, reposts {x('reposts')}.",
        f"Weighted x{k['index']:.2f} (replies and reposts 0.3 each, likes and views 0.2 each) -> engagement = "
        f"0.5 + 0.25 x log2({k['index']:.2f}) = {k['value']:.2f}.",
    ]
    extra = []
    if k.get("posts_per_week") is not None:
        extra.append(f"{k['posts_per_week']:g} original posts a week")
    if k.get("per_view") is not None:
        extra.append(f"{1000 * k['per_view']:.0f} interactions per 1,000 views")
    if extra:
        lines.append("; ".join(extra).capitalize() + ".")
    return lines


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Community engagement KPIs from stored latest-posts samples (free).")
    ap.add_argument("handles", nargs="*", help="handles to explain (default: the benchmark only)")
    a = ap.parse_args(argv)
    conn = db.connect()
    db.init(conn)
    k_all = kpis_for(conn)
    any_k = next(iter(k_all.values()), None)
    b = (any_k or {}).get("benchmark") or {}
    print(f"{len(k_all)} sampled accounts; benchmark {'fitted on ' + str(b.get('accounts')) + ' accounts' if b.get('fitted') else 'PRIOR'}")
    raws = [k for k in k_all.values()]
    bench, accounts, fitted = fit(raws)
    for m, (ia, ib) in bench.items():
        print(f"  {m:<10} log10(rate) = {ia:+.3f} {ib:+.3f} x log10(followers)   10k: {pct(typical_rate(m, 1e4, bench))}  "
              f"1M: {pct(typical_rate(m, 1e6, bench))}")
    by_handle = {r["handle"].lower(): r["x_id"] for r in db.rows(conn, "SELECT x_id, handle FROM x_users WHERE handle IS NOT NULL")}
    for h in a.handles:
        xid = by_handle.get(h.lstrip("@").lower())
        print(f"\n@{h.lstrip('@')}")
        for line in explain(k_all.get(xid) if xid else None):
            print("  " + line)


if __name__ == "__main__":
    main()
