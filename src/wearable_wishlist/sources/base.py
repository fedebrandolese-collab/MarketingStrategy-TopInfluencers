"""Shared data shapes, the Source protocol every X adapter implements, persistence helpers, and the offline FixtureSource.
Adapters: sources/x_api.py (official X API), sources/monid.py (monid -> Apify actors). Both must:
  * return XUser / XPost objects exactly as defined here,
  * never write to X (read-only),
  * call costs.guard(...) BEFORE and costs.record(...) AFTER every paid call,
  * cache in SQLite via persist_* so re-runs are cheap."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Iterable, Protocol

from .. import config, db


def norm_handle(h: str | None) -> str | None:
    return h.strip().lstrip("@").lower() if h else None


@dataclass
class XUser:
    x_id: str
    handle: str
    name: str | None = None
    bio: str | None = None
    location: str | None = None
    url: str | None = None
    verified: bool | None = None
    verified_type: str | None = None   # 'blue' | 'business' | 'government' | 'none' | None
    created_at: str | None = None
    followers: int | None = None
    following: int | None = None
    posts_count: int | None = None
    can_dm: bool | None = None
    profile_image: str | None = None
    raw: dict | None = None

    def __post_init__(self):
        self.x_id = str(self.x_id)
        self.handle = norm_handle(self.handle) or self.handle


@dataclass
class XPost:
    post_id: str
    x_id: str
    text: str
    handle: str | None = None
    created_at: str | None = None
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0
    views: int | None = None
    url: str | None = None
    in_reply_to_x_id: str | None = None
    mentions: list[str] = field(default_factory=list)
    query: str | None = None
    raw: dict | None = None

    def __post_init__(self):
        self.post_id = str(self.post_id)
        self.x_id = str(self.x_id)
        self.handle = norm_handle(self.handle)
        if not self.url and self.handle:
            self.url = f"https://x.com/{self.handle}/status/{self.post_id}"


class Source(Protocol):
    """Read-only access to X. `max_*` are hard caps the adapter must respect (they bound the cost)."""
    name: str

    def get_user_by_handle(self, handle: str) -> XUser | None: ...
    def get_users_by_handles(self, handles: list[str]) -> list[XUser]: ...
    def search_users(self, query: str, max_results: int = 10) -> list[XUser]: ...
    def get_following(self, handle_or_id: str, max_users: int = 1000) -> list[XUser]: ...
    def get_followers(self, handle_or_id: str, max_users: int = 1000) -> list[XUser]: ...
    def search_posts(self, query: str, max_results: int = 100, since: str | None = None) -> list[XPost]: ...
    def get_user_posts(self, handle_or_id: str, max_results: int = 100) -> list[XPost]: ...
    # newest first, no reposts, no replies: the engagement sample (sample.py)
    def get_latest_originals(self, handle: str, max_results: int) -> list[XPost]: ...


# ---------------- persistence helpers (shared by all adapters) ----------------

def persist_users(conn: sqlite3.Connection, users: Iterable[XUser], source: str) -> int:
    """Upsert profiles. A newer fetch wins field by field, but a NULL never erases a known value: the official API
    does not expose DM permission, the follower scraper does not expose blue badges, and neither should wipe the other.
    can_dm is sticky once true: a scraped author's canDm depends on the account that loaded the page (the same author
    reads true on one post and false on the next, 30 Sep), so any sighting of open DMs counts and a later false does not
    erase it (sticky_dm_backfill applies the same rule to the posts already stored)."""
    cols = ["x_id", "handle", "name", "bio", "location", "url", "verified", "verified_type", "created_at", "followers",
            "following", "posts_count", "can_dm", "profile_image", "raw_json", "source", "fetched_at"]
    keep_if_null = [c for c in cols if c not in ("x_id", "handle", "source", "fetched_at", "can_dm")]
    sql = (f"INSERT INTO x_users ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) ON CONFLICT(x_id) DO UPDATE SET "
           + ",".join(["handle=excluded.handle", "source=excluded.source", "fetched_at=excluded.fetched_at",
                       "can_dm=CASE WHEN x_users.can_dm=1 THEN 1 ELSE COALESCE(excluded.can_dm, x_users.can_dm) END"]
                      + [f"{c}=COALESCE(excluded.{c}, x_users.{c})" for c in keep_if_null]))
    n = 0
    now = db.utcnow()
    for u in users:
        conn.execute(sql, (u.x_id, u.handle, u.name, u.bio, u.location, u.url,
                           None if u.verified is None else int(u.verified), u.verified_type, u.created_at, u.followers,
                           u.following, u.posts_count, None if u.can_dm is None else int(u.can_dm), u.profile_image,
                           db.jdumps(u.raw) if u.raw else None, source, now))
        n += 1
    conn.commit()
    return n


def sticky_dm_backfill(conn: sqlite3.Connection) -> int:
    """can_dm = 1 for every account seen with open DMs before (persist_users' rule): an author object on a stored post
    said canDm true, or an earlier run found an open_dm path to them (paths are built from can_dm at the time)."""
    cur = conn.execute("""UPDATE x_users SET can_dm=1 WHERE COALESCE(can_dm, 0) != 1 AND (x_id IN (
        SELECT DISTINCT x_id FROM x_posts WHERE raw_json IS NOT NULL AND json_extract(raw_json, '$.author.canDm') = 1)
        OR x_id IN (SELECT c.x_id FROM paths p JOIN candidates c USING (candidate_id) WHERE p.how = 'open_dm'))""")
    conn.commit()
    return cur.rowcount


def persist_posts(conn: sqlite3.Connection, posts: Iterable[XPost], source: str) -> int:
    n = 0
    now = db.utcnow()
    for p in posts:
        db.upsert(conn, "x_posts", {
            "post_id": p.post_id, "x_id": p.x_id, "handle": p.handle, "created_at": p.created_at, "text": p.text,
            "likes": p.likes, "reposts": p.reposts, "replies": p.replies, "quotes": p.quotes, "views": p.views,
            "url": p.url, "in_reply_to_x_id": p.in_reply_to_x_id, "mentions": db.jdumps(p.mentions or []),
            "query": p.query, "raw_json": db.jdumps(p.raw) if p.raw else None, "source": source, "fetched_at": now,
        }, keys=("post_id",))
        n += 1
    conn.commit()
    return n


def persist_follows(conn: sqlite3.Connection, src_x_id: str, users: Iterable[XUser], direction: str, source: str) -> int:
    """direction='following': src follows each user. direction='followers': each user follows src."""
    assert direction in ("following", "followers")
    n = 0
    now = db.utcnow()
    users = list(users)
    persist_users(conn, users, source)
    for u in users:
        a, b = (str(src_x_id), u.x_id) if direction == "following" else (u.x_id, str(src_x_id))
        db.upsert(conn, "x_follows", {"src_x_id": a, "dst_x_id": b, "source": source, "fetched_at": now}, keys=("src_x_id", "dst_x_id"))
        n += 1
    conn.commit()
    return n


# ---------------- offline source for tests and DRY_RUN ----------------

class FixtureSource:
    """Serves data/fixtures/x_users.json and x_posts.json. Free, deterministic, no network."""
    name = "fixture"

    def __init__(self, fixtures_dir=None):
        d = fixtures_dir or config.FIXTURES_DIR
        raw_users = json.loads((d / "x_users.json").read_text())
        raw_posts = json.loads((d / "x_posts.json").read_text())
        self._users = {u["x_id"]: XUser(**{k: v for k, v in u.items() if k not in ("following",)}) for u in raw_users}
        self._following = {u["x_id"]: [str(x) for x in u.get("following", [])] for u in raw_users}
        self._posts = [XPost(**p) for p in raw_posts]

    def _resolve(self, handle_or_id: str) -> XUser | None:
        h = norm_handle(handle_or_id)
        if handle_or_id in self._users:
            return self._users[handle_or_id]
        return next((u for u in self._users.values() if u.handle == h), None)

    def get_user_by_handle(self, handle):
        return self._resolve(handle)

    def get_users_by_handles(self, handles):
        return [u for u in (self._resolve(h) for h in handles) if u]

    def search_users(self, query, max_results=10):
        q = query.lower()
        hits = [u for u in self._users.values() if q in (u.name or "").lower() or q in (u.bio or "").lower() or q in u.handle]
        return hits[:max_results]

    def get_following(self, handle_or_id, max_users=1000):
        u = self._resolve(handle_or_id)
        if not u:
            return []
        return [self._users[x] for x in self._following.get(u.x_id, []) if x in self._users][:max_users]

    def get_followers(self, handle_or_id, max_users=1000):
        u = self._resolve(handle_or_id)
        if not u:
            return []
        return [self._users[s] for s, dsts in self._following.items() if u.x_id in dsts][:max_users]

    def search_posts(self, query, max_results=100, since=None):
        # quoted phrases and bare words; operators (lang:en, -is:retweet, OR, parentheses) are ignored
        tokens = re.findall(r'"([^"]+)"|([^\s()]+)', query)
        terms = [(a or b).lower() for a, b in tokens if (a or b) and not (b and (b.startswith("-") or ":" in b or b.upper() in ("OR", "AND")))]
        hits = [p for p in self._posts if any(t in p.text.lower() for t in terms)]
        for p in hits:
            p.query = query
        return hits[:max_results]

    def get_user_posts(self, handle_or_id, max_results=100):
        u = self._resolve(handle_or_id)
        return [p for p in self._posts if u and p.x_id == u.x_id][:max_results]

    def get_latest_originals(self, handle, max_results):
        u = self._resolve(handle)
        own = [p for p in self._posts if u and p.x_id == u.x_id and not p.in_reply_to_x_id and not p.text.startswith("RT @")]
        return sorted(own, key=lambda p: p.created_at or "", reverse=True)[:max_results]


def get_source(name: str | None = None, conn: sqlite3.Connection | None = None, run_id: int | None = None) -> Source:
    """conn/run_id let paid adapters use the cost ledger (costs.guard/record). FixtureSource ignores them."""
    name = name or config.source_name()
    if name == "fixture":
        return FixtureSource()
    if name == "x_api":
        from .x_api import XApiSource
        return XApiSource(conn=conn, run_id=run_id)
    if name == "monid":
        from .monid import MonidSource
        return MonidSource(conn=conn, run_id=run_id)
    raise ValueError(f"unknown source {name!r}")


def user_to_dict(u: XUser) -> dict:
    return asdict(u)
