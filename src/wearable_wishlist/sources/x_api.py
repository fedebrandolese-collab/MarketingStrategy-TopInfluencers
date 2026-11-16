"""Official X API v2 adapter: app-only Bearer token, read-only by construction (GET only, no write scopes).

Implements `sources.base.Source`. Endpoints, limits and prices verified against docs.x.com (OpenAPI v2.168,
fundamentals/rate-limits, getting-started/pricing) on 2026-09-29:

  GET /2/users/by/username/{username}         user lookup                       $0.010 / user   300 req / 15 min
  GET /2/users/by?usernames=a,b   (<= 100)    batch user lookup                 $0.010 / user   300 / 15 min
  GET /2/users/search?query=      (<= 50 ch)  user search, max_results 1..1000  $0.010 / user   300 / 15 min
                                              (documented with user-context auth only; see search_users)
  GET /2/users/{id}/following                 max_results 1..1000, paginate     $0.010 / user   300 / 15 min
  GET /2/users/{id}/followers                 max_results 1..1000, paginate     $0.010 / user   300 / 15 min
  GET /2/tweets/search/recent                 max_results 10..100, last 7 days  $0.005 / post   450 / 15 min
  GET /2/tweets/search/all                    max_results 10..500, archive      $0.005 / post   300 / 15 min, 1 / sec
  GET /2/users/{id}/tweets                    max_results 5..100                $0.005 / post   10,000 / 15 min

Billing (pay-per-usage): charged per resource returned, deduplicated per UTC day; failed requests are not billed.
The docs do not say whether objects returned through `expansions=author_id` are billed as user reads. Being wrong
in the cheap direction is worse than being wrong in the expensive one, so BILL_EXPANSIONS=True counts them
(projection: one author profile per post as an upper bound; ledger: the distinct user objects actually returned).
Flip it to False once the Developer Console shows a paid run was billed for posts only.

The wire parameter is `tweet.fields` (the OpenAPI spec lists it as `post.fields`; the quickstarts use `tweet.fields`)
and the wire response still uses `referenced_tweets` / `retweet_count` / `tweet_count`; the mappers accept both spellings.

Every paid call: print `[cost] x_api <endpoint>: ~$X for N units` -> costs.guard -> request -> costs.record.
Rate limits: `x-rate-limit-remaining` / `x-rate-limit-reset` are honoured (sleep until reset, capped), 429 sleeps and
retries, 5xx retries with backoff, 402 raises XApiCreditsError. With `conn` given, user lookups are served from
`x_users` when fetched within CACHE_DAYS, and everything fetched is persisted with source="x_api".
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sqlite3
import time
import unicodedata
from typing import Any

import requests

from .. import config, costs, db
from .base import XPost, XUser, norm_handle, persist_follows, persist_posts, persist_users

log = logging.getLogger("wearable_wishlist.sources.x_api")

BASE_URL = "https://api.x.com/2"
USER_FIELDS = "description,location,url,verified,verified_type,created_at,public_metrics,profile_image_url,entities,protected"
TWEET_FIELDS = "created_at,public_metrics,entities,referenced_tweets,author_id,in_reply_to_user_id"
POST_EXPANSIONS = "author_id"

CACHE_DAYS = 7                       # x_users rows younger than this are served without a paid call
RECENT_WINDOW = dt.timedelta(days=7)  # search/recent covers the last 7 days; older `since` -> search/all
MAX_RATE_LIMIT_SLEEP = 15 * 60 + 10   # never sleep longer than one window (+ margin)
DEFAULT_429_SLEEP = 60                # when the reset header is missing
RETRIES_5XX = 3
BACKOFF_5XX = (1.0, 2.0, 4.0)
RETRIES_429 = 3
TIMEOUT = 30
BILL_EXPANSIONS = True                # count expanded author profiles as user reads (see module docstring)
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")            # X username rule (docs: users/by usernames pattern)
USER_SEARCH_RE = re.compile(r"[^A-Za-z0-9_' ]+")           # docs: query pattern ^[A-Za-z0-9_' ]{1,50}$
USER_SEARCH_MAX_LEN = 50

# endpoint key -> (price key in costs.X_PRICES, max results per page, min results per page, pagination param)
ENDPOINTS: dict[str, tuple[str, int, int, str]] = {
    "users/by/username": ("user_read", 1, 1, ""),
    "users/by": ("user_read", 100, 1, ""),
    "users/search": ("user_read", 1000, 1, "next_token"),
    "users/:id/following": ("follow_read", 1000, 1, "pagination_token"),
    "users/:id/followers": ("follow_read", 1000, 1, "pagination_token"),
    "tweets/search/recent": ("post_read", 100, 10, "next_token"),
    "tweets/search/all": ("post_read", 500, 10, "next_token"),
    "users/:id/tweets": ("post_read", 100, 5, "pagination_token"),
}
ALIASES = {
    "user_lookup": "users/by/username", "users_lookup": "users/by", "user_search": "users/search",
    "search_users": "users/search", "following": "users/:id/following", "followers": "users/:id/followers",
    "search_posts": "tweets/search/recent", "search_recent": "tweets/search/recent", "search_all": "tweets/search/all",
    "user_posts": "users/:id/tweets", "user_tweets": "users/:id/tweets",
}


class XApiError(RuntimeError):
    """Any non-retryable X API failure (4xx other than 429, or retries exhausted)."""


class XApiAuthError(XApiError):
    """401/403: bad token, or the endpoint needs user-context auth / a higher access level."""


class XApiCreditsError(XApiError):
    """402: the pay-per-usage credit balance is empty."""


class XApiRateLimited(XApiError):
    """429 persisted after RETRIES_429 sleeps."""


# ---------------------------------------------------------------- cost helpers (no network)

def _endpoint_key(endpoint: str) -> str:
    key = ALIASES.get(endpoint, endpoint)
    if key not in ENDPOINTS:
        raise KeyError(f"unknown x_api endpoint {endpoint!r}; known: {sorted(ENDPOINTS)}")
    return key


def unit_cost(endpoint: str) -> float:
    return costs.X_PRICES[ENDPOINTS[_endpoint_key(endpoint)][0]]


def estimate(endpoint: str, units: int, include_expansions: bool | None = None) -> float:
    """Projected USD for `units` resources from `endpoint` (an ENDPOINTS key or an alias like 'following').
    For post endpoints, adds one author profile per post as an upper bound when BILL_EXPANSIONS (or the override)."""
    key = _endpoint_key(endpoint)
    price_key = ENDPOINTS[key][0]
    usd = units * costs.X_PRICES[price_key]
    bill_exp = BILL_EXPANSIONS if include_expansions is None else include_expansions
    if price_key == "post_read" and bill_exp:
        usd += units * costs.X_PRICES["user_read"]
    return round(usd, 6)


def _iso(ts: str | None) -> str | None:
    """'2015-06-01T00:00:00.000Z' -> '2015-06-01T00:00:00Z' (the db convention)."""
    if not ts:
        return None
    return re.sub(r"\.\d+Z$", "Z", ts)


def _parse_since(since: str | None) -> dt.datetime | None:
    """Accepts 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM:SSZ' or an ISO string with an offset. Returns aware UTC or None."""
    if not since:
        return None
    s = since.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _rfc3339(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_user_query(query: str) -> str:
    """Fold to the characters /2/users/search accepts (letters, digits, _, ', space), max 50 chars."""
    folded = unicodedata.normalize("NFKD", query or "").encode("ascii", "ignore").decode("ascii")
    folded = USER_SEARCH_RE.sub(" ", folded)
    folded = re.sub(r"\s+", " ", folded).strip()
    if len(folded) > USER_SEARCH_MAX_LEN:
        folded = folded[:USER_SEARCH_MAX_LEN].rsplit(" ", 1)[0] if " " in folded[:USER_SEARCH_MAX_LEN] else folded[:USER_SEARCH_MAX_LEN]
    return folded.strip()


# ---------------------------------------------------------------- mapping (v2 JSON -> XUser / XPost)

def map_user(u: dict) -> XUser:
    pm = u.get("public_metrics") or {}
    url = u.get("url") or None
    for item in ((u.get("entities") or {}).get("url") or {}).get("urls") or []:   # replace the t.co link
        if item.get("expanded_url"):
            url = item["expanded_url"]
            break
    return XUser(
        x_id=u["id"], handle=u["username"], name=u.get("name"), bio=u.get("description") or None,
        location=u.get("location") or None, url=url, verified=u.get("verified"), verified_type=u.get("verified_type"),
        created_at=_iso(u.get("created_at")), followers=pm.get("followers_count"), following=pm.get("following_count"),
        posts_count=pm.get("tweet_count", pm.get("post_count")), can_dm=None,   # can_dm is not exposed to app-only auth
        profile_image=u.get("profile_image_url"), raw=u,
    )


def map_post(t: dict, users_by_id: dict[str, dict], query: str | None = None, fallback_handle: str | None = None) -> XPost:
    pm = t.get("public_metrics") or {}
    author_id = str(t.get("author_id") or "")
    author = users_by_id.get(author_id) or {}
    handle = author.get("username") or fallback_handle
    mentions: list[str] = []
    for m in ((t.get("entities") or {}).get("mentions") or []):
        h = norm_handle(m.get("username"))
        if h and h not in mentions:
            mentions.append(h)
    raw = dict(t)
    if handle:
        raw["author_username"] = norm_handle(handle)
    return XPost(
        post_id=t["id"], x_id=author_id, text=t.get("text") or "", handle=handle, created_at=_iso(t.get("created_at")),
        likes=pm.get("like_count") or 0, reposts=pm.get("retweet_count", pm.get("repost_count")) or 0,
        replies=pm.get("reply_count") or 0, quotes=pm.get("quote_count") or 0, views=pm.get("impression_count"),
        in_reply_to_x_id=t.get("in_reply_to_user_id"), mentions=mentions, query=query, raw=raw,
    )


def _row_to_user(r: dict) -> XUser:
    return XUser(
        x_id=r["x_id"], handle=r["handle"], name=r.get("name"), bio=r.get("bio"), location=r.get("location"), url=r.get("url"),
        verified=None if r.get("verified") is None else bool(r["verified"]), verified_type=r.get("verified_type"),
        created_at=r.get("created_at"), followers=r.get("followers"), following=r.get("following"), posts_count=r.get("posts_count"),
        can_dm=None if r.get("can_dm") is None else bool(r["can_dm"]), profile_image=r.get("profile_image"),
        raw=db.jloads(r.get("raw_json")),
    )


def _error_detail(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return (getattr(resp, "text", "") or "")[:300]
    if isinstance(body, dict):
        parts = [str(body.get(k)) for k in ("title", "detail") if body.get(k)]
        for e in body.get("errors") or []:
            if isinstance(e, dict):
                parts.append(str(e.get("detail") or e.get("message") or e))
        return " | ".join(parts)[:500]
    return str(body)[:300]


# ---------------------------------------------------------------- the adapter

class XApiSource:
    """`conn` enables costs.guard/record and the x_users cache; without it only projected costs are printed.
    `session` is injectable for tests (anything with .request(method, url, params=, headers=, timeout=))."""
    name = "x_api"

    def __init__(self, conn: sqlite3.Connection | None = None, run_id: int | None = None, session: Any = None):
        self.conn = conn
        self.run_id = run_id
        self._session = session or requests.Session()
        self._session_injected = session is not None
        self._token = config.x_bearer_token()
        # Optional user-context token (OAuth 2.0 user token, read scopes only) used ONLY for /2/users/search,
        # which the docs list with user-context auth only. Not required for anything else.
        self._user_token = config.env("X_USER_ACCESS_TOKEN")
        self._rate: dict[str, tuple[int, float]] = {}   # endpoint key -> (remaining, reset epoch)
        self._last_all_search = 0.0
        if not self._token:
            log.warning("X_BEARER_TOKEN missing: XApiSource will raise on first use")

    # ---- Source protocol ----

    def get_user_by_handle(self, handle: str) -> XUser | None:
        h = norm_handle(handle)
        if not h or not HANDLE_RE.match(h):
            log.warning("invalid X handle %r", handle)
            return None
        cached = self._cached_users([h])
        if h in cached:
            log.debug("x_users cache hit for @%s", h)
            return cached[h]
        key = "users/by/username"
        self._announce(key, 1)
        payload = self._request(key, f"users/by/username/{h}", {"user.fields": USER_FIELDS})
        data = payload.get("data")
        if not data:
            log.info("@%s not found: %s", h, _error_detail_from_payload(payload))
            self._record(key, 0, note=f"@{h} not found")
            return None
        user = map_user(data)
        self._record(key, 1)
        self._persist_users([user])
        return user

    def get_users_by_handles(self, handles: list[str]) -> list[XUser]:
        wanted: list[str] = []
        for raw in handles:
            h = norm_handle(raw)
            if h and HANDLE_RE.match(h) and h not in wanted:
                wanted.append(h)
            elif h:
                log.warning("skipping invalid X handle %r", raw)
        found = self._cached_users(wanted)
        missing = [h for h in wanted if h not in found]
        if missing:
            key = "users/by"
            self._announce(key, len(missing))
            for i in range(0, len(missing), 100):
                batch = missing[i:i + 100]
                self._guard(key, len(batch))
                payload = self._request(key, "users/by", {"usernames": ",".join(batch), "user.fields": USER_FIELDS})
                users = [map_user(u) for u in payload.get("data") or []]
                self._record(key, len(users), note=f"{len(batch)} requested")
                for e in payload.get("errors") or []:
                    log.info("lookup miss: %s", e.get("detail") or e)
                self._persist_users(users)
                found.update({u.handle: u for u in users})
        return [found[h] for h in wanted if h in found]

    def search_users(self, query: str, max_results: int = 10) -> list[XUser]:
        q = sanitize_user_query(query)
        if not q:
            log.warning("search_users: nothing searchable in %r", query)
            return []
        key = "users/search"
        n = max(1, min(int(max_results), ENDPOINTS[key][1]))
        try:
            users = self._paged_users(key, "users/search", {"query": q, "user.fields": USER_FIELDS}, n,
                                      token=self._user_token or self._token)
        except XApiAuthError as e:
            raise XApiAuthError(
                f"{e} -- GET /2/users/search is documented with user-context auth only (OAuth 2.0 user token, users.read). "
                "Set X_USER_ACCESS_TOKEN in .env (read-only scopes), or resolve handles with SOURCE=monid.") from e
        self._persist_users(users)
        return users

    def get_following(self, handle_or_id: str, max_users: int = 1000) -> list[XUser]:
        return self._follow_list("users/:id/following", "following", handle_or_id, max_users)

    def get_followers(self, handle_or_id: str, max_users: int = 1000) -> list[XUser]:
        return self._follow_list("users/:id/followers", "followers", handle_or_id, max_users)

    def search_posts(self, query: str, max_results: int = 100, since: str | None = None) -> list[XPost]:
        start = _parse_since(since)
        use_all = start is not None and (dt.datetime.now(dt.timezone.utc) - start) > (RECENT_WINDOW - dt.timedelta(minutes=5))
        key = "tweets/search/all" if use_all else "tweets/search/recent"
        params = {"query": query, "tweet.fields": TWEET_FIELDS, "expansions": POST_EXPANSIONS, "user.fields": USER_FIELDS}
        if start is not None:
            params["start_time"] = _rfc3339(start)
        if use_all:
            log.info("since=%s is older than 7 days: using full-archive search", since)
        return self._paged_posts(key, key, params, max_results, query=query)

    def get_user_posts(self, handle_or_id: str, max_results: int = 100) -> list[XPost]:
        x_id, handle = self._resolve(handle_or_id)
        if not x_id:
            return []
        key = "users/:id/tweets"
        params = {"tweet.fields": TWEET_FIELDS, "expansions": POST_EXPANSIONS, "user.fields": USER_FIELDS, "exclude": "retweets"}
        return self._paged_posts(key, f"users/{x_id}/tweets", params, max_results, fallback_handle=handle)

    def get_latest_originals(self, handle: str, max_results: int) -> list[XPost]:
        """The person's latest original posts, newest first: the timeline (reverse-chronological) without reposts and
        replies, the engagement sample (sample.py). A stray reply or repost is dropped all the same."""
        x_id, h = self._resolve(handle)
        if not x_id:
            return []
        key = "users/:id/tweets"
        params = {"tweet.fields": TWEET_FIELDS, "expansions": POST_EXPANSIONS, "user.fields": USER_FIELDS,
                  "exclude": "retweets,replies"}
        posts = self._paged_posts(key, f"users/{x_id}/tweets", params, max_results, fallback_handle=h)
        return [p for p in posts if not p.in_reply_to_x_id and not p.text.startswith("RT @")]

    # ---- internals: resolution, cache, persistence ----

    def _resolve(self, handle_or_id: str) -> tuple[str | None, str | None]:
        """-> (x_id, handle). Numeric input is an id (handle looked up in the local cache only, never bought)."""
        s = (handle_or_id or "").strip()
        if s.isdigit():
            handle = None
            if self.conn is not None:
                r = db.one(self.conn, "SELECT handle FROM x_users WHERE x_id=?", (s,))
                handle = r["handle"] if r else None
            return s, handle
        user = self.get_user_by_handle(s)
        if not user:
            log.warning("cannot resolve %r to an X id", handle_or_id)
            return None, None
        return user.x_id, user.handle

    def _cached_users(self, handles: list[str]) -> dict[str, XUser]:
        # Only rows this adapter bought itself: the official lookup is the independent check in verify.py,
        # so a profile scraped through monid must not answer for it.
        if self.conn is None or not handles:
            return {}
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, XUser] = {}
        for i in range(0, len(handles), 500):
            chunk = handles[i:i + 500]
            qmarks = ",".join("?" for _ in chunk)
            for r in db.rows(self.conn, f"SELECT * FROM x_users WHERE source = 'x_api' AND fetched_at >= ? AND handle IN ({qmarks})", [cutoff, *chunk]):
                out[r["handle"]] = _row_to_user(r)
        return out

    def _persist_users(self, users: list[XUser]) -> None:
        if self.conn is not None and users:
            persist_users(self.conn, users, self.name)

    def _follow_list(self, key: str, direction: str, handle_or_id: str, max_users: int) -> list[XUser]:
        x_id, _ = self._resolve(handle_or_id)
        if not x_id:
            return []
        users = self._paged_users(key, f"users/{x_id}/{direction}", {"user.fields": USER_FIELDS}, max_users)
        if self.conn is not None and users:
            persist_follows(self.conn, x_id, users, direction, self.name)
        return users

    # ---- internals: pagination ----

    def _paged_users(self, key: str, path: str, params: dict, max_users: int, token: str | None = None) -> list[XUser]:
        _, page_max, page_min, token_param = ENDPOINTS[key]
        max_users = max(0, int(max_users))
        if max_users == 0:
            return []
        self._announce(key, max_users)
        out: list[XUser] = []
        seen: set[str] = set()
        next_token = None
        while len(out) < max_users:
            n = max(page_min, min(page_max, max_users - len(out)))
            p = dict(params, max_results=n)
            if next_token:
                p[token_param] = next_token
            self._guard(key, n)
            payload = self._request(key, path, p, token=token)
            users = [map_user(u) for u in payload.get("data") or []]
            self._record(key, len(users))
            for u in users:
                if u.x_id not in seen:
                    seen.add(u.x_id)
                    out.append(u)
            next_token = (payload.get("meta") or {}).get("next_token")
            if not next_token or not users:
                break
        return out[:max_users]

    def _paged_posts(self, key: str, path: str, params: dict, max_results: int, query: str | None = None,
                     fallback_handle: str | None = None) -> list[XPost]:
        _, page_max, page_min, token_param = ENDPOINTS[key]
        max_results = max(0, int(max_results))
        if max_results == 0:
            return []
        self._announce(key, max_results)
        out: list[XPost] = []
        seen: set[str] = set()
        users_seen: dict[str, XUser] = {}
        next_token = None
        while len(out) < max_results:
            n = max(page_min, min(page_max, max_results - len(out)))
            p = dict(params, max_results=n)
            if next_token:
                p[token_param] = next_token
            self._guard(key, n)
            payload = self._request(key, path, p)
            inc_users = {str(u.get("id")): u for u in (payload.get("includes") or {}).get("users") or []}
            posts = [map_post(t, inc_users, query=query, fallback_handle=fallback_handle) for t in payload.get("data") or []]
            self._record(key, len(posts), expanded_users=len(inc_users))
            for u in inc_users.values():
                users_seen[str(u["id"])] = map_user(u)
            for post in posts:
                if post.post_id not in seen:
                    seen.add(post.post_id)
                    out.append(post)
            next_token = (payload.get("meta") or {}).get("next_token")
            if not next_token or not posts:
                break
        out = out[:max_results]
        if self.conn is not None:
            self._persist_users(list(users_seen.values()))
            if out:
                persist_posts(self.conn, out, self.name)
        return out

    # ---- internals: cost ledger ----

    def _announce(self, key: str, units: int) -> float:
        usd = estimate(key, units)
        note = ""
        if ENDPOINTS[key][0] == "post_read" and BILL_EXPANSIONS:
            note = (f" (upper bound: {units} posts {costs.fmt(units * costs.X_PRICES['post_read'])} + up to {units} author "
                    f"profiles {costs.fmt(units * costs.X_PRICES['user_read'])})")
        print(f"[cost] x_api {key}: ~{costs.fmt(usd)} for {units} units{note}")
        self._guard(key, units)
        return usd

    def _guard(self, key: str, units: int) -> None:
        if self.conn is not None:
            costs.guard(self.conn, "x_api", estimate(key, units), self.run_id)

    def _record(self, key: str, units: int, note: str = "", expanded_users: int = 0) -> None:
        if self.conn is None:
            return
        costs.record(self.conn, "x_api", key, units, unit_cost(key), self.run_id, note)
        if expanded_users and BILL_EXPANSIONS:
            costs.record(self.conn, "x_api", f"{key}#{POST_EXPANSIONS}", expanded_users, costs.X_PRICES["user_read"],
                         self.run_id, "expanded author profiles (user reads; flip BILL_EXPANSIONS if the console disagrees)")

    # ---- internals: HTTP with rate limits, retries and error mapping ----

    def _request(self, key: str, path: str, params: dict, token: str | None = None) -> dict:
        bearer = token or self._token
        if not bearer:
            raise RuntimeError("X_BEARER_TOKEN missing: put the app-only Bearer token from console.x.com in .env")
        if config.dry_run() and not self._session_injected:
            raise RuntimeError("DRY_RUN=1: refusing to call the X API (use SOURCE=fixture or unset DRY_RUN)")
        url = f"{BASE_URL}/{path}"
        headers = {"Authorization": f"Bearer {bearer}", "User-Agent": "wearable-wishlist/0.1 (read-only)"}
        self._wait_for_window(key)
        n5xx = n429 = 0
        while True:
            try:
                resp = self._session.request("GET", url, params=params, headers=headers, timeout=TIMEOUT)
            except requests.RequestException as e:
                n5xx += 1
                if n5xx > RETRIES_5XX:
                    raise XApiError(f"X API {path}: network error after {RETRIES_5XX} retries: {e}") from e
                delay = BACKOFF_5XX[min(n5xx, len(BACKOFF_5XX)) - 1]
                log.warning("X API %s: %s; retry %d/%d in %.0fs", path, e, n5xx, RETRIES_5XX, delay)
                time.sleep(delay)
                continue
            hdr = {str(k).lower(): v for k, v in (resp.headers or {}).items()}
            self._remember_rate(key, hdr)
            status = resp.status_code
            if status == 200:
                log.debug("GET %s %s -> 200 (rate remaining %s)", path, {k: v for k, v in params.items() if k not in ("user.fields", "tweet.fields")},
                          hdr.get("x-rate-limit-remaining"))
                return resp.json() or {}
            if status == 429:
                n429 += 1
                if n429 > RETRIES_429:
                    raise XApiRateLimited(f"X API {path}: still rate limited after {RETRIES_429} waits: {_error_detail(resp)}")
                delay = self._seconds_until_reset(hdr)
                log.warning("X API %s: rate limited (429), sleeping %ds until the window resets (%d/%d)", path, delay, n429, RETRIES_429)
                time.sleep(delay)
                continue
            if status >= 500:
                n5xx += 1
                if n5xx > RETRIES_5XX:
                    raise XApiError(f"X API {path}: HTTP {status} after {RETRIES_5XX} retries: {_error_detail(resp)}")
                delay = BACKOFF_5XX[min(n5xx, len(BACKOFF_5XX)) - 1]
                log.warning("X API %s: HTTP %d; retry %d/%d in %.0fs", path, status, n5xx, RETRIES_5XX, delay)
                time.sleep(delay)
                continue
            detail = _error_detail(resp)
            if status == 402:
                raise XApiCreditsError(
                    f"X API: insufficient credits (402) for {path}. Nothing was charged; top up the pay-per-usage balance "
                    f"at https://console.x.com and re-run. {detail}")
            if status == 401:
                raise XApiAuthError(f"X API {path}: 401 unauthorized (is X_BEARER_TOKEN a valid app-only Bearer token?): {detail}")
            if status == 403:
                raise XApiAuthError(f"X API {path}: 403 forbidden (endpoint not available to this token/access level): {detail}")
            raise XApiError(f"X API {path}: HTTP {status}: {detail}")

    def _remember_rate(self, key: str, hdr: dict) -> None:
        try:
            remaining = int(hdr.get("x-rate-limit-remaining", ""))
            reset = float(hdr.get("x-rate-limit-reset", ""))
        except (TypeError, ValueError):
            return
        self._rate[key] = (remaining, reset)

    def _wait_for_window(self, key: str) -> None:
        """Proactive: if the last response said 0 requests remain, sleep until the reset instead of collecting a 429.
        Also paces /2/tweets/search/all to its 1 request / second limit."""
        rate = self._rate.get(key)
        if rate and rate[0] <= 0:
            delay = rate[1] - time.time() + 1
            if 0 < delay:
                delay = min(delay, MAX_RATE_LIMIT_SLEEP)
                log.warning("X API %s: rate window exhausted, sleeping %ds until reset", key, delay)
                time.sleep(delay)
                self._rate.pop(key, None)
        if key == "tweets/search/all":
            gap = 1.0 - (time.time() - self._last_all_search)
            if gap > 0:
                time.sleep(gap)
            self._last_all_search = time.time()

    @staticmethod
    def _seconds_until_reset(hdr: dict) -> int:
        try:
            reset = float(hdr.get("x-rate-limit-reset", ""))
        except (TypeError, ValueError):
            return DEFAULT_429_SLEEP
        delay = int(reset - time.time()) + 1
        if delay <= 0:
            return DEFAULT_429_SLEEP
        return min(delay, MAX_RATE_LIMIT_SLEEP)


def _error_detail_from_payload(payload: dict) -> str:
    return " | ".join(str(e.get("detail") or e) for e in payload.get("errors") or []) or "no data"


# ---------------------------------------------------------------- cost table (no network)

TYPICAL_BATCHES = [
    ("users/by/username", 1, "look up one handle"),
    ("users/by", 100, "verify 100 handles in one request"),
    ("users/search", 10, "resolve one map person (10 hits)"),
    ("users/search", 1500, "resolve 150 map people x 10 hits"),
    ("users/:id/following", 1000, "one hub's following list"),
    ("users/:id/following", 2000, "the operator's following (crawl cap)"),
    ("users/:id/followers", 2000, "the operator's followers (crawl cap)"),
    ("tweets/search/recent", 100, "one discovery query (100 posts)"),
    ("tweets/search/recent", 2000, "20 discovery queries x 100 posts"),
    ("tweets/search/all", 500, "one full-archive query (500 posts)"),
    ("users/:id/tweets", 100, "one candidate's last 100 posts"),
    ("users/:id/tweets", 20000, "200 candidates x 100 posts"),
]


def cost_table() -> list[tuple[str, int, float, float, str]]:
    return [(ep, n, estimate(ep, n, include_expansions=False), estimate(ep, n, include_expansions=True), what)
            for ep, n, what in TYPICAL_BATCHES]


if __name__ == "__main__":
    print(f"X API pay-per-usage, list prices from costs.X_PRICES: post read {costs.fmt(costs.X_PRICES['post_read'])}, "
          f"user read {costs.fmt(costs.X_PRICES['user_read'])}, follow read {costs.fmt(costs.X_PRICES['follow_read'])}")
    print(f"{'endpoint':<24}{'units':>7}{'posts/users':>13}{'+expansions':>13}  what")
    for ep, n, low, high, what in cost_table():
        print(f"{ep:<24}{n:>7}{costs.fmt(low):>13}{costs.fmt(high):>13}  {what}")
    print("\nA typical first run (crawl 2000+2000, 20 queries x 100 posts, 200 candidates x 100 posts, 150 user searches): "
          f"{costs.fmt(estimate('following', 2000) + estimate('followers', 2000) + estimate('search_posts', 2000) + estimate('user_posts', 20000) + estimate('search_users', 1500))} "
          f"(posts-only billing: {costs.fmt(estimate('following', 2000) + estimate('followers', 2000) + estimate('search_posts', 2000, False) + estimate('user_posts', 20000, False) + estimate('search_users', 1500))}). "
          "Re-runs are cheaper: same resources are deduplicated per UTC day and x_users is cached for 7 days.")
