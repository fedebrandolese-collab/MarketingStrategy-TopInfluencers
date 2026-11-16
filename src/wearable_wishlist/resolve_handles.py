"""Resolve X handles for the people in the operator's map, and seed the plausible wearable wearers among them as candidates.

Methods, free ones first (`map_x_handles.method` records which one produced a row):
  override_verified        free, curated. config/handle_overrides.yaml: handles found by web search and confirmed by
  override_likely          x_id on the official X API. 'verified' (bio carries the company, outlet or role): 0.99;
                           'likely' (a web source ties the handle to the person, the bio does not): 0.85. Matched by
                           map_person_id AND name. An override replaces any stored row except 'manual', and no later
                           method (not even --force) touches it. Rows with handle null + status unknown were searched
                           by hand: the free methods may still match them, the paid fallback skips them. Override people
                           get a candidates row even outside the selected set. Not applied under DRY_RUN (the ids and
                           x_ids belong to the live map and the live X) unless a path is passed.
  map_text                 free. An @handle the operator wrote into the map (name/role/company, e.g. "Editor, AndroidGuys
                           (@swebster77)"; URLs like youtube.com/@x and e-mails are ignored). Accepted when the
                           handle's profile (x_users, or one paid user lookup) carries the person's name: 0.98. Without
                           a profile, only when the handle itself carries the name: 0.8 (0.7 with a first initial),
                           x_id left NULL until a lookup confirms it.
  seed_follow_list         free. The crawl stores the operator's following and followers (x_follows rows with src or dst = the
                           seed's x_id). A map person tagged x_following must be in the following list, x_follower in
                           the followers: name vs X display name (rapidfuzz token_set_ratio behind a first/last-name
                           gate), company or role words in the bio and being in both lists (when the map says both)
                           break ties. Look-alikes still tied -> nothing stored.
  seed_follow_list+strict  free. Everyone else (untagged people, or tagged people not found in their list) against
                           both lists with a stricter rule: token_set_ratio >= 0.92 AND company/outlet or role words in
                           the bio, or the exact name with a unique hit.
  local_x_users            free. Anything else already in x_users (discovered authors), scored with score_hit: the name
                           gate plus company or role words in the bio are required.
  user_search              paid fallback, only for selected people still unresolved, bounded by `max_paid_lookups`
                           calls (handle lookups for map_text count too). An auth error (X's user search needs a
                           user-context token, so the app-only token gets 403), empty credits or the budget guard stop
                           paid calls for the run; the stage carries on.
Unknown is NULL, never a guess: below MIN_CONFIDENCE nothing is stored and the reason is logged. Parody markers sink a hit.
The free methods run for every map person; paid lookups and new candidate rows only for the selected people (tagged
first, then warmth desc, `max_people`).

Candidates: map people who are plausible wearable wearers (sector vc -> vc, angel/founder -> founder_angel, press -> media)
get a candidates row with map_person_id. A person discovery already found (same x_id) ends up as ONE row: the map link
goes onto the discovery row, and a map-only row made by an earlier run is merged into it (evidence, events,
classifications, scores, changes, status moved; duplicates dropped).

CLI: python -m wearable_wishlist.resolve_handles [--all] [--max N] [--max-paid N] [--force] [--overrides PATH]
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import unicodedata
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from . import config, costs, db
from .sources.base import XUser, get_source, norm_handle, persist_users

log = logging.getLogger("wearable_wishlist.resolve_handles")

MIN_CONFIDENCE = 0.6
LIKELY_REAL_CONFIDENCE = 0.8        # at or above: candidates.handle_verified = 'likely_real'
AMBIGUITY_MARGIN = 0.05             # a runner-up this close to the best match makes the match ambiguous
STRICT_NAME_SIM = 0.92              # untagged people: token_set_ratio needed together with a bio corroboration
MAX_PAID_LOOKUPS = 20               # default cap on paid calls (search_users, get_user_by_handle) per run
USERS_PER_SEARCH = 10
METHOD_SEED = "seed_follow_list"
METHOD_SEED_STRICT = "seed_follow_list+strict"
METHOD_MAP_TEXT = "map_text"
HANDLE_OVERRIDES_FILE = config.CONFIG_DIR / "handle_overrides.yaml"
OVERRIDE_METHOD = {"verified": "override_verified", "likely": "override_likely"}
OVERRIDE_CONFIDENCE = {"verified": 0.99, "likely": 0.85}
X_TAGS = {"x_following", "x_follower"}
SECTOR_CATEGORY = {"vc": "vc", "angel": "founder_angel", "founder": "founder_angel", "press": "media", "connector": "other"}
# when the sector says nothing (e.g. 'connector'), the tags may: first hit wins
TAG_CATEGORY = (("press", "media"), ("creator", "x_voice"), ("vc", "vc"), ("investor", "vc"), ("angel", "founder_angel"),
                ("founder", "founder_angel"))
RING_WEARER_CATEGORIES = {"vc", "founder_angel", "media", "x_voice"}
# '@handle' as written in map text; not inside e-mails (dan@engadget.com) or URLs (youtube.com/@gregtoope)
MAP_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_.@/])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_]|\.[A-Za-z]{2,})")
PARODY_MARKERS = ("parody", "fan account", "fanpage", "fan page", "fan club", "stan account", "not affiliated",
                  "unofficial", "satire", "impersonat", "tribute", "bot account", "not the real")
# whole words in a display name that say "not the person": 'Josh Constine Fan', 'Rina Wearable Updates'
NOT_A_PERSON_WORDS = {"parody", "fan", "fans", "fanpage", "bot", "archive", "quotes", "updates", "unofficial", "satire",
                      "tribute", "news"}
NAME_AFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "mba", "esq", "dr", "mr", "mrs", "ms", "prof"}
GENERIC_COMPANY_WORDS = {
    "ventures", "venture", "capital", "partners", "partner", "fund", "funds", "inc", "llc", "co", "company", "group",
    "vc", "the", "and", "of", "labs", "lab", "media", "news", "holdings", "management", "advisors", "global",
    "technologies", "technology", "tech", "studio", "studios", "network", "team", "a", "an", "corp", "corporation",
    "newsletter", "magazine", "columnist", "contributor", "formerly", "former", "now",
}
# stems that are ordinary words: never match a company on these alone
STOP_STEMS = {
    "true", "align", "first", "general", "human", "open", "one", "next", "new", "north", "south", "east", "west",
    "main", "bling", "tomorrow", "future", "life", "light", "point", "sound", "signal", "day", "spark", "base",
    "level", "prime", "core", "meta", "alpha", "beta", "omega", "guide", "digital", "trends", "creative",
    "strategies", "week", "voice", "times", "post", "daily", "review", "wire", "verge", "insider", "business",
}
ROLE_WORDS = {
    "vc": {"vc", "venture", "investor", "investing", "partner", "gp", "fund", "invests", "backing", "investments"},
    "angel": {"angel", "investor", "advisor", "founder", "investing", "invests"},
    "founder": {"founder", "cofounder", "co-founder", "ceo", "cto", "building", "founded", "founding"},
    "press": {"editor", "reporter", "journalist", "writer", "columnist", "correspondent", "covers", "covering",
              "analyst", "news", "podcast", "host", "author", "editorial", "critic", "reviewer"},
    "connector": {"community", "connector", "operator"},
}


# ---------------- small shared helpers (paths.py imports these) ----------------

def tags_of(tags: str | None) -> set[str]:
    return {t.strip().lower() for t in (tags or "").split(",") if t.strip()}


def category_for_sector(sector: str | None) -> str:
    return SECTOR_CATEGORY.get((sector or "").strip().lower(), "other")


def category_for_person(person: dict) -> str:
    """Category from the map sector; for a sector that maps to 'other' (connector, unknown), from the tags."""
    cat = category_for_sector(person.get("sector"))
    if cat != "other":
        return cat
    tags = tags_of(person.get("tags"))
    return next((c for t, c in TAG_CATEGORY if t in tags), "other")


def norm_text(s: str | None) -> str:
    """lowercase ascii, everything non-alphanumeric becomes a space ('@ExampleVentures' -> 'exampleventures')."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def company_keys(company: str | None) -> set[str]:
    """Distinctive forms of a company name to look for in a bio: full name, no-space form, one distinctive stem.
    'Radiate Ventures / MarketX' -> {'radiate ventures', 'radiateventures', 'radiate', 'marketx'}"""
    keys: set[str] = set()
    for part in re.split(r"\s*(?:/|,|&|\||\(|\)| and )\s*", company or ""):
        p = norm_text(part)
        if not p:
            continue
        words = p.split()
        if len(p) >= 4 and not (len(words) == 1 and p in STOP_STEMS | GENERIC_COMPANY_WORDS):
            keys.add(p)
        if len(words) > 1:
            keys.add("".join(words))
            distinct = [w for w in words if w not in GENERIC_COMPANY_WORDS and w not in STOP_STEMS and len(w) >= 6]
            if len(distinct) == 1:
                keys.add(distinct[0])
    return keys


def company_in_text(company: str | None, text: str | None) -> str | None:
    """The company key found as a whole word in `text` (already normalized or not), else None."""
    t = norm_text(text)
    if not t:
        return None
    for k in sorted(company_keys(company), key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t):
            return k
    return None


def role_in_text(sector: str | None, role: str | None, text: str | None) -> set[str]:
    words = set(ROLE_WORDS.get((sector or "").lower(), set()))
    words |= {w for w in norm_text(role).split() if len(w) >= 4 and w not in GENERIC_COMPANY_WORDS}
    return words & set(norm_text(text).split())


def seed_handle_norm(h: str | None) -> str | None:
    """'@WearableAI', 'wearableAI', 'https://x.com/wearableAI/' -> 'wearableai' (x_users.handle is stored lowercase, no '@')."""
    if not h:
        return None
    s = str(h).strip()
    m = re.search(r"(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})", s)
    return norm_handle(m.group(1) if m else s)


def seed_user(conn: sqlite3.Connection, seed_handle: str | None = None) -> dict | None:
    """The seed's x_users row (default SEED_X_HANDLE), matched case-insensitively. When several rows carry the handle
    (a renamed or re-created account), the one with the most follow edges wins, then the freshest."""
    h = seed_handle_norm(seed_handle) or seed_handle_norm(config.seed_handle())
    if not h:
        return None
    rows = db.rows(conn, "SELECT * FROM x_users WHERE lower(handle)=?", (h,))
    if len(rows) > 1:
        for r in rows:
            r["_edges"] = db.one(conn, "SELECT COUNT(*) AS n FROM x_follows WHERE src_x_id=? OR dst_x_id=?",
                                 (r["x_id"], r["x_id"]))["n"]
        rows.sort(key=lambda r: (r["_edges"], r["fetched_at"] or ""), reverse=True)
        for r in rows:
            r.pop("_edges", None)
    return rows[0] if rows else None


# ---------------- names ----------------

def _clean_name(s: str | None) -> str:
    """Drop parentheticals and trailing descriptors: 'Daniel Cooper (alt dan@...)', 'Max M. | Superpower',
    'Jane Doe, PhD', 'Scott Webster / Andrew Allen' (two people in one map row: the first one)."""
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s or "")
    return re.split(r"\s[|/·•–—-]\s|[|,/]", s)[0]


def name_tokens(s: str | None) -> list[str]:
    return [t for t in norm_text(_clean_name(s)).split() if t not in NAME_AFFIXES]


def _alnum(h: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _tok_match(a: str, b: str) -> bool:
    """Same name token: equal, a nickname prefix (dan/daniel, nik/nikolai) or a one-letter spelling variant."""
    if a == b:
        return True
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return True
    return min(len(a), len(b)) >= 3 and fuzz.ratio(a, b) >= 85


def _gate(p: list[str], d: list[str], h: str) -> tuple[bool, str]:
    if not p:
        return False, "map name has no usable letters"
    if len(p) == 1:
        ok = d == p or (not d and h == p[0])
        return ok, "" if ok else f"single-word name '{p[0]}' is not the display name"
    first, last = p[0], p[-1]
    last_ok = any(_tok_match(last, t) for t in d) or (len(last) >= 4 and last in h)
    first_ok = (any(_tok_match(first, t) for t in d) or (len(first) >= 3 and first in h)
                or any(len(t) == 1 and t == first[0] for t in d))
    if first_ok and last_ok:
        return True, ""
    missing = [w for w, ok in (("first name", first_ok), ("last name", last_ok)) if not ok]
    return False, " and ".join(missing) + " not in the display name or handle"


def name_gate(person_name: str, display_name: str | None, handle: str | None) -> tuple[bool, str]:
    """A hit must carry the person's first AND last name (in the display name, or the handle; an initial counts for the
    first name). A shared surname alone is a namesake: 'Marco Veronese' is not 'Roberto Veronese'."""
    return _gate(name_tokens(person_name), name_tokens(display_name), _alnum(handle))


def _sim(pn: str, hn: str, h: str) -> tuple[float, float]:
    """(display-name similarity, overall similarity incl. the handle), rapidfuzz token_set_ratio on cleaned names."""
    name_sim = fuzz.token_set_ratio(pn, hn) / 100.0 if pn and hn else 0.0
    handle_sim = fuzz.ratio(pn.replace(" ", ""), h) / 100.0 if pn and h else 0.0
    return round(name_sim, 3), round(max(name_sim, 0.9 * handle_sim), 3)


def name_similarity(person_name: str, display_name: str | None, handle: str | None) -> float:
    """token_set_ratio of the cleaned names (tolerates middle names, emojis, '| Company'), or 0.9 x the handle ratio."""
    return _sim(" ".join(name_tokens(person_name)), " ".join(name_tokens(display_name)), _alnum(handle))[1]


def looks_parody(name: str | None, bio: str | None, handle: str | None) -> bool:
    blob = f"{name or ''} {bio or ''} {handle or ''}".lower()
    if any(m in blob for m in PARODY_MARKERS):
        return True
    return bool(set(norm_text(name).split()) & NOT_A_PERSON_WORDS)


def _company_hit(person: dict, bio: str | None) -> str | None:
    for co in (person.get("company"), person.get("linkedin_company")):
        if co and (k := company_in_text(co, bio)):
            return k
    return None


def _user(row: dict) -> XUser:
    return XUser(x_id=row["x_id"], handle=row["handle"], name=row.get("name"), bio=row.get("bio"),
                 location=row.get("location"), url=row.get("url"),
                 verified=bool(row["verified"]) if row.get("verified") is not None else None,
                 verified_type=row.get("verified_type"), followers=row.get("followers"), following=row.get("following"),
                 can_dm=bool(row["can_dm"]) if row.get("can_dm") is not None else None)


# ---------------- scoring a hit (local x_users and paid search) ----------------

def score_hit(person: dict, hit: XUser, company_query_hits: set[str] = frozenset(),
              seed_graph: set[str] = frozenset()) -> tuple[float, list[str]]:
    """Confidence 0..1 that `hit` is the map person, and the reasons. The name gate decides; the bio (company or role
    words), the operator's X graph (when passed) or a name+company search corroborate, and one of them is required; a verified
    badge adds a little; parody markers sink it."""
    reasons: list[str] = []
    sim = name_similarity(person["name"], hit.name, hit.handle)
    gate_ok, gate_why = name_gate(person["name"], hit.name, hit.handle)
    conf = 0.55 * sim
    reasons.append(f"name similarity {sim:.2f}")
    corroborated = False
    if (k := _company_hit(person, hit.bio)):
        conf += 0.25
        corroborated = True
        reasons.append(f"company '{k}' in bio")
    if (r := role_in_text(person.get("sector"), person.get("role"), hit.bio)):
        conf += 0.10
        corroborated = True
        reasons.append("role words in bio: " + ",".join(sorted(r)[:3]))
    if hit.verified:
        conf += 0.05
        reasons.append("verified")
    if hit.x_id in seed_graph:
        conf += 0.20
        corroborated = True
        reasons.append("in the operator's X graph")
    if hit.x_id in company_query_hits:
        conf += 0.10
        corroborated = True
        reasons.append("also found by name+company")
    if looks_parody(hit.name, hit.bio, hit.handle):
        conf -= 0.50
        reasons.append("parody/fan marker")
    if not gate_ok or sim < 0.6:  # a wrong person at the right company is still the wrong person
        conf = min(conf, MIN_CONFIDENCE - 0.05)
        reasons.append("name too different")
        if gate_why:
            reasons.append(gate_why)
    elif not corroborated:        # a bare name (even with a paid badge) is a namesake until something else agrees
        conf = min(conf, MIN_CONFIDENCE - 0.05)
        reasons.append("nothing but the name")
    return max(0.0, min(1.0, round(conf, 3))), reasons


def _pick(hits: dict[str, XUser], scored: dict[str, tuple[float, list[str]]], method: str) -> tuple[dict | None, str]:
    if not scored:
        return None, "no user found"
    ranked = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)
    x_id, (conf, reasons) = ranked[0]
    u = hits[x_id]
    if conf < MIN_CONFIDENCE:
        return None, f"best @{u.handle} at {conf:.2f} < {MIN_CONFIDENCE} ({'; '.join(reasons)})"
    if len(ranked) > 1 and ranked[1][1][0] >= max(MIN_CONFIDENCE, conf - AMBIGUITY_MARGIN):
        return None, f"ambiguous: @{u.handle} {conf:.2f} vs @{hits[ranked[1][0]].handle} {ranked[1][1][0]:.2f}"
    return {"handle": u.handle, "x_id": u.x_id, "confidence": conf, "method": method, "reasons": reasons}, "ok"


def _local_hits(conn: sqlite3.Connection, name: str, exclude: set[str] = frozenset()) -> list[XUser]:
    tokens = [t for t in name_tokens(name) if len(t) >= 3]
    if not tokens:
        return []
    needle = max(tokens, key=len)
    rows = db.rows(conn, "SELECT * FROM x_users WHERE lower(name) LIKE ? OR handle LIKE ? LIMIT 50",
                   (f"%{needle}%", f"%{needle}%"))
    return [_user(r) for r in rows if r["x_id"] not in exclude]


def resolve_local(conn: sqlite3.Connection, person: dict, exclude: set[str] = frozenset()) -> tuple[dict | None, str]:
    """Free: match against x_users rows outside the operator's lists (those were judged by the seed-list rules already)."""
    hits = {u.x_id: u for u in _local_hits(conn, person["name"], exclude)}
    scored = {x: score_hit(person, u) for x, u in hits.items()}
    return _pick(hits, scored, "local_x_users")


# ---------------- paid calls, bounded ----------------

_FATAL_ERRORS = {"XApiAuthError", "XApiCreditsError", "XApiRateLimited", "MonidPaymentRequired", "MonidBlocked",
                 "BudgetExceeded", "NotImplementedError"}


def _is_fatal(e: Exception) -> bool:
    """Errors that will repeat on every call (auth, credits, budget, unsupported endpoint, missing key): stop paid calls."""
    if {c.__name__ for c in type(e).__mro__} & _FATAL_ERRORS:
        return True
    msg = str(e).lower()
    return any(k in msg for k in (" 401", " 403", "unauthorized", "forbidden", "missing"))


class PaidLookups:
    """At most `max_calls` paid calls (search_users, get_user_by_handle); the first auth/credit/budget error stops the rest."""

    def __init__(self, source, max_calls: int):
        self.source = source
        self.max_calls = max(0, int(max_calls))
        self.calls = 0
        self.errors: list[str] = []
        self.stopped: str | None = None
        self._consecutive = 0

    def available(self) -> bool:
        return self.stopped is None and self.calls < self.max_calls

    def why_not(self) -> str:
        return self.stopped or f"paid lookup cap reached ({self.max_calls})"

    def _call(self, label: str, fn):
        if not self.available():
            return None
        self.calls += 1
        try:
            out = fn()
        except Exception as e:  # noqa: BLE001 - adapter errors are RuntimeErrors; every failure ends in a log line
            detail = f"{type(e).__name__}: {e}"
            self.errors.append(detail)
            self._consecutive += 1
            if _is_fatal(e):
                self.stopped = f"paid lookups stopped: {detail}"
                log.warning("resolve_handles: %s (continuing with the free methods only)", self.stopped)
            elif self._consecutive >= 3:
                self.stopped = f"paid lookups stopped after 3 failures in a row, last: {detail}"
                log.warning("resolve_handles: %s", self.stopped)
            else:
                log.warning("resolve_handles: %s failed: %s", label, detail)
            return None
        self._consecutive = 0
        return out

    def search(self, query: str) -> list[XUser] | None:
        out = self._call(f"search {query!r}", lambda: self.source.search_users(query, max_results=USERS_PER_SEARCH))
        return None if out is None else list(out)

    def lookup_handle(self, handle: str) -> XUser | None:
        return self._call(f"lookup @{handle}", lambda: self.source.get_user_by_handle(handle))


def resolve_search(conn: sqlite3.Connection, person: dict, lookups: PaidLookups) -> tuple[dict | None, str]:
    """Paid fallback: source.search_users(name), then name + company when the result is ambiguous and a lookup is left."""
    name_q = " ".join(name_tokens(person["name"])) or person["name"]
    hits = {u.x_id: u for u in _local_hits(conn, person["name"])}
    found = lookups.search(name_q)
    if found is None:
        return None, lookups.why_not()
    searched = {u.x_id: u for u in found}
    for x, u in searched.items():
        hits.setdefault(x, u)
    method = "user_search"
    scored = {x: score_hit(person, u) for x, u in hits.items()}
    ranked = sorted((v[0] for v in scored.values()), reverse=True)
    ambiguous = bool(ranked) and (ranked[0] < MIN_CONFIDENCE or (len(ranked) > 1 and ranked[1] >= ranked[0] - 0.15))
    company = (person.get("company") or "").strip()
    if ambiguous and company and lookups.available():
        stem = min(company_keys(company), key=len, default=company)
        company_users = lookups.search(f"{name_q} {stem}")
        if company_users:
            method = "user_search+company"
            company_hits = {u.x_id for u in company_users}
            for u in company_users:
                searched.setdefault(u.x_id, u)
                hits.setdefault(u.x_id, u)
            scored = {x: score_hit(person, u, company_hits) for x, u in hits.items()}
    if searched:
        persist_users(conn, searched.values(), getattr(lookups.source, "name", "unknown"))
    return _pick(hits, scored, method)


# ---------------- map_text: an @handle the operator wrote into the map ----------------

def map_text_handles(person: dict) -> list[str]:
    text = " ".join(str(person.get(k) or "") for k in ("name", "role", "company"))
    return list(dict.fromkeys(norm_handle(h) for h in MAP_HANDLE_RE.findall(text)))


def _profile(conn: sqlite3.Connection, handle: str) -> XUser | None:
    row = db.one(conn, "SELECT * FROM x_users WHERE lower(handle)=? ORDER BY fetched_at DESC LIMIT 1", (handle,))
    return _user(row) if row else None


def match_map_text(conn: sqlite3.Connection, person: dict, lookups: PaidLookups | None = None) -> tuple[dict | None, str]:
    """Free with a profile in x_users; `lookups` (paid, one user each) fetches a missing profile when given."""
    handles = map_text_handles(person)
    if not handles:
        return None, "no @handle in the map text"
    confirmed, unconfirmed, notes = [], [], []
    p = name_tokens(person["name"])
    for h in handles:
        prof = _profile(conn, h)
        if prof is None and lookups is not None and lookups.available():
            errors_before = len(lookups.errors)
            prof = lookups.lookup_handle(h)
            if prof is None and len(lookups.errors) == errors_before:
                notes.append(f"@{h}: looked up, no such account on X")
                continue
            if prof is not None:
                persist_users(conn, [prof], getattr(lookups.source, "name", "unknown"))
        if prof is not None:
            ok, why = name_gate(person["name"], prof.name, prof.handle)
            if ok and not looks_parody(prof.name, prof.bio, prof.handle):
                confirmed.append({"handle": prof.handle, "x_id": prof.x_id, "confidence": 0.98, "method": METHOD_MAP_TEXT,
                                  "reasons": [f"@{h} is written in the map", f"its profile name '{prof.name}' is the person"]})
            else:
                notes.append(f"@{h}: profile '{prof.name}' is not {person['name']} ({why or 'parody marker'})")
            continue
        hh = _alnum(h)
        if len(p) >= 2 and len(p[-1]) >= 4 and p[-1] in hh and (p[0] in hh or hh.startswith(p[0][0])):
            conf = 0.8 if p[0] in hh else 0.7
            unconfirmed.append({"handle": h, "x_id": None, "confidence": conf, "method": METHOD_MAP_TEXT,
                                "reasons": [f"@{h} is written in the map", "the handle carries the name",
                                            "profile not fetched yet: unconfirmed"]})
        else:
            notes.append(f"@{h}: no profile to check and the handle does not carry the name")
    for group in (confirmed, unconfirmed):
        if len({r["handle"] for r in group}) == 1:
            return group[0], "ok"
        if len(group) > 1:
            return None, "several map-text handles fit: " + ", ".join(f"@{r['handle']}" for r in group)
    return None, "; ".join(notes) or "no usable @handle"


# ---------------- overrides: config/handle_overrides.yaml ----------------

def load_overrides(path: str | Path | None = None) -> dict[int, dict]:
    """{map_person_id: row} from the overrides file (a missing file is no overrides). A row with a handle needs a numeric
    x_id and confidence verified|likely; a row without one needs status: unknown. Invalid rows, a repeated
    map_person_id and an x_id given to two people are logged and skipped (unknown is never guessed)."""
    p = Path(path) if path else HANDLE_OVERRIDES_FILE
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    rows = data.get("overrides") if isinstance(data, dict) else data
    out: dict[int, dict] = {}
    by_xid: dict[str, int] = {}
    for i, r in enumerate(rows or []):
        why = ""
        pid, name = (r.get("map_person_id"), r.get("name")) if isinstance(r, dict) else (None, None)
        handle = norm_handle(str(r["handle"])) if isinstance(r, dict) and r.get("handle") else None
        x_id = str(r.get("x_id") or "").strip() if isinstance(r, dict) else ""
        conf = str(r.get("confidence") or "").strip().lower() if isinstance(r, dict) else ""
        if not isinstance(pid, int) or not name:
            why = "needs map_person_id and name"
        elif pid in out:
            why = f"map_person_id {pid} repeated"
        elif handle and not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
            why = f"invalid handle {r.get('handle')!r}"
        elif handle and not x_id.isdigit():
            why = f"@{handle} needs the numeric x_id from the official lookup"
        elif handle and conf not in OVERRIDE_CONFIDENCE:
            why = f"@{handle} needs confidence verified|likely, got {conf or 'nothing'}"
        elif handle and x_id in by_xid:
            why = f"x_id {x_id} already given to map #{by_xid[x_id]}"
        elif not handle and str(r.get("status") or "").lower() != "unknown":
            why = "no handle: status must be 'unknown'"
        if why:
            log.warning("handle overrides %s row %d skipped: %s", p.name, i + 1, why)
            continue
        loc = r.get("location")
        out[pid] = {"map_person_id": pid, "name": str(name), "handle": handle, "x_id": x_id if handle else None,
                    "confidence": conf if handle else None, "status": "ok" if handle else "unknown",
                    "source_url": r.get("source_url"), "match_reason": r.get("match_reason"),
                    "non_us": (str(loc) if loc else True) if r.get("non_us") else None}
        if handle:
            by_xid[x_id] = pid
    return out


def override_result(ov: dict) -> dict:
    """An override row as a resolution (the shape the other methods return), non-US marker included for the notes."""
    reasons = [f"override ({ov['confidence']}): {ov.get('match_reason') or 'no reason given'}"]
    if ov.get("source_url"):
        reasons.append(f"found at {ov['source_url']}")
    return {"handle": ov["handle"], "x_id": ov["x_id"], "confidence": OVERRIDE_CONFIDENCE[ov["confidence"]],
            "method": OVERRIDE_METHOD[ov["confidence"]], "reasons": reasons, "non_us": ov.get("non_us")}


def override_matches_person(ov: dict, person: dict) -> bool:
    """The file names the person it was written for: a re-ingested map that reuses an id for someone else gets nothing."""
    return norm_text(ov["name"]) == norm_text(person["name"]) or name_gate(person["name"], ov["name"], None)[0]


# ---------------- seed_follow_list: the operator's own follow lists ----------------

def seed_list_users(conn: sqlite3.Connection, seed_x_id: str) -> list[dict]:
    """Accounts in the seed's following or followers (x_follows src or dst = seed), with which list(s) they are in."""
    s = str(seed_x_id)
    return db.rows(conn, """
        SELECT u.x_id, u.handle, u.name, u.bio, u.verified,
               EXISTS(SELECT 1 FROM x_follows f WHERE f.src_x_id=? AND f.dst_x_id=u.x_id) AS in_following,
               EXISTS(SELECT 1 FROM x_follows f WHERE f.dst_x_id=? AND f.src_x_id=u.x_id) AS in_followers
        FROM x_users u
        WHERE u.x_id <> ? AND u.x_id IN (SELECT dst_x_id FROM x_follows WHERE src_x_id=?
                                         UNION SELECT src_x_id FROM x_follows WHERE dst_x_id=?)""", (s, s, s, s, s))


class SeedLists:
    """the operator's following + followers, tokenized once so every map person can be matched against ~8k accounts cheaply."""

    def __init__(self, users: list[dict], seed_handle: str):
        self.seed_handle = seed_handle
        self.entries = []
        for u in users:
            d = name_tokens(u["name"])
            h = _alnum(u["handle"])
            self.entries.append({"u": u, "d": d, "dn": " ".join(d), "h": h, "blob": " ".join(d) + " " + h})
        self.following = sum(1 for u in users if u["in_following"])
        self.followers = sum(1 for u in users if u["in_followers"])

    def __len__(self):
        return len(self.entries)

    def _candidates(self, person: dict, lists: tuple[str, ...]) -> list[dict]:
        """Accounts in `lists` passing the name gate, not parodies, with similarity and tie-breakers."""
        p = name_tokens(person["name"])
        if not p:
            return []
        key = p[-1][:4]
        pn = " ".join(p)
        out = []
        for e in self.entries:
            u = e["u"]
            if key not in e["blob"]:
                continue
            if not (("following" in lists and u["in_following"]) or ("followers" in lists and u["in_followers"])):
                continue
            ok, _ = _gate(p, e["d"], e["h"])
            if not ok or looks_parody(u["name"], u["bio"], u["handle"]):
                continue
            name_sim, sim = _sim(pn, e["dn"], e["h"])
            rel = [label for flag, label in ((u["in_following"], f"@{self.seed_handle} follows them"),
                                             (u["in_followers"], f"follows @{self.seed_handle}")) if flag]
            reasons = [" and ".join(rel), f"name match {sim:.2f} ('{u['name']}')"]
            tie = 0.0
            company = _company_hit(person, u["bio"])
            role = role_in_text(person.get("sector"), person.get("role"), u["bio"])
            if company:
                tie += 0.10
                reasons.append(f"company '{company}' in bio")
            if role:
                tie += 0.05
                reasons.append("role words in bio: " + ",".join(sorted(role)[:3]))
            if len(p) == 1 and (len(p[0]) < 3 or not (company or role)):
                continue    # a single word ('MB', 'Sama') is no identity without the company or role in the bio
            out.append({"u": u, "sim": sim, "name_sim": name_sim, "exact": sorted(e["d"]) == sorted(p), "tie": tie,
                        "corroborated": bool(company or role), "both": bool(u["in_following"] and u["in_followers"]),
                        "reasons": reasons})
        return out

    @staticmethod
    def _decide(scored: list[tuple[float, dict]], where: str, method: str) -> tuple[dict | None, str]:
        scored.sort(key=lambda s: -s[0])
        conf, c = scored[0]
        if len(scored) > 1 and scored[1][0] >= conf - AMBIGUITY_MARGIN:
            tied = ", ".join(f"@{s[1]['u']['handle']} {s[0]:.2f}" for s in scored[:3])
            return None, f"ambiguous in {where}: {tied} (no company/role words to break the tie)"
        if conf < MIN_CONFIDENCE:
            return None, f"best @{c['u']['handle']} at {conf:.2f} < {MIN_CONFIDENCE}"
        return {"handle": c["u"]["handle"], "x_id": str(c["u"]["x_id"]), "confidence": conf, "method": method,
                "reasons": c["reasons"]}, "ok"

    def match(self, person: dict) -> tuple[dict | None, str]:
        """Tagged people: their tagged list(s), normal rule. Then (untagged, or not found there): both lists, strict rule."""
        tags = tags_of(person.get("tags"))
        lists = tuple(n for n, t in (("following", "x_following"), ("followers", "x_follower")) if t in tags)
        why_tagged = ""
        if lists:
            where = " and ".join(f"@{self.seed_handle}'s {n}" for n in lists)
            cands = self._candidates(person, lists)
            if cands:
                scored = []
                for c in cands:
                    both = len(lists) == 2 and c["both"]
                    if both:
                        c["reasons"].append("in both lists, as the map says")
                    scored.append((round(min(0.99, 0.35 + 0.5 * c["sim"] + c["tie"] + (0.05 if both else 0.0)), 3), c))
                res, why = self._decide(scored, where, METHOD_SEED)
                if res or why.startswith("ambiguous"):
                    return res, why
                why_tagged = why
            else:
                why_tagged = f"no name match in {where}"
        cands = self._candidates(person, ("following", "followers"))
        if not cands:
            return None, (why_tagged + "; " if why_tagged else "") + f"no name match in @{self.seed_handle}'s lists"
        unique = len(cands) == 1
        scored = []
        for c in cands:
            if (c["name_sim"] >= STRICT_NAME_SIM and c["corroborated"]) or (c["exact"] and unique):
                if c["exact"] and unique and not c["corroborated"]:
                    c["reasons"].append("exact name, the only such account in the lists")
                scored.append((round(min(0.95, 0.25 + 0.5 * c["sim"] + c["tie"] + (0.05 if c["both"] else 0.0)), 3), c))
        if not scored:
            best = max(cands, key=lambda c: c["name_sim"])
            return None, ((why_tagged + "; ") if why_tagged else "") + (
                f"strict rule not met by @{best['u']['handle']} (name {best['name_sim']:.2f}, "
                f"{'corroborated' if best['corroborated'] else 'no company/role words in bio'}, "
                f"{len(cands)} look-alike{'s' if len(cands) != 1 else ''})")
        return self._decide(scored, f"@{self.seed_handle}'s lists (strict rule)", METHOD_SEED_STRICT)


def match_seed_lists(person: dict, seed_users: list[dict], seed_handle: str) -> tuple[dict | None, str]:
    """One-off convenience wrapper around SeedLists.match."""
    return SeedLists(seed_users, seed_handle).match(person)


# ---------------- candidates seeded from the map ----------------

_CHILD_UNIQUE = ("evidence", "events", "classifications", "scores")   # tables with a unique key that includes candidate_id


def _status_key(s: dict) -> tuple:
    return (s["status"] != "candidate", s.get("updated_at") or "")


def merge_candidates(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Fold candidate `drop_id` into `keep_id` (same person). Child rows are re-pointed (a duplicate on a unique key keeps the
    survivor's row), the status that says more wins (a set status beats 'candidate', then the latest), gaps in the
    survivor's columns are filled from the other row, and the other row is deleted."""
    keep = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (keep_id,))
    drop = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (drop_id,))
    if not keep or not drop or keep_id == drop_id:
        return
    for table in _CHILD_UNIQUE:
        conn.execute(f"UPDATE OR IGNORE {table} SET candidate_id=? WHERE candidate_id=?", (keep_id, drop_id))
        conn.execute(f"DELETE FROM {table} WHERE candidate_id=?", (drop_id,))
    conn.execute("UPDATE changes SET candidate_id=? WHERE candidate_id=?", (keep_id, drop_id))
    # paths are recomputed every run: move the other row's only for runs where the survivor has none
    conn.execute("UPDATE paths SET candidate_id=? WHERE candidate_id=? AND run_id NOT IN "
                 "(SELECT run_id FROM paths WHERE candidate_id=?)", (keep_id, drop_id, keep_id))
    conn.execute("DELETE FROM paths WHERE candidate_id=?", (drop_id,))
    sk = db.one(conn, "SELECT * FROM status WHERE candidate_id=?", (keep_id,))
    sd = db.one(conn, "SELECT * FROM status WHERE candidate_id=?", (drop_id,))
    if sd and (not sk or _status_key(sd) > _status_key(sk)):
        conn.execute("DELETE FROM status WHERE candidate_id=?", (keep_id,))
        conn.execute("UPDATE status SET candidate_id=? WHERE candidate_id=?", (keep_id, drop_id))
    else:
        conn.execute("DELETE FROM status WHERE candidate_id=?", (drop_id,))
    upd: dict = {}
    for col in ("x_id", "handle", "name", "category", "us_based", "map_person_id", "linkedin_url", "handle_verified",
                "handle_verification_method", "competitor_detail"):
        if keep[col] is None and drop[col] is not None:
            upd[col] = drop[col]
    upd["competitor_flag"] = max(keep["competitor_flag"] or 0, drop["competitor_flag"] or 0)
    firsts = [x for x in (keep["first_seen"], drop["first_seen"]) if x]
    lasts = [x for x in (keep["last_seen"], drop["last_seen"]) if x]
    upd["first_seen"] = min(firsts) if firsts else None
    upd["last_seen"] = max(lasts) if lasts else None
    notes = [s for s in (keep["notes"] or "").split(" | ") if s.strip()]
    keys = {_note_key(s) for s in notes}
    notes += [s for s in (drop["notes"] or "").split(" | ") if s.strip() and _note_key(s) not in keys]   # survivor's win
    upd["notes"] = " | ".join(notes) or None
    conn.execute("DELETE FROM candidates WHERE candidate_id=?", (drop_id,))   # first: x_id is UNIQUE
    conn.execute(f"UPDATE candidates SET {','.join(f'{k}=?' for k in upd)} WHERE candidate_id=?", (*upd.values(), keep_id))
    log.info("merged candidate %s into %s (%s)", drop_id, keep_id, keep["name"] or drop["name"])


def _map_note(person: dict, resolved: dict | None) -> str:
    note = f"map: {person.get('sector') or '?'} · {person.get('company') or '?'} · warmth {float(person.get('warmth') or 0):.1f}"
    if resolved and resolved.get("handle"):
        note += f" · @{resolved['handle']} ({resolved.get('method') or '?'}, {float(resolved.get('confidence') or 0):.2f})"
    else:
        note += " · x handle unresolved"
    if resolved and resolved.get("non_us"):
        where = resolved["non_us"]
        note += f" · non-US ({where})" if isinstance(where, str) else " · non-US"
    return note


def _note_key(segment: str) -> str:
    return segment.split(":", 1)[0].strip() if ":" in segment else segment.strip()


def merge_notes(old: str | None, new: str | None) -> str | None:
    """candidates.notes are ' | '-separated 'key:value' segments shared by the stages (discover's hint:, seed:, posts:,
    and run_enrich's 'enriched:YYYY-MM-DD posts=N', which stops it paying twice). A new segment replaces the one with
    the same key; every other segment is kept."""
    if not new:
        return old
    segs = [s for s in (old or "").split(" | ") if s.strip()]
    for n in [s for s in new.split(" | ") if s.strip()]:
        segs = [s for s in segs if _note_key(s) != _note_key(n)]
        segs.append(n)
    return " | ".join(segs)


def _with_map_note(old: str | None, note: str) -> str:
    return merge_notes(old, note) or note


def upsert_map_candidate(conn: sqlite3.Connection, person: dict, resolved: dict | None, now: str,
                         stats: dict | None = None, create: bool = True) -> int | None:
    """ONE candidates row per map person. The row is found by map_person_id or by the resolved account (x_id, or the
    handle when the x_id is not known yet); when both exist and differ they are merged into the discovery row. No new
    row for people who are not plausible wearable wearers or when `create` is False (an existing row for their X account
    still gets the map link). Returns the candidate_id or None."""
    stats = stats if stats is not None else {}
    category = category_for_person(person)
    x_id = str(resolved["x_id"]) if resolved and resolved.get("x_id") else None
    handle = resolved.get("handle") if resolved else None
    conf = float(resolved.get("confidence") or 0) if resolved else 0.0
    note = _map_note(person, resolved)
    mrows = db.rows(conn, "SELECT * FROM candidates WHERE map_person_id=? ORDER BY candidate_id", (person["id"],))
    if x_id:
        xrow = db.one(conn, "SELECT * FROM candidates WHERE x_id=?", (x_id,))
    elif handle:
        xrow = db.one(conn, "SELECT * FROM candidates WHERE lower(handle)=? ORDER BY candidate_id LIMIT 1", (handle,))
    else:
        xrow = None
    if xrow and xrow["map_person_id"] not in (None, person["id"]):
        log.warning("@%s (candidate %s) is already linked to map #%s; not linking map #%s %s",
                    handle, xrow["candidate_id"], xrow["map_person_id"], person["id"], person["name"])
        note += f" · @{handle} already linked to map #{xrow['map_person_id']}"
        xrow, x_id, handle = None, None, None
    keep = xrow or (mrows[0] if mrows else None)
    for r in mrows:
        if keep is None or r["candidate_id"] == keep["candidate_id"]:
            continue
        if r["x_id"] and keep["x_id"] and r["x_id"] != keep["x_id"]:
            log.warning("map #%s has candidates %s (x_id %s) and %s (x_id %s): different accounts, not merged",
                        person["id"], r["candidate_id"], r["x_id"], keep["candidate_id"], keep["x_id"])
            continue
        merge_candidates(conn, keep["candidate_id"], r["candidate_id"])
        stats["candidates_merged"] = stats.get("candidates_merged", 0) + 1
    verified = handle and conf >= LIKELY_REAL_CONFIDENCE and x_id
    if keep is None:
        if not create:
            return None
        if category not in RING_WEARER_CATEGORIES:
            stats["not_ring_wearers"] = stats.get("not_ring_wearers", 0) + 1
            log.debug("map #%s %s (sector %s) is not a plausible wearable wearer: no candidate row",
                      person["id"], person["name"], person.get("sector"))
            return None
        row = {"x_id": x_id, "handle": handle, "name": person["name"], "category": category, "map_person_id": person["id"],
               "discovered_via": "map", "first_seen": now, "last_seen": now, "notes": note}
        if verified:
            row["handle_verified"] = "likely_real"
            row["handle_verification_method"] = f"{resolved.get('method') or 'map_name_match'}:{conf:.2f}"
        li = db.one(conn, "SELECT profile_url FROM map_linkedin WHERE person_id=?", (person["id"],))
        if li and li["profile_url"]:
            row["linkedin_url"] = li["profile_url"]
        cur = conn.execute(f"INSERT INTO candidates ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        return int(cur.lastrowid)
    keep = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (keep["candidate_id"],))
    cid = keep["candidate_id"]
    upd: dict = {"map_person_id": person["id"], "last_seen": now, "notes": _with_map_note(keep["notes"], note)}
    if not keep["name"]:
        upd["name"] = person["name"]
    if category != "other" and (not keep["category"] or keep["discovered_via"] == "map"):
        upd["category"] = category
    if x_id and not keep["x_id"]:
        upd["x_id"], upd["handle"] = x_id, handle
    elif x_id and keep["x_id"] != x_id:
        log.warning("map #%s %s resolves to @%s (x_id %s) but candidate %s holds x_id %s: left as is",
                    person["id"], person["name"], handle, x_id, cid, keep["x_id"])
    elif handle and not x_id and not keep["handle"] and not keep["x_id"]:
        upd["handle"] = handle          # map_text handle without a profile yet: discovery claims the row by handle
    if verified and not keep["handle_verified"]:
        upd["handle_verified"] = "likely_real"
        upd["handle_verification_method"] = f"{resolved.get('method') or 'map_name_match'}:{conf:.2f}"
    if not keep["linkedin_url"]:
        li = db.one(conn, "SELECT profile_url FROM map_linkedin WHERE person_id=?", (person["id"],))
        if li and li["profile_url"]:
            upd["linkedin_url"] = li["profile_url"]
    conn.execute(f"UPDATE candidates SET {','.join(f'{k}=?' for k in upd)} WHERE candidate_id=?", (*upd.values(), cid))
    return cid


# ---------------- stage ----------------

def _people(conn: sqlite3.Connection) -> list[dict]:
    """All map people, tagged x_following/x_follower first, then warmth desc, LinkedIn company attached."""
    people = db.rows(conn, "SELECT p.*, l.company AS linkedin_company FROM map_people p "
                           "LEFT JOIN map_linkedin l ON l.person_id=p.id")
    people.sort(key=lambda p: (-(1 if tags_of(p["tags"]) & X_TAGS else 0), -(p["warmth"] or 0), p["name"]))
    return people


def select_people(conn: sqlite3.Connection, only_warm: bool = True, max_people: int = 150) -> list[dict]:
    """The people this stage may spend on and seeds as candidates: warm or X-tagged (unless only_warm=False), capped."""
    people = _people(conn)
    if only_warm:
        people = [p for p in people if (p["warmth"] or 0) > 0 or tags_of(p["tags"]) & X_TAGS]
    return people[:max_people]


def _store(conn, person_id: int, res: dict, now: str) -> None:
    db.upsert(conn, "map_x_handles", {"person_id": person_id, "handle": res["handle"],
                                      "x_id": str(res["x_id"]) if res.get("x_id") else None,
                                      "confidence": res["confidence"], "method": res["method"], "resolved_at": now},
              keys=("person_id",))


def run_resolve_handles(conn: sqlite3.Connection, run_id: int | None, source, only_warm: bool = True,
                        max_people: int = 150, force: bool = False, max_paid_lookups: int = MAX_PAID_LOOKUPS,
                        seed_handle: str | None = None, overrides_path: str | Path | None = None) -> dict:
    """`overrides_path`: the handle overrides file; None = config/handle_overrides.yaml, except under DRY_RUN."""
    now = db.utcnow()
    everyone = _people(conn)
    selected = select_people(conn, only_warm, max_people)
    selected_ids = {p["id"] for p in selected}
    existing = {r["person_id"]: r for r in db.rows(conn, "SELECT * FROM map_x_handles")}
    claimed = {str(r["x_id"]): pid for pid, r in existing.items() if r["x_id"]}
    seed = seed_user(conn, seed_handle)
    seed_h = seed["handle"] if seed else (seed_handle_norm(seed_handle) or seed_handle_norm(config.seed_handle()))
    lists = SeedLists(seed_list_users(conn, seed["x_id"]) if seed else [], seed_h or "seed")
    list_ids = {str(e["u"]["x_id"]) for e in lists.entries}
    src_name = getattr(source, "name", "unknown")
    people_by_id = {p["id"]: p for p in everyone}
    overrides = load_overrides(overrides_path) if (overrides_path or not config.dry_run()) else {}
    overridden: set[int] = set()        # a handle from the overrides file decides: no later method touches them
    override_unknown: set[int] = set()  # searched by hand, nothing confirmed: free methods only, no paid search
    ov_counts = {"verified": 0, "likely": 0, "unknown": 0, "applied": 0, "kept": 0, "not_in_map": 0, "name_mismatch": 0,
                 "blocked": 0}
    per_method = {"override": 0, "map_text": 0, "seed_follow_list_tagged": 0, "seed_follow_list_untagged": 0,
                  "local_x_users": 0, "paid_fallback": 0}
    counts: dict = {
        "people": len(selected), "map_people": len(everyone), "already_resolved": 0, "resolved": 0,
        "per_method": per_method, "unresolved": 0,
        "seed": {"handle": seed_h, "found": bool(seed), "following": lists.following, "followers": lists.followers},
        "searched": 0, "paid_lookups": 0, "paid_lookups_max": max_paid_lookups, "paid_stopped": None,
        "deferred_to_seed_lists": 0, "not_searched_cap": 0, "skipped_low_confidence": 0, "ambiguous": 0,
        "candidates_upserted": 0, "candidates_merged": 0, "not_ring_wearers": 0, "candidate_ids": [],
        "overrides": ov_counts, "not_searched_override_unknown": 0,
    }
    resolved: dict[int, dict] = {}
    for p in everyone:
        ex = existing.get(p["id"])
        if ex and (ex["x_id"] or ex["handle"]):
            resolved[p["id"]] = dict(ex)
    counts["already_resolved"] = len(resolved)
    log.info("resolve_handles: %d map people (%d selected), %d already resolved; seed @%s %s (%d following, %d followers)",
             len(everyone), len(selected), len(resolved), seed_h, "found" if seed else "NOT in x_users",
             lists.following, lists.followers)
    if not seed:
        log.warning("resolve_handles: seed @%s not in x_users: run the crawl stage first (seed_follow_list skipped)", seed_h)

    def redo(pid: int) -> bool:
        if pid in overridden:
            return False
        ex = existing.get(pid)
        return pid not in resolved or (force and (ex or {}).get("method") != "manual")

    def accept(p: dict, res: dict, bucket: str) -> bool:
        x = str(res["x_id"]) if res.get("x_id") else None
        if x and claimed.get(x, p["id"]) != p["id"]:
            log.info("not resolved %s: @%s is already resolved for map #%s", p["name"], res["handle"], claimed[x])
            return False
        ex = existing.get(p["id"])
        if ex and ex["method"] == "manual":
            return False
        _store(conn, p["id"], res, now)
        resolved[p["id"]] = res
        if x:
            claimed[x] = p["id"]
        counts["resolved"] += 1
        per_method[bucket] += 1
        log.info("resolved %s -> @%s (%.2f, %s: %s)", p["name"], res["handle"], res["confidence"], res["method"],
                 "; ".join(res.get("reasons") or []))
        return True

    # 0. overrides (free, curated, x_id-confirmed): before everything else, so a fuzzy match can never outrank them
    for pid, ov in overrides.items():
        p = people_by_id.get(pid)
        if p is None:
            ov_counts["not_in_map"] += 1
            log.warning("handle override for map #%s %s: no such map person (re-ingested map?): skipped", pid, ov["name"])
            continue
        if not override_matches_person(ov, p):
            ov_counts["name_mismatch"] += 1
            log.warning("handle override for map #%s names %r but the map says %r: skipped", pid, ov["name"], p["name"])
            continue
        if not ov["handle"]:
            ov_counts["unknown"] += 1
            override_unknown.add(pid)
            continue
        ov_counts[ov["confidence"]] += 1
        ex = existing.get(pid)
        if ex and ex["method"] == "manual":
            continue
        res = override_result(ov)
        if ex and str(ex["x_id"] or "") == res["x_id"] and ex["method"] == res["method"]:
            resolved[pid] = res                 # stored by an earlier run: keep, carry the non-US marker to the notes
            overridden.add(pid)
            ov_counts["kept"] += 1
            continue
        if ex and ex["x_id"] and str(ex["x_id"]) != res["x_id"]:
            log.warning("handle override: %s @%s replaces @%s (%s)", p["name"], res["handle"], ex["handle"], ex["method"])
        if accept(p, res, "override"):
            overridden.add(pid)
            ov_counts["applied"] += 1
        else:
            ov_counts["blocked"] += 1
    if overrides:
        log.info("resolve_handles: %d handle overrides (%d verified, %d likely, %d unknown): %d applied, %d kept, "
                 "%d skipped", len(overrides), ov_counts["verified"], ov_counts["likely"], ov_counts["unknown"],
                 ov_counts["applied"], ov_counts["kept"],
                 ov_counts["not_in_map"] + ov_counts["name_mismatch"] + ov_counts["blocked"])

    # 1. map_text (free when the profile is local; a missing profile waits for the paid phase)
    pending_text: list[dict] = []
    pending_ids: set[int] = set()
    for p in everyone:
        if not redo(p["id"]) or not map_text_handles(p):
            continue
        res, why = match_map_text(conn, p)
        if res and res.get("x_id"):
            accept(p, res, "map_text")
        else:
            pending_text.append(p)
            pending_ids.add(p["id"])
            log.debug("map_text %s: %s", p["name"], why)

    # 2. seed_follow_list (free): every map person, tagged ones on their list with the normal rule, others strict
    proposals: dict[int, dict] = {}
    if len(lists):
        for p in everyone:
            if not redo(p["id"]) or p["id"] in pending_ids:
                continue
            res, why = lists.match(p)
            if res:
                proposals[p["id"]] = res
            else:
                if why.startswith("ambiguous"):
                    counts["ambiguous"] += 1
                log.log(logging.INFO if tags_of(p["tags"]) & X_TAGS else logging.DEBUG,
                        "seed_follow_list: no match for %s: %s", p["name"], why)
    by_xid: dict[str, list[int]] = {}
    for pid, res in proposals.items():
        by_xid.setdefault(res["x_id"], []).append(pid)
    for x_id, pids in by_xid.items():
        pids.sort(key=lambda pid: -proposals[pid]["confidence"])
        if len(pids) > 1 and proposals[pids[1]]["confidence"] >= proposals[pids[0]]["confidence"] - AMBIGUITY_MARGIN:
            counts["ambiguous"] += len(pids)
            log.info("seed_follow_list: @%s fits %s equally well: none stored", proposals[pids[0]]["handle"],
                     ", ".join(people_by_id[pid]["name"] for pid in pids))
            continue
        p, res = people_by_id[pids[0]], proposals[pids[0]]
        ex = existing.get(p["id"])
        if ex and ex["x_id"] and str(ex["x_id"]) != x_id and not force:
            log.warning("seed_follow_list: %s fits @%s but map_x_handles holds @%s (%s): kept (use --force)",
                        p["name"], res["handle"], ex["handle"], ex["method"])
            continue
        tagged = bool(tags_of(p["tags"]) & X_TAGS)
        accept(p, res, "seed_follow_list_tagged" if tagged else "seed_follow_list_untagged")

    # 3. local x_users outside the operator's lists (free)
    for p in everyone:
        if not redo(p["id"]) or p["id"] in pending_ids:
            continue
        res, why = resolve_local(conn, p, exclude=list_ids)
        if res:
            accept(p, res, "local_x_users")

    # 4. paid fallback, bounded, selected people only: map_text profile lookups first (one user each), then name search
    lookups = PaidLookups(source, max_paid_lookups)
    paid = src_name != "fixture"
    counts["not_searched_override_unknown"] = sum(1 for p in selected if redo(p["id"]) and p["id"] in override_unknown)
    text_todo = [p for p in pending_text if p["id"] in selected_ids and redo(p["id"]) and p["id"] not in override_unknown]
    search_todo = [p for p in selected if redo(p["id"]) and p["id"] not in pending_ids and p["id"] not in override_unknown]
    if paid and not len(lists):
        deferred = [p for p in search_todo if tags_of(p["tags"]) & X_TAGS]
        if deferred:
            counts["deferred_to_seed_lists"] = len(deferred)
            log.warning("resolve_handles: no crawled follow lists for @%s: %d people the map tags as X contacts are not "
                        "searched (they match free after the crawl stage)", seed_h, len(deferred))
            search_todo = [p for p in search_todo if not tags_of(p["tags"]) & X_TAGS]
    if paid and (text_todo or search_todo) and lookups.max_calls:
        n_text = min(len(text_todo), lookups.max_calls)
        n_search = min(len(search_todo), lookups.max_calls - n_text)
        projected = (n_text + n_search * USERS_PER_SEARCH) * costs.X_PRICES["user_read"]
        print(f"[cost] {src_name} users/by/username + users/search: ~{costs.fmt(projected)} for up to {n_text} handle "
              f"lookups and {n_search} searches (max {USERS_PER_SEARCH} users each, X list price as the upper bound)")
    for p in text_todo:
        res, why = match_map_text(conn, p, lookups if lookups.available() else None)
        if res and accept(p, res, "map_text"):
            continue
        counts["skipped_low_confidence"] += 1
        log.info("not resolved %s (map text): %s", p["name"], why)
    for p in search_todo:
        if not lookups.available():
            counts["not_searched_cap"] += 1
            log.debug("not resolved %s: not searched (%s)", p["name"], lookups.why_not())
            continue
        calls_before = lookups.calls
        res, why = resolve_search(conn, p, lookups)
        if lookups.calls > calls_before:
            counts["searched"] += 1
        if res and accept(p, res, "paid_fallback"):
            continue
        counts["skipped_low_confidence"] += 1
        log.info("not resolved %s: %s", p["name"], why)
    counts["paid_lookups"] = lookups.calls
    counts["paid_stopped"] = lookups.stopped
    counts["paid_errors"] = lookups.errors
    counts["unresolved"] = sum(1 for p in everyone if p["id"] not in resolved)
    counts["unresolved_selected"] = sum(1 for p in selected if p["id"] not in resolved)

    # 5. candidates: a row per plausible wearable wearer among the selected people (and the overridden ones), merged with
    #    discovery's row for the same account; everyone else resolved only gets the map link on a row discovery made
    for p in everyone:
        create = p["id"] in selected_ids or p["id"] in overridden
        if not create and p["id"] not in resolved:
            continue
        cid = upsert_map_candidate(conn, p, resolved.get(p["id"]), now, counts, create=create)
        if cid is not None:
            counts["candidates_upserted"] += 1
            counts["candidate_ids"].append(cid)
    conn.commit()
    log.info("resolve_handles done: per method %s, unresolved %d of %d (%d of %d selected), paid lookups %d/%d%s",
             per_method, counts["unresolved"], len(everyone), counts["unresolved_selected"], len(selected),
             lookups.calls, lookups.max_calls, f" ({lookups.stopped})" if lookups.stopped else "")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Resolve X handles for map people and seed them as candidates")
    ap.add_argument("--all", action="store_true", help="select people with warmth 0 and no X tag too")
    ap.add_argument("--max", type=int, default=150, help="selected people (paid lookups and new candidate rows)")
    ap.add_argument("--max-paid", type=int, default=MAX_PAID_LOOKUPS, help="cap on paid calls (0 = free methods only)")
    ap.add_argument("--force", action="store_true",
                    help="re-resolve people already resolved (never touches method 'manual' or a handle override)")
    ap.add_argument("--overrides", default=None, help=f"handle overrides file (default {HANDLE_OVERRIDES_FILE.name})")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    run_id = db.new_run(conn, "resolve_handles")
    name = "fixture" if config.dry_run() else config.env("SOURCE_VERIFY", "x_api")
    source = get_source(name, conn=conn, run_id=run_id)
    counts = run_resolve_handles(conn, run_id, source, only_warm=not a.all, max_people=a.max, force=a.force,
                                 max_paid_lookups=a.max_paid, overrides_path=a.overrides)
    db.finish_run(conn, run_id)
    print({k: v for k, v in counts.items() if k not in ("candidate_ids",)})


if __name__ == "__main__":
    main()
