"""Workstream B: XApiSource against recorded X API v2 payloads (tests/samples/b). No network, no spend:
a FakeSession serves the payloads; time.sleep is patched on the rate-limit and retry paths."""
import datetime as dt
import json
import time
from pathlib import Path

import pytest

from wearable_wishlist import costs, db
from wearable_wishlist.sources import x_api
from wearable_wishlist.sources.x_api import XApiSource

SAMPLES = Path(__file__).parent / "samples" / "b"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text())


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = {"x-rate-limit-limit": "300", "x-rate-limit-remaining": "299",
                        "x-rate-limit-reset": str(int(time.time()) + 900), **(headers or {})}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Serves a queue of responses in order, or delegates to `handler(method, url, params)`. Records every call."""

    def __init__(self, responses=None, handler=None):
        self.queue = list(responses or [])
        self.handler = handler
        self.calls: list[dict] = []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if self.handler:
            return self.handler(method, url, params or {})
        assert self.queue, f"unexpected extra request to {url}"
        return self.queue.pop(0)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "[not-a-token]")
    monkeypatch.delenv("X_USER_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("BUDGET_X_API_USD", "100")      # tests must not depend on the .env caps


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


def cost_rows(conn):
    return db.rows(conn, "SELECT endpoint, units, unit_cost_usd, cost_usd, note FROM costs ORDER BY cost_id")


# ---------------------------------------------------------------- token / dry-run safety

def test_missing_token_raises_on_first_use(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    src = XApiSource(session=FakeSession([FakeResponse(200, sample("user_by_username"))]))  # constructing is fine
    with pytest.raises(RuntimeError, match="X_BEARER_TOKEN"):
        src.get_user_by_handle("wearablenerd_test")


def test_dry_run_never_touches_a_real_session():
    src = XApiSource()  # real requests.Session, but DRY_RUN=1 (conftest) must stop it before any network call
    with pytest.raises(RuntimeError, match="DRY_RUN"):
        src.get_user_by_handle("wearablenerd_test")


# ---------------------------------------------------------------- mapping

def test_user_by_handle_mapping_request_cost_and_persistence(conn, capsys):
    run_id = db.new_run(conn, "crawl")
    fake = FakeSession([FakeResponse(200, sample("user_by_username"))])
    src = XApiSource(conn=conn, run_id=run_id, session=fake)
    u = src.get_user_by_handle("@WearableNerd_Test")
    assert u is not None
    assert u.x_id == "2244994945" and isinstance(u.x_id, str)
    assert u.handle == "wearablenerd_test"
    assert u.name == "Rina Wearable" and u.location == "Austin, TX"
    assert u.bio.startswith("Reviewing wearables")
    assert u.url == "https://wearablenerd.example"            # expanded, not the t.co link
    assert u.verified is True and u.verified_type == "blue"
    assert u.created_at == "2015-06-01T00:00:00Z"           # normalised to the db convention
    assert (u.followers, u.following, u.posts_count) == (48000, 1200, 12000)
    assert u.can_dm is None                                  # unknown for app-only auth, never guessed
    assert u.profile_image.endswith("rina_normal.jpg") and u.raw["id"] == "2244994945"
    # request shape
    call = fake.calls[0]
    assert call["method"] == "GET" and call["url"] == "https://api.x.com/2/users/by/username/wearablenerd_test"
    assert call["params"]["user.fields"] == x_api.USER_FIELDS
    assert call["headers"]["Authorization"] == "Bearer [not-a-token]"
    # cost line printed, guard + record
    assert "[cost] x_api users/by/username: ~$0.01 for 1 units" in capsys.readouterr().out
    rows = cost_rows(conn)
    assert len(rows) == 1 and rows[0]["endpoint"] == "users/by/username" and rows[0]["units"] == 1
    assert rows[0]["unit_cost_usd"] == costs.X_PRICES["user_read"] and abs(rows[0]["cost_usd"] - 0.01) < 1e-9
    assert abs(costs.spent(conn, "x_api", run_id) - 0.01) < 1e-9
    # persisted
    row = db.one(conn, "SELECT * FROM x_users WHERE x_id='2244994945'")
    assert row["handle"] == "wearablenerd_test" and row["source"] == "x_api" and row["can_dm"] is None
    assert json.loads(row["raw_json"])["username"] == "WearableNerd_Test"


def test_user_not_found_returns_none_and_bills_nothing(conn):
    fake = FakeSession([FakeResponse(200, sample("user_not_found"))])
    src = XApiSource(conn=conn, session=fake)
    assert src.get_user_by_handle("nobody_here_xyz") is None
    assert sum(r["cost_usd"] for r in cost_rows(conn)) == 0
    assert src.get_user_by_handle("this handle is not valid!") is None and len(fake.calls) == 1


def test_search_posts_mapping_expansions_and_persistence(conn):
    fake = FakeSession([FakeResponse(200, sample("search_recent_page1")), FakeResponse(200, sample("search_recent_page2"))])
    src = XApiSource(conn=conn, session=fake)
    q = '"smart wearable" (AI OR voice) -is:retweet lang:en'
    posts = src.search_posts(q, max_results=100)
    assert [p.post_id for p in posts] == ["1841000000000000101", "1841000000000000102", "1841000000000000103"]
    p1, p2, p3 = posts
    assert p1.x_id == "2" and p1.handle == "wearablenerd_test"           # handle from includes.users via author_id
    assert p2.handle == "techwriter_test"                             # lowercased
    assert p1.url == "https://x.com/wearablenerd_test/status/1841000000000000101"
    assert (p1.likes, p1.reposts, p1.replies, p1.quotes, p1.views) == (420, 60, 35, 8, 90000)
    assert p1.created_at == "2026-09-20T15:00:00Z"
    assert p1.mentions == ["vc_test"]
    assert p2.mentions == ["wearablenerd_test", "seed_test"] and p2.in_reply_to_x_id == "2"
    assert p1.in_reply_to_x_id is None and p3.mentions == [] and p3.views == 250000
    assert all(p.query == q for p in posts)
    assert p1.raw["author_username"] == "wearablenerd_test" and p1.raw["entities"]["urls"][0]["expanded_url"].endswith("/oura5")
    # request shape: recent search, both pages, next_token on the second
    assert fake.calls[0]["url"] == "https://api.x.com/2/tweets/search/recent"
    prm = fake.calls[0]["params"]
    assert prm["query"] == q and prm["max_results"] == 100 and prm["expansions"] == "author_id"
    assert prm["tweet.fields"] == x_api.TWEET_FIELDS and prm["user.fields"] == x_api.USER_FIELDS and "start_time" not in prm
    assert fake.calls[1]["params"]["next_token"] == "b26v89c19zqg8o3fpzhl1lmdzcjrb5o6bxnq0b9n8kpul"
    assert fake.calls[1]["params"]["max_results"] == 98
    # persisted posts and the expanded authors
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_posts WHERE source='x_api'")["n"] == 3
    assert json.loads(db.one(conn, "SELECT mentions FROM x_posts WHERE post_id='1841000000000000102'")["mentions"]) == ["wearablenerd_test", "seed_test"]
    assert {r["handle"] for r in db.rows(conn, "SELECT handle FROM x_users")} == {"wearablenerd_test", "techwriter_test", "founder_test"}
    # ledger: posts at post price (2 + 1), expanded authors as user reads when BILL_EXPANSIONS
    rows = cost_rows(conn)
    post_rows = [r for r in rows if r["endpoint"] == "tweets/search/recent"]
    assert [r["units"] for r in post_rows] == [2, 1] and all(r["unit_cost_usd"] == costs.X_PRICES["post_read"] for r in post_rows)
    exp_rows = [r for r in rows if r["endpoint"] == "tweets/search/recent#author_id"]
    if x_api.BILL_EXPANSIONS:
        assert [r["units"] for r in exp_rows] == [2, 1] and all(r["unit_cost_usd"] == costs.X_PRICES["user_read"] for r in exp_rows)
    else:
        assert exp_rows == []


def test_search_posts_full_archive_when_since_is_old(no_sleep):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%d")
    fake = FakeSession([FakeResponse(200, sample("search_recent_page2"))])
    posts = XApiSource(session=fake).search_posts("Wearable Wishlist", max_results=10, since=old)
    assert len(posts) == 1
    assert fake.calls[0]["url"] == "https://api.x.com/2/tweets/search/all"
    assert fake.calls[0]["params"]["start_time"] == f"{old}T00:00:00Z"
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake2 = FakeSession([FakeResponse(200, sample("search_recent_page2"))])
    XApiSource(session=fake2).search_posts("Wearable Wishlist", max_results=3, since=recent)
    assert fake2.calls[0]["url"] == "https://api.x.com/2/tweets/search/recent"
    assert fake2.calls[0]["params"]["start_time"] == recent
    assert fake2.calls[0]["params"]["max_results"] == 10        # endpoint minimum is 10


def test_get_user_posts_uses_timeline_and_excludes_retweets(conn):
    fake = FakeSession([FakeResponse(200, sample("user_by_username")), FakeResponse(200, sample("user_tweets"))])
    src = XApiSource(conn=conn, session=fake)
    posts = src.get_user_posts("wearablenerd_test", max_results=50)
    assert fake.calls[1]["url"] == "https://api.x.com/2/users/2244994945/tweets"
    assert fake.calls[1]["params"]["exclude"] == "retweets" and fake.calls[1]["params"]["max_results"] == 50
    assert [p.handle for p in posts] == ["wearablenerd_test", "wearablenerd_test"]
    assert posts[1].in_reply_to_x_id == "1" and posts[1].mentions == ["seed_test"]
    # numeric id: no lookup call, handle from the local cache
    fake2 = FakeSession([FakeResponse(200, sample("user_tweets"))])
    src2 = XApiSource(conn=conn, session=fake2)
    assert src2.get_user_posts("2244994945", max_results=5)[0].handle == "wearablenerd_test"
    assert fake2.calls[0]["url"] == "https://api.x.com/2/users/2244994945/tweets"


# ---------------------------------------------------------------- pagination, caps, cache, batching

def test_following_pagination_cap_cost_and_follows(conn):
    run_id = db.new_run(conn, "crawl")
    fake = FakeSession([FakeResponse(200, sample("following_page1")), FakeResponse(200, sample("following_page2"))])
    src = XApiSource(conn=conn, run_id=run_id, session=fake)
    users = src.get_following("1", max_users=1000)
    assert [u.handle for u in users] == ["wearablenerd_test", "techwriter_test", "vc_test"]
    assert fake.calls[0]["url"] == "https://api.x.com/2/users/1/following"
    assert fake.calls[0]["params"]["max_results"] == 1000 and "pagination_token" not in fake.calls[0]["params"]
    assert fake.calls[1]["params"]["pagination_token"] == "7140dibdnow9c7btw3w29grvxfmqzoxc4tj7k4wd1"
    assert fake.calls[1]["params"]["max_results"] == 998
    rows = cost_rows(conn)
    assert [(r["endpoint"], r["units"], r["unit_cost_usd"]) for r in rows] == [
        ("users/:id/following", 2, costs.X_PRICES["follow_read"]), ("users/:id/following", 1, costs.X_PRICES["follow_read"])]
    assert abs(costs.spent(conn, "x_api", run_id) - 0.03) < 1e-9
    assert {r["dst_x_id"] for r in db.rows(conn, "SELECT dst_x_id FROM x_follows WHERE src_x_id='1'")} == {"2", "3", "4"}
    # the cap stops pagination even when a next_token is present
    fake2 = FakeSession([FakeResponse(200, sample("following_page1"))])
    assert len(XApiSource(conn=conn, session=fake2).get_following("1", max_users=2)) == 2
    assert len(fake2.calls) == 1 and fake2.calls[0]["params"]["max_results"] == 2


def test_followers_direction(conn):
    fake = FakeSession([FakeResponse(200, sample("following_page2"))])
    XApiSource(conn=conn, session=fake).get_followers("1", max_users=10)
    assert fake.calls[0]["url"] == "https://api.x.com/2/users/1/followers"
    assert db.one(conn, "SELECT src_x_id, dst_x_id FROM x_follows") == {"src_x_id": "4", "dst_x_id": "1"}


def test_user_cache_serves_recent_rows_and_fetches_only_missing(conn):
    fake = FakeSession([FakeResponse(200, sample("user_by_username"))])
    src = XApiSource(conn=conn, session=fake)
    first = src.get_user_by_handle("wearablenerd_test")
    second = src.get_user_by_handle("WearableNerd_Test")          # cache hit: no second request
    assert len(fake.calls) == 1 and second.x_id == first.x_id and second.followers == 48000 and second.raw["id"] == first.x_id

    def handler(method, url, params):
        assert url == "https://api.x.com/2/users/by"
        assert params["usernames"] == "techwriter_test,vc_test,ghost_handle"   # the cached one is not re-bought
        return FakeResponse(200, sample("users_by"))

    fake.handler = handler
    users = src.get_users_by_handles(["@wearablenerd_test", "TechWriter_Test", "vc_test", "ghost_handle", "bad handle!"])
    assert [u.handle for u in users] == ["wearablenerd_test", "techwriter_test", "vc_test"]     # input order, misses dropped
    assert len(fake.calls) == 2
    assert [r["units"] for r in cost_rows(conn)] == [1, 2]                                    # only what came back is billed
    # a stale row (older than CACHE_DAYS) is re-fetched
    conn.execute("UPDATE x_users SET fetched_at='2020-01-01T00:00:00Z' WHERE handle='vc_test'")
    conn.commit()
    fake.handler = lambda m, u, p: FakeResponse(200, {"data": [x for x in sample("users_by")["data"] if x["username"] == "vc_test"]})
    assert [u.handle for u in src.get_users_by_handles(["vc_test"])] == ["vc_test"] and len(fake.calls) == 3


def test_users_by_handles_batches_of_100():
    seen = []

    def handler(method, url, params):
        names = params["usernames"].split(",")
        seen.append(len(names))
        return FakeResponse(200, {"data": [{"id": str(1000 + i), "name": n, "username": n, "public_metrics": {"followers_count": 1, "following_count": 1, "tweet_count": 1}}
                                           for i, n in enumerate(names)]})

    users = XApiSource(session=FakeSession(handler=handler)).get_users_by_handles([f"h{i}" for i in range(150)])
    assert seen == [100, 50] and len(users) == 150


def test_search_users_sanitizes_query_and_caps_results(conn):
    fake = FakeSession([FakeResponse(200, sample("users_search"))])
    src = XApiSource(conn=conn, session=fake)
    users = src.search_users("Marwan Refaat · a16z (Andreessen-Horowitz)!", max_results=10)
    assert fake.calls[0]["url"] == "https://api.x.com/2/users/search"
    assert fake.calls[0]["params"]["query"] == "Marwan Refaat a16z Andreessen Horowitz"
    assert fake.calls[0]["params"]["max_results"] == 10                     # never the endpoint default of 100
    assert [u.handle for u in users] == ["marwanrefaat", "notmarwan"]
    assert [r["units"] for r in cost_rows(conn)] == [2]
    assert db.one(conn, "SELECT COUNT(*) AS n FROM x_users")["n"] == 2
    assert x_api.sanitize_user_query("Zoë Müller-Schmidt @ Café") == "Zoe Muller Schmidt Cafe"
    assert len(x_api.sanitize_user_query("a" * 80)) == 50
    assert XApiSource(session=FakeSession()).search_users("!!!") == []       # nothing searchable: no call


def test_search_users_403_explains_user_context_auth():
    fake = FakeSession([FakeResponse(403, {"title": "Forbidden", "detail": "Your app is not authorized for this endpoint"})])
    with pytest.raises(x_api.XApiAuthError, match="user-context"):
        XApiSource(session=fake).search_users("Vera Capital")


# ---------------------------------------------------------------- rate limits, retries, errors, budget

def test_429_sleeps_until_reset_then_retries(no_sleep):
    reset = int(time.time()) + 30
    fake = FakeSession([FakeResponse(429, {"title": "Too Many Requests"}, {"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(reset)}),
                        FakeResponse(200, sample("user_by_username"))])
    u = XApiSource(session=fake).get_user_by_handle("wearablenerd_test")
    assert u.handle == "wearablenerd_test" and len(fake.calls) == 2
    assert len(no_sleep) == 1 and 29 <= no_sleep[0] <= 32


def test_429_sleep_is_capped_and_gives_up_after_retries(no_sleep):
    far = str(int(time.time()) + 100_000)
    fake = FakeSession([FakeResponse(429, {}, {"x-rate-limit-reset": far})] * 5)
    with pytest.raises(x_api.XApiRateLimited):
        XApiSource(session=fake).get_user_by_handle("wearablenerd_test")
    assert len(no_sleep) == x_api.RETRIES_429 and all(s == x_api.MAX_RATE_LIMIT_SLEEP for s in no_sleep)
    assert len(fake.calls) == x_api.RETRIES_429 + 1


def test_exhausted_window_sleeps_proactively_before_next_page(no_sleep):
    reset = int(time.time()) + 20
    page1 = FakeResponse(200, sample("following_page1"), {"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(reset)})
    fake = FakeSession([page1, FakeResponse(200, sample("following_page2"))])
    users = XApiSource(session=fake).get_following("1", max_users=1000)
    assert len(users) == 3 and len(no_sleep) == 1 and 19 <= no_sleep[0] <= 22


def test_5xx_retries_with_backoff_then_succeeds(no_sleep):
    fake = FakeSession([FakeResponse(503, sample("error_503")), FakeResponse(502, {}), FakeResponse(200, sample("user_by_username"))])
    assert XApiSource(session=fake).get_user_by_handle("wearablenerd_test").x_id == "2244994945"
    assert no_sleep == [1.0, 2.0] and len(fake.calls) == 3


def test_5xx_gives_up_after_three_retries(no_sleep):
    fake = FakeSession([FakeResponse(503, sample("error_503"))] * 6)
    with pytest.raises(x_api.XApiError, match="503"):
        XApiSource(session=fake).get_user_by_handle("wearablenerd_test")
    assert len(fake.calls) == 1 + x_api.RETRIES_5XX and no_sleep == [1.0, 2.0, 4.0]


def test_402_insufficient_credits_is_a_clear_error(conn):
    fake = FakeSession([FakeResponse(402, sample("error_402"))])
    with pytest.raises(x_api.XApiCreditsError, match="insufficient credits"):
        XApiSource(conn=conn, session=fake).get_user_by_handle("wearablenerd_test")
    assert cost_rows(conn) == []                       # failed requests are not billed and not recorded


def test_401_and_400_map_to_clear_errors():
    with pytest.raises(x_api.XApiAuthError, match="401"):
        XApiSource(session=FakeSession([FakeResponse(401, {"title": "Unauthorized"})])).get_user_by_handle("wearablenerd_test")
    with pytest.raises(x_api.XApiError, match="Invalid Request"):
        XApiSource(session=FakeSession([FakeResponse(400, {"title": "Invalid Request", "errors": [{"message": "bad query"}]})])).search_posts("x", max_results=10)


def test_budget_guard_refuses_before_any_request(conn, monkeypatch):
    monkeypatch.setenv("BUDGET_X_API_USD", "0.05")
    fake = FakeSession([FakeResponse(200, sample("following_page1"))])
    src = XApiSource(conn=conn, run_id=db.new_run(conn, "crawl"), session=fake)
    with pytest.raises(costs.BudgetExceeded):
        src.get_following("1", max_users=1000)         # projected $10 > cap
    assert fake.calls == []
    assert len(src.get_following("1", max_users=2)) == 2   # $0.02 fits
    with pytest.raises(costs.BudgetExceeded):
        src.get_following("1", max_users=4)            # spent 0.02 + 0.04 > 0.05
    assert len(fake.calls) == 1


def test_without_conn_only_prints_projection(capsys):
    fake = FakeSession([FakeResponse(200, sample("following_page2"))])
    users = XApiSource(session=fake).get_following("1", max_users=1)
    assert len(users) == 1
    assert "[cost] x_api users/:id/following: ~$0.01 for 1 units" in capsys.readouterr().out


# ---------------------------------------------------------------- estimate helper and cost table

def test_estimate_and_cost_table():
    assert x_api.estimate("users/:id/following", 1000) == 10.0
    assert x_api.estimate("following", 1000) == x_api.estimate("users/:id/following", 1000)
    assert x_api.estimate("users/by", 100) == 1.0
    assert x_api.estimate("tweets/search/recent", 100, include_expansions=False) == 0.5
    assert x_api.estimate("search_posts", 100, include_expansions=True) == 1.5
    assert x_api.estimate("users/:id/tweets", 20, include_expansions=False) == 0.1
    with pytest.raises(KeyError):
        x_api.estimate("dm_events", 1)
    table = x_api.cost_table()
    assert table and all(low <= high for _, _, low, high, _ in table)
