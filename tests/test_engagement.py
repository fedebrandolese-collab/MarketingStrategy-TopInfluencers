"""Community engagement KPIs (src/wearable_wishlist/engagement.py): the average post per follower, against accounts of the same size."""
import datetime as dt
import math

import pytest

from wearable_wishlist import db, engagement as E, score, weights

FETCHED = "2026-09-30T12:00:00Z"
F = 10_000


def ago(days: float) -> str:
    return (dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def typical_counts(followers=F, k=1.0):
    """Per-post counts exactly k times what PRIOR expects for an account this size."""
    return {m: k * E.typical_rate(m, followers, E.PRIOR) * followers for m in E.METRICS}


def post(i, c, days=None, **kw):
    return {"post_id": f"p{i}", "created_at": ago(days if days is not None else 2 + i), "text": f"post {i}",
            "likes": c["likes"], "replies": c["replies"], "reposts": c["reposts"], "quotes": 0, "views": c["views"], **kw}


def test_trimmed_mean_and_usable_posts():
    assert E.trimmed_mean([1, 2, 3, 4, 100]) == 3.0                     # one viral post cannot carry the average
    assert E.trim_count(5) == 1 and E.trim_count(20) == 2 and E.trim_count(25) == 3
    c = typical_counts()
    posts = [post(1, c), post(2, c, in_reply_to_x_id="99"), dict(post(3, c), text="RT @x: hi"),
             post(4, c, days=0.5), post(5, c, days=200)]
    assert [p["post_id"] for p in E.usable(posts, FETCHED)] == ["p1"]   # reply, repost, too young, too old left out


def test_typical_for_their_size_is_half_and_twice_is_three_quarters():
    raw = E.measure([post(i, typical_counts()) for i in range(8)], F, FETCHED)
    k = E.finalize(raw, E.PRIOR, 3, False)
    assert k["index"] == pytest.approx(1.0) and k["value"] == pytest.approx(0.5)
    assert k["rates"]["likes"] == pytest.approx(k["typical"]["likes"])
    k2 = E.finalize(E.measure([post(i, typical_counts(k=2.0)) for i in range(8)], F, FETCHED), E.PRIOR, 3, False)
    assert k2["value"] == pytest.approx(0.75) and k2["short"].startswith("ER ") and "2.0× peers" in k2["short"]
    k4 = E.finalize(E.measure([post(i, typical_counts(k=0.25)) for i in range(8)], F, FETCHED), E.PRIOR, 3, False)
    assert k4["value"] == pytest.approx(0.0)
    # replies and reposts weigh 0.3, likes and views 0.2: only replies doubled -> index 2 ** 0.3
    c = dict(typical_counts(), replies=2 * typical_counts()["replies"])
    k5 = E.finalize(E.measure([post(i, c) for i in range(8)], F, FETCHED), E.PRIOR, 3, False)
    assert k5["index"] == pytest.approx(2 ** 0.3, rel=1e-3)
    lines = E.explain(k2)
    assert lines[0].startswith("Average post over their 8 latest original posts") and "÷ 10,000 followers" in lines[1]
    assert "0.5 + 0.25 x log2(2.00) = 0.75" in lines[3]


def test_unknown_when_too_few_posts_or_no_followers():
    k = E.finalize(E.measure([post(i, typical_counts()) for i in range(4)], F, FETCHED), E.PRIOR, 3, False)
    assert k["value"] is None and "(5 needed)" in k["why"] and k["short"] == "unknown"
    k = E.finalize(E.measure([post(i, typical_counts()) for i in range(8)], None, FETCHED), E.PRIOR, 3, False)
    assert k["value"] is None and k["why"] == "follower count unknown"
    assert "no sample of their latest posts yet" in E.explain(None)[0]


def test_fit_recovers_a_power_law():
    raws = []
    for i in range(30):
        fol = 10 ** (3 + 4 * i / 29)
        rate = 10 ** (-1.0 - 0.5 * math.log10(fol))
        raws.append({"followers": fol, "why": None, "rates": {m: rate for m in E.RATE_KEYS}})
    bench, n, fitted = E.fit(raws)
    assert fitted and n == 30 and bench["likes"] == (pytest.approx(-1.0, abs=1e-3), pytest.approx(-0.5, abs=1e-3))
    assert E.fit(raws[:5]) == (E.PRIOR, 5, False)


def add_sample(conn, x_id, handle, followers, posts, fetched=FETCHED):
    db.upsert(conn, "x_users", {"x_id": x_id, "handle": handle, "followers": followers}, keys=("x_id",))
    for p in posts:
        db.upsert(conn, "x_posts", {**{k: v for k, v in p.items()}, "post_id": f"{x_id}-{p['post_id']}", "x_id": x_id,
                                    "handle": handle}, keys=("post_id",))
    conn.execute("INSERT INTO engagement_samples (x_id, fetched_at, source, requested, post_ids) VALUES (?,?,?,?,?)",
                 (x_id, fetched, "fixture", 30, db.jdumps([f"{x_id}-{p['post_id']}" for p in posts])))
    conn.commit()


def test_kpis_for_reads_the_newest_sample(conn):
    add_sample(conn, "1", "alpha", F, [post(i, typical_counts(k=4.0)) for i in range(6)], fetched="2026-08-01T00:00:00Z")
    add_sample(conn, "1", "alpha", F, [post(10 + i, typical_counts(k=2.0)) for i in range(6)])
    add_sample(conn, "2", "bravo", F, [post(i, typical_counts()) for i in range(3)])
    k = E.kpis_for(conn)
    assert k["1"]["value"] == pytest.approx(0.75) and k["1"]["n"] == 6 and k["1"]["fetched_at"] == FETCHED
    assert k["2"]["value"] is None
    assert set(E.kpis_for(conn, ["2", "nobody"])) == {"2"}


def test_score_stores_the_factor_and_the_total_uses_it(conn):
    run = db.new_run(conn, "score", "t")
    for x_id, handle, k in (("1", "alpha", 4.0), ("2", "bravo", 1.0)):
        add_sample(conn, x_id, handle, F, [post(i, typical_counts(k=k)) for i in range(8)])
        cid = db.upsert(conn, "candidates", {"x_id": x_id, "handle": handle, "name": handle, "category": "x_voice",
                                             "us_based": 1}, keys=("x_id",))
        cid = db.one(conn, "SELECT candidate_id FROM candidates WHERE x_id=?", (x_id,))["candidate_id"]
        db.upsert(conn, "classifications", {"candidate_id": cid, "run_id": run, "json": db.jdumps({"category": "x_voice"}),
                                            "model": "fixture", "created_at": FETCHED}, keys=("candidate_id", "run_id"))
    conn.commit()
    score.run_score(conn, run)
    s = {r["candidate_id"]: r for r in db.rows(conn, "SELECT c.handle, s.* FROM scores s JOIN candidates c USING (candidate_id)")}
    a, b = (next(r for r in s.values() if r["handle"] == h) for h in ("alpha", "bravo"))
    assert a["engagement"] == pytest.approx(1.0) and b["engagement"] == pytest.approx(0.5)
    fa = db.jloads(a["reasons_json"])["factors"]["engagement"]
    assert fa["kpis"]["n"] == 8 and fa["short"].startswith("ER ") and len(fa["reasons"]) >= 4
    # the engagement term: x1.3 for four times typical, x1.0 for typical (impact also moves: their posts engage more)
    assert a["total"] == pytest.approx(weights.weighted_total(dict(a, category="x_voice", engagement=0.5)) * 1.3, abs=0.01)
    assert b["total"] == weights.weighted_total(dict(b, category="x_voice", engagement=None))
    assert a["total"] == weights.weighted_total(dict(a, category="x_voice"))
    assert "(0.7 + 0.6 x engagement 1.00)" in db.jloads(a["reasons_json"])["factors"]["total"]["reasons"][0]
