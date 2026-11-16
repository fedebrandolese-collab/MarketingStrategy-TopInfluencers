"""Discovery on X: seed graph crawl + topic search -> PEOPLE (candidates), never posts; then enrichment of the most
promising candidates with their own recent posts, so classification reads their words and not only search hits.

Stages (idempotent: re-running upserts, first_seen and the original discovered_via are kept):
  run_crawl(conn, run_id, source, seed_handle)  the operator's following + followers -> x_users / x_follows (batched writes, fine with
                                                thousands of users); candidates for people whose bio reads like an X voice,
                                                media, VC or founder/angel (discovered_via='seed'); counts per hint and per
                                                skip reason are logged and returned
  run_discover(conn, run_id, source)            queries.yaml QUERY_GROUPS (topics + competitor_products + wearable_wishlist + authors + evidence:
                                                first-person device and voice-tool posts) -> x_posts -> aggregate by
                                                author -> candidates (discovered_via='query:<q>'); then the following lists of
                                                `hubs` (discovered_via='following:<hub>')
  expand.run_expand(conn, run_id, source)      (own module) who the top-ranked people follow; accounts followed by
                                                >= max(3, 20%) of them -> candidates (discovered_via='cofollow:<k>/<n>')
  run_enrich(conn, run_id, source, top=80)      the highest-priority candidates not enriched yet -> two searches of their own
                                                posts (device/voice evidence + recent context; mode='timeline' reads their
                                                timeline instead) -> x_posts; marker 'enriched:YYYY-MM-DD posts=N' in notes
Helpers: candidate_signals(conn, candidate_id); candidate_priorities(conn) (bulk signals + the PRIORITY formula, shared with
classify so both spend on the same people first); suggest_hubs(conn, top=20) (real fetched handles only, never invented).

Rules: the seed is never a candidate and its own posts are ignored. Retweets and non-English posts are dropped from discovery
even when the source did not filter them. A candidate from search needs >= 2 topical posts, or 1 topical post with
>= limits.min_likes likes, or a bio match. Parody / fan accounts, brand / outlet / bot accounts and spam are not created as
candidates (logged and counted). Category is NEVER set here: a bio heuristic is a hint stored in `notes`
("hint:vc kw:partner @"); classify sets the category.

Cost: adapters guard and record their own spend (costs.guard / costs.record). This module prints a projection before every
paid batch with source-aware unit prices (_unit_cost: monid follows $0.000225/row, tweets $0.0006/post, profile $0.0015/call;
x_api via x_api.estimate; fixture 0) and keeps a running estimate: an explicit `budget_usd` caps the stage itself, otherwise
the provider's per-run cap from .env (minus what this run already spent) applies. Calls stop before the estimate would
exceed the cap."""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from . import config, costs, db
from .sources.base import XPost, XUser, norm_handle, persist_posts

log = logging.getLogger("wearable_wishlist.discover")

# ---------------------------------------------------------------------------------------------------------------------
# Heuristics tables. Matching is case-insensitive, on word boundaries, over "name + bio".
# ---------------------------------------------------------------------------------------------------------------------

# Role hints: which bucket a bio smells like. Only decides "worth a candidates row"; the category comes from classify.
# Deliberately specific: generic words ("building", "capital", "fund", "producer", "streamer") matched half of any
# follower list and are left out.
ROLE_KEYWORDS: dict[str, list[str]] = {
    "vc": [
        "vc", "venture", "ventures", "venture capital", "venture capitalist", "general partner", "managing partner",
        "partner @", "partner at", "gp @", "gp at", "principal @", "principal at", "investor", "investing in", "invests in",
        "invested in", "seed stage", "pre-seed", "backing founders", "solo capitalist", "angel investor", "lp in",
    ],
    "founder_angel": [
        "founder", "co-founder", "cofounder", "founded", "founding", "ceo", "cto", "angel", "angels", "acquired", "acq.",
        "exited", "serial entrepreneur", "entrepreneur", "y combinator", "yc", "startup", "startups",
    ],
    "media": [
        "journalist", "reporter", "editor", "correspondent", "columnist", "staff writer", "writer @", "writer at", "writes for",
        "writing for", "covers", "covering", "newsletter", "podcast", "podcaster", "host of", "the verge", "wired",
        "techcrunch", "bloomberg", "nytimes", "wsj", "financial times", "the information", "cnbc", "axios", "bbc", "guardian",
        "engadget", "gizmodo", "cnet", "zdnet", "mashable", "tom's guide", "tips open", "dms open for tips",
    ],
    "x_voice": [
        "reviewer", "reviews", "reviewing", "unboxing", "creator", "youtuber", "quantified self", "biohacker", "biohacking",
        "gadgets", "gadget", "wearable", "wearables", "smart wearable", "smart wearables", "voice-first", "voice first", "screen-free",
        "screenless", "ai agent", "ai agents", "personal agent", "personal ai", "ambient computing", "post-smartphone",
        "earbuds", "pendant", "ai pin", "oura", "@ouraring", "ultrahuman", "humane pin", "rabbit r1", "limitless pendant",
        "plaud", "friend pendant", "wispr flow", "sandbar", "wearable wishlist", "tinynature", "humanpods",
    ],
}
ROLE_PRIORITY = ("vc", "founder_angel", "media", "x_voice")   # tie-break order when a bio matches several buckets

# Topical words in a POST text (used when a post did not come from a query, e.g. from get_user_posts).
TOPIC_KEYWORDS = [
    "smart wearable", "smart wearables", "ai wearable", "ai wearables", "wearable", "wearables", "voice-first", "voice first",
    "voice assistant", "voice agent", "voice interface", "ai agent", "ai agents", "personal agent", "personal ai", "screen-free",
    "screenless", "screen free", "ambient computing", "post-smartphone", "ai earbuds", "ai pendant", "ai pin", "oura",
    "ultrahuman", "humane", "rabbit r1", "limitless pendant", "plaud", "omi wearable", "bee wearable", "friend pendant",
    "sesame", "wispr flow", "sandbar", "wearable wishlist", "tinynature", "humanpods", "unboxing", "earbuds", "pendant",
]

# Topic words in a BIO for the seed crawl: the wearable's themes plus the adjacent tech a plausible wearer talks about.
BIO_TOPIC_KEYWORDS = TOPIC_KEYWORDS + [
    "ai", "a.i.", "artificial intelligence", "genai", "llm", "llms", "agents", "agentic", "voice", "voice ai", "speech",
    "hardware", "consumer hardware", "consumer tech", "consumer electronics", "devices", "device", "gadgets", "iot", "hci",
    "human-computer interaction", "interfaces", "ux", "product design", "industrial design", "quantified self", "biohacking",
    "longevity", "healthtech", "health tech", "digital health", "wellness", "digital wellbeing", "mindfulness",
    "spatial computing", "ar/vr", "vr", "xr", "robotics", "tech", "technology",
]

# Parody / fan / impersonation words in name or bio -> never a candidate here, "suspicious" in classify.
PARODY_WORDS = [
    "parody", "satire", "satirical", "fan account", "fan page", "fanpage", "fan acct", "unofficial", "not affiliated",
    "impersonation", "impersonating", "spoof", "tribute account", "(fake)", "fake account", "commentary account",
]

# Brand / outlet / bot accounts: a wearable goes to a person. X's 'business' / 'government' badges are organisations too.
ORG_WORDS = [
    "official account", "official twitter", "official x account", "official page", "the official", "official home of",
    "customer support", "support account", "help desk", "news and updates", "latest news", "breaking news", "updates from",
    "news from", "we're hiring", "we are hiring", "automated account", "bot account", "i am a bot", "i'm a bot", "(bot)",
    "public relations", "pr agency", "pr firm", "press releases", "communications agency", "marketing agency", "digital agency",
]
_ORG_START_RE = re.compile(r"^\s*(we|we're|we’re|our)\b", re.I)
ORG_VERIFIED_TYPES = {"business", "government"}

# Spam / NSFW / promo accounts: obviously irrelevant for a wearable.
SPAM_WORDS = [
    "nft", "nfts", "memecoin", "memecoins", "airdrop", "airdrops", "forex", "trading signals", "crypto signals", "onlyfans",
    "18+", "nsfw", "dm for promo", "dm for promotion", "promo code", "giveaway", "giveaways", "follow back", "followback",
    "f4f", "sugar daddy", "escort",
]

# Metrics: a big audience makes a topical author an X voice worth a look (discover only; never alone in the crawl).
VOICE_MIN_FOLLOWERS = 25_000
# Seed crawl: below this audience a follower/following needs a mutual follow with the seed to become a candidate.
CRAWL_MIN_FOLLOWERS = 500

# English detection when the source gave no language: a few function words; short texts get the benefit of the doubt.
ENGLISH_STOPWORDS = {"the", "and", "is", "with", "to", "of", "a", "for", "in", "on", "it", "my", "i", "this", "that", "are", "you", "we"}


def kw_regex(words: list[str]) -> re.Pattern:
    parts = []
    for w in words:
        w = re.escape(w.lower())
        # word boundaries only where the keyword starts/ends with a word character (so "acq." and "@ouraring" still work)
        left = r"(?<![a-z0-9])" if w[0].isalnum() else ""
        right = r"(?![a-z0-9])" if w[-1].isalnum() else ""
        parts.append(f"{left}{w}{right}")
    return re.compile("|".join(parts))


_ROLE_RE = {role: kw_regex(words) for role, words in ROLE_KEYWORDS.items()}
_TOPIC_RE = kw_regex(TOPIC_KEYWORDS)
_BIO_TOPIC_RE = kw_regex(BIO_TOPIC_KEYWORDS)
_PARODY_RE = kw_regex(PARODY_WORDS)
_ORG_RE = kw_regex(ORG_WORDS)
_SPAM_RE = kw_regex(SPAM_WORDS)


def bio_hint(name: str | None, bio: str | None, followers: int | None = None) -> tuple[str | None, list[str]]:
    """(role hint or None, matched keywords). The bucket with most keyword hits wins; ties go by ROLE_PRIORITY.
    With no keyword hit, a large audience (>= VOICE_MIN_FOLLOWERS) still hints 'x_voice'."""
    text = f"{name or ''} {bio or ''}".lower()
    hits = {role: sorted(set(m.group(0) for m in rx.finditer(text))) for role, rx in _ROLE_RE.items()}
    hits = {r: kws for r, kws in hits.items() if kws}
    if hits:
        best = max(ROLE_PRIORITY, key=lambda r: (len(hits.get(r, [])), -ROLE_PRIORITY.index(r)))
        return best, hits[best]
    if followers is not None and followers >= VOICE_MIN_FOLLOWERS:
        return "x_voice", [f"followers>={VOICE_MIN_FOLLOWERS // 1000}k"]
    return None, []


def bio_topics(name: str | None, bio: str | None) -> list[str]:
    return sorted(set(m.group(0) for m in _BIO_TOPIC_RE.finditer(f"{name or ''} {bio or ''}".lower())))


def looks_parody(name: str | None, bio: str | None) -> bool:
    return bool(_PARODY_RE.search(f"{name or ''} {bio or ''}".lower()))


def looks_org(name: str | None, bio: str | None, verified_type: str | None = None) -> bool:
    """Brand, outlet, support or bot account rather than a person."""
    if (verified_type or "").lower() in ORG_VERIFIED_TYPES:
        return True
    return bool(_ORG_RE.search(f"{name or ''} {bio or ''}".lower()) or _ORG_START_RE.search(bio or ""))


def looks_spam(name: str | None, bio: str | None) -> bool:
    return bool(_SPAM_RE.search(f"{name or ''} {bio or ''}".lower()))


def is_topical(text: str | None) -> bool:
    return bool(text) and bool(_TOPIC_RE.search(text.lower()))


NON_SEARCH_QUERIES = {"latest_status"}     # markers adapters store in x_posts.query for posts no search surfaced


def query_kind(query: str | None) -> str | None:
    """Which search stored a post: 'discovery' (a queries.yaml topic query), 'evidence' (run_enrich's device/voice search
    'from:<h> (...)'), 'recent' (run_enrich's 'from:<h> -is:retweet'), or None (timeline / unknown). The crawl's
    'latest_status' marker is a timeline post, not a search hit: it counts as topical only when its text is."""
    q = (query or "").strip()
    if not q or q.lower() in NON_SEARCH_QUERIES:
        return None
    if q.lower().startswith("from:"):
        return "evidence" if "(" in q else "recent"
    return "discovery"


def post_is_topical(query: str | None, text: str | None) -> bool:
    """Topical = surfaced by a discovery query, or on-topic text. Enrichment searches ('from:<h> ...') do not count by
    themselves: 'ChatGPT' or 'glasses' in a post is evidence for the classifier, not a topic signal for the priority."""
    return query_kind(query) == "discovery" or is_topical(text)


def is_retweet(post: XPost) -> bool:
    if (post.text or "").startswith("RT @"):
        return True
    raw = post.raw or {}
    if raw.get("isRetweet") or raw.get("retweeted_tweet") or raw.get("retweetedTweet"):
        return True
    return any((r or {}).get("type") == "retweeted" for r in raw.get("referenced_tweets") or [])


def looks_english(post: XPost) -> bool:
    raw = post.raw or {}
    lang = raw.get("lang")
    if lang:
        return str(lang).lower().startswith("en")
    text = post.text or ""
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if ord(c) > 0x024F) / len(letters) > 0.3:   # mostly non-Latin script
        return False
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 6:
        return True
    return any(w in ENGLISH_STOPWORDS for w in words)


def crawl_verdict(u: XUser, mutual: bool = False) -> tuple[str | None, str]:
    """Seed-crawl filter: (hint, note) when the account deserves a candidates row, else (None, skip reason).
    In order: parody/fan words -> 'parody'; brand/outlet/bot (business/government badge, 'official account', a bio that
    starts with 'We'/'Our') -> 'org'; spam/NSFW/promo words -> 'spam'; no role word and no topic word in name + bio ->
    'no_signal' (a big audience alone is not a reason: famous is not a justification); fewer than CRAWL_MIN_FOLLOWERS
    followers and no mutual follow with the seed -> 'small_audience'. Otherwise the hint is the role bucket, or 'x_voice'
    for a topic-only bio."""
    if looks_parody(u.name, u.bio):
        return None, "parody"
    if looks_org(u.name, u.bio, u.verified_type):
        return None, "org"
    if looks_spam(u.name, u.bio):
        return None, "spam"
    role, role_kws = bio_hint(u.name, u.bio)          # no followers fallback here
    topics = bio_topics(u.name, u.bio)
    if not role and not topics:
        return None, "no_signal"
    if (u.followers or 0) < CRAWL_MIN_FOLLOWERS and not mutual:
        return None, "small_audience"
    hint = role or "x_voice"
    kws = role_kws if role else topics
    note = _hint_note(hint, kws)
    if role and topics:
        note += f" topics:{','.join(topics[:4])}"
    return hint, note


# ---------------------------------------------------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------------------------------------------------

def _note_key(segment: str) -> str:
    return segment.split(":", 1)[0].strip() if ":" in segment else segment.strip()


def _merge_notes(old: str | None, new: str | None) -> str | None:
    """Notes are ' | '-separated 'key:value' segments; a new segment replaces the old one with the same key
    (hint:, seed:, posts:, enriched:, ...), other modules' segments are kept as they are."""
    if not new:
        return old
    if not old:
        return new
    segs = [s for s in old.split(" | ") if s.strip()]
    for n in [s for s in new.split(" | ") if s.strip()]:
        segs = [s for s in segs if _note_key(s) != _note_key(n)]
        segs.append(n)
    return " | ".join(segs)


def upsert_candidate(conn: sqlite3.Connection, x_id: str, handle: str | None, name: str | None, discovered_via: str,
                     note: str | None = None, now: str | None = None) -> tuple[int, bool]:
    """Insert or refresh a candidates row for an X account. Keeps first_seen and the original discovered_via.
    A map-only row with the same handle and no x_id (written by resolve_handles) is claimed instead of duplicated.
    Returns (candidate_id, created)."""
    now = now or db.utcnow()
    handle = norm_handle(handle)
    row = db.one(conn, "SELECT candidate_id, notes FROM candidates WHERE x_id=?", (str(x_id),))
    if row is None and handle:
        row = db.one(conn, "SELECT candidate_id, notes FROM candidates WHERE x_id IS NULL AND handle=?", (handle,))
        if row:
            conn.execute("UPDATE candidates SET x_id=? WHERE candidate_id=?", (str(x_id), row["candidate_id"]))
    if row:
        conn.execute("UPDATE candidates SET handle=COALESCE(?, handle), name=COALESCE(?, name), last_seen=?, notes=? WHERE candidate_id=?",
                     (handle, name, now, _merge_notes(row["notes"], note), row["candidate_id"]))
        return int(row["candidate_id"]), False
    cur = conn.execute(
        "INSERT INTO candidates (x_id, handle, name, discovered_via, first_seen, last_seen, notes) VALUES (?,?,?,?,?,?,?)",
        (str(x_id), handle, name, discovered_via, now, now, note))
    return int(cur.lastrowid), True


def bulk_upsert_candidates(conn: sqlite3.Connection, entries: list[tuple], now: str | None = None) -> tuple[int, int]:
    """Batched upsert_candidate for thousands of rows: entries are (x_id, handle, name, discovered_via, note).
    Two SELECTs for the existing rows, then executemany. Same semantics (first_seen / discovered_via kept, map-only rows
    with the same handle claimed). Returns (created, updated). Does not commit."""
    if not entries:
        return 0, 0
    now = now or db.utcnow()
    by_x = {r["x_id"]: r for r in db.rows(conn, "SELECT candidate_id, x_id, notes FROM candidates WHERE x_id IS NOT NULL")}
    by_handle = {r["handle"]: r for r in db.rows(
        conn, "SELECT candidate_id, handle, notes FROM candidates WHERE x_id IS NULL AND handle IS NOT NULL")}
    inserts, updates, claims, seen = [], [], [], set()
    for x_id, handle, name, via, note in entries:
        x_id, handle = str(x_id), norm_handle(handle)
        if x_id in seen:
            continue
        seen.add(x_id)
        row = by_x.get(x_id)
        if row is None and handle and handle in by_handle:
            row = by_handle.pop(handle)
            claims.append((x_id, row["candidate_id"]))
        if row:
            updates.append((handle, name, now, _merge_notes(row["notes"], note), row["candidate_id"]))
        else:
            inserts.append((x_id, handle, name, via, now, now, note))
    if claims:
        conn.executemany("UPDATE candidates SET x_id=? WHERE candidate_id=?", claims)
    if updates:
        conn.executemany("UPDATE candidates SET handle=COALESCE(?, handle), name=COALESCE(?, name), last_seen=?, notes=? "
                         "WHERE candidate_id=?", updates)
    if inserts:
        conn.executemany("INSERT INTO candidates (x_id, handle, name, discovered_via, first_seen, last_seen, notes) "
                         "VALUES (?,?,?,?,?,?,?)", inserts)
    return len(inserts), len(updates)


def _hint_note(role: str, kws: list[str]) -> str:
    return f"hint:{role} kw:{','.join(kws[:6])}"


_USER_COLS = ("x_id", "handle", "name", "bio", "location", "url", "verified", "verified_type", "created_at", "followers",
              "following", "posts_count", "can_dm", "profile_image", "raw_json", "source", "fetched_at")


def _user_tuple(u: XUser, source: str, now: str) -> tuple:
    return (u.x_id, u.handle, u.name, u.bio, u.location, u.url, None if u.verified is None else int(u.verified),
            u.verified_type, u.created_at, u.followers, u.following, u.posts_count,
            None if u.can_dm is None else int(u.can_dm), u.profile_image, db.jdumps(u.raw) if u.raw else None, source, now)


_USER_ALWAYS = ("handle", "source", "fetched_at")   # a newer fetch always wins these; other fields only when not NULL


def persist_users_batch(conn: sqlite3.Connection, users: list[XUser], source: str, now: str | None = None) -> int:
    """sources.base.persist_users with one executemany and the same semantics: a newer fetch wins field by field, but a
    NULL never erases a known value (one adapter's missing can_dm must not wipe another's). Does not commit."""
    if not users:
        return 0
    now = now or db.utcnow()
    upd = ",".join([f"{c}=excluded.{c}" for c in _USER_ALWAYS]
                   + [f"{c}=COALESCE(excluded.{c}, x_users.{c})" for c in _USER_COLS if c not in _USER_ALWAYS and c != "x_id"])
    conn.executemany(f"INSERT INTO x_users ({','.join(_USER_COLS)}) VALUES ({','.join('?' * len(_USER_COLS))}) "
                     f"ON CONFLICT(x_id) DO UPDATE SET {upd}", [_user_tuple(u, source, now) for u in users])
    return len(users)


def persist_follows_batch(conn: sqlite3.Connection, src_x_id: str, users: list[XUser], direction: str, source: str,
                          now: str | None = None) -> int:
    """sources.base.persist_follows with executemany. direction='following': src follows each user; 'followers': each
    user follows src. Does not commit."""
    assert direction in ("following", "followers")
    now = now or db.utcnow()
    persist_users_batch(conn, users, source, now)
    pairs = [((str(src_x_id), u.x_id) if direction == "following" else (u.x_id, str(src_x_id))) for u in users]
    conn.executemany("INSERT INTO x_follows (src_x_id, dst_x_id, source, fetched_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(src_x_id, dst_x_id) DO UPDATE SET source=excluded.source, fetched_at=excluded.fetched_at",
                     [(a, b, source, now) for a, b in pairs])
    return len(pairs)


def _screen_users(users: list[XUser], discovered_via: str, rel: dict[str, str] | None, mutual_ids: set[str],
                  seed_x_id: str | None, stats: dict) -> list[tuple]:
    """crawl_verdict over a user list -> bulk_upsert_candidates entries; counts per hint and per skip reason in stats."""
    entries = []
    for u in users:
        if seed_x_id and u.x_id == seed_x_id:
            continue
        hint, why = crawl_verdict(u, mutual=u.x_id in mutual_ids)
        if hint is None:
            stats["skipped"][why] += 1
            if why == "parody" and len(stats["parody_skipped"]) < 50:
                stats["parody_skipped"].append(u.handle)
            continue
        stats["by_hint"][hint] += 1
        note = _merge_notes(why, rel.get(u.x_id) if rel else None)
        entries.append((u.x_id, u.handle, u.name, discovered_via, note))
    return entries


# ---------------------------------------------------------------------------------------------------------------------
# Cost estimate (the adapters guard for real; this is the stage's own pre-check so it stops before over-asking)
# ---------------------------------------------------------------------------------------------------------------------

_MONID_KEYS = {"follow_read": "follows", "post_read": "tweets", "user_read": "profile"}
_XAPI_KEYS = {"follow_read": "following", "post_read": "search_posts", "user_read": "users_lookup", "timeline_read": "user_posts"}


def _unit_cost(source, kind: str) -> float:
    """Projected USD per unit on this source. kind: follow_read | post_read | user_read | timeline_read (a user's own posts).
    fixture: 0. monid: sources.monid.estimate (follows $0.000225/row, tweets $0.0006/post, profile $0.0015/call).
    x_api: sources.x_api.estimate (post reads include the expanded author as an upper bound, as workstream B asks).
    Any other source: X list prices (an upper bound)."""
    name = getattr(source, "name", "")
    if name == "fixture":
        return 0.0
    if name == "monid":
        from .sources import monid
        return monid.estimate(_MONID_KEYS["post_read" if kind == "timeline_read" else kind], 1)
    if name == "x_api":
        from .sources import x_api
        return x_api.estimate(_XAPI_KEYS[kind], 1)
    return costs.X_PRICES["post_read" if kind == "timeline_read" else kind]


def _billed_units(source, kind: str, n: int) -> int:
    """Units a request for n items is billed for: monid's follower actor bills at least 200 rows per list."""
    if getattr(source, "name", "") == "monid" and kind == "follow_read":
        from .sources import monid
        return max(int(n), getattr(monid, "FOLLOW_MIN_CAP", 200))
    return int(n)


def estimate_cost(source, kind: str, n: int, requests: int = 1) -> float:
    """Projected USD for `requests` calls of n items each."""
    return round(requests * _billed_units(source, kind, n) * _unit_cost(source, kind), 6)


class _Budget:
    """Running estimate for one stage. An explicit budget_usd caps the stage's own projected spend (starts at 0);
    otherwise the provider's per-run cap from .env applies, starting from what this run already spent."""

    def __init__(self, conn, source, run_id, budget_usd):
        self.provider = getattr(source, "name", "unknown")
        self.cap = budget_usd
        if budget_usd is not None:
            self.estimate = 0.0
        else:
            if self.provider in ("x_api", "monid"):
                self.cap = config.budget_usd(self.provider)
            self.estimate = costs.spent(conn, self.provider, run_id) if self.provider != "fixture" else 0.0
        self.stopped = False

    def reserve(self, projected: float) -> bool:
        """True when `projected` fits under the cap; the estimate is bumped by it. Once it does not fit, nothing else is issued."""
        if self.stopped:
            return False
        if self.cap is not None and self.estimate + projected > self.cap + 1e-9:
            self.stopped = True
            log.warning("budget: estimate %s + %s would exceed cap %s for %s; stopping further calls",
                        costs.fmt(self.estimate), costs.fmt(projected), costs.fmt(self.cap), self.provider)
            return False
        self.estimate += projected
        return True

    def settle(self, projected: float, actual: float) -> None:
        self.estimate += actual - projected

    def label(self) -> str:
        return costs.fmt(self.cap) if self.cap is not None else "none"


# ---------------------------------------------------------------------------------------------------------------------
# Stage: crawl
# ---------------------------------------------------------------------------------------------------------------------

def run_crawl(conn: sqlite3.Connection, run_id: int, source, seed_handle: str, max_following: int = 2000,
              max_followers: int = 2000) -> dict:
    """Seed graph: the seed's following and followers -> x_users / x_follows -> candidates (discovered_via='seed').
    Writes are batched (executemany, one commit), so lists of thousands of users are fine."""
    seed_handle = norm_handle(seed_handle)
    if not seed_handle:
        raise ValueError("run_crawl needs a seed handle (SEED_X_HANDLE in .env)")
    now = db.utcnow()
    stats: dict = {"seed": seed_handle, "following": 0, "followers": 0, "users": 0, "candidates_new": 0,
                   "candidates_updated": 0, "by_hint": Counter(), "skipped": Counter(), "parody_skipped": [],
                   "estimated_cost_usd": 0.0, "cost_usd": 0.0}
    projected = (estimate_cost(source, "follow_read", max_following) + estimate_cost(source, "follow_read", max_followers)
                 + estimate_cost(source, "user_read", 1))
    stats["estimated_cost_usd"] = round(projected, 4)
    print(f"[cost] {source.name} crawl @{seed_handle}: ~{costs.fmt(projected)} for up to {max_following + max_followers} "
          f"follow reads + 1 profile")
    seed = source.get_user_by_handle(seed_handle)
    if seed is None:
        raise RuntimeError(f"seed handle @{seed_handle} not found by source {source.name}")
    persist_users_batch(conn, [seed], source.name, now)
    conn.commit()
    log.info("crawl: seed @%s (x_id %s), fetching following (<=%d) and followers (<=%d)", seed.handle, seed.x_id,
             max_following, max_followers)

    # handles, not ids: monid maps an id to a handle only through x_users, and caches follow lists by handle
    following = [u for u in source.get_following(seed.handle, max_users=max_following) if u.x_id != seed.x_id]
    stats["following"] = persist_follows_batch(conn, seed.x_id, following, "following", source.name, now)
    conn.commit()
    followers = [u for u in source.get_followers(seed.handle, max_users=max_followers) if u.x_id != seed.x_id]
    stats["followers"] = persist_follows_batch(conn, seed.x_id, followers, "followers", source.name, now)
    conn.commit()

    following_ids = {u.x_id for u in following}
    follower_ids = {u.x_id for u in followers}
    mutual = following_ids & follower_ids
    seen: dict[str, XUser] = {}
    for u in following + followers:
        seen.setdefault(u.x_id, u)
    stats["users"] = len(seen)
    rel = {x: "seed:mutual" if x in mutual else ("seed:following" if x in following_ids else "seed:follower") for x in seen}
    entries = _screen_users(list(seen.values()), "seed", rel, mutual, seed.x_id, stats)
    stats["candidates_new"], stats["candidates_updated"] = bulk_upsert_candidates(conn, entries, now)
    conn.commit()
    stats["by_hint"], stats["skipped"] = dict(stats["by_hint"]), dict(stats["skipped"])
    stats["cost_usd"] = costs.spent(conn, source.name, run_id)
    log.info("crawl done: %d following, %d followers, %d distinct users (%d mutual) -> %d candidates (%d new, %d refreshed); "
             "by hint %s; skipped %s", stats["following"], stats["followers"], stats["users"], len(mutual), len(entries),
             stats["candidates_new"], stats["candidates_updated"], stats["by_hint"], stats["skipped"])
    return stats


# ---------------------------------------------------------------------------------------------------------------------
# Stage: discover
# ---------------------------------------------------------------------------------------------------------------------

QUERY_GROUPS = ("topics", "competitor_products", "wearable_wishlist", "authors", "evidence")   # queries.yaml search groups, in run order


def load_queries(queries_file: str | Path = config.QUERIES_FILE) -> dict:
    cfg = yaml.safe_load(Path(queries_file).read_text()) or {}
    for k in (*QUERY_GROUPS, "hubs"):
        cfg[k] = cfg.get(k) or []
    limits = dict(cfg.get("limits") or {})
    limits.setdefault("posts_per_query", 100)
    limits.setdefault("users_per_hub", 1000)
    limits.setdefault("min_likes", 20)
    cfg["limits"] = limits
    return cfg


def seed_x_id(conn, seed_handle: str | None = None) -> str | None:
    h = norm_handle(seed_handle) or config.seed_handle()
    if not h:
        return None
    row = db.one(conn, "SELECT x_id FROM x_users WHERE handle=?", (h,))
    return row["x_id"] if row else None


def _run_started_at(conn, run_id) -> str | None:
    row = db.one(conn, "SELECT started_at FROM runs WHERE run_id=?", (run_id,))
    return row["started_at"] if row else None


def _cached_query_posts(conn, query: str, since_ts: str | None) -> list[dict] | None:
    """Posts this query already bought in this run (fetched after the run started): reused instead of re-bought."""
    if not since_ts:
        return None
    rows = db.rows(conn, "SELECT post_id, x_id, handle, likes, text, query FROM x_posts WHERE query=? AND fetched_at>=?", (query, since_ts))
    return rows or None


def _post_row(p: XPost) -> dict:
    return {"post_id": p.post_id, "x_id": p.x_id, "handle": p.handle, "likes": p.likes or 0, "text": p.text, "query": p.query}


def run_discover(conn: sqlite3.Connection, run_id: int, source, queries_file: str | Path = config.QUERIES_FILE,
                 budget_usd: float | None = None, seed_handle: str | None = None,
                 groups: tuple[str, ...] | list[str] | None = None) -> dict:
    """Topic queries -> posts -> people. Then hub following lists. Returns counts and the cost estimate/ledger for this run.
    `groups` limits the run to some queries.yaml groups (e.g. ('authors',)) and skips the hubs; None runs everything."""
    cfg = load_queries(queries_file)
    limits = cfg["limits"]
    per_query = int(limits["posts_per_query"])
    per_hub = int(limits["users_per_hub"])
    min_likes = int(limits["min_likes"])
    now = db.utcnow()
    sid = seed_x_id(conn, seed_handle)
    started_at = _run_started_at(conn, run_id)
    budget = _Budget(conn, source, run_id, budget_usd)
    stats: dict = {"queries_run": 0, "queries_cached": 0, "queries_skipped_budget": 0, "queries_failed": 0, "posts": 0,
                   "posts_dropped": 0, "authors": 0, "authors_looked_up": 0, "candidates_new": 0, "candidates_updated": 0,
                   "hubs_crawled": 0, "hubs_skipped": 0, "by_hint": Counter(), "skipped": Counter(), "parody_skipped": [],
                   "estimated_cost_usd": 0.0, "cost_usd": 0.0}

    if groups is not None:
        unknown = [g for g in groups if g not in QUERY_GROUPS]
        if unknown:
            raise ValueError(f"unknown query group(s) {unknown}; known: {', '.join(QUERY_GROUPS)}")
    queries = [(group, q) for group in (groups or QUERY_GROUPS) for q in cfg.get(group) or []]
    hubs = [norm_handle(h) for h in cfg.get("hubs") or [] if h] if groups is None else []
    projected_total = (estimate_cost(source, "post_read", per_query, len(queries))
                       + estimate_cost(source, "follow_read", per_hub, len(hubs)))
    print(f"[cost] {source.name} discover: ~{costs.fmt(projected_total)} for {len(queries)} queries x {per_query} posts"
          f" + {len(hubs)} hubs x {per_hub} follows (cap {budget.label()})")

    # 1) queries -> posts, aggregated by author
    by_author: dict[str, dict] = defaultdict(lambda: {"posts": {}, "queries": defaultdict(int), "handle": None})
    for group, q in queries:
        cached = _cached_query_posts(conn, q, started_at)
        if cached is not None:
            rows, stats["queries_cached"] = cached, stats["queries_cached"] + 1
        else:
            projected = estimate_cost(source, "post_read", per_query)
            if not budget.reserve(projected):
                stats["queries_skipped_budget"] += 1
                continue
            log.info("discover: [%s] %s", group, q)
            try:
                fetched = source.search_posts(q, max_results=per_query)
            except costs.BudgetExceeded as e:
                log.warning("discover: adapter budget guard stopped the queries: %s", e)
                budget.stopped = True
                stats["queries_skipped_budget"] += 1
                continue
            except Exception as e:   # one failing query must not lose the others
                log.warning("discover: query failed, skipped: %s (%s)", q, e)
                stats["queries_failed"] += 1
                continue
            budget.settle(projected, estimate_cost(source, "post_read", len(fetched)))
            kept = []
            for p in fetched:
                if (sid and p.x_id == sid) or is_retweet(p) or not looks_english(p):
                    stats["posts_dropped"] += 1
                    continue
                p.query = q
                kept.append(p)
            persist_posts(conn, kept, source.name)
            stats["queries_run"] += 1
            rows = [_post_row(p) for p in kept]
        for r in rows:
            if sid and r["x_id"] == sid:
                continue
            a = by_author[r["x_id"]]
            a["posts"][r["post_id"]] = r
            a["queries"][q] += 1
            a["handle"] = a["handle"] or r["handle"]
        stats["posts"] += len(rows)
    stats["authors"] = len(by_author)

    # 2) profiles for authors we do not have yet (bio heuristics need them); skipped when over budget.
    #    monid caches every tweet author with a full profile, so this is usually served locally for free.
    known = {r["x_id"] for r in db.rows(conn, "SELECT x_id FROM x_users WHERE bio IS NOT NULL OR followers IS NOT NULL")}
    missing = sorted({a["handle"] for x, a in by_author.items() if x not in known and a["handle"]})
    if missing:
        projected = estimate_cost(source, "user_read", len(missing))
        if budget.reserve(projected):
            print(f"[cost] {source.name} users lookup: up to ~{costs.fmt(projected)} for {len(missing)} users (cached ones are free)")
            fetched_users: list[XUser] = []
            for i in range(0, len(missing), 100):
                try:
                    fetched_users.extend(source.get_users_by_handles(missing[i:i + 100]))
                except costs.BudgetExceeded as e:
                    log.warning("discover: adapter budget guard stopped the author lookups: %s", e)
                    break
            budget.settle(projected, estimate_cost(source, "user_read", len(fetched_users)))
            persist_users_batch(conn, fetched_users, source.name, now)
            conn.commit()
            stats["authors_looked_up"] = len(fetched_users)
        else:
            log.warning("discover: %d author profiles not fetched (budget); candidates from post counts only", len(missing))

    # 3) people, not posts
    users = {r["x_id"]: r for r in db.rows(conn, "SELECT x_id, handle, name, bio, followers, verified_type FROM x_users")}
    entries = []
    for x_id, a in by_author.items():
        u = users.get(x_id) or {}
        name, bio, handle = u.get("name"), u.get("bio"), u.get("handle") or a["handle"]
        if looks_parody(name, bio):
            stats["skipped"]["parody"] += 1
            stats["parody_skipped"].append(handle)
            continue
        if looks_org(name, bio, u.get("verified_type")):
            stats["skipped"]["org"] += 1
            continue
        if looks_spam(name, bio):
            stats["skipped"]["spam"] += 1
            continue
        n_posts = len(a["posts"])
        top_likes = max((p["likes"] or 0) for p in a["posts"].values())
        role, kws = bio_hint(name, bio, u.get("followers"))
        reasons = []
        if n_posts >= 2:
            reasons.append(f"posts:{n_posts} topical, top {top_likes} likes")
        elif n_posts == 1 and top_likes >= min_likes:
            reasons.append(f"posts:1 topical with {top_likes} likes")
        if role:
            reasons.append(_hint_note(role, kws))
        if not reasons:
            stats["skipped"]["too_few_posts"] += 1
            continue
        stats["by_hint"][role or "none"] += 1
        best_q = max(a["queries"], key=a["queries"].get)
        entries.append((x_id, handle, name, f"query:{best_q}", " | ".join(reasons)))
    created, updated = bulk_upsert_candidates(conn, entries, now)
    stats["candidates_new"] += created
    stats["candidates_updated"] += updated
    conn.commit()

    # 4) hubs: following lists of verified hub accounts from queries.yaml
    for hub in hubs:
        hub_user = db.one(conn, "SELECT x_id, handle FROM x_users WHERE handle=?", (hub,))
        hub_x_id = hub_user["x_id"] if hub_user else None
        if hub_x_id and db.one(conn, "SELECT 1 AS ok FROM x_follows WHERE src_x_id=? AND fetched_at>=? LIMIT 1", (hub_x_id, started_at or "")):
            log.info("discover: hub @%s following already fetched this run, reusing", hub)
            following = [XUser(**{k: r[k] for k in ("x_id", "handle", "name", "bio", "location", "followers", "following",
                                                   "verified", "verified_type", "created_at", "can_dm")})
                         for r in db.rows(conn, "SELECT u.* FROM x_follows f JOIN x_users u ON u.x_id=f.dst_x_id WHERE f.src_x_id=?", (hub_x_id,))]
        else:
            projected = estimate_cost(source, "follow_read", per_hub) + (0 if hub_x_id else estimate_cost(source, "user_read", 1))
            if not budget.reserve(projected):
                stats["hubs_skipped"] += 1
                continue
            try:
                hu = source.get_user_by_handle(hub)
                if hu is None:
                    log.warning("discover: hub @%s not found; check config/queries.yaml (hubs must be real handles)", hub)
                    stats["hubs_skipped"] += 1
                    budget.settle(projected, 0.0)
                    continue
                persist_users_batch(conn, [hu], source.name, now)
                following = list(source.get_following(hu.handle, max_users=per_hub))
            except costs.BudgetExceeded as e:
                log.warning("discover: adapter budget guard stopped the hub crawl: %s", e)
                stats["hubs_skipped"] += 1
                break
            except Exception as e:
                log.warning("discover: hub @%s failed, skipped: %s", hub, e)
                stats["hubs_skipped"] += 1
                continue
            budget.settle(projected, estimate_cost(source, "follow_read", len(following)))
            persist_follows_batch(conn, hu.x_id, following, "following", source.name, now)
        hub_entries = _screen_users(following, f"following:{hub}", None, set(), sid, stats)
        created, updated = bulk_upsert_candidates(conn, hub_entries, now)
        stats["candidates_new"] += created
        stats["candidates_updated"] += updated
        stats["hubs_crawled"] += 1
        conn.commit()

    stats["by_hint"], stats["skipped"] = dict(stats["by_hint"]), dict(stats["skipped"])
    stats["estimated_cost_usd"] = round(budget.estimate, 4)
    stats["cost_usd"] = costs.spent(conn, source.name, run_id)
    log.info("discover done: %d queries (%d cached, %d skipped for budget, %d failed), %d posts, %d authors, %d new candidates, "
             "%d refreshed, %d hubs; by hint %s; skipped %s", stats["queries_run"], stats["queries_cached"],
             stats["queries_skipped_budget"], stats["queries_failed"], stats["posts"], stats["authors"], stats["candidates_new"],
             stats["candidates_updated"], stats["hubs_crawled"], stats["by_hint"], stats["skipped"])
    return stats


# ---------------------------------------------------------------------------------------------------------------------
# Signals and priority (shared with classify)
# ---------------------------------------------------------------------------------------------------------------------

def account_age_years(created_at: str | None, today: str | None = None) -> float | None:
    if not created_at:
        return None
    try:
        y, m, d = int(created_at[:4]), int(created_at[5:7]), int(created_at[8:10])
    except ValueError:
        return None
    today = today or db.utcnow()
    ty, tm, td = int(today[:4]), int(today[5:7]), int(today[8:10])
    return round((ty - y) + (tm - m) / 12 + (td - d) / 365, 2)


def _engagement(likes, reposts, replies) -> int:
    return (likes or 0) + 2 * (reposts or 0) + (replies or 0)


def candidate_signals(conn: sqlite3.Connection, candidate_id: int, seed_handle: str | None = None) -> dict:
    """Counts the scorer, classifier and dashboard can use without an LLM."""
    c = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,))
    if not c:
        raise KeyError(f"candidate {candidate_id} not found")
    u = db.one(conn, "SELECT * FROM x_users WHERE x_id=?", (c["x_id"],)) if c["x_id"] else None
    u = u or {}
    posts = db.rows(conn, "SELECT post_id, text, likes, reposts, replies, quotes, query FROM x_posts WHERE x_id=?", (c["x_id"],)) if c["x_id"] else []
    topical = [p for p in posts if post_is_topical(p["query"], p["text"])]
    role, kws = bio_hint(u.get("name") or c["name"], u.get("bio"), u.get("followers"))
    sid = seed_x_id(conn, seed_handle)
    seed_follows = follows_seed = False
    if sid and c["x_id"]:
        seed_follows = bool(db.one(conn, "SELECT 1 AS ok FROM x_follows WHERE src_x_id=? AND dst_x_id=?", (sid, c["x_id"])))
        follows_seed = bool(db.one(conn, "SELECT 1 AS ok FROM x_follows WHERE src_x_id=? AND dst_x_id=?", (c["x_id"], sid)))
    sig = {
        "candidate_id": candidate_id, "handle": c["handle"], "x_id": c["x_id"],
        "posts": len(posts), "topical_posts": len(topical),
        "engagement": sum(_engagement(p["likes"], p["reposts"], p["replies"]) for p in topical),
        "max_likes": max([p["likes"] or 0 for p in topical], default=0),
        "bio_hint": role, "bio_keywords": kws,
        "followers": u.get("followers"), "verified": u.get("verified"), "verified_type": u.get("verified_type"),
        "account_age_years": account_age_years(u.get("created_at")),
        "can_dm": u.get("can_dm"), "location": u.get("location"),
        "seed_follows_candidate": seed_follows, "candidate_follows_seed": follows_seed,
        "mutual_follow_with_seed": seed_follows and follows_seed,
        "bio_on_topic": is_topical(u.get("bio")),
        "parody_words": looks_parody(u.get("name"), u.get("bio")),
        "map_person_id": c["map_person_id"], "discovered_via": c["discovered_via"],
        "enriched_on": enriched_on(c["notes"]), "cofollows": cofollows(c["notes"]),
    }
    sig["priority"] = priority(sig, c["handle_verified"])
    return sig


# Priority of a candidate for enrichment and classification (higher first). Both stages spend (money or LLM time) on
# the same people first. Topical activity first, then the bio, with a map link only a small hint; the audience is deliberately
# weak so a celebrity with a huge following and no topical post does not float to the top (famous is not a reason):
#   priority = 4.0 * min(topical_posts, 5)        posts a topic query surfaced, or on-topic text (five is plenty)
#            + 1.5 * log10(1 + engagement)         likes + 2*reposts + replies on those topical posts
#            + 2.0 * map_link                      in the operator's map: a hint he would accept, not a sign of quality (the operator,
#                                                  29 Sep: wearables may go outside the map; half a topical post)
#            + 2.0 * mutual_follow_with_seed       the operator and they follow each other
#            + 2.0 * bio_on_topic                  the bio itself mentions wearables, wearables, voice, agents, screen-free...
#            + 1.0 * bio_hint                      the bio reads like a VC, founder, journalist or reviewer
#            + 0.5 * log10(1 + followers)          audience (10M followers: +3.5, i.e. less than one topical post)
#            + 0.5 * verified                      X verification badge
#            + 4.0 * min(cofollows, 5)             seeds (our best people) who follow them (expand stage, note
#                                                  'cofollow:k/n'): the network vouches like topical posts would
#            - 20  * suspicious                    parody words in name/bio, or handle_verified='suspicious'
# Examples: a reviewer with 3 topical posts (~3.6k engagement), 48k followers, verified, mutual follow, bio on topic
# -> about 25; a 10M-follower verified mutual with a founder bio and no topical post -> about 7; one topical post with
# 50 likes from a 2k-follower account -> about 8.
PRIORITY_WEIGHTS = {"topical": 4.0, "topical_cap": 5, "engagement": 1.5, "map": 2.0, "mutual": 2.0, "bio_on_topic": 2.0,
                    "bio_hint": 1.0, "followers": 0.5, "verified": 0.5, "cofollow": 4.0, "cofollow_cap": 5,
                    "suspicious": -20.0}
_COFOLLOW_RE = re.compile(r"(?:^|\| )cofollow:(\d+)/(\d+)")


def cofollows(notes: str | None) -> int:
    """Seeds following this candidate, from the expand stage's 'cofollow:k/n' note (0 when absent)."""
    m = _COFOLLOW_RE.search(notes or "")
    return int(m.group(1)) if m else 0


def priority(sig: dict, handle_verified: str | None = None) -> float:
    w = PRIORITY_WEIGHTS
    p = (w["topical"] * min(sig.get("topical_posts") or 0, w["topical_cap"])
         + w["engagement"] * math.log10(1 + max(0, sig.get("engagement") or 0))
         + w["map"] * (1 if sig.get("map_person_id") else 0)
         + w["mutual"] * (1 if sig.get("mutual_follow_with_seed") else 0)
         + w["bio_on_topic"] * (1 if sig.get("bio_on_topic") else 0)
         + w["bio_hint"] * (1 if sig.get("bio_hint") else 0)
         + w["followers"] * math.log10(1 + max(0, sig.get("followers") or 0))
         + w["verified"] * (1 if sig.get("verified") else 0)
         + w["cofollow"] * min(sig.get("cofollows") or 0, w["cofollow_cap"]))
    if sig.get("parody_words") or handle_verified == "suspicious":
        p += w["suspicious"]
    return round(p, 3)


def candidate_priorities(conn: sqlite3.Connection, candidate_ids: list[int] | None = None,
                         seed_handle: str | None = None) -> list[dict]:
    """Signals + priority for every candidate with an X account (or only `candidate_ids`), highest priority first.
    Bulk queries (not one per candidate), so it stays fast with thousands of candidates. Each item: candidate_id, x_id,
    handle, posts, topical_posts, engagement, followers, verified, map_person_id, mutual_follow_with_seed, bio_hint,
    parody_words, handle_verified, category, enriched_on, last_post_fetched_at, priority."""
    cands = db.rows(conn, """
        SELECT c.candidate_id, c.x_id, c.handle, c.name AS c_name, c.map_person_id, c.notes, c.handle_verified, c.category,
               u.name, u.bio, u.location, u.url, u.followers, u.verified, u.verified_type, u.handle AS u_handle
        FROM candidates c LEFT JOIN x_users u ON u.x_id = c.x_id
        WHERE c.x_id IS NOT NULL""")
    if candidate_ids is not None:
        wanted = {int(i) for i in candidate_ids}
        cands = [c for c in cands if c["candidate_id"] in wanted]
    if not cands:
        return []
    agg: dict[str, dict] = defaultdict(lambda: {"posts": 0, "topical": 0, "eng": 0, "last": ""})
    for p in db.rows(conn, """SELECT p.x_id, p.text, p.likes, p.reposts, p.replies, p.query, p.fetched_at
                              FROM x_posts p JOIN candidates c ON c.x_id = p.x_id"""):
        a = agg[p["x_id"]]
        a["posts"] += 1
        a["last"] = max(a["last"], p["fetched_at"] or "")
        if post_is_topical(p["query"], p["text"]):
            a["topical"] += 1
            a["eng"] += _engagement(p["likes"], p["reposts"], p["replies"])
    sid = seed_x_id(conn, seed_handle)
    seed_following = seed_followers = set()
    if sid:
        seed_following = {r[0] for r in conn.execute("SELECT dst_x_id FROM x_follows WHERE src_x_id=?", (sid,))}
        seed_followers = {r[0] for r in conn.execute("SELECT src_x_id FROM x_follows WHERE dst_x_id=?", (sid,))}
    out = []
    for c in cands:
        a = agg.get(c["x_id"]) or {"posts": 0, "topical": 0, "eng": 0, "last": ""}
        role, _ = bio_hint(c["name"] or c["c_name"], c["bio"], c["followers"])
        sig = {"candidate_id": c["candidate_id"], "x_id": c["x_id"], "handle": c["handle"] or c["u_handle"],
               "posts": a["posts"], "topical_posts": a["topical"], "engagement": a["eng"],
               "followers": c["followers"], "verified": c["verified"], "map_person_id": c["map_person_id"],
               "mutual_follow_with_seed": c["x_id"] in seed_following and c["x_id"] in seed_followers,
               "bio_hint": role, "bio_on_topic": is_topical(c["bio"]), "has_bio": bool((c["bio"] or "").strip()),
               "parody_words": looks_parody(c["name"], c["bio"]),
               "handle_verified": c["handle_verified"], "category": c["category"], "notes": c["notes"],
               "name": c["name"] or c["c_name"], "bio": c["bio"], "location": c["location"], "url": c["url"],
               "verified_type": c["verified_type"], "enriched_on": enriched_on(c["notes"]), "last_post_fetched_at": a["last"] or None,
               "cofollows": cofollows(c["notes"])}
        sig["priority"] = priority(sig, c["handle_verified"])
        out.append(sig)
    out.sort(key=lambda s: (-s["priority"], s["candidate_id"]))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Stage: enrich (their own words, not only search hits)
# ---------------------------------------------------------------------------------------------------------------------

_ENRICHED_RE = re.compile(r"enriched:(\d{4}-\d{2}-\d{2})")


def enriched_on(notes: str | None) -> str | None:
    """Date (YYYY-MM-DD) of the last enrichment recorded in candidates.notes, or None."""
    m = _ENRICHED_RE.search(notes or "")
    return m.group(1) if m else None


# ---- gates on PAID enrichment (classification is not gated: these people are still classified and flagged) ----
ENRICH_MIN_FOLLOWERS = 1500       # below this, no paid posts (people in the operator's map are exempt: a real path exists)

# Competitors' own X handles and domains (brand accounts, and employers named in bios). Handles as listed by the lead.
COMPETITOR_HANDLES = {"ouraring": "Oura", "ultrahumanhq": "Ultrahuman", "rabbit_hmi": "Rabbit", "wisprflow": "Wispr Flow",
                      "sesame": "Sesame", "omi": "Omi", "limitlessai": "Limitless", "plaudai": "Plaud", "bee": "Bee",
                      "friend": "Friend"}
COMPETITOR_DOMAINS = {"wisprflow.ai": "Wispr Flow"}
_COMP_NAME_RES = {name: re.escape(name.lower()).replace(r"\ ", r"[\s_-]?") for name in config.COMPETITORS}
_COMP_HANDLE_RE = re.compile(r"@(" + "|".join(map(re.escape, COMPETITOR_HANDLES)) + r")(?![a-z0-9_])")
_COMP_DOMAIN_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, COMPETITOR_DOMAINS)) + r")(?![a-z0-9])")
_ROLE_BEFORE = (r"(?:co-?founder|founder|founding \w+|ceo|cto|coo|cpo|cmo|cfo|chief \w+ officer|president|head of [\w ]{1,25}?|"
                r"vp(?: of)?(?: \w+)?|director(?: of [\w ]{1,25}?)?|lead|engineer(?:ing)?|designer|design|product(?: \w+)?|pm|"
                r"research(?:er)?|scientist|advisor|adviser|investor|ambassador|partner|intern|staff|team|building|working|work)")
_COMP_ROLE_RE = re.compile(rf"(?<![a-z0-9]){_ROLE_BEFORE}\s*(?:(?:at|@|of|for|with|on|in)\s*)?(?:the\s+)?"
                           rf"({'|'.join(_COMP_NAME_RES.values())})(?![a-z0-9])")
_WE_RE = re.compile(r"(?<![a-z0-9])(we|we're|we’re|our|ours)(?![a-z0-9])")
_FIRST_PERSON_RE = re.compile(r"(?<![a-z0-9])(i|i'm|i’m|i've|my|me)(?![a-z0-9])")
_OUTLET_RE = kw_regex(["news", "rumors", "rumours", "headlines", "deals", "breaking", "latest", "coverage"])
_PERSON_ROLE_RE = kw_regex(["journalist", "reporter", "editor", "writer", "columnist", "correspondent", "host", "founder",
                            "co-founder", "cofounder", "ceo", "partner", "investor", "engineer", "designer", "author", "dad",
                            "mom", "father", "mother", "husband", "wife", "he/him", "she/her", "they/them", "phd", "professor"])
_ORG_NAME_RE = kw_regex(["ventures", "capital", "partners", "labs", "fund", "combinator", "accelerator", "inc", "inc.", "llc",
                         "ltd", "hq", "media", "news", "magazine", "official", "studio", "studios", "agency", "group",
                         "foundation", "institute"])
BRAND_HANDLE_SUFFIXES = ("hq", "ai", "official", "app", "inc", "labs", "news", "media", "pr")


def _norm_brand(s: str | None) -> str:
    n = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    for suffix in ("official", "inc", "hq", "ai", "app", "wearable", "labs"):
        if n.endswith(suffix) and len(n) > len(suffix) + 1:
            return n[:-len(suffix)]
    return n


_COMP_BRANDS = {_norm_brand(c): c for c in config.COMPETITORS}


def competitor_brand_account(handle: str | None, name: str | None) -> str | None:
    """The competitor whose own brand account this is (@ouraring, @plaudai, a display name 'Ultrahuman'), else None."""
    h = norm_handle(handle) or ""
    if h in COMPETITOR_HANDLES:
        return COMPETITOR_HANDLES[h]
    return _COMP_BRANDS.get(_norm_brand(h)) or _COMP_BRANDS.get(_norm_brand(name))


def competitor_role(bio: str | None) -> str | None:
    """The competitor a bio states a role at ('founder and ceo @rabbit_hmi', 'CEO at wisprflow.ai', 'head of design
    @sesame', 'product lead at Oura'), else None. A competitor's handle or domain in a bio counts as the employer; a bare
    product name needs a role word right before it, so 'Oura on one hand, Ultrahuman on the other' is not a tie."""
    low = (bio or "").lower()
    m = _COMP_HANDLE_RE.search(low)
    if m:
        return COMPETITOR_HANDLES[m.group(1)]
    m = _COMP_DOMAIN_RE.search(low)
    if m:
        return COMPETITOR_DOMAINS[m.group(1)]
    m = _COMP_ROLE_RE.search(low)
    if m:
        hit = m.group(1)
        return next((name for name, rx in _COMP_NAME_RES.items() if re.fullmatch(rx, hit)), hit)
    return None


def looks_brand_account(name: str | None, handle: str | None, bio: str | None, verified_type: str | None = None) -> bool:
    """Organisation rather than a person, stricter than looks_org (used before paying for posts): looks_org; or a bio that
    speaks only as 'we'; or a brand handle (@acmehq, @fooai, @barofficial) with a one-word name or a 'we' bio; or an
    outlet bio ('news', 'rumors', 'deals', 'latest'); or an organisation word in the name (Ventures, Capital, Labs, Media,
    'AI' last); the last four only when the bio has no first person and no personal role (editor, founder, dad...)."""
    if looks_org(name, bio, verified_type):
        return True
    low = (bio or "").lower()
    if _FIRST_PERSON_RE.search(low) or _PERSON_ROLE_RE.search(low):
        return False
    we = bool(_WE_RE.search(low))
    if we:
        return True
    h = norm_handle(handle) or ""
    one_word = " " not in (name or "").strip()
    if any(h.endswith(suf) and len(h) - len(suf) >= 3 for suf in BRAND_HANDLE_SUFFIXES) and one_word:
        return True
    if _OUTLET_RE.search(low):
        return True
    nm = (name or "").strip().lower()
    return bool(_ORG_NAME_RE.search(nm) or re.search(r"\sai$", nm))


def enrich_gate(sig: dict) -> str | None:
    """Why a candidate gets no PAID enrichment, or None when eligible. Reasons, in order: 'suspicious' (parody words or
    handle_verified='suspicious'); 'competitor_brand' (a competitor's own brand account: not a person); 'org'
    (looks_brand_account); 'small_audience' (fewer than ENRICH_MIN_FOLLOWERS followers, unless the person is in the operator's
    map; unknown stays eligible); 'paid_promo' (sells_posts); then red_flag's kinds: 'crypto', 'paid_promo', 'feed', 'org'
    and 'anonymous'. Where someone lives and a tie to a competitor gate nothing here (Federico, 30 Sep):
    competitors are flagged, never removed: their people get evidence like anyone else and carry a competitor:* flag;
    the US only filter must be usable: with it off, non-US people who meet the bar show, so they need evidence too."""
    name, bio, handle = sig.get("name"), sig.get("bio"), sig.get("handle")
    if sig.get("parody_words") or sig.get("handle_verified") == "suspicious":
        return "suspicious"
    if competitor_brand_account(handle, name):
        return "competitor_brand"
    if looks_brand_account(name, handle, bio, sig.get("verified_type")):
        return "org"
    # No competitor-role or location gate (see above): classify reads both from the profile and the posts bought here,
    # and score turns them into competitor:* and not_us flags, which the dashboard's filters hide or show.
    followers = sig.get("followers")
    if followers is not None and followers < ENRICH_MIN_FOLLOWERS and not sig.get("map_person_id"):
        return "small_audience"
    # No reachability gate: the operator (29 Sep) sends wearables outside his map too, so a big on-topic creator with no path
    # (@mkbhd) is enriched like anyone else. Paths are computed later to show how to approach, not to exclude.
    # Accounts selling posts ("DM for paid partnership") would say yes for a fee, which is not the love the wearable needs.
    if sells_posts(bio, sig.get("location")):
        return "paid_promo"
    # The QA's repeat rejects (red_flag): crypto personas, AI-news feeds and anonymous accounts get their own reason; promo
    # and brand accounts share 'paid_promo' and 'org' with the checks above.
    flag = red_flag(name, handle, bio, sig.get("location"), sig.get("url"), in_map=bool(sig.get("map_person_id")))
    if flag:
        return {"promo": "paid_promo", "brand": "org"}.get(flag[0], flag[0])
    return None


PAID_PROMO_RE = re.compile(
    r"(paid (partnership|promo)|dm for (promo|promotion|collab|paid|ads?)\b|for promotion|promotion(s)? (dm|contact)|"
    r"sponsored posts?|shoutouts?\b|dm for business|growth specialist|get massive exposure|"
    # QA of run 14: collab calls to action in the bio or the location field ("Collabs & partnerships 📩", "📩 DM for Collabs",
    # "Open to collabs", "Free Products + Sponsorships →"). A job title such as "Collab Manager" has no call to action.
    r"(dms?|e-?mail|mail|📩|📧|✉️|inbox|open|contact)[^.|•\n]{0,20}(collab|partnership|sponsor)|"
    r"(collabs?|collaborations?|partnerships?|sponsorships?)\s*[^\w\s]{0,3}\s*(📩|📧|✉️|→|:|dm\b|via dm|e-?mail)|"
    r"sponsorships?\b|free products)", re.I)


def sells_posts(bio: str | None, location: str | None = None) -> str | None:
    """The phrase showing the account sells posts or collabs, from the bio or the location field (creators put the call to
    action there: '📩 DM for Collabs'), else None."""
    for text in (bio, location):
        m = PAID_PROMO_RE.search(text or "")
        if m:
            return m.group(0).strip()
    return None


# Persona guards. Three hand reviews (docs/QA_TOP.md, runs 14, 30 and 39) rejected more than half of each batch of new
# top-100 entrants, most of them crypto personas, anonymous accounts, AI-news or AI-tools feeds, brands or shops, and
# accounts selling posts that sells_posts missed. red_flag turns those reasons into profile-only rules, so the daily run
# stops pulling them back in: enrich_gate pays for none of them, and classify files them as 'other' with a wear and an
# accept row against (reapply_guards applies that to stored classifications with no LLM call). Each rule was measured on
# the 205 people reviewed in runs 14, 30, 39 and 40 (tests/samples/qa_reviewed.json): no keep or fix is hit.
CRYPTO_WORDS = ["crypto", "web3", "web 3", "defi", "depin", "onchain", "on-chain", "hodl", "hodler", "dyor", "degen", "degens",
                "memecoin", "memecoins", "meme coin", "airdrop", "airdrops", "altcoin", "altcoins", "bitcoin", "btc", "ethereum",
                "solana", "tokenomics", "wagmi", "alpha calls", "alpha caller", "nft", "nfts", "binance", "okx", "bybit",
                "kucoin", "bitget", "mexc", "token2049", "stablecoin", "stablecoins"]
_CRYPTO_RE = kw_regex(CRYPTO_WORDS)
# Case-sensitive: 'Dao' and 'Kol' are also names, 'nfa' is rare in lower case.
_CRYPTO_CAPS_RE = re.compile(r"\b(DAOs?|NFA|KOL|DEX|CEX)\b")
_CASHTAG_RE = re.compile(r"(?<![\w$])\$([a-z]{2,6})(?![\w])", re.I)               # $BTC, $SOL, $ZEC (not $70k, $$$)
# Coin tickers count always; any other cashtag only in a bio with no real-person marker ('Investor in $SPCX (pre-IPO)' under
# 'Founder of ...' is a founder, not a crypto persona).
COIN_TICKERS = {"btc", "eth", "sol", "xrp", "doge", "ada", "bnb", "avax", "matic", "pol", "dot", "link", "zec", "xmr", "ltc",
                "pepe", "shib", "bonk", "wif", "sui", "apt", "arb", "op", "ton", "trx", "atom", "near", "tao", "hype", "ena",
                "ondo", "jup", "sei", "tia", "inj", "fet", "usdt", "usdc", "kas", "hbar", "algo", "icp", "stx", "sc"}
_CRYPTO_MARK_RE = re.compile(r"(\.eth\b|@\w*(dao|_eth)\b|\b0x[0-9a-f]{8,})", re.I)  # vitalik.eth, @_BubbleDAO, a contract address
_CRYPTO_HANDLE_RE = re.compile(r"(_eth$|xbt$|^0x|crypto|100x|1000x|x100)", re.I)   # @ridark_eth, @kellenxbt, @0x_jadex, @coinmaster100x
# Selling posts, promotion or collabs beyond sells_posts: the reviewers' phrases (run 30: 'influencer marketing',
# 'available for projects', 'for partnerships', 'growth for ... brands', 'ambassador @'; run 39: 'Join 600+ marketing
# teams', 'powering brands', '#ad'), agencies selling growth to brands, giveaways and codes.
PROMO_RE = re.compile(
    r"(influencer marketing|available for (projects|work|hire|collabs?|promos?)|for (partnerships?|collabs?|sponsorships?|promotions?)\b|"
    r"ambassador\s*(@|at\b|for\b|of\b)|\bgrowth (engines? |strateg\w+ )?for\b|(powering|helping|scaling|growing)\s+(\w+\s+){0,2}brands\b|"
    r"\d+\+?\s+(ecom\s+|e-?commerce\s+)?brands\b|join\s+[\d,.]+[km]?\+?\s+(brands|agencies|marketing|marketers|businesses)|"
    r"scale your (ecom|brand|business|store)|(running|owner of|founder of|ceo of)\s+(\S+\s+){0,5}?\S*\s*agenc(y|ies)\b|"
    r"\b(cro|marketing|growth|ads|ecom|creative|seo|ai-powered|smma)\s+agenc(y|ies)\b|clients( include|\s*:)|ghost-?writ(er|ing)|"
    r"#ad\b|giveaways?\b|business\s*(→|:|inquir|enquir|e-?mail)|promo codes?\b|discount codes?\b|use (my )?code\b|\bhiggs-[a-z0-9]{3,})", re.I)
_LOCATION_CTA_RE = re.compile(r"^\W*(try\b|contact\b|get an? \w)", re.I)          # 'Try Seedance 2.0 →', 'contact →'
# Feeds: AI-news, AI-tools or prompt accounts. Only without a first-person voice and without a job at a named employer
# ('Reporter @verge covering AI news' is a person; 'AI Insider / Reporter as featured in Axios' is a feed).
FEED_RE = re.compile(r"(\bai news\b|\bdaily ai\b|\bai daily\b|\blatest (ai|tech) (news|tools|updates)\b|\bai updates\b|"
                     r"\b(top|best|latest|new) ai tools\b|\bai tools (and|&) (news|prompts)\b|\bprompts\b|\bprompt (library|packs?)\b|"
                     r"\bai insider\b|\bsharing what'?s (wild|new|next|hot)\b|\bcurat(ed|ing|or) (ai|tech)\b)", re.I)
FEED_NAME_RE = re.compile(r"\b(news|daily|digest|insider|updates|alerts)\b", re.I)
_EMPLOYED_RE = re.compile(r"(journalist|reporter|editor|writer|columnist|correspondent|host|founder|co-?founder|ceo|cto|coo|"
                          r"partner|engineer|analyst|professor|researcher|director|lead|manager|head of \w+)\s*[:,]?\s*"
                          r"(@|at\b|of\b|for\b|on\b|with\b)", re.I)
# Brands and shops: an organisation's voice in the bio, or a name that is the handle plus an organisation word (@seeedstudio).
_ORG_VOICE_RE = re.compile(r"((email|contact|dm|message|follow|join|tag) us\b|\ba team of\b|\bour team\b|\bpart of @|"
                           r"\byour (go-to|source|daily dose|trusted|one-stop|#1)\b|\bshop now\b|\bfree shipping\b|\border now\b|"
                           r"\bofficial (store|shop)\b)", re.I)
BRAND_NAME_WORDS = ("studio", "studios", "labs", "media", "shop", "store", "hq", "official", "inc", "app", "tv", "news",
                    "magazine", "group", "network")
# Real-person markers for the anonymity rule: a role at a named place, family words, pronouns, a degree.
_IDENTITY_RE = re.compile(r"(\b(dad|mom|mama|father|mother|husband|wife|he/him|she/her|they/them|phd|md|professor)\b|"
                          r"\b(co-?founder|founder|ceo|cto|editor|reporter|journalist|writer|host|partner|author)\b\s*[:,]?\s*"
                          r"(@|at\b|of\b|for\b|and\b|&|/|\w+\s*@)|\b(previously|prev|formerly|ex)\b[\s:-]*@?\w|"
                          r"\b(opinions|views)( are)? (my|mine)|\bviews ≠|\baffiliations?\b|"
                          r"(?<![@\w])[a-z][\w-]*\s+@[a-z0-9_]{2,})", re.I)                  # 'head of design @sesame'


def _norm_name(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)).lower())


def _name_words(name: str | None) -> list[str]:
    """Alphabetic words of two letters or more in a display name ('Lil Auntie 🌮token2049' -> ['Lil', 'Auntie', 'token'])."""
    return re.findall(r"[^\W\d_]{2,}", name or "")


def _own_domain(handle: str | None, url: str | None) -> bool:
    """The profile links a site on the handle's own domain (shl0ms.com for @shl0ms): someone stands behind the name."""
    host = re.sub(r"^https?://(www\.)?", "", (url or "").lower()).split("/")[0]
    labels = host.split(".")
    return len(labels) >= 2 and bool(handle) and labels[-2] == _norm_name(handle)


def anonymous_account(name: str | None, handle: str | None, bio: str | None, url: str | None = None) -> str | None:
    """Why the account hides who is behind it, else None. The display name is not a person's name and the bio carries no
    real-person marker (a role at a named place, family words, pronouns, 'ex-'/'prev' a named employer):
    an empty bio under a name that is not a first-and-last name ('string', 'DogeDesigner', only symbols), a one-word
    name on a handle with four digits or more ('Matt' @matt503ea5sf9z5, 'Dustin' @r0ck3t23), or a phrase for a name
    ('The Random Recruiter'). A profile linking the
    handle's own domain (shl0ms.com, swyx.io) is never anonymous: someone stands behind the name. A backup account
    ('@omoreyy_ backup') always is."""
    low = (bio or "").strip()
    if re.search(r"\bbackup\b", low, re.I) and re.search(r"@\w+", low):
        return "a backup account"
    if _IDENTITY_RE.search(low) or _own_domain(handle, url):
        return None
    words = _name_words(name)
    if not re.search(r"[^\W\d_]", low) and len(words) < 2:
        return f"empty bio, name {name!r}" if words else "empty bio, a name of symbols only"
    # Not a rule on its own: a one-word name identical to the handle. It caught @teslaconomics, but on 2,066 candidates it
    # also hit @jack, @ev, @kwindla and @nathie, whose bios carry no marker a rule can read.
    if len(words) == 1 and sum(ch.isdigit() for ch in handle or "") >= 4:
        return f"first name only ({name!r}) on handle @{handle}"
    if re.match(r"\s*the\s+[^\W\d_]", name or "", re.I):     # a phrase for a name: 'The Random Recruiter'
        return f"a phrase as the name ({name.strip()!r})"
    return None


def red_flag(name: str | None, handle: str | None, bio: str | None, location: str | None = None, url: str | None = None,
             in_map: bool = False) -> tuple[str, str] | None:
    """(kind, phrase) when the profile alone shows an account the reviewers reject, else None. Kinds, checked in order:
    'crypto' (crypto words, a cashtag, .eth, a DAO or '_eth' mention, a crypto handle), 'promo' (sells_posts, PROMO_RE, or a
    'Try ... →' / 'contact →' call to action as the location), 'feed' (an AI-news, AI-tools or prompts account, or a
    news/daily/digest name, with no first person and no job at a named employer), 'brand' (an organisation's voice, a
    third-person bio that opens with the handle, or the handle as a name with an organisation word) and 'anonymous'
    (anonymous_account; never for someone in the operator's map, who knows who they are)."""
    bio, name, location = bio or "", name or "", location or ""
    text = f"{name} {bio}"
    low = text.lower()
    m = (_CRYPTO_RE.search(low) or _CRYPTO_CAPS_RE.search(bio) or _CRYPTO_MARK_RE.search(text)
         or _CRYPTO_HANDLE_RE.search(handle or ""))
    if m:
        return "crypto", m.group(0)
    for m in _CASHTAG_RE.finditer(text):
        if m.group(1).lower() in COIN_TICKERS or not _IDENTITY_RE.search(bio):
            return "crypto", m.group(0)
    promo = sells_posts(bio, location)
    m = PROMO_RE.search(bio) or PROMO_RE.search(location) or _LOCATION_CTA_RE.search(location)
    if promo or m:
        return "promo", promo or (m.group(0) if m.re is not _LOCATION_CTA_RE else location.strip())
    personal = bool(_FIRST_PERSON_RE.search(bio.lower()) or _EMPLOYED_RE.search(bio))
    m = FEED_RE.search(bio) or FEED_NAME_RE.search(name)
    if m and not personal:
        return "feed", m.group(0)
    h = _norm_name(handle)
    shop = re.search(r"^(https?://)?(www\.)?([^/.]+)\.(shop|store)(/|$)", (url or "").strip(), re.I)
    if shop and h and _norm_name(shop.group(3)) == h:       # stufflistings.shop, not a creator's stan.store page
        return "brand", f"a shop's site ({shop.group(3)}.{shop.group(4)})"
    if not (_FIRST_PERSON_RE.search(bio.lower()) or _PERSON_ROLE_RE.search(bio.lower())):
        m = _ORG_VOICE_RE.search(bio)
        if m:
            return "brand", m.group(0)
        head = re.match(r"\s*([\w .'-]{3,40}?)\s+is\s+(a|an|the|your)\b", bio)
        if head and h and _norm_name(head.group(1)) == h:
            return "brand", head.group(0).strip()
    if h and _norm_name(name) == h and not _FIRST_PERSON_RE.search(bio.lower()):
        org = next((w for w in _name_words(name) if w.lower() in BRAND_NAME_WORDS), None)
        if org:
            return "brand", f"{name.strip()} ({org.lower()})"
        if re.fullmatch(r"[A-Z]{2,5}", name.strip()) and not _IDENTITY_RE.search(bio):     # 'BGR' @bgr, not 'DHH': 'CTO of'
            return "brand", f"{name.strip()} (an acronym as the name)"
    why = None if in_map else anonymous_account(name, handle, bio, url)
    if why:
        return "anonymous", why
    return None


# run_enrich's two searches per person (mode="search"): device / voice evidence first, then recent context.
ENRICH_EVIDENCE_TERMS = ('(wearable OR "smart wearable" OR Oura OR Ultrahuman OR Whoop OR "Apple Watch" OR AirPods OR earbuds OR '
                         'wearable OR wearables OR pendant OR Limitless OR Plaud OR Humane OR "Rabbit r1" OR "Meta Ray-Ban" OR '
                         '"Ray-Ban Meta" OR glasses OR voice OR "voice notes" OR dictation OR "Wispr" OR "screen time" OR '
                         '"screen-free" OR dumbphone OR "Light Phone" OR ChatGPT OR Claude OR agent)')
ENRICH_EVIDENCE_SHARE = 2 / 3     # of posts_per_user; the rest goes to recent context


def enrich_queries(handle: str) -> tuple[str, str]:
    """(device/voice evidence search, recent-context search) for one handle."""
    return f"from:{handle} {ENRICH_EVIDENCE_TERMS} -is:retweet", f"from:{handle} -is:retweet"


# Devices and voice tools only (run_enrich device_only, 30 Sep). ENRICH_EVIDENCE_TERMS also matches ChatGPT, Claude and
# agent, so an AI person's engagement-ranked top 20 fills with AI posts and pushes a quieter "my Oura" post out.
DEVICE_TERMS = ('(wearable OR "smart wearable" OR Oura OR Ultrahuman OR Whoop OR "Apple Watch" OR AirPods OR earbuds OR wearable OR '
                'pendant OR Limitless OR Plaud OR Humane OR "Rabbit r1" OR "Meta Ray-Ban" OR "Ray-Ban Meta" OR "smart glasses" '
                'OR Garmin OR Fitbit OR "voice notes" OR "voice memo" OR dictation OR dictate OR Wispr OR "voice mode" OR '
                'Superwhisper)')


def device_queries(handle: str) -> tuple[str, str]:
    """(devices-and-voice-tools search, no recent search) for one handle."""
    return f"from:{handle} {DEVICE_TERMS} -is:retweet", ""


def run_enrich(conn: sqlite3.Connection, run_id: int, source, top: int = 80, posts_per_user: int = 30,
               budget_usd: float | None = None, *, mode: str = "search", refresh_days: int | None = 30,
               seed_handle: str | None = None, candidate_ids: list[int] | None = None,
               first_seen_since: str | None = None, device_only: bool = False) -> dict:
    """Gather each person's own words for the `top` highest-priority candidates (see PRIORITY_WEIGHTS) that were never
    enriched, or last enriched more than `refresh_days` ago (None: never refresh). Parody-looking and 'suspicious' handles
    are not paid for.
    mode="search" (default; monid searches 365 days engagement-ranked and serves a repeated query from x_posts for 20 h):
      two searches per person via source.search_posts, A) device / voice evidence (enrich_queries, ~2/3 of posts_per_user)
      and B) recent context (the rest). Posts keep the query that found them (classify shows it and ranks A first),
      except posts a discovery query surfaced, which keep that query.
    mode="timeline": one source.get_user_posts(handle, posts_per_user) (the official X API's user timeline).
    Only the person's own posts are stored (retweets dropped, replies kept: paths use them). Each enriched candidate gets
    'enriched:YYYY-MM-DD posts=N' in notes (N = own posts returned; 0 counts too, so an empty account is not re-bought
    daily). The projected cost is printed first; the loop stops before the running estimate would exceed `budget_usd`
    (an explicit cap for this stage; None: the provider's per-run cap from .env).
    `candidate_ids` (a manual check, wearable_wishlist.check): only these people, fetched now whatever the refresh window and the
    gates say (the person asked for them and saw the cost first); the budget guard still applies.
    `first_seen_since` (ISO time): only candidates first seen at or after it (the people a new discovery pass found);
    gates and the refresh window still apply.
    `device_only`: one search per person, devices and voice tools only (device_queries), for people whose evidence search
    found nothing: its AI terms can crowd their device posts out of the top posts."""
    if mode not in ("search", "timeline"):
        raise ValueError(f"run_enrich mode must be 'search' or 'timeline', not {mode!r}")
    now = db.utcnow()
    today = now[:10]
    cutoff = None
    if refresh_days is not None:
        cutoff = (dt.datetime.strptime(today, "%Y-%m-%d") - dt.timedelta(days=refresh_days)).strftime("%Y-%m-%d")
    ranked = candidate_priorities(conn, candidate_ids=candidate_ids, seed_handle=seed_handle)
    if first_seen_since:
        fresh = {r["candidate_id"] for r in db.rows(conn, "SELECT candidate_id FROM candidates WHERE first_seen >= ?",
                                                    (first_seen_since,))}
        ranked = [s for s in ranked if s["candidate_id"] in fresh]
    n_evidence = max(1, round(posts_per_user * ENRICH_EVIDENCE_SHARE))
    n_recent = max(0, int(posts_per_user) - n_evidence)
    queries = enrich_queries
    if device_only:   # one search, devices and voice tools only (DEVICE_TERMS), all posts_per_user of it
        queries, n_evidence, n_recent = device_queries, max(1, int(posts_per_user)), 0
    stats: dict = {"mode": mode, "eligible": 0, "selected": 0, "enriched": 0, "posts": 0, "posts_new": 0, "topical_new": 0,
                   "evidence_posts": 0, "empty": 0, "errors": 0, "skipped_budget": 0, "skipped_suspicious": 0,
                   "budget_stop": False, "estimated_cost_usd": 0.0, "cost_usd": 0.0, "handles": []}
    eligible = []
    gated: Counter = Counter()
    for s in ranked:
        if not (s["handle"] or s["x_id"]):
            continue
        last = s["enriched_on"]
        if candidate_ids is None and last and (cutoff is None or last >= cutoff):
            continue
        why = enrich_gate(s) if candidate_ids is None else None
        if why:
            gated[why] += 1
            continue
        eligible.append(s)
    stats["skipped"] = dict(gated)
    stats["skipped_suspicious"] = gated.get("suspicious", 0)
    stats["eligible"] = len(eligible)
    chosen = eligible[:max(0, int(top))]       # the top slots are filled from the next eligible candidates
    log.info("enrich: %d candidates eligible, %d chosen; not paid for: %s", len(eligible), len(chosen),
             ", ".join(f"{k} {v}" for k, v in gated.most_common()) or "none")
    if mode == "search":
        per_user = estimate_cost(source, "post_read", n_evidence) + (estimate_cost(source, "post_read", n_recent) if n_recent else 0.0)
        shape = f"2 searches ({n_evidence} evidence + {n_recent} recent posts)"
    else:
        per_user = estimate_cost(source, "timeline_read", posts_per_user)
        shape = f"{posts_per_user} timeline posts"
    budget = _Budget(conn, source, run_id, budget_usd)
    if budget.cap is not None and per_user > 0:      # never prefetch (buy) more people than the budget allows
        fit = max(0, int((budget.cap - budget.estimate + 1e-9) // per_user))
        if fit < len(chosen):
            log.warning("enrich: budget %s fits %d of %d chosen people", budget.label(), fit, len(chosen))
            stats["skipped_budget"], stats["budget_stop"] = len(chosen) - fit, True
            chosen = chosen[:fit]
    stats["selected"] = len(chosen)
    print(f"[cost] {source.name} enrich: ~{costs.fmt(per_user * len(chosen))} for {len(chosen)} people x {shape} "
          f"(~{costs.fmt(per_user)} each, cap {budget.label()})")
    if mode == "search" and chosen and hasattr(source, "prefetch_searches"):
        # the adapter runs the searches concurrently and caches them; the loop below then reads the same query strings
        # with the same max_results from that cache for $0
        qs: list[tuple[str, int]] = []
        for s in chosen:
            if s["handle"]:
                q_evidence, q_recent = queries(s["handle"])
                qs.append((q_evidence, n_evidence))
                if n_recent:
                    qs.append((q_recent, n_recent))
        try:
            stats["prefetch"] = source.prefetch_searches(qs)
        except costs.BudgetExceeded as e:
            log.warning("enrich: prefetch refused by the budget guard (%s); searching one by one", e)
            stats["prefetch"] = {"error": str(e)}
        except Exception as e:   # the loop below still works, only slower
            log.warning("enrich: prefetch failed (%s); searching one by one", e)
            stats["prefetch"] = {"error": str(e)}
    for i, s in enumerate(chosen):
        if not budget.reserve(per_user):
            stats["skipped_budget"] += len(chosen) - i
            stats["budget_stop"] = True
            break
        x_id, handle = s["x_id"], s["handle"]
        known = {r["post_id"]: r["query"] for r in db.rows(conn, "SELECT post_id, query FROM x_posts WHERE x_id=?", (x_id,))}
        try:
            if mode == "search" and handle:
                q_evidence, q_recent = queries(handle)
                posts = list(source.search_posts(q_evidence, max_results=n_evidence))
                for p in posts:
                    p.query = q_evidence
                if n_recent:
                    got = {p.post_id for p in posts}
                    for p in source.search_posts(q_recent, max_results=n_recent):
                        if p.post_id not in got:
                            p.query = q_recent
                            posts.append(p)
            else:
                posts = list(source.get_user_posts(handle or x_id, max_results=posts_per_user))
        except costs.BudgetExceeded as e:
            log.warning("enrich: adapter budget guard stopped enrichment at @%s: %s", handle, e)
            stats["skipped_budget"] += len(chosen) - i
            stats["budget_stop"] = True
            budget.settle(per_user, 0.0)
            break
        except Exception as e:   # one bad account must not stop the batch; the reservation stays (it may have been billed)
            log.warning("enrich: @%s failed, skipped: %s", handle, e)
            stats["errors"] += 1
            continue
        own = [p for p in posts if (p.x_id == x_id or (handle and p.handle == handle)) and not is_retweet(p)]
        budget.settle(per_user, min(per_user, estimate_cost(source, "post_read" if mode == "search" else "timeline_read", len(posts))))
        for p in own:
            p.x_id = x_id                          # the candidate's id, whatever representation the source used
            if query_kind(known.get(p.post_id)) == "discovery":
                p.query = known[p.post_id]         # a topic query found it first: keep that (it is a topical signal)
            elif p.query is None and known.get(p.post_id):
                p.query = known[p.post_id]
        persist_posts(conn, own, source.name)
        # adapters that persist on their own may have overwritten a discovery query: restore it
        restore = [(q, pid) for pid, q in known.items() if query_kind(q) == "discovery"]
        if restore:
            conn.executemany("UPDATE x_posts SET query=? WHERE post_id=? AND (query IS NULL OR query LIKE 'from:%')", restore)
        new = [p for p in own if p.post_id not in known]
        stats["posts"] += len(own)
        stats["posts_new"] += len(new)
        stats["topical_new"] += sum(1 for p in new if is_topical(p.text))
        stats["evidence_posts"] += sum(1 for p in own if query_kind(p.query) == "evidence")
        stats["empty"] += not own
        row = db.one(conn, "SELECT notes FROM candidates WHERE candidate_id=?", (s["candidate_id"],)) or {}
        conn.execute("UPDATE candidates SET notes=? WHERE candidate_id=?",
                     (_merge_notes(row.get("notes"), f"enriched:{today} posts={len(own)}"), s["candidate_id"]))
        conn.commit()
        stats["enriched"] += 1
        stats["handles"].append(handle)
        log.info("enrich: @%s (priority %.1f): %d own posts, %d new", handle, s["priority"], len(own), len(new))
    stats["estimated_cost_usd"] = round(budget.estimate, 4)
    stats["cost_usd"] = costs.spent(conn, getattr(source, "name", ""), run_id)
    log.info("enrich done: %d/%d enriched (%d eligible), %d posts (%d new, %d topical new), %d errors, %d skipped for budget, "
             "estimate %s; not paid for: %s", stats["enriched"], stats["selected"], stats["eligible"], stats["posts"],
             stats["posts_new"], stats["topical_new"], stats["errors"], stats["skipped_budget"],
             costs.fmt(stats["estimated_cost_usd"]), stats["skipped"])
    return stats


# ---------------------------------------------------------------------------------------------------------------------
# Hub suggestions
# ---------------------------------------------------------------------------------------------------------------------

def suggest_hubs(conn: sqlite3.Connection, top: int = 20, min_followers: int = 5000, seed_handle: str | None = None) -> list[dict]:
    """Verified, fetched accounts with the most topical posts and a large audience: the lead picks hubs for queries.yaml from
    these real handles (never typed from memory). Parody-looking accounts and the seed are excluded."""
    sid = seed_x_id(conn, seed_handle)
    rows = db.rows(conn, """
        SELECT u.x_id, u.handle, u.name, u.bio, u.followers, u.verified, u.verified_type,
               COUNT(DISTINCT p.post_id) AS topical_posts, COALESCE(SUM(p.likes),0) AS likes
        FROM x_posts p JOIN x_users u ON u.x_id = p.x_id
        WHERE p.query IS NOT NULL AND p.query NOT LIKE 'from:%'
        GROUP BY u.x_id
        HAVING COALESCE(u.followers,0) >= ? AND u.verified = 1
        ORDER BY topical_posts DESC, u.followers DESC""", (min_followers,))
    out = []
    for r in rows:
        if r["x_id"] == sid or looks_parody(r["name"], r["bio"]):
            continue
        out.append({"handle": r["handle"], "name": r["name"], "topical_posts": r["topical_posts"], "likes": r["likes"],
                    "followers": r["followers"], "verified": bool(r["verified"]), "bio": r["bio"],
                    "yaml": f"  - {r['handle']}   # {r['topical_posts']} topical posts, {r['followers']:,} followers"})
        if len(out) >= top:
            break
    return out


if __name__ == "__main__":   # standalone runs; the pipeline (wearable_wishlist.pipeline) is the normal entry point
    import argparse
    from .sources.base import get_source

    ap = argparse.ArgumentParser(description="wearable_wishlist discovery stages")
    ap.add_argument("stage", choices=["crawl", "discover", "enrich", "hubs", "priorities"])
    ap.add_argument("--seed", default=None, help="seed handle (default SEED_X_HANDLE)")
    ap.add_argument("--budget", type=float, default=None, help="cap in USD for this stage")
    ap.add_argument("--max-following", type=int, default=2000)
    ap.add_argument("--max-followers", type=int, default=2000)
    ap.add_argument("--top", type=int, default=80, help="enrich: candidates to enrich")
    ap.add_argument("--posts-per-user", type=int, default=30, help="enrich: posts per candidate")
    ap.add_argument("--mode", choices=["search", "timeline"], default="search", help="enrich: two searches or one timeline read")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    c = db.connect()
    if args.stage == "hubs":
        for h in suggest_hubs(c, seed_handle=args.seed):
            print(h["yaml"])
    elif args.stage == "priorities":
        for s in candidate_priorities(c, seed_handle=args.seed)[:args.top]:
            print(f"{s['priority']:6.1f}  @{s['handle']:20} topical={s['topical_posts']} followers={s['followers']} "
                  f"map={bool(s['map_person_id'])} mutual={s['mutual_follow_with_seed']} enriched={s['enriched_on']}")
    else:
        rid = db.new_run(c, args.stage)
        src = get_source(conn=c, run_id=rid)
        if args.stage == "crawl":
            print(run_crawl(c, rid, src, args.seed or config.seed_handle() or "", args.max_following, args.max_followers))
        elif args.stage == "discover":
            print(run_discover(c, rid, src, budget_usd=args.budget, seed_handle=args.seed))
        else:
            print(run_enrich(c, rid, src, top=args.top, posts_per_user=args.posts_per_user, budget_usd=args.budget,
                             mode=args.mode, seed_handle=args.seed))
        db.finish_run(c, rid)
