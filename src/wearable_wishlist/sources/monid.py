"""monid adapter: X data through monid.ai, a pay-per-use gateway in front of Apify actors and TikHub.

Why it exists: the official X API charges $0.010 per follower/following row; the Apify follower actor behind
monid charges $0.000225 (about 45x less), and tweets cost $0.0006 instead of $0.005. We use it for the bulk jobs
(seed graph crawl, topic search, evidence posts, engagement samples) and keep the official API for handle verification.

Endpoints (verified live on 2026-09-29 with discover + inspect + small probe runs, see data/cache/probe_*.json):
  follows  apify  /kaitoeasyapi/premium-x-follower-scraper-following-data  PER_RESULT $0.000225
           body: user_names[], getFollowing, getFollowers, maxFollowings, maxFollowers (both caps must be >= 200)
  tweets   apify  /apidojo/tweet-scraper                                    PER_RESULT $0.0006
           body: searchTerms[] (X web search syntax), maxItems, sort, tweetLanguage, start
           Every tweet carries a full author profile (incl. canDm), which we cache so discovery needs no extra lookups.
  profile  tikhub /api/v1/twitter/web/fetch_user_profile                    PER_CALL   $0.0015
           queryParams: screen_name | rest_id
Run protocol: POST /v1/run {provider, endpoint, input:{body|queryParams}} -> 202 + runId -> poll GET /v1/runs/:id
until COMPLETED/FAILED/BLOCKED/STOPPED/TIMED_OUT. Provider errors (providerResponse.httpStatus >= 400) are not charged.

Read-only: nothing here can follow, post or DM. Every paid run is announced on stdout, checked against the per-run
budget (costs.guard) and written to the ledger with the actual cost reported by monid (costs.record)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import time
from typing import Any

import requests

from .. import config, costs
from .base import XPost, XUser, norm_handle, persist_posts, persist_users

log = logging.getLogger("wearable_wishlist.sources.monid")

BASE_URL = "https://api.monid.ai"
CACHE_DAYS = 7
TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "STOPPED", "TIMED_OUT"}
FOLLOW_MIN_CAP = 200          # the follower actor rejects maxFollowers/maxFollowings below 200
SEARCH_WINDOW_DAYS = 365      # topic search looks back one year unless a `since` is given
SEARCH_CACHE_HOURS = 20       # the same query within this window is served from x_posts (crash-safe re-runs)

ENDPOINTS: dict[str, dict] = {
    "follows": {"provider": "apify", "endpoint": "/kaitoeasyapi/premium-x-follower-scraper-following-data",
                "price_type": "PER_RESULT", "unit_usd": 0.000225},
    "tweets": {"provider": "apify", "endpoint": "/apidojo/tweet-scraper", "price_type": "PER_RESULT", "unit_usd": 0.0006},
    "profile": {"provider": "tikhub", "endpoint": "/api/v1/twitter/web/fetch_user_profile", "price_type": "PER_CALL",
                "unit_usd": 0.0015},
}


class MonidError(RuntimeError):
    pass


class MonidBalanceError(MonidError):
    """402: the monid wallet cannot cover the run."""


class MonidBlocked(MonidError):
    """A monid control gate rejected the run before execution."""


# ----------------------------------------------------------------------------------------------- helpers

def _load_inspected_prices() -> None:
    """Refresh unit prices from the cached inspect output when present (prices can change)."""
    p = config.CACHE_DIR / "monid_inspect.json"
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for ep in ENDPOINTS.values():
        rec = data.get(f"{ep['provider']}{ep['endpoint']}")
        amount = (((rec or {}).get("price") or {}).get("amount") or {}).get("value")
        if amount:
            ep["unit_usd"] = float(amount)
            ep["price_type"] = rec["price"].get("type", ep["price_type"])


_load_inspected_prices()


def estimate(key: str, units: int) -> float:
    """Projected USD for `units` results (PER_RESULT) or calls (PER_CALL) on endpoint `key`."""
    return round(ENDPOINTS[key]["unit_usd"] * max(0, units), 6)


def _twitter_time(s: str | None) -> str | None:
    """'Fri Sep 25 06:59:59 +0000 2026' or ISO -> '2026-09-25T06:59:59Z'."""
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = dt.datetime.strptime(s.replace("Z", "+0000") if fmt != "%a %b %d %H:%M:%S %z %Y" else s, fmt)
            return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return s


def _first(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def map_user(o: dict) -> XUser | None:
    """Map a user object to XUser. Three formats reach us:
      * Apify tweet authors (camelCase: userName, isBlueVerified, followers, following=count, canDm)
      * the follower actor's rows (X v1.1: screen_name, followers_count, friends_count, legacy `verified`,
        `following` = a boolean about the scraper's own account, `type` = 'following'|'followers', `status` = last post)
      * TikHub profiles (profile, rest_id, blue_verified, sub_count, friends)
    Unknown stays None: the v1.1 `verified` flag is legacy verification only, so False there means "unknown"."""
    if not isinstance(o, dict):
        return None
    x_id = _first(o, "id_str", "rest_id", "id", "userId", "user_id")
    handle = _first(o, "userName", "screen_name", "profile", "username", "handle")
    if not x_id or not handle:
        return None
    blue = _first(o, "isBlueVerified", "blue_verified", "is_blue_verified")
    legacy = o.get("isVerified") if "isVerified" in o else (o.get("verified") if isinstance(o.get("verified"), bool) else None)
    if blue is None:
        verified = True if legacy else None
        vtype = "legacy" if legacy else None
    else:
        verified = bool(blue or legacy)
        vtype = _first(o, "verifiedType", "verification_type") or ("blue" if blue else ("legacy" if legacy else "none"))
    following = _first(o, "friends_count", "friends")
    if following is None and not isinstance(o.get("following"), bool):
        following = o.get("following")
    website = o.get("website")
    if not website:
        urls = (((o.get("entities") or {}).get("url") or {}).get("urls") or [])
        website = urls[0].get("expanded_url") if urls else None
    if not website and o.get("url") and not str(o.get("url")).startswith(("https://x.com/", "https://twitter.com/", "https://t.co/")):
        website = o.get("url")
    bio = _first(o, "description", "desc", "bio")
    for u in ((((o.get("entities") or {}).get("description") or {}).get("urls")) or []):
        if bio and u.get("url") and u.get("expanded_url"):
            bio = bio.replace(u["url"], u["expanded_url"])
    return XUser(
        x_id=str(x_id), handle=str(handle), name=o.get("name"), bio=bio, location=o.get("location") or None,
        url=website, verified=verified, verified_type=vtype, created_at=_twitter_time(_first(o, "createdAt", "created_at")),
        followers=_int(_first(o, "followers", "sub_count", "followers_count")), following=_int(following),
        posts_count=_int(_first(o, "statusesCount", "statuses_count")),
        can_dm=o.get("canDm") if isinstance(o.get("canDm"), bool) else (o.get("can_dm") if isinstance(o.get("can_dm"), bool) else None),
        profile_image=_first(o, "profilePicture", "avatar", "profile_image_url_https"), raw=o,
    )


def map_status(user: XUser, st: dict) -> XPost | None:
    """The follower actor embeds each user's latest post (X v1.1 `status`): free recency and reply signals."""
    if not isinstance(st, dict) or not (st.get("id_str") or st.get("id")):
        return None
    mentions = [m.get("screen_name") for m in ((st.get("entities") or {}).get("user_mentions") or []) if m.get("screen_name")]
    return XPost(post_id=str(st.get("id_str") or st.get("id")), x_id=user.x_id, handle=user.handle,
                 text=st.get("full_text") or st.get("text") or "", created_at=_twitter_time(st.get("created_at")),
                 likes=_int(st.get("favorite_count")) or 0, reposts=_int(st.get("retweet_count")) or 0,
                 in_reply_to_x_id=st.get("in_reply_to_user_id_str") or None,
                 mentions=[norm_handle(m) for m in mentions], query="latest_status", raw=None)


def map_post(t: dict, query: str | None = None) -> XPost | None:
    if not isinstance(t, dict) or not t.get("id"):
        return None
    author = t.get("author") or {}
    mentions = [m.get("screen_name") or m.get("username") for m in ((t.get("entities") or {}).get("user_mentions") or [])]
    return XPost(
        post_id=str(t["id"]), x_id=str(author.get("id") or t.get("authorId") or ""),
        handle=author.get("userName"), text=t.get("fullText") or t.get("text") or "",
        created_at=_twitter_time(t.get("createdAt")),
        likes=_int(t.get("likeCount")) or 0, reposts=_int(t.get("retweetCount")) or 0,
        replies=_int(t.get("replyCount")) or 0, quotes=_int(t.get("quoteCount")) or 0, views=_int(t.get("viewCount")),
        url=t.get("url"), in_reply_to_x_id=str(t["inReplyToUserId"]) if t.get("inReplyToUserId") else None,
        mentions=[norm_handle(m) for m in mentions if m], query=query, raw=t,
    )


_V2_TO_WEB = [  # X API v2 operators -> X web/advanced search operators used by the scraper
    (r"-is:retweet\b", "-filter:retweets"), (r"\bis:retweet\b", "filter:retweets"),
    (r"-is:reply\b", "-filter:replies"), (r"\bis:reply\b", "filter:replies"),
    (r"-is:quote\b", "-filter:quote"), (r"\bis:quote\b", "filter:quote"),
    (r"\bis:verified\b", "filter:blue_verified"), (r"\bhas:links\b", "filter:links"), (r"\bhas:media\b", "filter:media"),
]


def to_web_query(query: str) -> str:
    q = query
    for pat, rep in _V2_TO_WEB:
        q = re.sub(pat, rep, q)
    return re.sub(r"\s+", " ", q).strip()


def _latest_body(handle: str, max_results: int) -> dict:
    """The tweets actor body for a person's latest original posts: newest first, no reposts, no replies, no date window."""
    return {"searchTerms": [f"from:{handle} -filter:retweets -filter:replies"], "maxItems": int(max_results), "sort": "Latest"}


# ----------------------------------------------------------------------------------------------- source

class MonidSource:
    """Source protocol implementation over monid. `conn` enables the budget guard, the ledger and the local cache."""
    name = "monid"

    def __init__(self, conn: sqlite3.Connection | None = None, run_id: int | None = None, session: Any = None,
                 api_key: str | None = None, endpoints: dict | None = None, poll_interval: float = 4.0,
                 max_wait: float = 900.0):
        self.conn, self.run_id = conn, run_id
        self.session = session or requests.Session()
        self.api_key = api_key or config.monid_api_key()
        self.endpoints = {k: dict(v) for k, v in ENDPOINTS.items()}
        for k, v in (endpoints or {}).items():
            self.endpoints.setdefault(k, {}).update(v)
        self.poll_interval, self.max_wait = poll_interval, max_wait
        self._latest: dict[tuple[str, int], list[XPost]] = {}   # prefetch_latest_originals results, served once

    # ---- users -------------------------------------------------------------------------------------------
    def get_user_by_handle(self, handle: str) -> XUser | None:
        h = norm_handle(handle)
        cached = self._cached_users([h])
        if h in cached:
            return cached[h]
        out = self._run("profile", {"queryParams": {"screen_name": h}}, units=1, note=f"@{h}")
        user = map_user(out) if isinstance(out, dict) and out.get("status", "active") != "notfound" else None
        if user:
            self._persist([user])
        return user

    def get_users_by_handles(self, handles: list[str]) -> list[XUser]:
        wanted = [norm_handle(h) for h in handles if h]
        cached = self._cached_users(wanted)
        missing = [h for h in dict.fromkeys(wanted) if h not in cached]
        if missing:
            print(f"[cost] monid profile x{len(missing)}: ~{costs.fmt(estimate('profile', len(missing)))} "
                  f"({len(cached)} served from cache)")
        found = dict(cached)
        for h in missing:
            try:
                u = self.get_user_by_handle(h)
            except MonidError as e:
                log.warning("profile @%s failed: %s", h, e)
                continue
            if u:
                found[u.handle] = u
        return [found[h] for h in wanted if h in found]

    def search_users(self, query: str, max_results: int = 10) -> list[XUser]:
        """No people-search endpoint is pinned on monid: handle resolution matches names against the seed's
        follow lists instead (free, and a real relationship). Returns [] so callers fall through cleanly."""
        log.info("monid has no user search pinned; skipping search_users(%r)", query)
        return []

    def get_following(self, handle_or_id: str, max_users: int = 1000) -> list[XUser]:
        return self._follow_list(handle_or_id, "following", max_users)

    def get_followers(self, handle_or_id: str, max_users: int = 1000) -> list[XUser]:
        return self._follow_list(handle_or_id, "followers", max_users)

    def _follow_list(self, handle_or_id: str, direction: str, max_users: int) -> list[XUser]:
        handle = self._to_handle(handle_or_id)
        cached = self._cached_follow_list(handle, direction, max_users)
        if cached is not None:
            print(f"[cost] monid follows: $0 ({direction} of @{handle} served from the local crawl of the last {CACHE_DAYS} days)")
            return cached
        cap = max(FOLLOW_MIN_CAP, int(max_users))
        body = {"user_names": [handle],
                "getFollowing": direction == "following", "maxFollowings": cap if direction == "following" else FOLLOW_MIN_CAP,
                "getFollowers": direction == "followers", "maxFollowers": cap if direction == "followers" else FOLLOW_MIN_CAP}
        out = self._run("follows", {"body": body}, units=cap, note=f"{direction} of @{handle}")
        items = out if isinstance(out, list) else (out or {}).get("items", []) if isinstance(out, dict) else []
        users, statuses, skipped = [], [], 0
        for it in items:
            kind = str(it.get("type") or it.get("follow_type") or "").lower() if isinstance(it, dict) else ""
            if kind in ("follower", "followers") and direction == "following" or kind in ("following", "followings") and direction == "followers":
                skipped += 1
                continue
            u = map_user(it)
            if u and u.handle != handle:
                users.append(u)
                st = map_status(u, it.get("status")) if isinstance(it, dict) else None
                if st:
                    statuses.append(st)
        if skipped:
            log.info("dropped %d rows of the other direction", skipped)
        uniq = list({u.x_id: u for u in users}.values())[:max_users]
        log.info("monid %s @%s: %d users", direction, handle, len(uniq))
        if self.conn is not None and statuses:
            persist_users(self.conn, uniq, self.name)   # users first: posts reference them
            persist_posts(self.conn, statuses, self.name)
        self._remember_follow_crawl(handle, direction, len(uniq), max_users)
        return uniq

    # A full list crawl is remembered in data/cache/monid_follow_crawls.json; re-runs within CACHE_DAYS read the
    # persisted x_follows rows instead of paying again (set FORCE_REFRESH=1 to bypass).
    def _crawl_log_path(self):
        return config.CACHE_DIR / "monid_follow_crawls.json"

    def _remember_follow_crawl(self, handle: str, direction: str, n: int, max_users: int) -> None:
        if self.conn is None:
            return
        p = self._crawl_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(p.read_text()) if p.exists() else {}
        data[f"{handle}:{direction}"] = {"fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "count": n, "max_users": max_users}
        p.write_text(json.dumps(data, indent=1))

    def _cached_follow_list(self, handle: str, direction: str, max_users: int) -> list[XUser] | None:
        if self.conn is None or config.env("FORCE_REFRESH") == "1":
            return None
        p = self._crawl_log_path()
        rec = (json.loads(p.read_text()) if p.exists() else {}).get(f"{handle}:{direction}")
        if not rec or rec.get("max_users", 0) < max_users and rec.get("count", 0) >= rec.get("max_users", 0):
            return None
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if rec["fetched_at"] < cutoff:
            return None
        row = self.conn.execute("SELECT x_id FROM x_users WHERE handle=?", (handle,)).fetchone()
        if not row:
            return None
        if direction == "following":
            q = "SELECT u.handle FROM x_follows f JOIN x_users u ON u.x_id=f.dst_x_id WHERE f.src_x_id=?"
        else:
            q = "SELECT u.handle FROM x_follows f JOIN x_users u ON u.x_id=f.src_x_id WHERE f.dst_x_id=?"
        handles = [r[0] for r in self.conn.execute(q, (row[0],)).fetchall()]
        users = self._cached_users(handles)
        return list(users.values())[:max_users] if users else None

    # ---- posts -------------------------------------------------------------------------------------------
    def search_posts(self, query: str, max_results: int = 100, since: str | None = None) -> list[XPost]:
        cached = self._cached_search(query, max_results)
        if cached is not None:
            print(f"[cost] monid tweets: $0 ({len(cached)} posts for this query already bought in the last {SEARCH_CACHE_HOURS}h)")
            return cached
        body = self._search_body(query, max_results, since)
        out = self._run("tweets", {"body": body}, units=int(max_results), note=f"search {body['searchTerms'][0][:60]}")
        posts = self._posts_from(out, query=query, drop_retweets="-filter:retweets" in body["searchTerms"][0])
        self._log_search(query, posts)
        return posts

    def _search_body(self, query: str, max_results: int, since: str | None) -> dict:
        web_q = to_web_query(query)
        # Engagement-ranked ("Top") by default: popular posts come from people with an audience, which is who the
        # wishlist needs. Bounded to the last SEARCH_WINDOW_DAYS so "discussing" means recently.
        body: dict[str, Any] = {"searchTerms": [web_q], "maxItems": int(max_results), "sort": config.env("MONID_SEARCH_SORT", "Top")}
        if "lang:en" in web_q:
            body["tweetLanguage"] = "en"
        body["start"] = since[:10] if since else (dt.date.today() - dt.timedelta(days=SEARCH_WINDOW_DAYS)).isoformat()
        return body

    def prefetch_searches(self, queries: list[tuple[str, int]], workers: int = 8) -> dict:
        """Run many searches concurrently (HTTP only in threads) and store the results, so the caller's own
        sequential search_posts calls are served from the 20-hour cache. Same cost as running them one by one,
        a fraction of the wall time. Failed searches are left for the caller to retry."""
        from concurrent.futures import ThreadPoolExecutor
        todo = [(q, int(n)) for q, n in dict.fromkeys((q, int(n)) for q, n in queries) if self._cached_search(q, n) is None]
        stats = {"requested": len(queries), "to_fetch": len(todo), "fetched": 0, "posts": 0, "failed": 0}
        if not todo:
            return stats
        projected = sum(estimate("tweets", n) for _, n in todo)
        ep = self.endpoints["tweets"]
        print(f"[cost] monid prefetch {len(todo)} searches x up to {max(n for _, n in todo)} posts: ~{costs.fmt(projected)} "
              f"({workers} at a time)")
        if self.conn is not None:
            costs.guard(self.conn, "monid", projected, self.run_id)

        def one(item):
            q, n = item
            body = self._search_body(q, n, None)
            try:
                d = self._start_and_wait({"provider": ep["provider"], "endpoint": ep["endpoint"], "input": {"body": body}},
                                         session=requests.Session() if isinstance(self.session, requests.Session) else self.session)
                return q, n, body, d, None
            except Exception as e:  # noqa: BLE001 - reported below, retried by the caller
                return q, n, body, None, e

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            results = list(ex.map(one, todo))
        for q, n, body, d, err in results:          # ledger, mapping and SQLite writes on the calling thread
            if err is not None or d is None:
                stats["failed"] += 1
                log.warning("prefetch failed for %r: %s", q[:80], err)
                continue
            self._record("tweets", d, note=f"prefetch {q[:60]}")
            http = int((d.get("providerResponse") or {}).get("httpStatus") or 200)
            if d.get("status") != "COMPLETED" or (http >= 400 and http != 404):
                stats["failed"] += 1
                continue
            posts = self._posts_from(d.get("output") if http != 404 else [], query=q,
                                     drop_retweets="-filter:retweets" in body["searchTerms"][0])
            if self.conn is not None and posts:
                persist_posts(self.conn, posts, self.name)
            self._log_search(q, posts)
            stats["fetched"] += 1
            stats["posts"] += len(posts)
        log.info("monid prefetch: %s", stats)
        return stats

    def get_user_posts(self, handle_or_id: str, max_results: int = 100) -> list[XPost]:
        handle = self._to_handle(handle_or_id)
        body = {"searchTerms": [f"from:{handle} -filter:retweets"], "maxItems": int(max_results), "sort": "Latest"}
        out = self._run("tweets", {"body": body}, units=int(max_results), note=f"posts of @{handle}")
        return [p for p in self._posts_from(out, query=None, drop_retweets=True) if p.handle == handle]

    def get_latest_originals(self, handle: str, max_results: int) -> list[XPost]:
        """The person's latest original posts, newest first (sort Latest, no reposts, no replies): the engagement sample
        (sample.py). enrich's searches are engagement-ranked, so their average is the person's best post, not an average
        one. Authors are cached as in every search (fresh follower counts). Served from memory, free, when
        prefetch_latest_originals already bought it."""
        handle = self._to_handle(handle)
        got = self._latest.pop((handle, int(max_results)), None)
        if got is not None:
            print(f"[cost] monid tweets: $0 (latest originals of @{handle} already bought by the prefetch)")
        else:
            out = self._run("tweets", {"body": _latest_body(handle, max_results)}, units=int(max_results),
                            note=f"latest originals of @{handle}")
            got = self._posts_from(out, query=None, drop_retweets=True)
        # the search already excludes reposts and replies; a stray one would skew the average
        return [p for p in got if p.handle == handle and not p.in_reply_to_x_id and not (p.raw or {}).get("isReply")
                and not p.text.startswith("RT @")]

    def prefetch_latest_originals(self, handles: list[str], max_results: int, workers: int = 8) -> dict:
        """get_latest_originals for many people concurrently (HTTP only in threads, as prefetch_searches); the caller's
        own get_latest_originals calls are then served from memory. Same cost as one by one; a run takes ~15 s, so 200
        people take minutes instead of an hour. Failed runs are left for the caller to retry."""
        from concurrent.futures import ThreadPoolExecutor
        n = int(max_results)
        todo = [h for h in dict.fromkeys(norm_handle(h) for h in handles if h) if (h, n) not in self._latest]
        stats = {"requested": len(handles), "to_fetch": len(todo), "fetched": 0, "posts": 0, "failed": 0}
        if not todo:
            return stats
        projected = estimate("tweets", n * len(todo))
        ep = self.endpoints["tweets"]
        print(f"[cost] monid prefetch latest originals of {len(todo)} people x up to {n} posts: ~{costs.fmt(projected)} "
              f"({workers} at a time)")
        if self.conn is not None:
            costs.guard(self.conn, "monid", projected, self.run_id)

        def one(h):
            try:
                d = self._start_and_wait({"provider": ep["provider"], "endpoint": ep["endpoint"],
                                          "input": {"body": _latest_body(h, n)}},
                                         session=requests.Session() if isinstance(self.session, requests.Session) else self.session)
                return h, d, None
            except Exception as e:  # noqa: BLE001 - reported below, retried by the caller
                return h, None, e

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            results = list(ex.map(one, todo))
        for h, d, err in results:          # ledger, mapping and SQLite writes on the calling thread
            if err is not None or d is None:
                stats["failed"] += 1
                log.warning("prefetch of @%s's latest originals failed: %s", h, err)
                continue
            self._record("tweets", d, note=f"prefetch latest originals of @{h}")
            http = int((d.get("providerResponse") or {}).get("httpStatus") or 200)
            if d.get("status") != "COMPLETED" or (http >= 400 and http != 404):
                stats["failed"] += 1
                continue
            posts = self._posts_from(d.get("output") if http != 404 else [], query=None, drop_retweets=True)
            self._latest[(h, n)] = posts
            stats["fetched"] += 1
            stats["posts"] += len(posts)
        log.info("monid prefetch latest originals: %s", stats)
        return stats

    def _posts_from(self, out: Any, query: str | None, drop_retweets: bool) -> list[XPost]:
        items = out if isinstance(out, list) else []
        posts, authors = [], {}
        for it in items:
            if not isinstance(it, dict) or (it.get("type") not in (None, "tweet")):
                continue
            if drop_retweets and it.get("isRetweet"):
                continue
            p = map_post(it, query=query)
            if not p or not p.x_id:
                continue
            posts.append(p)
            a = map_user(it.get("author") or {})
            if a:
                authors[a.x_id] = a
        # authors arrive with every tweet: caching them makes discovery's author lookups free
        self._persist(list(authors.values()))
        return list({p.post_id: p for p in posts}.values())

    # Every completed search is logged with the ids of the posts it returned (empty results too), so re-runs within
    # SEARCH_CACHE_HOURS cost nothing and do not depend on which query a post was first stored under.
    def _search_log_path(self):
        return config.CACHE_DIR / "monid_search_log.json"

    def _read_search_log(self) -> dict:
        p = self._search_log_path()
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _log_search(self, query: str, posts: list[XPost]) -> None:
        if self.conn is None:
            return
        data = self._read_search_log()
        data[query] = {"fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "post_ids": [p.post_id for p in posts]}
        p = self._search_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))

    def _cached_search(self, query: str, max_results: int) -> list[XPost] | None:
        if self.conn is None or config.env("FORCE_REFRESH") == "1":
            return None
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=SEARCH_CACHE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = self._read_search_log().get(query)
        if rec and rec.get("fetched_at", "") >= cutoff:
            ids = rec.get("post_ids") or []
            rows = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                rows += self.conn.execute(f"SELECT * FROM x_posts WHERE post_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        else:  # searches bought before the log existed
            rows = self.conn.execute("SELECT * FROM x_posts WHERE query=? AND source='monid' AND fetched_at>=? LIMIT ?",
                                     (query, cutoff, int(max_results))).fetchall()
            if not rows:
                return None
        out = []
        for r in rows:
            r = dict(r)
            out.append(XPost(post_id=r["post_id"], x_id=r["x_id"], handle=r["handle"], text=r["text"] or "",
                             created_at=r["created_at"], likes=r["likes"] or 0, reposts=r["reposts"] or 0,
                             replies=r["replies"] or 0, quotes=r["quotes"] or 0, views=r["views"], url=r["url"],
                             in_reply_to_x_id=r["in_reply_to_x_id"], mentions=json.loads(r["mentions"] or "[]"),
                             query=query))
        return out[:int(max_results)]

    # ---- plumbing ----------------------------------------------------------------------------------------
    def _to_handle(self, handle_or_id: str) -> str:
        s = str(handle_or_id).strip()
        if s.isdigit():
            if self.conn is not None:
                r = self.conn.execute("SELECT handle FROM x_users WHERE x_id=?", (s,)).fetchone()
                if r:
                    return r[0]
            raise MonidError(f"cannot map x_id {s} to a handle (fetch the profile first)")
        return norm_handle(s)

    def _cached_users(self, handles: list[str]) -> dict[str, XUser]:
        if self.conn is None or not handles:
            return {}
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, XUser] = {}
        for i in range(0, len(handles), 500):
            chunk = handles[i:i + 500]
            q = f"SELECT * FROM x_users WHERE handle IN ({','.join('?' * len(chunk))}) AND fetched_at >= ?"
            for r in self.conn.execute(q, (*chunk, cutoff)).fetchall():
                r = dict(r)
                out[r["handle"]] = XUser(
                    x_id=r["x_id"], handle=r["handle"], name=r["name"], bio=r["bio"], location=r["location"], url=r["url"],
                    verified=None if r["verified"] is None else bool(r["verified"]), verified_type=r["verified_type"],
                    created_at=r["created_at"], followers=r["followers"], following=r["following"],
                    posts_count=r["posts_count"], can_dm=None if r["can_dm"] is None else bool(r["can_dm"]),
                    profile_image=r["profile_image"])
        return out

    def _persist(self, users: list[XUser]) -> None:
        if self.conn is not None and users:
            persist_users(self.conn, users, self.name)

    def _headers(self) -> dict:
        if not self.api_key:
            raise MonidError("MONID_API_KEY missing in .env")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _run(self, key: str, inp: dict, units: int, note: str = "") -> Any:
        ep = self.endpoints[key]
        projected = estimate(key, units)
        print(f"[cost] monid {key} {ep['provider']}{ep['endpoint']}: ~{costs.fmt(projected)} for up to {units} "
              f"{'results' if ep['price_type'] == 'PER_RESULT' else 'calls'} ({note})")
        if self.conn is not None:
            costs.guard(self.conn, "monid", projected, self.run_id)
        body = {"provider": ep["provider"], "endpoint": ep["endpoint"], "input": inp}
        for attempt in range(2):  # one retry for provider-side 429/5xx (not charged) and empty-but-ok flukes
            d = self._start_and_wait(body)
            http = int((d.get("providerResponse") or {}).get("httpStatus") or 200)
            self._record(key, d, note)
            status = d.get("status")
            if status == "BLOCKED":
                raise MonidBlocked(f"monid blocked the run: {d.get('reason')} {d.get('controls')}")
            if status != "COMPLETED":
                raise MonidError(f"monid run {d.get('runId')} ended {status}")
            if http == 404:
                return None
            if http in (429, 500, 502, 503) and attempt == 0:
                log.warning("provider HTTP %s on %s, retrying once", http, key)
                time.sleep(3)
                continue
            if http >= 400:
                err = (d.get("providerResponse") or {}).get("error")
                raise MonidError(f"provider HTTP {http} on {key}: {err}")
            return d.get("output")
        return None

    def _start_and_wait(self, body: dict, session: Any = None) -> dict:
        sess = session or self.session
        t0 = time.time()
        r = sess.post(f"{BASE_URL}/v1/run", headers=self._headers(), json=body, timeout=60)
        if r.status_code == 402:
            raise MonidBalanceError("monid wallet balance too low for this run (top up at https://app.monid.ai/wallet)")
        if r.status_code in (401, 403):
            raise MonidError(f"monid auth failed (HTTP {r.status_code}): check MONID_API_KEY")
        try:
            d = r.json()
        except ValueError as e:
            raise MonidError(f"monid returned non-JSON (HTTP {r.status_code})") from e
        if r.status_code not in (200, 202) and not d.get("status"):
            raise MonidError(f"monid run failed HTTP {r.status_code}: {d}")
        run_id = d.get("runId")
        while d.get("status") not in TERMINAL:
            if time.time() - t0 > self.max_wait:
                try:
                    sess.post(f"{BASE_URL}/v1/runs/{run_id}/stop", headers=self._headers(), timeout=30)
                finally:
                    raise MonidError(f"monid run {run_id} exceeded {self.max_wait:.0f}s; stop requested")
            time.sleep(self.poll_interval)
            g = sess.get(f"{BASE_URL}/v1/runs/{run_id}", headers=self._headers(), timeout=60)
            if g.status_code >= 500:
                continue
            d = g.json()
        log.debug("monid run %s %s in %.1fs", run_id, d.get("status"), time.time() - t0)
        return d

    def _record(self, key: str, d: dict, note: str) -> None:
        if self.conn is None:
            return
        ep = self.endpoints[key]
        units = int(d.get("billedUnits") or 0)
        c = d.get("cost") or (d.get("billing") or {}).get("actualCost") or (d.get("billing") or {}).get("calculatedCost")
        actual = None
        if isinstance(c, dict) and c.get("value") is not None:
            actual = c["value"] / 1e6 if c.get("unit") == "MICRO_DOLLAR" else float(c["value"])
        unit_cost = (actual / units) if (actual is not None and units) else (0.0 if actual == 0 else ep["unit_usd"])
        costs.record(self.conn, "monid", f"{ep['provider']}{ep['endpoint']}", units, unit_cost, self.run_id,
                     note=f"run {d.get('runId')} {note} actual={actual}")


def remap_cached(conn: sqlite3.Connection) -> dict:
    """Re-map every stored monid user from its raw JSON (after a mapper fix) and store embedded latest posts. Free."""
    n_users = n_posts = 0
    rows = conn.execute("SELECT raw_json FROM x_users WHERE source='monid' AND raw_json IS NOT NULL").fetchall()
    users, posts = [], []
    for (raw,) in rows:
        try:
            o = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        u = map_user(o)
        if not u:
            continue
        users.append(u)
        st = map_status(u, o.get("status"))
        if st:
            posts.append(st)
    if users:
        n_users = persist_users(conn, users, "monid")
    if posts:
        n_posts = persist_posts(conn, posts, "monid")
    return {"users_remapped": n_users, "latest_posts": n_posts}


def cost_table() -> list[tuple[str, int, float]]:
    return [("seed following (1,210)", 1210, estimate("follows", 1210)),
            ("seed followers (6,852)", 6852, estimate("follows", 6852)),
            ("topic search, 20 queries x 100 posts", 2000, estimate("tweets", 2000)),
            ("evidence posts, 60 people x 40", 2400, estimate("tweets", 2400)),
            ("profile lookups x 100", 100, estimate("profile", 100))]


if __name__ == "__main__":
    print("monid endpoints:")
    for k, v in ENDPOINTS.items():
        print(f"  {k:8s} {v['provider']}{v['endpoint']}  {v['price_type']} ${v['unit_usd']}")
    print("\ntypical batches:")
    for label, n, usd in cost_table():
        print(f"  {label:42s} {n:6d}  {costs.fmt(usd)}")
