"""Offline tests for the monid adapter, using real payloads captured by the 2026-09-29 probe runs."""
import json

import pytest

from ring_wishlist import costs, db
from ring_wishlist.sources import monid
from ring_wishlist.sources.monid import MonidBalanceError, MonidError, MonidSource, to_web_query

SAMPLES = monid.config.ROOT / "tests" / "samples" / "c"


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    """The adapter's search log and follow-crawl log live in CACHE_DIR: never let tests touch the real one."""
    monkeypatch.setattr(monid.config, "CACHE_DIR", tmp_path / "cache")


def sample(name):
    return json.loads((SAMPLES / name).read_text())


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class FakeSession:
    """First POST /v1/run answers 202 RUNNING, then GET /v1/runs/:id answers the completed payload."""
    def __init__(self, completed, post_status=202):
        self.completed, self.post_status = completed, post_status
        self.posts, self.gets = [], []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        if self.post_status != 202:
            return FakeResp(self.post_status, {"code": self.post_status, "message": "x"})
        return FakeResp(202, {"runId": self.completed.get("runId", "R1"), "status": "RUNNING"})

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        return FakeResp(200, self.completed)


def src(conn, completed, **kw):
    return MonidSource(conn=conn, run_id=db.new_run(conn, "test"), session=FakeSession(completed, **kw),
                       api_key="k", poll_interval=0)


def test_profile_maps_and_caches(conn):
    s = src(conn, sample("run_profile_completed.json"))
    u = s.get_user_by_handle("@CarloAI")
    assert u.x_id == "1413073125542178819" and u.handle == "carloai"
    assert u.followers == 6852 and u.following == 1210 and u.verified is True
    assert u.created_at == "2021-07-08T09:51:28Z"
    # second call served from the local cache: no new run
    before = len(s.session.posts)
    assert s.get_user_by_handle("carloai").x_id == u.x_id
    assert len(s.session.posts) == before
    assert costs.spent(conn, "monid") == pytest.approx(0.0015)


def test_search_posts_maps_and_caches_authors(conn):
    s = src(conn, sample("run_tweets_completed.json"))
    posts = s.search_posts('"smart ring" voice -is:retweet lang:en', max_results=10)
    body = s.session.posts[0][1]["input"]["body"]
    assert body["searchTerms"] == ['"smart ring" voice -filter:retweets lang:en'] and body["tweetLanguage"] == "en"
    assert posts and all(p.url and p.x_id and p.handle for p in posts)
    assert posts[0].created_at.endswith("Z")
    n = db.one(conn, "SELECT COUNT(*) AS n FROM x_users")["n"]
    assert n >= 1, "authors must be cached so discovery does not pay for lookups"
    # the ledger holds the actual cost monid reported
    assert costs.spent(conn, "monid") == pytest.approx(0.006)


def test_follow_list_enforces_actor_minimum(conn):
    s = src(conn, sample("run_follows_completed.json"))
    users = s.get_following("carloai", max_users=50)
    body = s.session.posts[0][1]["input"]["body"]
    assert body["getFollowing"] is True and body["getFollowers"] is False
    assert body["maxFollowings"] >= 200 and body["maxFollowers"] >= 200
    assert len(users) == 3 and all(u.x_id and u.handle for u in users)
    assert any(u.can_dm is not None for u in users)


def test_numeric_id_needs_known_handle(conn):
    s = src(conn, sample("run_follows_completed.json"))
    with pytest.raises(MonidError):
        s.get_followers("123456")


def test_budget_guard_blocks_before_spending(conn, monkeypatch):
    monkeypatch.setenv("BUDGET_MONID_USD", "0.01")
    s = src(conn, sample("run_follows_completed.json"))
    with pytest.raises(costs.BudgetExceeded):
        s.get_followers("carloai", max_users=7000)
    assert s.session.posts == []


def test_balance_error(conn):
    s = src(conn, sample("run_follows_completed.json"), post_status=402)
    with pytest.raises(MonidBalanceError):
        s.get_following("carloai")


def test_query_translation():
    assert to_web_query('"AI wearable" -is:retweet -is:reply lang:en') == '"AI wearable" -filter:retweets -filter:replies lang:en'


def test_estimates():
    assert monid.estimate("follows", 1000) == pytest.approx(0.225)
    assert monid.estimate("tweets", 100) == pytest.approx(0.06)


def test_prefetch_then_search_is_free(conn):
    s = src(conn, sample("run_tweets_completed.json"))
    stats = s.prefetch_searches([("from:someone (ring OR voice) -is:retweet", 20), ("from:someone -is:retweet", 10)], workers=2)
    assert stats["fetched"] == 2 and stats["failed"] == 0
    posts_before = len(s.session.posts)
    got = s.search_posts("from:someone -is:retweet", max_results=10)
    assert got and len(s.session.posts) == posts_before, "prefetched search must be served from the cache"
    # an empty result is remembered too
    s2 = src(conn, {"runId": "E", "status": "COMPLETED", "providerResponse": {"httpStatus": 200}, "cost": {"value": 0}, "billedUnits": 0, "output": []})
    assert s2.search_posts("from:nobody -is:retweet", 10) == []
    n = len(s2.session.posts)
    assert s2.search_posts("from:nobody -is:retweet", 10) == [] and len(s2.session.posts) == n
