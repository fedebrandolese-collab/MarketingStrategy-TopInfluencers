"""Offline tests for the expand stage (people our best people follow), the authors queries and the pipeline wiring."""
import json

import pytest

from ring_wishlist import db, discover, expand, pipeline
from ring_wishlist.discover import persist_users_batch
from ring_wishlist.sources import monid
from ring_wishlist.sources.base import XUser
from ring_wishlist.sources.monid import MonidSource, to_web_query

SAMPLES = monid.config.ROOT / "tests" / "samples" / "c"


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """monid's follow-crawl and search logs live in CACHE_DIR: never touch the real ones."""
    monkeypatch.setattr(monid.config, "CACHE_DIR", tmp_path / "cache")


class FakeSource:
    name = "fixture"

    def __init__(self, following):
        self.following, self.calls, self.queries = following, [], []

    def get_following(self, handle, max_users=1000):
        self.calls.append(handle)
        return list(self.following.get(handle, []))[:max_users]

    def search_posts(self, query, max_results=100, since=None):
        self.queries.append(query)
        return []

    def get_users_by_handles(self, handles):
        return []


def U(handle, followers=50_000, bio="I write about software.", name=None, following=None):
    return XUser(x_id=f"id_{handle}", handle=handle, name=name or handle.title(), bio=bio, followers=followers,
                 following=following)


SEEDS = [f"s{i}" for i in range(13)]


def world(conn, n_seeds=13, following=None):
    """A scored run whose top rows fail one seed filter each, then seeds s0..s12 (ranks 6..18), plus Carlo and a known
    candidate. Returns the scored run id."""
    excluded = [("notus", ["not_us"], None, 1.0, 50_000), ("comp", ["competitor:Oura"], None, 1.0, 50_000),
                ("rej", [], "rejected", 1.0, 50_000), ("lowconf", [], None, 0.667, 50_000), ("small", [], None, 1.0, 4000)]
    rows = excluded + [(h, [], None, 1.0, 10_000) for h in SEEDS[:n_seeds]]
    persist_users_batch(conn, [U(h, followers=f, following=following) for h, _, _, _, f in rows]
                        + [U("carlo"), U("known1")], "fixture")
    rid = db.new_run(conn, "score")
    for rank, (h, flags, status, conf, _) in enumerate(rows, 1):
        cid, _ = discover.upsert_candidate(conn, f"id_{h}", h, h, "seed")
        conn.execute("INSERT INTO scores (run_id, candidate_id, total, rank, confidence, flags) VALUES (?,?,?,?,?,?)",
                     (rid, cid, 100 - rank, rank, conf, json.dumps(flags)))
        if status:
            conn.execute("INSERT INTO status (candidate_id, status) VALUES (?,?)", (cid, status))
    discover.upsert_candidate(conn, "id_known1", "known1", "Known One", "query:x")
    conn.commit()
    return rid


def follows(spec):
    """{account XUser: number of seeds (s0.. in order) following it} -> {seed handle: [XUser]}."""
    out = {h: [] for h in SEEDS}
    for u, k in spec:
        for h in SEEDS[:k]:
            out[h].append(u)
    return out


def test_threshold():
    assert [expand.cofollow_threshold(n) for n in (1, 5, 12, 15, 16, 20, 30)] == [3, 3, 3, 3, 4, 4, 6]


def test_seeds_follow_the_dashboard_filters_and_order(conn):
    world(conn)
    seeds = expand.select_seeds(conn, 12)
    assert [s["handle"] for s in seeds] == SEEDS[:12]        # notus, comp, rej, lowconf, small left out; s12 is 13th
    assert expand.select_seeds(conn, 3, min_confidence=0.6, min_followers=0)[0]["handle"] == "lowconf"


def test_cofollow_counts_thresholds_and_filters(conn, capsys):
    world(conn)
    src = FakeSource(follows([
        (U("alice"), 5), (U("bob"), 2), (U("carol", followers=3000), 3), (U("jester", bio="Parody account"), 4),
        (U("acmehq", name="Acme", bio="The official account of Acme"), 4), (U("coins", bio="NFT giveaways daily"), 4),
        (U("ouraring", name="Oura"), 6), (U("known1"), 6), (U("carlo"), 12), (U("simonw", followers=227_000), 4),
        (U("s3"), 7)]))
    st = expand.run_expand(conn, db.new_run(conn, "expand"), src, seed_handle="carlo")
    assert src.calls == SEEDS[:12] and st["lists"] == 12 and st["threshold"] == 3
    assert st["candidates_new"] == 2 and set(st["new"]) == {"@alice 5/12", "@simonw 4/12"}
    assert st["skipped"] == {"small_audience": 1, "parody": 1, "org": 1, "spam": 1, "brand": 1}
    assert "@known1 6/12" in st["known"] and "@s3 7/12" in st["known"]
    a = db.one(conn, "SELECT discovered_via, notes FROM candidates WHERE handle='alice'")
    assert a["discovered_via"] == "cofollow:5/12" and a["notes"] == "cofollow:5/12 seeds:@s0,@s1,@s2,@s3,@s4"
    k = db.one(conn, "SELECT discovered_via, notes FROM candidates WHERE handle='known1'")
    assert k["discovered_via"] == "query:x" and "cofollow:6/12" in k["notes"]
    assert db.one(conn, "SELECT 1 AS x FROM candidates WHERE handle IN ('bob','carlo','jester','acmehq')") is None
    w = st["watch"]["simonw"]
    assert (w["cofollows"], w["of_seeds"], w["would_be_found"]) == (4, 12, True)
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_follows WHERE src_x_id='id_s0'")["n"] == 11   # follows persisted
    assert "[expand] 12 seeds from scored run" in capsys.readouterr().out


def test_idempotent(conn):
    world(conn)
    src = FakeSource(follows([(U("alice"), 5), (U("dave"), 3)]))
    first = expand.run_expand(conn, db.new_run(conn, "expand"), src, seed_handle="carlo", watch=())
    before = db.rows(conn, "SELECT candidate_id, handle, discovered_via, first_seen, notes FROM candidates ORDER BY 1")
    second = expand.run_expand(conn, db.new_run(conn, "expand"), src, seed_handle="carlo", watch=())
    after = db.rows(conn, "SELECT candidate_id, handle, discovered_via, first_seen, notes FROM candidates ORDER BY 1")
    assert first["candidates_new"] == 2 and second["candidates_new"] == 0 and before == after


def test_watch_excludes_own_list_when_a_seed(conn):
    world(conn)
    src = FakeSource(follows([(U("s5"), 3)]))
    st = expand.run_expand(conn, db.new_run(conn, "expand"), src, seed_handle="carlo", watch=("s5",))
    assert st["watch"]["s5"]["of_seeds"] == 11 and st["watch"]["s5"]["cofollows"] == 3


# ---- monid: projection, budget, cache -------------------------------------------------------------------------

class FollowSession:
    """Answers every monid run with the captured follows payload; counts POSTs (paid calls)."""
    def __init__(self):
        self.completed = json.loads((SAMPLES / "run_follows_completed.json").read_text())
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json)
        return type("R", (), {"status_code": 200, "json": lambda s: self.completed})()

    def get(self, url, headers=None, timeout=None):
        return type("R", (), {"status_code": 200, "json": lambda s: self.completed})()


def msrc(conn, run_id):
    return MonidSource(conn=conn, run_id=run_id, session=FollowSession(), api_key="k", poll_interval=0)


def test_cost_projection(conn):
    world(conn)
    for h, n in (("s0", 150), ("s1", 800), ("s2", 5000)):
        conn.execute("UPDATE x_users SET following=? WHERE handle=?", (n, h))
    s = msrc(conn, None)
    plan = expand.plan_follow_reads(s, expand.select_seeds(conn, 3), 1000)
    assert [p["rows"] for p in plan] == [150, 800, 1000]
    assert [p["cost_usd"] for p in plan] == pytest.approx([200 * 0.000225, 800 * 0.000225, 1000 * 0.000225])
    p = expand.project_pass(conn, s, seeds=3, max_following=1000, enrich_top=50, posts_per_user=30)
    assert p["queries"] == 12 and p["query_usd"] == pytest.approx(12 * 100 * 0.0006)
    assert p["enrich_usd"] == pytest.approx(50 * 30 * 0.0006) and p["total_usd"] == pytest.approx(
        p["follow_usd"] + p["query_usd"] + p["enrich_usd"])
    assert s.session.posts == []                                  # projecting never calls monid


def test_budget_guard_stops_before_paying(conn):
    world(conn)
    rid = db.new_run(conn, "expand")
    s = msrc(conn, rid)
    st = expand.run_expand(conn, rid, s, seeds=3, budget_usd=0.01, watch=())
    assert s.session.posts == [] and st["seeds_skipped_budget"] == 3 and st["lists"] == 0


def test_follow_lists_come_from_the_cache_the_second_time(conn):
    world(conn)
    rid = db.new_run(conn, "expand")
    s = msrc(conn, rid)
    first = expand.run_expand(conn, rid, s, seeds=3, min_followers=0, watch=())
    assert len(s.session.posts) == 3 and first["lists"] == 3 and first["candidates_new"] == 2 and first["skipped"] == {"brand": 1}
    assert db.one(conn, "SELECT SUM(units) AS u FROM costs WHERE run_id=?", (rid,))["u"] is not None   # ledger row
    rid2 = db.new_run(conn, "expand")
    s2 = msrc(conn, rid2)
    assert all(p["cached"] and p["cost_usd"] == 0 for p in expand.plan_follow_reads(s2, expand.select_seeds(conn, 3)))
    second = expand.run_expand(conn, rid2, s2, seeds=3, min_followers=0, watch=())
    assert s2.session.posts == [] and second["lists_cached"] == 3 and second["candidates_new"] == 0


# ---- priority, queries, enrich filter, pipeline wiring -----------------------------------------------------------

def test_cofollow_priority_term(conn):
    assert discover.cofollows("hint:vc kw:x | cofollow:4/12 seeds:@a,@b") == 4 and discover.cofollows(None) == 0
    assert discover.priority({"cofollows": 5}) - discover.priority({}) == pytest.approx(20.0)   # = 5 topical posts
    assert discover.priority({"cofollows": 9}) == discover.priority({"cofollows": 5})
    world(conn)
    cid, _ = discover.upsert_candidate(conn, "id_alice", "alice", "Alice", "cofollow:5/12", note="cofollow:5/12 seeds:@s0")
    sig = {s["candidate_id"]: s for s in discover.candidate_priorities(conn)}[cid]
    assert sig["cofollows"] == 5 and discover.candidate_signals(conn, cid)["cofollows"] == 5


def test_authors_queries_keep_the_engagement_floor():
    q = discover.load_queries()["authors"]
    assert 10 <= len(q) <= 14
    assert all("min_faves:" in x and "-is:retweet" in x and "lang:en" in x for x in q)
    assert to_web_query('"voice mode" min_faves:200 -is:retweet lang:en') == '"voice mode" min_faves:200 -filter:retweets lang:en'


def test_discover_runs_only_the_chosen_groups(conn):
    src = FakeSource({})
    discover.run_discover(conn, db.new_run(conn, "discover"), src, groups=("authors",))
    assert src.queries == discover.load_queries()["authors"]
    with pytest.raises(ValueError):
        discover.run_discover(conn, db.new_run(conn, "discover"), src, groups=("nope",))


def test_enrich_first_seen_since(conn):
    persist_users_batch(conn, [U("old1"), U("new1")], "fixture")
    discover.upsert_candidate(conn, "id_old1", "old1", "Old", "seed", now="2026-09-01T00:00:00Z")
    discover.upsert_candidate(conn, "id_new1", "new1", "New", "cofollow:3/12", now="2026-09-30T00:00:00Z")
    conn.commit()
    st = discover.run_enrich(conn, db.new_run(conn, "enrich"), FakeSource({}), first_seen_since="2026-09-29T00:00:00Z")
    assert st["handles"] == ["new1"]


def test_pipeline_wiring(conn, monkeypatch):
    assert pipeline.STAGES.index("expand") == pipeline.STAGES.index("discover") + 1 == pipeline.STAGES.index("enrich") - 1
    res = pipeline.run("expand")                                  # DRY_RUN: fixture source, empty DB -> no seeds
    assert res["expand"]["seeds"] == [] and "error" not in res["expand"]
    seen = {}
    monkeypatch.setattr(pipeline, "run", lambda stage, **kw: seen.update(stage=stage, **kw) or {"run_id": 1, "cost_usd": 0})
    pipeline.main(["run", "--stage", "discover,expand", "--query-groups", "authors", "--new-since", "31"])
    assert seen["stage"] == "discover,expand" and seen["query_groups"] == ["authors"] and seen["new_since"] == "31"
    assert seen["expand_seeds"] == 12 and seen["max_following"] is None     # each stage keeps its own default
