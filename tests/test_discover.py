"""Discovery stages on the offline FixtureSource (DRY_RUN=1 via conftest): no network, no spend."""
import json
import re
import time
from pathlib import Path

import pytest

from wearable_wishlist import classify, costs, db, discover
from wearable_wishlist.sources.base import FixtureSource, XPost, XUser, persist_posts, persist_users

REAL_VOICES = {"wearablenerd_test", "techwriter_test", "vc_test", "founder_test", "competitor_test"}
REAL_CATEGORIES = {"x_voice", "media", "vc", "founder_angel"}


@pytest.fixture(autouse=True)
def _seed_env(monkeypatch):
    monkeypatch.setenv("SEED_X_HANDLE", "seed_test")


@pytest.fixture
def run_id(conn):
    return db.new_run(conn, "test")


@pytest.fixture
def queries_file(tmp_path):
    p = tmp_path / "queries.yaml"
    p.write_text("\n".join([
        "topics:",
        "  - '\"smart wearable\" -is:retweet lang:en'",
        "  - '\"screen-free\" OR \"voice-first\" -is:retweet lang:en'",
        "competitor_products:",
        "  - '\"Oura\" (unboxing OR review) -is:retweet lang:en'",
        "wearable_wishlist: []",
        "hubs: []",
        "limits:",
        "  posts_per_query: 100",
        "  users_per_hub: 1000",
        "  min_likes: 20",
    ]))
    return p


def cands(conn):
    return {r["handle"]: r for r in db.rows(conn, "SELECT * FROM candidates")}


# ------------------------------------------------------------------ crawl

def test_crawl_creates_candidates_for_real_voices(conn, run_id, fixture_source):
    stats = discover.run_crawl(conn, run_id, fixture_source, "seed_test")
    c = cands(conn)
    assert REAL_VOICES <= set(c)
    assert "seed_test" not in c                      # the seed is never a candidate
    assert all(r["discovered_via"] == "seed" for r in c.values())
    assert all(r["category"] is None for r in c.values())   # category comes from classify, never from a bio heuristic
    assert "seed:mutual" in c["wearablenerd_test"]["notes"] and "hint:x_voice" in c["wearablenerd_test"]["notes"]
    assert "hint:vc" in c["vc_test"]["notes"]
    assert stats["following"] == 5 and stats["followers"] == 3
    assert sum(stats["by_hint"].values()) == stats["candidates_new"] == 5
    follows = db.rows(conn, "SELECT * FROM x_follows")
    assert len(follows) == 8
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_users")["n"] == 6   # seed + 5
    # idempotent: a re-run refreshes, never duplicates, and keeps discovered_via
    again = discover.run_crawl(conn, run_id, fixture_source, "seed_test")
    assert again["candidates_new"] == 0 and again["candidates_updated"] == 5
    assert len(cands(conn)) == 5


def test_crawl_never_makes_parody_a_real_candidate(conn, run_id, fixture_source):
    discover.run_crawl(conn, run_id, fixture_source, "seed_test")
    row = cands(conn).get("parody_test")
    assert row is None or row["category"] not in REAL_CATEGORIES
    # wearablenerd follows the parody account: crawling from there must skip it explicitly
    stats = discover.run_crawl(conn, run_id, fixture_source, "wearablenerd_test")
    assert stats["skipped"].get("parody") == 1 and stats["parody_skipped"] == ["parody_test"]
    assert "parody_test" not in cands(conn)


class BigGraphSource(FixtureSource):
    """The seed follows / is followed by thousands of synthetic accounts of every kind."""

    def __init__(self, n=3000):
        super().__init__()
        self.name = "fixture"
        kinds = [
            ("Partner at Foo Ventures. Consumer hardware and voice AI.", 5000, "blue"),     # vc
            ("Founder & CEO @startup. Building agents.", 2500, None),                       # founder
            ("Senior reporter covering wearables for a tech site.", 12000, None),           # media
            ("Coffee, dogs and hiking.", 300, None),                                        # no signal
            ("Official account of Foo Corp. News and updates.", 90000, "business"),         # org
            ("NFT giveaway every day! Follow back", 4000, None),                            # spam
            ("Actor. Dad. Lakers.", 5_000_000, "blue"),                                     # famous, no signal
            ("AI tinkerer", 120, None),                                                     # tiny, not mutual
        ]
        self.big = []
        for i in range(n):
            bio, followers, vt = kinds[i % len(kinds)]
            self.big.append(XUser(x_id=str(10_000 + i), handle=f"user{i}", name=f"User {i}", bio=bio, followers=followers,
                                  verified=vt is not None, verified_type=vt))

    def get_following(self, handle_or_id, max_users=1000):
        return self.big[:max_users]

    def get_followers(self, handle_or_id, max_users=1000):
        return self.big[len(self.big) // 2:][:max_users]


def test_crawl_handles_thousands_of_users(conn, run_id):
    src = BigGraphSource(3000)
    t0 = time.time()
    stats = discover.run_crawl(conn, run_id, src, "seed_test", max_following=3000, max_followers=3000)
    elapsed = time.time() - t0
    assert elapsed < 10, f"crawl of 3000 users took {elapsed:.1f}s"
    assert stats["users"] == 3000
    n = db.one(conn, "SELECT COUNT(*) AS n FROM candidates")["n"]
    # the vc / founder / reporter bios (3 of every 8) become candidates; the tiny "AI tinkerer" accounts only when they
    # are mutual follows of the seed (users 1500..2999 are both followed and followers: 188 of them)
    assert stats["by_hint"] == {"vc": 375, "founder_angel": 375, "media": 375, "x_voice": 188}
    assert n == stats["candidates_new"] == sum(stats["by_hint"].values())
    sk = stats["skipped"]
    assert sk["org"] == sk["spam"] == 375 and sk["no_signal"] == 750 and sk["small_audience"] == 375 - 188
    bios = {r["bio"] for r in db.rows(conn, "SELECT u.bio FROM candidates c JOIN x_users u ON u.x_id=c.x_id")}
    assert not any(b.startswith(("Actor", "NFT", "Official", "Coffee")) for b in bios)
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_follows")["n"] == 3000 + 1500


def test_crawl_projection_is_source_aware(conn, run_id, capsys):
    from wearable_wishlist.sources import monid, x_api

    class Named(FixtureSource):
        def __init__(self, name):
            super().__init__()
            self.name = name

    discover.run_crawl(conn, run_id, Named("monid"), "seed_test", max_following=5300, max_followers=3000)
    out = capsys.readouterr().out
    expected = monid.estimate("follows", 5300) + monid.estimate("follows", 3000) + monid.estimate("profile", 1)
    assert f"~{costs.fmt(expected)}" in out and expected < 3
    assert discover.estimate_cost(Named("x_api"), "follow_read", 8300) == pytest.approx(x_api.estimate("following", 8300))
    assert discover.estimate_cost(Named("monid"), "follow_read", 50) == pytest.approx(monid.estimate("follows", 200))  # 200-row floor
    assert discover.estimate_cost(FixtureSource(), "post_read", 1000) == 0


# ------------------------------------------------------------------ discover

def _fixtures_with_lurker(tmp_path):
    """The shared fixtures plus a low-signal author: one topical post with 3 likes and an unrelated bio."""
    from wearable_wishlist import config
    d = tmp_path / "fx"
    d.mkdir()
    users = json.loads((config.FIXTURES_DIR / "x_users.json").read_text())
    posts = json.loads((config.FIXTURES_DIR / "x_posts.json").read_text())
    users.append({"x_id": "9", "handle": "lurker_test", "name": "Lurker", "bio": "coffee and cats", "location": "",
                  "verified": False, "followers": 40, "following": []})
    posts.append({"post_id": "901", "x_id": "9", "handle": "lurker_test", "created_at": "2026-09-01T00:00:00Z",
                  "text": "my smart wearable died again", "likes": 3})
    (d / "x_users.json").write_text(json.dumps(users))
    (d / "x_posts.json").write_text(json.dumps(posts))
    return FixtureSource(d)


def test_discover_aggregates_posts_into_people(conn, run_id, queries_file, tmp_path):
    src = _fixtures_with_lurker(tmp_path)
    stats = discover.run_discover(conn, run_id, src, queries_file=queries_file)
    c = cands(conn)
    # people, not posts: one row per author, with the reason and the query that surfaced them
    assert {"wearablenerd_test", "techwriter_test", "vc_test", "founder_test", "eu_test", "competitor_test"} <= set(c)
    assert all(r["discovered_via"].startswith("query:") for r in c.values())
    assert "posts:2 topical" in c["wearablenerd_test"]["notes"]
    assert "posts:1 topical with 300 likes" in c["eu_test"]["notes"]
    assert "lurker_test" not in c            # 1 topical post, 3 likes, no bio match
    assert "parody_test" not in c and stats["parody_skipped"] == ["parody_test"]
    assert stats["queries_run"] == 3 and stats["authors"] == 8
    assert stats["authors_looked_up"] == 8   # profiles fetched for the bio heuristics
    stored = db.rows(conn, "SELECT post_id, query FROM x_posts")
    assert stored and all(r["query"] for r in stored)
    assert db.one(conn, "SELECT COUNT(DISTINCT post_id) AS n FROM x_posts WHERE x_id='2'")["n"] == 2


def test_discover_keeps_seed_provenance_and_skips_seed(conn, run_id, fixture_source, queries_file):
    discover.run_crawl(conn, run_id, fixture_source, "seed_test")
    discover.run_discover(conn, run_id, fixture_source, queries_file=queries_file)
    c = cands(conn)
    assert c["wearablenerd_test"]["discovered_via"] == "seed"          # first provenance kept
    assert "seed:mutual" in c["wearablenerd_test"]["notes"] and "posts:" in c["wearablenerd_test"]["notes"]
    assert c["eu_test"]["discovered_via"].startswith("query:")
    assert "seed_test" not in c


def test_discover_stops_at_budget(conn, run_id, queries_file):
    class PaidFixture(FixtureSource):
        name = "x_api"
        calls = 0

        def search_posts(self, query, max_results=100, since=None):   # a full page: the actual cost equals the projection
            PaidFixture.calls += 1
            return [XPost(post_id=f"{PaidFixture.calls}-{i}", x_id=str(500 + i), handle=f"a{i}", text="smart wearable review",
                          query=query) for i in range(max_results)]

    from wearable_wishlist.sources import x_api
    per_query = x_api.estimate("search_posts", 100)
    stats = discover.run_discover(conn, run_id, PaidFixture(), queries_file=queries_file, budget_usd=per_query * 1.5)
    assert PaidFixture.calls == 1
    assert stats["queries_run"] == 1 and stats["queries_skipped_budget"] == 2


def test_evidence_queries_are_first_person_searches_with_an_engagement_floor():
    from wearable_wishlist.sources.monid import to_web_query
    cfg = discover.load_queries()
    ev = cfg["evidence"]
    assert "evidence" in discover.QUERY_GROUPS and 18 <= len(ev) <= 22 and len(set(ev)) == len(ev)
    for q in ev:
        m = re.search(r"min_faves:(\d+) -is:retweet lang:en$", q)
        assert m and 50 <= int(m.group(1)) <= 150, q        # skips tiny accounts, keeps mid-size voices
        assert "-filter:retweets" in to_web_query(q) and "min_faves:" in to_web_query(q)
        assert q.count("(") == q.count(")") and q.count('"') % 2 == 0, q
    others = {q for g in discover.QUERY_GROUPS if g != "evidence" for q in cfg[g]}
    assert not others & set(ev)                              # different searches from topics, competitors and authors


def test_discover_runs_the_evidence_group_alone(conn, run_id, fixture_source):
    ev = discover.load_queries()["evidence"]
    stats = discover.run_discover(conn, run_id, fixture_source, groups=("evidence",))
    assert stats["queries_run"] == len(ev) and stats["hubs_crawled"] == 0
    c = cands(conn)
    assert {"wearablenerd_test", "techwriter_test", "vc_test", "founder_test"} <= set(c)
    assert all(r["discovered_via"].removeprefix("query:") in ev for r in c.values())
    assert "parody_test" not in c and "seed_test" not in c
    assert all(r["query"] in ev for r in db.rows(conn, "SELECT query FROM x_posts"))


# ------------------------------------------------------------------ signals and priority

def _person(conn, x_id, handle, bio, followers, verified=False, posts=(), now="2026-09-29T00:00:00Z"):
    persist_users(conn, [XUser(x_id=x_id, handle=handle, name=handle, bio=bio, followers=followers, verified=verified)], "test")
    persist_posts(conn, [XPost(post_id=f"{x_id}{i}", x_id=x_id, handle=handle, text=t, likes=likes, query=q)
                         for i, (t, likes, q) in enumerate(posts)], "test")
    return discover.upsert_candidate(conn, x_id, handle, handle, "manual", None, now)[0]


def test_priority_prefers_topical_people_over_celebrities(conn):
    celeb = _person(conn, "100", "celeb", "Founder. Actor. Investor.", 10_000_000, verified=True,
                    posts=[("great game tonight", 50_000, None)])
    small = _person(conn, "101", "smallvoice", "", 2_000, posts=[("my smart wearable finally has voice notes", 50, None)])
    reviewer = _person(conn, "102", "reviewer", "I review wearables", 48_000, verified=True,
                       posts=[("Oura unboxing", 400, "q"), ("smart wearables are the form factor", 1200, "q"),
                              ("three weeks with a pendant", 800, None)])
    parody = _person(conn, "103", "parodyacct", "PARODY account", 900_000, posts=[("smart wearable lol", 5000, "q")])
    conn.commit()
    order = [s["candidate_id"] for s in discover.candidate_priorities(conn)]
    assert order == [reviewer, small, celeb, parody]
    sig = discover.candidate_signals(conn, reviewer)
    assert sig["topical_posts"] == 3 and sig["priority"] == pytest.approx(order and discover.candidate_priorities(conn)[0]["priority"])
    # enrichment searches ('from:<h> ...') are evidence, not topic signals
    persist_posts(conn, [XPost(post_id="9999", x_id="100", handle="celeb", text="love my new glasses",
                               query="from:celeb (wearable OR glasses) -is:retweet")], "test")
    assert discover.candidate_signals(conn, celeb)["topical_posts"] == 0


def test_crawl_latest_status_is_not_a_topic_search_hit(conn):
    # the crawl stores each account's latest post with query='latest_status': topical only if the text is
    assert discover.query_kind("latest_status") is None
    assert not discover.post_is_topical("latest_status", "The only sells on the orderbooks are fake scare walls")
    assert discover.post_is_topical("latest_status", "day ten with my smart wearable")
    crypto = _person(conn, "200", "cryptoguy", "", 1_000_000, posts=[("fake scare walls on the orderbooks", 900, "latest_status")])
    conn.commit()
    assert discover.candidate_signals(conn, crypto)["topical_posts"] == 0


# ------------------------------------------------------------------ enrich

class SearchSource(FixtureSource):
    """Answers run_enrich's 'from:<handle> ...' searches with synthetic own posts (plus someone else's, to be filtered)."""

    def __init__(self, name="fixture", fill=False):
        super().__init__()
        self.name = name
        self.fill = fill          # return exactly max_results own posts (a paid source then bills the full projection)
        self.searches: list[tuple[str, int]] = []
        self.timelines: list[str] = []

    def search_posts(self, query, max_results=100, since=None):
        self.searches.append((query, max_results))
        if not query.startswith("from:"):
            return super().search_posts(query, max_results, since)
        handle = query.split()[0][5:]
        u = self._resolve(handle)
        if not u:
            return []
        if self.fill:
            kind = "e" if "(" in query else "r"
            return [XPost(post_id=f"{u.x_id}{kind}{i}", x_id=u.x_id, handle=handle, text="wearable", likes=1) for i in range(max_results)]
        if "(" in query:
            own = [XPost(post_id=f"{u.x_id}e{i}", x_id=u.x_id, handle=handle, text=f"day {i} with my Oura wearable and voice notes",
                         likes=10 + i, created_at=f"2026-09-{10 + i:02d}T00:00:00Z") for i in range(3)]
        else:
            own = [XPost(post_id=f"{u.x_id}r{i}", x_id=u.x_id, handle=handle, text=f"lunch thoughts {i}", likes=1,
                         created_at=f"2026-09-2{i}T00:00:00Z") for i in range(2)]
        other = XPost(post_id=f"x{u.x_id}", x_id="999", handle="someone_else", text="not theirs")
        return (own + [other])[:max_results]

    def get_user_posts(self, handle_or_id, max_results=100):
        self.timelines.append(handle_or_id)
        return super().get_user_posts(handle_or_id, max_results)


def test_enrich_fetches_own_posts_in_priority_order_and_marks_candidates(conn, run_id, fixture_source, queries_file):
    discover.run_crawl(conn, run_id, fixture_source, "seed_test")
    discover.run_discover(conn, run_id, fixture_source, queries_file=queries_file)
    ranked = discover.candidate_priorities(conn)
    eligible = [s["handle"] for s in ranked if discover.enrich_gate(s) is None]
    # competitor staff (bio "@ouraring") and the Berlin account are paid for like anyone else: flagged, never held back
    assert {s["handle"]: discover.enrich_gate(s) for s in ranked if discover.enrich_gate(s)} == {}
    top = eligible[:3]
    src = SearchSource()
    stats = discover.run_enrich(conn, run_id, src, top=3, posts_per_user=30)
    assert stats["skipped"] == {}          # was competitor_role 1, non_us 1: gates removed (Federico, 30 Sep)
    assert stats["enriched"] == 3 and stats["handles"] == top == ["wearablenerd_test", "techwriter_test", "competitor_test"]
    assert stats["posts_new"] == 3 * 5 and stats["evidence_posts"] == 3 * 3
    # two searches per person: device/voice evidence (20) then recent context (10)
    assert [q.split()[0] for q, _ in src.searches] == [f"from:{h}" for h in top for _ in (0, 1)]
    assert [n for _, n in src.searches] == [20, 10] * 3
    assert "Oura" in src.searches[0][0] and src.searches[1][0] == f"from:{top[0]} -is:retweet"
    for h in top:
        notes = cands(conn)[h]["notes"]
        assert f"enriched:{db.utcnow()[:10]} posts=5" in notes
    assert not db.rows(conn, "SELECT 1 FROM x_posts WHERE handle='someone_else'")     # only their own words
    kinds = {r["query"].split()[0] for r in db.rows(conn, "SELECT query FROM x_posts WHERE post_id LIKE '%e%' OR post_id LIKE '%r%'")}
    assert kinds == {f"from:{h}" for h in top}
    # second run: the enriched ones are not bought again, the next eligible ones in priority order are
    src2 = SearchSource()
    stats2 = discover.run_enrich(conn, run_id, src2, top=3)
    assert stats2["handles"] == eligible[3:6] == ["vc_test", "founder_test", "eu_test"]   # the non-US account too


def test_enrich_keeps_discovery_query_on_known_posts(conn, run_id, fixture_source, queries_file):
    discover.run_crawl(conn, run_id, fixture_source, "seed_test")
    discover.run_discover(conn, run_id, fixture_source, queries_file=queries_file)
    before = {r["post_id"]: r["query"] for r in db.rows(conn, "SELECT post_id, query FROM x_posts WHERE x_id='2'")}
    discover.run_enrich(conn, run_id, fixture_source, top=10, mode="timeline")
    after = {r["post_id"]: r["query"] for r in db.rows(conn, "SELECT post_id, query FROM x_posts WHERE x_id='2'")}
    assert set(after) == {"101", "102", "103"}          # the timeline added the post no query found
    assert all(after[p] == q for p, q in before.items())
    assert after["103"] is None or not after["103"].startswith("from:")


def test_enrich_stops_at_budget_and_skips_parody(conn, run_id):
    from wearable_wishlist.sources import monid
    for x_id, h, bio in [("2", "wearablenerd_test", "Reviewing wearables"), ("3", "techwriter_test", "Editor, covers wearables"),
                         ("4", "vc_test", "Partner @ExampleVentures"), ("6", "parody_test", "PARODY account")]:
        _person(conn, x_id, h, bio, 10_000, posts=[("smart wearable talk", 100, "q")])
    conn.commit()
    src = SearchSource(name="monid", fill=True)
    per_user = monid.estimate("tweets", 20) + monid.estimate("tweets", 10)
    stats = discover.run_enrich(conn, run_id, src, top=10, posts_per_user=30, budget_usd=per_user * 2.5)
    assert stats["enriched"] == 2 and stats["budget_stop"] and stats["skipped_budget"] == 1
    assert stats["skipped_suspicious"] == 1
    assert not any("parody_test" in q for q, _ in src.searches)
    assert stats["estimated_cost_usd"] <= per_user * 2.5


def test_enrich_survives_a_failing_account(conn, run_id):
    for x_id, h in [("2", "wearablenerd_test"), ("3", "techwriter_test")]:
        _person(conn, x_id, h, "reviews wearables", 10_000, posts=[("smart wearable", 100, "q")])
    conn.commit()

    class Flaky(SearchSource):
        def search_posts(self, query, max_results=100, since=None):
            if "wearablenerd_test" in query:
                raise RuntimeError("upstream 500")
            return super().search_posts(query, max_results, since)

    stats = discover.run_enrich(conn, run_id, Flaky(), top=5)
    assert stats["errors"] == 1 and stats["enriched"] == 1
    assert discover.enriched_on(cands(conn)["wearablenerd_test"]["notes"]) is None


def test_enrich_gates_skip_brands_and_tiny_accounts_but_not_competitor_staff_or_non_us(conn, run_id):
    people = [
        ("11", "ultrahumanhq", "The world's most advanced wearable. We make wearables.", 90_000, None, None),
        ("12", "techmeme", "Tech news, curated.", 500_000, None, None),
        ("13", "trueventures", "Early-stage venture capital firm based in San Francisco.", 50_000, None, None),
        ("14", "jesselyu", "founder and ceo @rabbit_hmi", 100_000, None, None),
        ("15", "tankots", "CEO at wisprflow.ai", 30_000, None, None),
        ("16", "copenhagener", "AI hardware and voice interfaces", 20_000, "Copenhagen, Denmark", None),
        ("17", "tinyvoice", "AI wearables nerd", 420, None, None),
        ("18", "mapfriend", "GP, early stage. Wearables.", 300, None, 7),          # in the operator's map: exempt from the floor
        ("19", "reviewer", "I review wearables. Oura on one hand, Ultrahuman on the other.", 48_000, "Austin, TX", None),
        ("20", "nolocation", "Voice-first believer and wearable reviewer", 9_000, "", None),
    ]
    names = {"ultrahumanhq": "Ultrahuman", "techmeme": "Techmeme", "trueventures": "True Ventures"}
    for x_id, h, bio, followers, loc, map_id in people:
        persist_users(conn, [XUser(x_id=x_id, handle=h, name=names.get(h, h.title() + " Person"), bio=bio,
                                   followers=followers, location=loc)], "test")
        cid_, _ = discover.upsert_candidate(conn, x_id, h, h, "manual")
        if map_id:
            conn.execute("UPDATE candidates SET map_person_id=? WHERE candidate_id=?", (map_id, cid_))
    conn.commit()
    src = SearchSource()
    src._users.update({x: XUser(x_id=x, handle=h) for x, h, *_ in people})
    stats = discover.run_enrich(conn, run_id, src, top=80)
    # @jesselyu, @tankots (competitor staff) and @copenhagener (Denmark) are paid for now: flagged, never held back
    assert stats["skipped"] == {"competitor_brand": 1, "org": 2, "small_audience": 1}
    assert sorted(stats["handles"]) == ["copenhagener", "jesselyu", "mapfriend", "nolocation", "reviewer", "tankots"]


def test_enrich_gate_skips_accounts_selling_collabs_in_bio_or_location():
    """QA of run 14 (docs/QA_TOP.md): collab-for-hire creators put the call to action in the bio or the location field."""
    base = {"name": "Creator Person", "handle": "creator", "followers": 80_000}
    assert discover.enrich_gate({**base, "bio": "300K+ AI Community | Collabs & partnerships 📩 x@gmail.com"}) == "paid_promo"
    assert discover.enrich_gate({**base, "bio": "Tinkering with AI", "location": "📩 DM for Collabs"}) == "paid_promo"
    assert discover.enrich_gate({**base, "bio": "AI & Tech Educator", "location": "Free Products + Sponsorships →"}) == "paid_promo"
    assert discover.enrich_gate({**base, "bio": "Tech & AI insights | Open to collabs 📩"}) == "paid_promo"
    assert discover.enrich_gate({**base, "bio": "EX Community & Collab Manager, writer"}) is None     # a job title, no call to action


def test_enrich_prefetches_the_exact_searches_and_respects_the_budget(conn, run_id):
    from wearable_wishlist.sources import monid
    for i in range(5):
        _person(conn, str(30 + i), f"voice{i}", "I review wearables", 10_000 + i, posts=[("smart wearable talk", 100 - i, "q")])
    conn.commit()

    class Prefetching(SearchSource):
        def __init__(self):
            super().__init__(name="monid", fill=True)
            self._users.update({str(30 + i): XUser(x_id=str(30 + i), handle=f"voice{i}") for i in range(5)})
            self.prefetched = []

        def prefetch_searches(self, queries, workers=8):
            self.prefetched = list(queries)
            return {"requested": len(queries), "fetched": len(queries)}

    src = Prefetching()
    per_user = monid.estimate("tweets", 20) + monid.estimate("tweets", 10)
    stats = discover.run_enrich(conn, run_id, src, top=80, posts_per_user=30, budget_usd=per_user * 3.5)
    # only the people the budget allows are prefetched (bought), then read back with the very same strings and sizes
    assert stats["selected"] == stats["enriched"] == 3 and stats["skipped_budget"] == 2 and stats["budget_stop"]
    assert src.prefetched == src.searches and len(src.prefetched) == 6
    assert [n for _, n in src.prefetched] == [20, 10] * 3
    assert stats["prefetch"] == {"requested": 6, "fetched": 6}


def test_enrich_gate_has_no_reachability_gate_and_map_is_a_small_hint():
    """the operator, 29 Sep: wearables may go outside the map. A big on-topic creator with no path is enriched; a map link weighs
    half a topical post in priority, so topic evidence outranks map membership."""
    mkbhd = {"name": "Marques Brownlee", "handle": "mkbhd", "bio": "Web Video Producer", "followers": 6_800_000,
             "location": "NJ", "map_person_id": None, "mutual_follow_with_seed": False}
    assert discover.enrich_gate(mkbhd) is None
    assert discover.PRIORITY_WEIGHTS["map"] == 2.0
    topical = {"topical_posts": 1, "engagement": 0, "followers": 1000}
    in_map = {"topical_posts": 0, "engagement": 0, "followers": 1000, "map_person_id": 7}
    assert discover.priority(topical) > discover.priority(in_map)


def test_enrich_gate_pays_for_competitor_staff_and_non_us_people_but_not_competitor_brands():
    """Federico, 30 Sep: competitors are flagged, never removed, and the US only filter must be usable, so neither a role
    at a competitor nor a non-US location keeps a person from evidence. A competitor's own brand account is no person."""
    staff = {"name": "Tan Person", "handle": "tankots", "bio": "CEO at wisprflow.ai", "followers": 30_000}
    abroad = {"name": "Kay Person", "handle": "copenhagener", "bio": "AI hardware and voice interfaces", "followers": 20_000,
              "location": "Copenhagen, Denmark"}
    assert discover.competitor_role(staff["bio"]) == "Wispr Flow" and discover.enrich_gate(staff) is None
    assert classify.precheck_us_based(abroad["location"])[0] is False and discover.enrich_gate(abroad) is None
    assert discover.enrich_gate({**staff, "location": "Bengaluru, India"}) is None                 # both at once
    for handle, name in (("ultrahumanhq", "Ultrahuman"), ("ouraring", "Oura"), ("plaudai", "PLAUD")):
        assert discover.enrich_gate({"name": name, "handle": handle, "bio": "Smart wearable", "followers": 90_000}) == "competitor_brand"


# ---- persona guards (red_flag): the reviewers' repeat rejects, docs/QA_TOP.md runs 14, 30 and 39 ----

# The 205 people reviewed by hand (runs 14, 30, 39, 40): latest verdict, the kinds read from the reviewers' reasons, and the profile fields.
QA_REVIEWED = json.loads((Path(__file__).parent / "samples" / "qa_reviewed.json").read_text())


def _flag(p):
    return discover.red_flag(p.get("name"), p["handle"], p.get("bio"), p.get("location"), p.get("url"), in_map=p.get("in_map", False))


def test_red_flag_never_hits_a_person_the_reviewers_kept_or_fixed():
    kept = [p for p in QA_REVIEWED if p["verdict"] in ("keep", "fix")]
    assert len(kept) == 75
    assert [(p["handle"], _flag(p)) for p in kept if _flag(p)] == []


def test_red_flag_recall_on_the_reviewers_rejects_by_kind():
    """The 130 rejects; a reject can have several kinds. Misses: an email-only bio (@matiasschrank), promos seen only in
    posts (@mrjlwilliams, @hubermanlab), curators with a first-person bio or none (@andrewcurran_, @aakashgupta,
    @anatolikopadze), agencies without a selling phrase (@aryanxmahajan), @synthpotato and @labstamil, and pseudonyms
    under a two-word name (@cocktailpeanut, @airkatakana, @ai_sentience, '10x Engineer', 'CardiovascularCorner') or the
    handle as the name (@teslaconomics)."""
    rejects = [p for p in QA_REVIEWED if p["verdict"] == "reject"]
    kinds = ("crypto", "promo", "aggregator", "brand", "anon")
    caught = {k: sum(bool(_flag(p)) for p in rejects if k in p["kinds"]) for k in kinds}
    total = {k: sum(k in p["kinds"] for p in rejects) for k in kinds}
    assert total == {"crypto": 12, "promo": 30, "aggregator": 12, "brand": 15, "anon": 18}
    assert caught == {"crypto": 12, "promo": 27, "aggregator": 9, "brand": 12, "anon": 12}
    targeted = [p for p in rejects if p["kinds"]]
    assert len(targeted) == 69 and sum(bool(_flag(p)) for p in targeted) == 54
    assert not any(_flag(p) for p in rejects if not p["kinds"])       # non-US, competitor, no evidence: not this guard's job


@pytest.mark.parametrize("name,handle,bio,location,url,kind", [
    # crypto personas
    ("Lil Auntie", "auntiepaca", "HODL since 2016. $BTC Crypto and Security with Tacos.", "Simulation", None, "crypto"),
    ("Jessie Xiao", "jessiexiao_eth", "Ecosystem Ventures Principal @Starknet | Prev: Co-Founder @_BubbleDAO", None, None, "crypto"),
    ("The Wheelie Investor", "wheelieinvestor", "Agentic Trading | Building @AgenticTG | NFA", None, None, "crypto"),
    ("Mr.Chongky", "chongkydudut", "Podcast Manager | Entropy Acolyte | DePIN Tester", None, None, "crypto"),
    ("Kellen", "kellenxbt", "meet - kellen || partner @zargates", None, None, "crypto"),
    ("Master", "coinmaster100x", "Kol // PR // BD || Listing Manager", "Space", None, "crypto"),
    ("Ryan G", "rydawg42", "$HOOD Gold Member est. 2018 | Sharing tips on $HOOD UI", None, None, "crypto"),
    ("x", "degen_x", "degen, memecoin and airdrop hunter. DYOR, alpha calls daily", None, None, "crypto"),
    # promo, collabs for hire, giveaways (the reviewers' phrases that sells_posts missed)
    ("Kevin Parker", "kevin_parker_ai", "AI Strategist. Influencer Marketing BIZ: k@gmail.com", "Contact me here 👉", None, "promo"),
    ("Perks", "perksverse", "AI Explorer, Creative Director", "✉️ Available for projects - DM", None, "promo"),
    ("Linus", "linusekenstam", "Designer. collab@example.com for partnerships 🤝", None, None, "promo"),
    ("GARLOTIC", "garlotic", "turning AI into real growth for creators & brands | @aiscwork co-founder", None, None, "promo"),
    ("Eric", "outsource_", "Building @hermesworldai | Ambassador @alibaba_qwen", "Miami, FL", None, "promo"),
    ("Rez Karim", "rezkhere", "I talk AI video, ads & prompt engineering.", "Try Seedance 2.0 →", None, "promo"),
    ("Mike Futia", "mikefutia", "Building AI systems for ecom & ads", "Join 600+ marketing teams 👉", None, "promo"),
    ("marcus", "marcusyul", "powering brands through AI | building @aiscwork", None, None, "promo"),
    ("Tech Deals", "techdealz", "Daily gadget giveaways! Follow + RT to enter", None, None, "promo"),
    ("Aema | The AI Girl", "successsparksx", "AI Educator & Consultant. Practical AI tools. HIGGS-06Q", None, None, "promo"),
    ("Rahul", "sairahul1", "Building with AI. Sharing what's wild, what's practical, and what's next.", None, None, "feed"),
    # feeds and brands
    ("Chris", "chrisgpt", "AI Insider / Reporter as featured in Axios • NYT • Techcrunch", None, None, "feed"),
    ("Daily AI", "dailyaiwire", "The latest AI news, AI tools and prompts, every morning.", None, None, "feed"),
    ("Seeed Studio", "seeedstudio", "The #AI Hardware Partner", "Shenzhen, Austin & Nagoya", "https://www.seeedstudio.com/", "brand"),
    ("Heather D", "mommypotamus", "Mommypotamus is a trusted resource for reliable health research", "Florida, USA", None, "brand"),
    ("TechRadar", "techradar", "A team of gadget obsessives. Part of @futureplc", "UK, US, AU", None, "brand"),
    ("Mukul Sharma", "stufflistings", "Techmaster. Building the biggest tech community.", None, "http://stufflistings.shop", "brand"),
    ("BGR", "bgr", "Expert analysis of the leading stories in tech", None, None, "brand"),
    # anonymous
    ("string", "doctorcalf", "", None, None, "anonymous"),
    ("DogeDesigner", "cb_doge", None, None, None, "anonymous"),
    ("Matt", "matt503ea5sf9z5", "recursive self improvement enjoyer", "Hallmark Channel Movie", None, "anonymous"),
    ("Dustin", "r0ck3t23", "Human intuition is linear. The AI transition is exponential.", None, None, "anonymous"),
    ("Joe", "omoreyy___", "Content Creator | Writer | @omoreyy_ backup", "Valhalla", None, "anonymous"),
    ("The Random Recruiter", "randomrecruiter", "Tech Recruiter with over a decade of recruiting experience", None, None, "anonymous"),
])
def test_red_flag_kinds(name, handle, bio, location, url, kind):
    assert discover.red_flag(name, handle, bio, location, url)[0] == kind


@pytest.mark.parametrize("name,handle,bio,location,url,in_map", [
    ("𒐪", "shl0ms", "", None, "http://shl0ms.com/", False),                      # symbols only, but his own domain
    ("swyx", "swyx", "achieve ambition. affiliations: - @smol_ai - @latentspacepod", None, "https://swyx.io", False),
    ("Stammy", "stammy", "head of design @sesame", None, None, False),               # the name is the handle: not a rule
    ("jack", "jack", "no state is the best state", None, None, False),
    ("DavidLinthicum", "davidlinthicum", "CTO, CEO, blogger, speaker, author. All opinions are my own.", None, None, False),
    ("DHH", "dhh", "Creator of Ruby on Rails, co-owner & CTO of 37signals", None, None, False),
    ("Hovhannes M", "hovinthenorth", "Founder of @sevakcoffee. Investor in $SPCX (pre-IPO).", "Seattle, WA", None, False),
    ("Tibo", "thsottiaux", "Codex & ChatGPT @OpenAI", None, None, False),
    ("robin", "zebird0", "stealth consumer bio | prev founding team @a16z @speedrun | writer", None, None, False),
    ("Peter Yang", "petergyang", "Practical AI tutorials and interviews for busy people", "Get my best tutorials →", None, False),
    ("Alex Finn", "alexfinn", "Founder/CEO of Henry Intelligent Machines", "Master Grok Bot 👉 ", None, False),
    ("Jesse Middleton", "srcasm", "GP @Flybridge. $1M-$3M checks into founders. 10x human potential.", "NYC. Say hi 👉", None, False),
    ("Nick Dobos", "nickadobos", "prompt engineer @browsercompany | I make apps", "Seattle, WA", None, False),
    ("JK Molina", "onejkmolina", "Reducing coaches' and agencies' churn by half (DFY).", None, None, False),
    ("Jane Dao", "janedao", "Reporter @verge covering AI news and gadgets", None, None, False),   # 'Dao' is a name; a job
    ("Oreoluwa B", "bukkyoa", "I coach young professionals", None, "https://stan.store/bukkyoa", False),
    ("ted", "tednotlasso", "", None, None, True),                                     # in the operator's map: he knows who it is
])
def test_red_flag_spares_real_people(name, handle, bio, location, url, in_map):
    assert discover.red_flag(name, handle, bio, location, url, in_map=in_map) is None


def test_enrich_gate_skips_red_flags_before_paying():
    base = {"handle": "x", "name": "X Person", "followers": 50_000, "location": "New York"}
    assert discover.enrich_gate({**base, "bio": "Web3 degen, NFA"}) == "crypto"
    assert discover.enrich_gate({**base, "bio": "Top AI tools and prompts, every morning"}) == "feed"
    assert discover.enrich_gate({**base, "bio": "Designer", "location": "✉️ Available for projects - DM"}) == "paid_promo"
    matt = {**base, "handle": "matt503ea5sf9z5", "name": "Matt", "bio": "recursive self improvement enjoyer"}
    assert discover.enrich_gate(matt) == "anonymous"
    assert discover.enrich_gate({**matt, "map_person_id": 7}) is None
    assert discover.enrich_gate({**base, "bio": "Senior Editor @9to5Mac // I write about AI, apps, and Apple."}) is None


def test_enrich_device_only_runs_one_device_search(conn, run_id):
    _person(conn, "2", "wearablenerd_test", "reviews wearables", 10_000, posts=[("smart wearable", 100, "q")])
    conn.commit()
    cid = cands(conn)["wearablenerd_test"]["candidate_id"]
    src = SearchSource()
    stats = discover.run_enrich(conn, run_id, src, top=5, posts_per_user=20, candidate_ids=[cid], device_only=True)
    assert stats["enriched"] == 1 and [n for _, n in src.searches] == [20]          # no recent-context search
    q = src.searches[0][0]
    assert q == discover.device_queries("wearablenerd_test")[0] and "Oura" in q and "Wispr" in q
    assert "ChatGPT" not in q and "agent" not in q and discover.query_kind(q) == "evidence"
