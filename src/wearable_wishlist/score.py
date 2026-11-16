"""Scoring and change log (workstream E). SCORING.md explains the method in plain language.

run_score(conn, run_id)              -> writes `scores` for every candidate with a classification, evidence or map link
                                        and `changes` versus the previous scored run; returns counts.
explain(conn, run_id, candidate_id)  -> {"factors": {...}, "reasons": [...], "evidence_ids": [...], ...} for the dashboard.

Structure (fixed by docs/ARCHITECTURE.md section E):
  p_accept, p_wear, p_love : category prior -> logistic on the weighted evidence sum -> dated event modifiers (90-day decay)
  impact                    : log followers, engagement per post, audience relevance, role leverage (attention > network > funding)
  reach                     : best path strength written by paths.py (0.05 when there is none). Information for the operator on how to
                              approach the person, NOT in the total (the operator, 29 Sep: wearables may go outside the map; quality over
                              connection). A real relationship (direct or via path) nudges p_accept by WARM_ACCEPT x strength.
  engagement                : community engagement 0..1 (engagement.py): the average post's views, likes, replies and
                              reposts per follower against accounts of the same size, from a sample of their latest original
                              posts. None when unknown (no sample, too few posts); the total then counts it as typical.
  total = 100 * p_accept * p_wear * p_love * (0.3 + 0.7*impact) * (0.7 + 0.6*engagement)   (default weights; unknown
                              engagement counts 0.5, i.e. x1.0; config/weights.yaml `default:` reweights it through
                              weights.weighted_total)
  affinity                : topic affinity 0..1 (affinity.py, from the person's own posts) bounded by the classifier's
                              topic_voice verdict (floor 0.4 when true, cap 0.3 when false); None when both are unknown.
                              Adds log-odds to p_love (+1.5 x affinity) and p_accept (+0.5 x affinity) outside the evidence cap;
                              impact uses topic-post engagement from 3 topical posts; supports='affinity' rows are cited only
  confidence                : share of the three probabilities backed by at least one evidence row or event
                              (p_love also counts as backed by a known affinity >= 0.3)
  flags                     : competitor:<which>, not_us, unverified_handle (flags never zero a score). No path is not a flag:
                              factors.reach.no_path and the reach reasons say so, as information.
  changes                   : one row per moved field vs the previous scored run; "new" evidence/events are the ids that run
                              had not seen (reasons_json.seen), because classify's upserts refresh fetched_at on every run
Each fact counts once: on accept/wear/love one post gives one value (its strongest quote) and further posts diminish
(EVIDENCE_DECAY); voice-tool quotes count half toward wear; wearable_wishlist praise inside a launch wave counts a quarter toward love;
wear/love posts spill 25% into accept only when classify did not already file them under accept;
profile_signal impact rows restate followers/relevance and are not added again; competitor evidence and a joined_competitor
event share one penalty; evidence rows with supports='affinity' quote the posts the computed affinity already measures, so
they are cited, never summed.

Every number comes from a stored row (evidence, events, x_users, x_posts, paths, classifications, map_*). No network, no LLM.
Reference date for decay is the run's started_at, so re-scoring a run is deterministic.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import re
import sqlite3
from typing import Any

from . import config, db, weights
from . import engagement as engagement_kpis   # a local `engagement` (mean engagement per post) already exists in _load

log = logging.getLogger("wearable_wishlist.score")

# ------------------------------------------------------------------ constants (all documented in SCORING.md)

# Priors when nothing is known. Low on purpose: an unknown person is a poor bet for a scarce wearable.
PRIORS: dict[str, dict[str, float]] = {
    "x_voice":       {"accept": 0.40, "wear": 0.30, "love": 0.30},
    "media":         {"accept": 0.45, "wear": 0.25, "love": 0.25},
    "vc":            {"accept": 0.35, "wear": 0.25, "love": 0.25},
    "founder_angel": {"accept": 0.40, "wear": 0.30, "love": 0.30},
    "other":         {"accept": 0.30, "wear": 0.20, "love": 0.20},
}
EVIDENCE_SLOPE = 1.0            # logit units per unit of weighted evidence (sum of polarity * weight)
EVIDENCE_CAP = 3.0              # the weighted sum is clamped to +-3: never certainty
EVIDENCE_DECAY = 0.5            # accept/wear/love: one post counts once (its strongest quote), and the k-th strongest post counts
                                # EVIDENCE_DECAY**(k-1): 1, +0.5, +0.25 ... so the sum tends to 2 and the 4th quote barely moves it.
                                # Five posts about the same habit are not five times the evidence (p_love sat at ~0.95 for the
                                # whole top under a linear sum capped at 3).
VOICE_WEAR_WEIGHT = 0.5         # a voice-tool quote (dictation, voice agents, Wispr Flow) counts half toward p_wear: it proves
                                # the interface, not the habit of wearing a device. A quote also filed as wearable_user counts in full.
ACCEPT_SPILLOVER = 0.25         # share of a wear/love post that also counts for accept (people who wear and review devices take them),
ACCEPT_SPILLOVER_CAP = 1.0      # once per post, and not for posts classify already filed under 'accept' (no double count)
COMPETITOR_PENALTY = 1.0        # logit penalty on accept per unit of positive 'competitor' evidence ...
COMPETITOR_PENALTY_CAP = 1.5    # ... capped here. A joined_competitor event is the same fact: the larger of the two counts, not the sum.
P_MIN, P_MAX = 0.02, 0.98       # a probability never reaches 0 or 1

EVENT_DECAY_DAYS = 90
# Probability effects in logit units, impact effects additive. Everything fades linearly to 0 over 90 days,
# except joined_competitor, which persists until a later left_competitor event.
EVENT_EFFECTS: dict[str, dict[str, float]] = {
    "fund_raised":         {"accept": +0.6, "impact": +0.15},   # the impact bonus applies to VCs only
    "wearable_unboxing":       {"wear": +1.0, "love": +0.5},
    "device_review":       {"wear": +0.6, "love": +0.8},
    "posted_about_product": {"love": +1.0, "accept": +1.0},
    "joined_competitor":   {"accept": -1.5},                    # the competitor penalty; not added on top of competitor evidence
    "left_competitor":     {},
    "other":               {},
}
PERSISTENT_EVENTS = {"joined_competitor"}
UNDATED_EVENT_WEIGHT = 0.5      # an event without its own date counts half and cannot fade (fetched_at is not the event's date:
                                # classify refreshes it on every re-classification)
EVENT_LABELS = {
    "fund_raised": "raised a fund", "wearable_unboxing": "posted a wearable unboxing", "device_review": "reviewed a device",
    "posted_about_product": "posted about wearable_wishlist", "joined_competitor": "joined a competitor",
    "left_competitor": "left a competitor", "other": "event",
}

# Impact: attention = 0.6 * log-followers + 0.4 * log-engagement, then role leverage, then audience relevance.
# base = leverage that does not depend on followers (network for founders, network and funding for VCs).
# Caps encode the brief's order attention > network > funding: media 1.00, x_voice 0.95, founders 0.85, VCs 0.85 (+0.15 fresh fund).
ROLE_LEVERAGE: dict[str, dict[str, float]] = {
    "media":         {"attention": 0.75, "base": 0.25},
    "x_voice":       {"attention": 0.85, "base": 0.10},
    "vc":            {"attention": 0.45, "base": 0.40},
    "founder_angel": {"attention": 0.55, "base": 0.30},
    "other":         {"attention": 0.60, "base": 0.05},
}
FOLLOWERS_UNKNOWN = 0.25        # attention component assumed when followers are unknown (equivalent to ~1k followers)
ENGAGEMENT_UNKNOWN = 0.30       # when no posts are stored (equivalent to ~15 engagements per post)
RELEVANCE_UNKNOWN = 0.30        # audience relevance when there is no classification
IMPACT_EVIDENCE_STEP = 0.05     # per unit of 'impact' evidence ...
IMPACT_EVIDENCE_CAP = 0.15      # ... capped at +-0.15
IMPACT_RESTATED_KINDS = {"profile_signal"}   # impact rows of this kind restate followers / audience relevance, which the formula
                                             # already reads directly: cited, not added a second time

# Launch-wave praise. Praise of wearable_wishlist's own products posted while many accounts post the same (the March 2026 TinyNature /
# HumanPods launch: 57 accounts on 2026-03-20, mostly replies to @WearableAI) says they joined the launch, not that they used the
# product and loved it. On p_love only (accept keeps it: they engaged with wearable_wishlist), such a quote or posted_about_product event
# counts LAUNCH_WAVE_WEIGHT. An account that sells posts (discover.sells_posts on bio and location, the enrich gate's
# 'paid_promo') gets the same weight on all its wearable_wishlist praise, whenever posted.
WEARABLE_WISHLIST_PRODUCT_RE = re.compile(r"(?<![a-z0-9])(@?wearable ?wishlist ?ai|wearable ?wishlist|wearable-wishlist\.example|tiny ?nature|human ?pods?)(?![a-z0-9])", re.I)
LAUNCH_WAVE_DAYS = 7            # a wearable_wishlist post is in a wave when, within 7 days either side of it ...
LAUNCH_WAVE_MIN_ACCOUNTS = 10   # ... at least 10 distinct accounts (itself included) posted about wearable_wishlist products
LAUNCH_WAVE_WEIGHT = 0.25       # its weight toward p_love (a quote's weight, or a posted_about_product event's +1.0)

# Stored in reasons_json. When the previous run was scored under another version, a move with no new evidence is explained by
# the method change instead of "re-scored with same evidence".
SCORING_VERSION = "2026-09-30.2"
SCORING_VERSION_NOTE = ("scoring method changed on 2026-09-30 (community engagement joined the total: the average post's "
                        "views, likes, replies and reposts per follower against accounts of the same size, x0.7 to x1.3, "
                        "unknown x1.0; reach left the total: wearables may go outside the operator's map, a warm "
                        "relationship only nudges p_accept; voice-tool quotes count half toward p_wear; one post counts once and "
                        "further posts diminish; launch-wave praise of wearable_wishlist counts a quarter toward p_love; see SCORING.md)")

COMPETITOR_TIES_FILE = config.CONFIG_DIR / "competitor_ties.yaml"   # researched funds (with aliases) and people tied to
                                # competitors; flags only, like a map fund match. Missing file: no extra ties.
WEIGHTS_FILE = weights.WEIGHTS_FILE   # its `default:` weights the total (weights.weighted_total); missing: today's formula

REACH_NONE = 0.05               # reach shown when no path is known (information only: reach is not in the total)
WARM_ACCEPT = 0.5               # log-odds on p_accept per unit of path strength, for a real relationship only: a direct map
WARM_PATH_KINDS = {"direct", "via"}  # contact or an introduction through a connector. A follow or open DMs is not one. At most
                                # +0.475 (a 0.95 direct relationship): a warm ask raises the chance of a yes, but no path never
                                # lowers anything.
CHANGE_THRESHOLD = 0.05         # probabilities, impact and reach must move this much to be logged
TOTAL_CHANGE_THRESHOLD = 1.0    # the total (0-100 points) must move this much: event fade alone should not log daily rows
TOTAL_DECIMALS, P_DECIMALS = 2, 3

# Topic affinity (docs/ARCHITECTURE.md "Topic affinity"). The computed score comes from affinity.topic_affinity (workstream AF),
# imported lazily: while that module is missing or fails, computed affinity is unknown and only the classifier verdict counts.
AFFINITY_WINDOW_DAYS = 90       # posts older than this do not count toward the computed affinity (passed to topic_affinity)
AFFINITY_FLOOR_IF_VOICE = 0.4   # classifier topic_voice true: affinity is at least this
AFFINITY_CAP_IF_NOT = 0.3       # classifier topic_voice false: affinity is at most this (0 when nothing is computed)
AFFINITY_LOVE = 1.5             # log-odds on p_love per unit of affinity ...
AFFINITY_ACCEPT = 0.5           # ... and on p_accept. Both sit OUTSIDE EVIDENCE_CAP (a separate signal, not evidence rows: a
                                # well-evidenced reviewer who also leads the conversation should still rank above one who does
                                # not) and are bounded by P_MAX; at most +1.5 / +0.5.
AFFINITY_BACKED_MIN = 0.3       # a known affinity at least this high backs p_love for confidence, even without love evidence
AFFINITY_MIN_TOPIC_POSTS = 3    # impact's engagement term uses topic-post engagement from this many topical posts, else all posts
AFFINITY_CHANGE_THRESHOLD = 0.10  # smallest move of affinity that is logged (unknown counts as 0, as it adds nothing)
AFFINITY_VERDICTS = {True: "topic voice", False: "not a topic voice", None: "no verdict"}

_FACTOR_SUPPORTS = {            # which evidence `supports` values feed each factor (for change reasons)
    "p_accept": {"accept", "competitor", "wear", "love"}, "p_wear": {"wear"}, "p_love": {"love"},
    "impact": {"impact"}, "reach": {"reach"},
}
_FACTOR_EVENT_KEY = {"p_accept": "accept", "p_wear": "wear", "p_love": "love", "impact": "impact"}


# ------------------------------------------------------------------ small helpers

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _logit(p: float) -> float:
    p = _clamp(p, 1e-6, 1 - 1e-6)
    return math.log(p / (1 - p))


def _date(s: Any) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def decay(days: int | None) -> float:
    """Linear fade: 1.0 today, 0.0 at EVENT_DECAY_DAYS and beyond. Future-dated or unknown -> 1.0 (the caller handles unknown)."""
    if days is None or days < 0:
        return 1.0
    return _clamp(1.0 - days / EVENT_DECAY_DAYS, 0.0, 1.0)


def log_followers(n: float | None) -> float:
    """100 -> 0, 1k -> 0.25, 10k -> 0.5, 100k -> 0.75, 1M+ -> 1."""
    if n is None:
        return FOLLOWERS_UNKNOWN
    return _clamp((math.log10(max(float(n), 1.0)) - 2.0) / 4.0, 0.0, 1.0)


def log_engagement(e: float | None) -> float:
    """Mean likes+reposts+replies+quotes per post: 10 -> 0.26, 100 -> 0.5, 1k -> 0.75, 10k+ -> 1."""
    if e is None:
        return ENGAGEMENT_UNKNOWN
    return _clamp(math.log10(1.0 + max(float(e), 0.0)) / 4.0, 0.0, 1.0)


def post_key(url: str | None) -> str | None:
    """One key per post: the status id of an X URL (case and host variants agree), else the lower-cased URL."""
    if not url:
        return None
    m = re.search(r"/status(?:es)?/(\d+)", url)
    return m.group(1) if m else url.strip().lower().rstrip("/")


def diminishing(values: Any) -> float:
    """Signed per-post weights -> one evidence sum: the k-th strongest post on each side counts EVIDENCE_DECAY**(k-1)."""
    vals = list(values)
    pos = sorted((v for v in vals if v > 0), reverse=True)
    neg = sorted((-v for v in vals if v < 0), reverse=True)
    return (sum(v * EVIDENCE_DECAY ** i for i, v in enumerate(pos))
            - sum(v * EVIDENCE_DECAY ** i for i, v in enumerate(neg)))


def _ids(ids: list[int], limit: int = 6) -> str:
    s = ", ".join(f"#{i}" for i in ids[:limit])
    return s + (f" +{len(ids) - limit} more" if len(ids) > limit else "")


def _short(text: str | None, n: int = 70) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _competitor_name(text: str | None) -> str | None:
    """Find a competitor from config.COMPETITORS in free text (proper-noun match, plus @handle prefixes like @ouraring)."""
    if not text:
        return None
    for name in sorted(config.COMPETITORS, key=len, reverse=True):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", text):
            return name
    handles = [h.lower() for h in re.findall(r"@([A-Za-z0-9_]+)", text)]
    for name in sorted(config.COMPETITORS, key=len, reverse=True):
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if any(h.startswith(key) for h in handles):
            return name
    return None


def fund_matches(text: str | None, funds: list[dict]) -> list[dict]:
    """Which map_competitor_funds rows name a fund that appears in `text` (a company field or a bio).
    Multi-word names match case-insensitively (also squashed, so 'Kindred Ventures' hits '@kindredventures');
    single-word names (Adjacent, Betaworks) must match as written, to avoid flagging the word 'adjacent', except as an
    @handle, which matches in any case ('@betaworks', '@sequoia')."""
    if not text:
        return []
    squashed = re.sub(r"[^a-z0-9]", "", text.lower())
    hits = []
    for f in funds:
        names = [(f.get("fund") or "").strip()] + [str(a).strip().lstrip("@") for a in f.get("aliases") or []]
        for name in (n for n in names if n):
            if " " in name:
                pat = r"(?<![A-Za-z0-9])" + r"\s+".join(re.escape(w) for w in name.split()) + r"(?![A-Za-z0-9])"
                hit = re.search(pat, text, re.IGNORECASE) or re.sub(r"[^a-z0-9]", "", name.lower()) in squashed
            else:                                    # '@a16z' matches 'a16z': '@' is not a word character
                hit = re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", text) or \
                    re.search(r"@" + re.escape(name) + r"(?![A-Za-z0-9_])", text, re.IGNORECASE)   # '@betaworks'
            if hit:
                hits.append(f)
                break
    return hits


def load_competitor_ties(path: Any = None) -> dict:
    """config/competitor_ties.yaml -> {"funds": [{fund, aliases, competitor, round, source_url}], "people": {handle: entry}}.
    A missing or unreadable file means no extra ties (DRY_RUN, or before the file exists)."""
    import yaml
    p = path or COMPETITOR_TIES_FILE
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except FileNotFoundError:
        return {"funds": [], "people": {}}
    except Exception:  # noqa: BLE001 - a broken file must not stop scoring; logged, and the map's funds still count
        log.exception("score: cannot read %s; no extra competitor ties", p)
        return {"funds": [], "people": {}}
    funds = [f for f in raw.get("funds") or [] if isinstance(f, dict) and f.get("fund") and f.get("competitor")]
    people = {}
    for e in raw.get("people") or []:
        if isinstance(e, dict) and e.get("handle") and e.get("competitor"):
            people[str(e["handle"]).strip().lstrip("@").lower()] = e
    return {"funds": funds, "people": people}


def _category(cand: dict, cls: dict, map_person: dict | None) -> str:
    c = cand.get("category") or cls.get("category")
    if c in PRIORS:
        return c
    sector = ((map_person or {}).get("sector") or "").lower()
    if sector == "vc":
        return "vc"
    if sector in ("angel", "founder"):
        return "founder_angel"
    if sector == "press":
        return "media"
    return "other"


# ------------------------------------------------------------------ loading

def _load(conn: sqlite3.Connection, run_id: int) -> dict:
    run = db.one(conn, "SELECT * FROM runs WHERE run_id=?", (run_id,))
    ref_date = (_date(run["started_at"]) if run else None) or dt.datetime.now(dt.timezone.utc).date()
    cands = db.rows(conn, """
        SELECT c.* FROM candidates c
        WHERE c.map_person_id IS NOT NULL
           OR EXISTS (SELECT 1 FROM classifications k WHERE k.candidate_id=c.candidate_id)
           OR EXISTS (SELECT 1 FROM evidence e WHERE e.candidate_id=c.candidate_id)
        ORDER BY c.candidate_id""")
    ids = {c["candidate_id"] for c in cands}

    evidence: dict[int, list[dict]] = {}
    for e in db.rows(conn, "SELECT * FROM evidence ORDER BY evidence_id"):
        if e["candidate_id"] in ids:
            evidence.setdefault(e["candidate_id"], []).append(e)
    events: dict[int, list[dict]] = {}
    for e in db.rows(conn, "SELECT * FROM events ORDER BY COALESCE(date, fetched_at), event_id"):
        if e["candidate_id"] in ids:
            events.setdefault(e["candidate_id"], []).append(e)

    # latest classification at or before this run; a later one only if nothing earlier exists
    classifications: dict[int, dict] = {}
    later: dict[int, dict] = {}
    # which classify field filed each wear quote, over every classification (evidence rows outlive the classification that wrote
    # them): {"wearable": {text}, "voice": {text}}, the text as classify stores it (quote[:500])
    wear_sources: dict[int, dict[str, set[str]]] = {}
    for k in db.rows(conn, "SELECT candidate_id, run_id, json FROM classifications ORDER BY run_id"):
        parsed = db.jloads(k["json"], {}) or {}
        if k["run_id"] <= run_id:
            classifications[k["candidate_id"]] = parsed
        else:
            later.setdefault(k["candidate_id"], parsed)
        for field, src in (("wearable_user", "wearable"), ("voice_tool_user", "voice")):
            block = parsed.get(field) if isinstance(parsed.get(field), dict) else {}
            for ev in block.get("evidence") or []:
                if isinstance(ev, dict) and isinstance(ev.get("quote"), str):
                    wear_sources.setdefault(k["candidate_id"], {"wearable": set(), "voice": set()})[src].add(ev["quote"][:500])
    for cid, parsed in later.items():
        classifications.setdefault(cid, parsed)

    x_users = {u["x_id"]: u for u in db.rows(
        conn, "SELECT x_id, handle, bio, location, verified, created_at, followers, can_dm FROM x_users")}
    engagement = {r["x_id"]: r for r in db.rows(
        conn, "SELECT x_id, AVG(likes+reposts+replies+quotes) AS eng, COUNT(*) AS n FROM x_posts GROUP BY x_id")}

    # best path per candidate: this run first, else the most recent other run (stale but real relationships)
    paths: dict[int, dict] = {}
    for p in db.rows(conn, "SELECT candidate_id, run_id, how, via_name, strength, rank FROM paths ORDER BY candidate_id, run_id, rank"):
        cid = p["candidate_id"]
        if cid not in ids:
            continue
        cur = paths.get(cid)
        better = (
            cur is None
            or (p["run_id"] == run_id and cur["run_id"] != run_id)
            or (p["run_id"] == cur["run_id"] and (p["strength"] or 0) > (cur["strength"] or 0))
            or (p["run_id"] != run_id and cur["run_id"] != run_id and _closer_run(p["run_id"], cur["run_id"], run_id))
        )
        if better:
            paths[cid] = p

    map_ids = [c["map_person_id"] for c in cands if c.get("map_person_id") is not None]
    map_people = {m["id"]: m for m in db.rows(conn, "SELECT * FROM map_people")} if map_ids else {}
    map_linkedin = {m["person_id"]: m for m in db.rows(conn, "SELECT * FROM map_linkedin")} if map_ids else {}
    funds = db.rows(conn, "SELECT fund, competitor, round FROM map_competitor_funds")
    ties = load_competitor_ties()
    known = {((f.get("fund") or "").lower(), f.get("competitor")) for f in funds}
    funds += [f for f in ties["funds"] if (f["fund"].lower(), f["competitor"]) not in known]
    affinity, affinity_status = _computed_affinity(conn, sorted(ids), run)

    return {
        "run_id": run_id, "run": run, "ref_date": ref_date, "candidates": cands, "evidence": evidence, "events": events,
        "classifications": classifications, "x_users": x_users, "engagement": engagement, "paths": paths,
        "map_people": map_people, "map_linkedin": map_linkedin, "funds": funds, "tie_people": ties["people"],
        "affinity": affinity, "affinity_status": affinity_status, "wear_sources": wear_sources,
        "community": engagement_kpis.kpis_for(conn),
        "wave": _launch_wave(conn, cands, evidence, events, x_users),
    }


def _launch_wave(conn: sqlite3.Connection, cands: list[dict], evidence: dict[int, list[dict]], events: dict[int, list[dict]],
                 x_users: dict[str, dict]) -> dict:
    """Posts that praise wearable_wishlist's own products, and which of them sit in a launch wave.
    A post is about wearable_wishlist when its text names a wearable_wishlist product (WEARABLE_WISHLIST_PRODUCT_RE: stored posts, evidence quotes, event texts)
    or classify filed it as a posted_about_product event. It is in a wave when at least LAUNCH_WAVE_MIN_ACCOUNTS distinct
    accounts posted about wearable_wishlist within LAUNCH_WAVE_DAYS of its date. Returns {"posts": {post key: {"date", "accounts"}},
    "days": {date: accounts in its window} for wave dates, "paid_promo": {candidate ids whose bio sells posts}}."""
    acct = {c["candidate_id"]: f"x:{c['x_id']}" if c.get("x_id") else f"c:{c['candidate_id']}" for c in cands}
    posts: dict[str, dict] = {}

    def note(key: str | None, d: dt.date | None, account: str) -> None:
        if not key:
            return
        p = posts.setdefault(key, {"date": None, "accounts": set()})
        if d and (p["date"] is None or d < p["date"]):
            p["date"] = d
        p["accounts"].add(account)

    for p in db.rows(conn, "SELECT post_id, x_id, url, created_at, text FROM x_posts WHERE text LIKE '%wearable_wishlist%' "
                           "OR text LIKE '%tiny%nature%' OR text LIKE '%human%pod%'"):
        if WEARABLE_WISHLIST_PRODUCT_RE.search(p["text"] or ""):
            note(post_key(p["url"]) or str(p["post_id"]), _date(p["created_at"]), f"x:{p['x_id']}")
    for cid, evs in events.items():
        for e in evs:
            if e["kind"] == "posted_about_product" or WEARABLE_WISHLIST_PRODUCT_RE.search(e.get("text") or ""):
                note(post_key(e.get("url")) or f"event:{e['event_id']}", _date(e.get("date")), acct.get(cid, f"c:{cid}"))
    for cid, rows in evidence.items():
        for e in rows:
            if e["supports"] in ("accept", "wear", "love", "affinity") and WEARABLE_WISHLIST_PRODUCT_RE.search(e.get("text") or ""):
                note(post_key(e.get("url")), _date(e.get("date")), acct.get(cid, f"c:{cid}"))

    by_day: dict[dt.date, set[str]] = {}
    for p in posts.values():
        if p["date"]:
            by_day.setdefault(p["date"], set()).update(p["accounts"])
    days: dict[dt.date, int] = {}
    for d in by_day:
        window = set().union(*(by_day.get(d + dt.timedelta(days=i), set()) for i in range(-LAUNCH_WAVE_DAYS, LAUNCH_WAVE_DAYS + 1)))
        if len(window) >= LAUNCH_WAVE_MIN_ACCOUNTS:
            days[d] = len(window)

    paid: set[int] = set()
    try:
        from .discover import sells_posts      # the enrich gate's and classify's own test, so all stages agree on who sells posts
    except Exception:  # noqa: BLE001 - scoring goes on; only the paid-promo check is skipped
        log.warning("score: discover.sells_posts unavailable; paid-promo bios are not checked")
    else:
        for c in cands:
            u = (x_users.get(c.get("x_id")) or {}) if c.get("x_id") else {}
            if sells_posts(u.get("bio"), u.get("location")):
                paid.add(c["candidate_id"])
    return {"posts": posts, "days": days, "paid_promo": paid}


def _computed_affinity(conn: sqlite3.Connection, candidate_ids: list[int], run: dict | None) -> tuple[dict[int, dict], str]:
    """Computed topic affinity per candidate from affinity.topic_affinity (workstream AF), imported lazily.
    Status 'ok', 'unavailable' (module missing: every computed affinity is unknown) or 'failed' (it raised; logged, and the run
    goes on with the classifier verdict alone rather than losing the whole list). The reference time is the run's started_at."""
    try:
        from .affinity import topic_affinity
    except ImportError:
        log.warning("score: wearable_wishlist.affinity is not available; computed topic affinity is unknown for everyone")
        return {}, "unavailable"
    if not candidate_ids:
        return {}, "ok"
    try:
        out = topic_affinity(conn, candidate_ids=candidate_ids, now=(run or {}).get("started_at"), window_days=AFFINITY_WINDOW_DAYS)
    except Exception:   # noqa: BLE001 - a bug there must not stop the scheduled scoring; it is logged and listed as unknown
        log.exception("score: topic_affinity failed; computed topic affinity is unknown for everyone")
        return {}, "failed"
    return {int(k): v for k, v in (out or {}).items() if isinstance(v, dict)}, "ok"


def _closer_run(a: int, b: int, run_id: int) -> bool:
    """Prefer the latest run before run_id; failing that, the earliest run after it."""
    if a < run_id and b < run_id:
        return a > b
    if a < run_id:
        return True
    if b < run_id:
        return False
    return a < b


# ------------------------------------------------------------------ scoring one candidate

def _score_one(ctx: dict, cand: dict) -> dict:
    cid = cand["candidate_id"]
    cls = ctx["classifications"].get(cid) or {}
    mp = ctx["map_people"].get(cand.get("map_person_id")) if cand.get("map_person_id") is not None else None
    li = ctx["map_linkedin"].get(cand.get("map_person_id")) if cand.get("map_person_id") is not None else None
    xu = ctx["x_users"].get(cand.get("x_id")) if cand.get("x_id") else None
    eng = ctx["engagement"].get(cand.get("x_id")) if cand.get("x_id") else None
    category = _category(cand, cls, mp)
    section = config.SECTIONS.get(category, "Other")
    ref: dt.date = ctx["ref_date"]
    unknowns: list[str] = [u for u in (cls.get("unknowns") or []) if isinstance(u, str)]
    if not cls:
        unknowns.append("no classification yet: category and relevance come from the map or defaults")

    # ---- launch-wave praise: a quote or event on a wearable_wishlist post dated inside a wave (or from a paid-promo account)
    wave = ctx.get("wave") or {"posts": {}, "days": {}, "paid_promo": set()}
    promo = cid in wave["paid_promo"]

    def wave_praise(key: str | None, d: dt.date | None, wearable_wishlist: bool = False) -> str | None:
        p = wave["posts"].get(key) if key else None
        if p is None and not wearable_wishlist:
            return None
        d = (p or {}).get("date") or d
        if d in wave["days"]:
            return f"{d.isoformat()}, {wave['days'][d]} accounts praised wearable_wishlist within {LAUNCH_WAVE_DAYS} days"
        return "paid-promo bio" if promo else None

    # ---- which wear quotes only show voice-tool use: classify filed them under voice_tool_user and never under wearable_user.
    # A quote no stored classification names falls back to its rows: voice_tool_user writes wear+love, wearable_user wear+accept.
    srcs = ctx.get("wear_sources", {}).get(cid) or {"wearable": set(), "voice": set()}
    cand_ev = ctx["evidence"].get(cid, [])
    siblings: dict[tuple, set[str]] = {}
    for e in cand_ev:
        siblings.setdefault((e["url"], e["text"]), set()).add(e["supports"])

    def voice_only(e: dict) -> bool:
        t = e["text"] or ""
        if t in srcs["wearable"] or t in srcs["voice"]:
            return t not in srcs["wearable"]
        sib = siblings.get((e["url"], e["text"]), set())
        return "love" in sib and "accept" not in sib

    # ---- evidence sums per `supports`. accept/wear/love: one value per post (its strongest quote), then diminishing returns.
    sums = {s: 0.0 for s in config.SUPPORTS}
    ev_ids: dict[str, list[int]] = {s: [] for s in config.SUPPORTS}
    counts = {s: [0, 0] for s in config.SUPPORTS}   # supporting, contradicting
    per_post: dict[str, dict[str, float]] = {"accept": {}, "wear": {}, "love": {}}
    voice_ids: list[int] = []                        # wear quotes counted at VOICE_WEAR_WEIGHT
    wave_ids: list[int] = []                         # love quotes counted at LAUNCH_WAVE_WEIGHT
    wave_whys: list[str] = []
    accept_posts = {post_key(e["url"]) or f"#{e['evidence_id']}" for e in cand_ev
                    if e["supports"] == "accept" and e.get("kind") != "profile_signal"}
    spill_by_post: dict[str, float] = {}             # strongest signed wear/love weight per post not already filed under accept
    impact_counted, impact_counted_ids, impact_restated_ids = 0.0, [], []
    for e in cand_ev:
        s = e["supports"]
        if s not in sums:
            continue
        pol = 1 if (e["polarity"] if e["polarity"] is not None else 1) >= 0 else -1
        w = _clamp(float(e["weight"] if e["weight"] is not None else 1.0), 0.0, 1.0)
        # a profile_signal row (classify's not_a_person / sells_posts guards) is its own fact, not a quote of the bio it shares a URL with
        key = f"#{e['evidence_id']}" if e.get("kind") == "profile_signal" else (post_key(e["url"]) or f"#{e['evidence_id']}")
        if s in ("wear", "love") and key not in accept_posts and e.get("kind") != "profile_signal":
            if abs(pol * w) > abs(spill_by_post.get(key, 0.0)):
                spill_by_post[key] = pol * w         # spill is about accept: the wear/love discounts below do not apply
        if s == "wear" and voice_only(e):
            w *= VOICE_WEAR_WEIGHT
            voice_ids.append(e["evidence_id"])
        if s == "love":
            why = wave_praise(key, _date(e.get("date")))
            if why:
                w *= LAUNCH_WAVE_WEIGHT
                wave_ids.append(e["evidence_id"])
                wave_whys.append(why)
        ev_ids[s].append(e["evidence_id"])
        counts[s][0 if pol > 0 else 1] += 1
        if s in per_post:
            cur = per_post[s].get(key)
            if cur is None or abs(pol * w) > abs(cur):
                per_post[s][key] = pol * w
        else:
            sums[s] += pol * w
        if s == "impact":
            if e.get("kind") in IMPACT_RESTATED_KINDS:
                impact_restated_ids.append(e["evidence_id"])
            else:
                impact_counted += pol * w
                impact_counted_ids.append(e["evidence_id"])
    for s, posts in per_post.items():
        sums[s] = diminishing(posts.values())

    # ---- events: dated modifiers with decay; joined_competitor persists unless a later left_competitor
    evs = ctx["events"].get(cid, [])

    def ev_date(e: dict) -> dt.date | None:
        return _date(e.get("date")) or _date(e.get("fetched_at"))

    lefts = [ev_date(e) for e in evs if e["kind"] == "left_competitor"]
    active_joined: list[dict] = []
    for e in evs:
        if e["kind"] != "joined_competitor":
            continue
        d = ev_date(e)
        cancelled = any(ld is None or d is None or ld >= d for ld in lefts)
        if not cancelled:
            active_joined.append(e)
    active_joined_ids = {e["event_id"] for e in active_joined}

    mods = {"accept": 0.0, "wear": 0.0, "love": 0.0, "impact": 0.0}
    mod_notes: dict[str, list[str]] = {k: [] for k in mods}
    event_ids: dict[str, list[int]] = {k: [] for k in mods}
    joined_pen, joined_ids = 0.0, []                 # competitor penalty from joined_competitor events (combined with evidence below)
    for e in evs:
        kind = e["kind"]
        eff = EVENT_EFFECTS.get(kind) or {}
        if not eff or (kind == "joined_competitor" and e["event_id"] not in active_joined_ids):
            continue
        d = _date(e.get("date"))                     # the event's own date only: fetched_at is refreshed by every re-classification
        days = (ref - d).days if d else None
        if kind in PERSISTENT_EVENTS:
            factor = 1.0
        elif d is None:
            factor = UNDATED_EVENT_WEIGHT
        else:
            factor = decay(days)
        when = f"on {d.isoformat()}, {days}d ago" if d else "undated"
        if factor <= 0:
            for k in eff:
                if k == "impact" and category != "vc":
                    continue
                mod_notes[k].append(f"{EVENT_LABELS.get(kind, kind).capitalize()} {when} (event #{e['event_id']}): "
                                    f"faded out (older than {EVENT_DECAY_DAYS} days).")
            continue
        ev_wave = wave_praise(post_key(e.get("url")) or f"event:{e['event_id']}", d, wearable_wishlist=kind == "posted_about_product") \
            if "love" in eff else None
        for k, v in eff.items():
            if k == "impact" and category != "vc":
                continue
            delta = v * factor
            event_ids[k].append(e["event_id"])
            if kind in PERSISTENT_EVENTS and k == "accept":
                joined_pen = max(joined_pen, -delta)
                joined_ids.append(e["event_id"])
                continue
            wave_note = ""
            if k == "love" and ev_wave:
                delta *= LAUNCH_WAVE_WEIGHT
                wave_note = f" x{LAUNCH_WAVE_WEIGHT:.2f} launch-wave praise ({ev_wave})"
            mods[k] += delta
            mod_notes[k].append(f"{EVENT_LABELS.get(kind, kind).capitalize()} {when} (event #{e['event_id']}, weight {factor:.2f}"
                                f"{wave_note}): {delta:+.2f}.")

    # ---- topic affinity: computed from the person's own posts (affinity.py), bounded by the classifier's topic_voice verdict
    aff_entry = ctx["affinity"].get(cid) or {}
    computed = aff_entry.get("score")
    computed = round(_clamp(float(computed), 0.0, 1.0), P_DECIMALS) \
        if isinstance(computed, (int, float)) and not isinstance(computed, bool) else None
    tv_obj = cls.get("topic_voice") if isinstance(cls.get("topic_voice"), dict) else {"value": cls.get("topic_voice")}
    verdict = tv_obj.get("value") if isinstance(tv_obj.get("value"), bool) else None
    # source (the dashboard's convention): "computed" | "classifier" | "computed+classifier"; bound: which verdict limit applied
    aff_bound: str | None = None
    if verdict is True:
        affinity: float | None = max(computed or 0.0, AFFINITY_FLOOR_IF_VOICE)
        aff_bound = "floor" if computed is None or computed < AFFINITY_FLOOR_IF_VOICE else None
    elif verdict is False:
        # a 'no' with nothing measured means they talk about other things: 0, never more than an unknown
        affinity = min(computed if computed is not None else 0.0, AFFINITY_CAP_IF_NOT)
        aff_bound = "cap" if computed is not None and computed > AFFINITY_CAP_IF_NOT else None
    else:
        affinity = computed
    affinity = round(affinity, P_DECIMALS) if affinity is not None else None
    aff_source = ("computed+classifier" if computed is not None and verdict is not None
                  else "classifier" if verdict is not None else "computed" if computed is not None else None)
    aff_how = aff_source + (f", verdict {aff_bound}" if aff_bound else "") if aff_source else ""

    # ---- the three probabilities
    priors = PRIORS[category]
    factors: dict[str, dict] = {}

    def prob(name: str, key: str, direct: float, extra: float, penalty: float, extra_note: str | None,
             aff_weight: float = 0.0, warm: float = 0.0) -> float:
        reasons = [f"Prior for {section}: {priors[key]:.2f}."]
        x = _logit(priors[key])
        s = _clamp(direct + extra, -EVIDENCE_CAP, EVIDENCE_CAP)
        if ev_ids[key] or extra:
            x += EVIDENCE_SLOPE * s
            bits = []
            if ev_ids[key]:
                n_posts = len(per_post[key])
                bits.append(f"{counts[key][0]} supporting, {counts[key][1]} against ({_ids(ev_ids[key])}) from {n_posts} post(s)"
                            + (f", one value per post, the k-th strongest x{EVIDENCE_DECAY:g}^(k-1)" if n_posts > 1 else ""))
            if extra_note:
                bits.append(extra_note)
            reasons.append(f"Evidence for {key}: " + "; ".join(bits) + f" -> weighted {s:+.2f} -> {_sigmoid(x):.2f}.")
        if penalty:
            x -= penalty
            reasons.append(f"{penalty_note}: -{penalty:.2f} on accept -> {_sigmoid(x):.2f}.")
        if warm:
            x += warm
            reasons.append(f"{warm_note}: {warm:+.2f} log-odds -> {_sigmoid(x):.2f}.")
        aff_term = aff_weight * affinity if affinity else 0.0   # outside EVIDENCE_CAP, bounded by P_MAX below
        if aff_term:
            x += aff_term
            reasons.append(f"Topic affinity {affinity:.2f} ({aff_how}) x {aff_weight:.1f} = {aff_term:+.2f} log-odds, "
                           f"outside the evidence cap -> {_sigmoid(x):.2f}.")
        for note in mod_notes[key]:
            reasons.append(note)
        x += mods[key]
        p = _clamp(_sigmoid(x), P_MIN, P_MAX)
        aff_backed = key == "love" and affinity is not None and affinity >= AFFINITY_BACKED_MIN
        backed = bool(ev_ids[key]) or bool(event_ids[key]) or (key == "accept" and sums["competitor"] > 0) or aff_backed
        moved_by = (["spillover"] if extra else []) + (["warm relationship"] if warm else []) + (["topic affinity"] if aff_term else [])
        if aff_backed and not (ev_ids[key] or event_ids[key]):
            reasons.append(f"Backed by topic affinity {affinity:.2f} (at least {AFFINITY_BACKED_MIN:.2f}) without other {key} evidence.")
        if not backed and moved_by:
            reasons.append(f"No direct evidence or event on {key}: prior moved only by the {' and '.join(moved_by)} above.")
            unknowns.append(f"{name}: no direct evidence (prior plus "
                            + " and ".join("spillover from wear/love evidence" if m == "spillover" else m for m in moved_by) + ")")
        elif not backed:
            reasons.append(f"No evidence or event touches {key}: this is the prior only.")
            unknowns.append(f"{name}: no evidence, prior only")
        reasons.append(f"{name} = {p:.2f}.")
        factors[name] = {
            "value": round(p, P_DECIMALS), "prior": priors[key], "backed": backed, "reasons": reasons,
            "evidence_ids": sorted(set(ev_ids[key] + (ev_ids["competitor"] if key == "accept" and penalty else []))),
            "event_ids": sorted(set(event_ids[key])),
        }
        return factors[name]["value"]

    # 25% of each wear/love post also counts for accept, once per post, skipping posts already filed under accept
    spill = _clamp(ACCEPT_SPILLOVER * sum(spill_by_post.values()), -ACCEPT_SPILLOVER_CAP, ACCEPT_SPILLOVER_CAP)
    spill_note = (f"{ACCEPT_SPILLOVER:.0%} of {len(spill_by_post)} wear/love post(s) not filed under accept spills over "
                  f"({spill:+.2f}; people who wear and review devices take them)" if spill else None)
    voice_note = (f"voice-tool quote(s) {_ids(voice_ids)} at x{VOICE_WEAR_WEIGHT:g}: they prove the interface, not a device worn "
                  "every day" if voice_ids else None)
    wave_note = (f"launch-wave praise of wearable_wishlist {_ids(wave_ids)} at x{LAUNCH_WAVE_WEIGHT:g} ({wave_whys[0]})"
                 if wave_ids else None)
    # competitor tie: evidence and a joined_competitor event describe the same fact, so the larger penalty counts, not the sum
    ev_pen = _clamp(COMPETITOR_PENALTY * max(sums["competitor"], 0.0), 0.0, COMPETITOR_PENALTY_CAP)
    penalty = max(ev_pen, joined_pen)
    pen_bits = ([f"competitor evidence {_ids(ev_ids['competitor'])} -> {ev_pen:.2f}"] if ev_pen else []) + \
               ([f"joined a competitor (event {_ids(joined_ids)}) -> {joined_pen:.2f}"] if joined_pen else [])
    penalty_note = "Competitor tie: " + "; ".join(pen_bits) + ("; the same fact, counted once" if len(pen_bits) > 1 else "")
    # a real relationship (a direct map contact, or an introduction via a connector) raises the chance of a yes; no path, a follow
    # or open DMs add nothing and take nothing away
    path = ctx["paths"].get(cid)
    warm_how = (path or {}).get("how")
    warm = round(WARM_ACCEPT * _clamp(float(path.get("strength") or 0.0), 0.0, 1.0), P_DECIMALS) \
        if path and warm_how in WARM_PATH_KINDS else 0.0
    warm_note = (f"Warm relationship ({warm_how} path" + (f" through {path['via_name']}" if path and path.get("via_name") else "")
                 + f", strength {float((path or {}).get('strength') or 0):.2f}) x {WARM_ACCEPT:g}") if warm else ""
    p_accept = prob("p_accept", "accept", sums["accept"], spill, penalty, spill_note, AFFINITY_ACCEPT, warm)
    p_wear = prob("p_wear", "wear", sums["wear"], 0.0, 0.0, voice_note)
    p_love = prob("p_love", "love", sums["love"], 0.0, 0.0, wave_note, AFFINITY_LOVE)
    factors["p_wear"]["voice_tool_ids"] = sorted(voice_ids)
    factors["p_love"]["launch_wave_ids"] = sorted(wave_ids)

    # the affinity factor: value, where it came from, the posts behind it. supports='affinity' rows are cited, never summed:
    # they quote the posts the computed score already measures (sums['affinity'] is deliberately unused).
    top_posts = [{"url": tp.get("url"), "date": tp.get("date"), "text": _short(tp.get("text"), 200),
                  "engagement": tp.get("engagement"), "replies": tp.get("replies")}
                 for tp in (aff_entry.get("top_posts") or []) if isinstance(tp, dict)][:3]
    topics = [t for t in (tv_obj.get("topics") or []) if isinstance(t, str)]
    areasons = []
    if computed is not None:
        areasons.append(f"Computed from own posts: {computed:.2f}." + (f" {aff_entry['reason']}" if aff_entry.get("reason") else ""))
    elif ctx.get("affinity_status") == "unavailable":
        areasons.append("Computed affinity unknown: the affinity module is not available in this run.")
    elif ctx.get("affinity_status") == "failed":
        areasons.append("Computed affinity unknown: the computation failed in this run (see the log).")
    else:
        areasons.append("Computed affinity unknown: no posts of this person are stored.")
    verdict_note = None
    if verdict is True:
        topics_txt = f" ({', '.join(topics)})" if topics else ""
        verdict_note = (f"Classifier: topic voice{topics_txt}, so at least {AFFINITY_FLOOR_IF_VOICE:.2f}"
                        + (" (raised to that floor)." if aff_bound == "floor" else " (the computed score is higher)."))
    elif verdict is False:
        verdict_note = (f"Classifier: not a topic voice, so at most {AFFINITY_CAP_IF_NOT:.2f}"
                        + (" (capped there)." if aff_bound == "cap" else " (the computed score is lower)."))
    if verdict_note:
        areasons.append(verdict_note)
    if ev_ids["affinity"]:
        areasons.append(f"Classifier evidence {_ids(ev_ids['affinity'])} ({counts['affinity'][0]} for, {counts['affinity'][1]} against): "
                        "cited, not added again (the affinity above already measures these posts).")
    if affinity is None:
        why_unknown = {"unavailable": "affinity module not available",
                       "failed": "affinity computation failed"}.get(ctx.get("affinity_status"), "no posts stored")
        areasons.append("Topic affinity unknown: no effect on p_love or p_accept.")
        unknowns.append(f"topic affinity unknown ({why_unknown})")
        aff_reason = f"Topic affinity unknown ({why_unknown}) and no classifier verdict."
    else:
        areasons.append(f"affinity = {affinity:.2f} ({aff_how}): p_love {AFFINITY_LOVE * affinity:+.2f}, "
                        f"p_accept {AFFINITY_ACCEPT * affinity:+.2f} log-odds.")
        # one string for the dashboard: affinity.py's sentence, then what the classifier verdict did to it
        aff_reason = " ".join(x for x in ((aff_entry.get("reason") or "").strip() if computed is not None else "", verdict_note or "") if x) \
            or f"Topic affinity {affinity:.2f}."
    factors["affinity"] = {
        "value": affinity, "source": aff_source, "reason": aff_reason, "top_posts": top_posts,
        "evidence_ids": sorted(ev_ids["affinity"]), "event_ids": [], "reasons": areasons,
        "computed": computed, "verdict": verdict, "bound": aff_bound, "topics": topics,
        "topical_posts": aff_entry.get("topical_posts"), "last_topical": aff_entry.get("last_topical"),
    }

    # ---- impact
    followers = xu["followers"] if xu and xu.get("followers") is not None else None
    topical_n = aff_entry.get("topical_posts") if isinstance(aff_entry.get("topical_posts"), int) else 0
    topic_eng = aff_entry.get("topic_eng_avg")
    # affinity.py counts replies twice; impact counts them once like the all-posts average, so remove one copy
    if isinstance(topic_eng, (int, float)) and isinstance(aff_entry.get("replies_per_topic_post"), (int, float)):
        topic_eng = max(0.0, float(topic_eng) - float(aff_entry["replies_per_topic_post"]))
    if topical_n >= AFFINITY_MIN_TOPIC_POSTS and isinstance(topic_eng, (int, float)):
        eng_avg, eng_source = float(topic_eng), "topic"      # what their audience does when they talk about the wearable's themes
        eng_text = (f"~{eng_avg:,.0f} engagements per topical post over {topical_n} topical posts "
                    f"(topic engagement: at least {AFFINITY_MIN_TOPIC_POSTS} topical posts)")
    else:
        eng_avg = float(eng["eng"]) if eng and eng.get("eng") is not None else None
        eng_source = "all" if eng_avg is not None else None
        eng_text = (f"~{eng_avg:,.0f} engagements per post over all {int(eng['n'])} posts"
                    + (f" (only {topical_n} topical, topic engagement needs {AFFINITY_MIN_TOPIC_POSTS})" if topical_n else "")
                    if eng_avg is not None else "")
    rel = cls.get("audience_relevance")
    rel = float(rel) if isinstance(rel, (int, float)) else None
    f_att, e_att = log_followers(followers), log_engagement(eng_avg)
    attention = 0.6 * f_att + 0.4 * e_att
    role = ROLE_LEVERAGE[category]
    leverage = role["base"] + role["attention"] * attention
    rel_factor = 0.5 + 0.5 * (rel if rel is not None else RELEVANCE_UNKNOWN)
    impact = leverage * rel_factor
    ireasons = []
    ireasons.append(
        (f"{int(followers):,} followers -> {f_att:.2f}" if followers is not None else f"Followers unknown -> assumed {f_att:.2f}")
        + "; " + (f"{eng_text} -> {e_att:.2f}" if eng_avg is not None else f"no posts stored -> engagement assumed {e_att:.2f}")
        + f"; attention {attention:.2f}.")
    ireasons.append(f"Role leverage for {section}: {role['base']:.2f} + {role['attention']:.2f} x attention = {leverage:.2f}.")
    ireasons.append((f"Audience relevance {rel:.2f} (classification)" if rel is not None else f"Audience relevance unknown -> assumed {RELEVANCE_UNKNOWN:.2f}")
                    + f" -> x{rel_factor:.2f} = {impact:.2f}.")
    if followers is None:
        unknowns.append("follower count unknown")
    if eng_avg is None:
        unknowns.append("engagement unknown (no posts stored)")
    if rel is None:
        unknowns.append("audience relevance unknown")
    if impact_counted_ids:
        bump = _clamp(IMPACT_EVIDENCE_STEP * impact_counted, -IMPACT_EVIDENCE_CAP, IMPACT_EVIDENCE_CAP)
        impact += bump
        ireasons.append(f"Impact evidence ({_ids(impact_counted_ids)}): {bump:+.2f}.")
    if impact_restated_ids:
        ireasons.append(f"Profile signals {_ids(impact_restated_ids)} restate the followers and audience relevance used above: "
                        "cited, not added again.")
    for note in mod_notes["impact"]:
        ireasons.append(note)
    impact += mods["impact"]
    impact = round(_clamp(impact, 0.0, 1.0), P_DECIMALS)
    ireasons.append(f"impact = {impact:.2f}.")
    factors["impact"] = {"value": impact, "reasons": ireasons, "evidence_ids": sorted(ev_ids["impact"]),
                         "event_ids": sorted(event_ids["impact"]),
                         "engagement": {"source": eng_source, "avg": round(eng_avg, 1) if eng_avg is not None else None}}

    # ---- community engagement: the average post's views, likes, replies and reposts per follower against accounts of the
    # same size (engagement.py). Its own term in the total (weights.py); unknown counts as typical (x1.0).
    kp = (ctx.get("community") or {}).get(cand.get("x_id")) if cand.get("x_id") else None
    eng_value = kp.get("value") if kp else None
    factors["engagement"] = {"value": eng_value, "short": engagement_kpis.short(kp), "reasons": engagement_kpis.explain(kp),
                             "kpis": kp, "evidence_ids": [], "event_ids": []}
    if eng_value is None:
        unknowns.append(f"community engagement unknown ({(kp or {}).get('why') or engagement_kpis.NO_SAMPLE})")

    # ---- reach: how the operator could approach the person. Information only, not in the total; a warm path nudged p_accept above.
    flags: list[str] = []
    flag_reasons: list[str] = []
    flag_details: list[dict] = []
    rreasons = []
    no_path = path is None or path.get("strength") is None or path.get("how") == "none"
    if no_path:
        reach = REACH_NONE
        if path is not None and path.get("how") == "none":
            rreasons.append(f"No path: paths.py found no relationship in the operator's network (run {path['run_id']}); approach cold. "
                            "Information only: it does not lower the score.")
        else:
            rreasons.append("No path computed for this candidate yet. Information only: it does not lower the score.")
            unknowns.append("no path computed in this run")
        if ev_ids["reach"]:
            rreasons.append(f"Reachability signals on file ({_ids(ev_ids['reach'])}).")
    else:
        reach = round(_clamp(float(path["strength"]), 0.0, 1.0), P_DECIMALS)
        via = f" via {path['via_name']}" if path.get("via_name") else ""
        stale = "" if path["run_id"] == ctx["run_id"] else f", from run {path['run_id']}"
        rreasons.append(f"Best path: {path['how']}{via} (strength {reach:.2f}{stale}). Information for the approach, not in the "
                        "total" + (f"; as a real relationship it adds {warm:+.2f} log-odds to p_accept." if warm else "."))
    factors["reach"] = {"value": reach, "reasons": rreasons, "evidence_ids": sorted(ev_ids["reach"]), "event_ids": [],
                        "no_path": no_path, "warm_accept": warm,
                        "path": {k: path.get(k) for k in ("how", "via_name", "strength", "run_id")} if path else None}

    # ---- flags (never zero the score)
    tie = cls.get("competitor_tie") if isinstance(cls.get("competitor_tie"), dict) else {}
    which: str | None = None
    if cand.get("competitor_flag"):
        which = _competitor_name(cand.get("competitor_detail")) or tie.get("which") or "unspecified"
        flag_reasons.append(f"competitor:{which}: {cand.get('competitor_detail') or 'competitor tie recorded by classification'}"
                            + (f" ({_ids(ev_ids['competitor'])})" if ev_ids["competitor"] else "") + ".")
    elif tie.get("value") is True:
        which = tie.get("which") or _competitor_name(str(tie.get("evidence"))) or "unspecified"
        flag_reasons.append(f"competitor:{which}: classification found a {tie.get('kind') or 'competitor'} tie.")
    elif sums["competitor"] > 0:
        texts = " ".join((e.get("text") or "") for e in ctx["evidence"].get(cid, []) if e["supports"] == "competitor")
        which = _competitor_name(texts) or "unspecified"
        flag_reasons.append(f"competitor:{which}: competitor evidence on file ({_ids(ev_ids['competitor'])}).")
    if which:
        flags.append(f"competitor:{which}")
    for e in active_joined:
        w = _competitor_name(e.get("text")) or "unspecified"
        flags.append(f"competitor:{w}")
        flag_reasons.append(f"competitor:{w}: joined a competitor on {_date(e.get('date')).isoformat() if _date(e.get('date')) else 'unknown date'}"
                            f" (event #{e['event_id']}); p_accept penalised until a left_competitor event.")
    if category == "vc" or ((mp or {}).get("sector") or "").lower() == "vc":
        text = " ".join(t for t in [(mp or {}).get("company"), (li or {}).get("company"), (li or {}).get("position"),
                                    (xu or {}).get("bio")] if t)
        for f in fund_matches(text, ctx["funds"]):
            flags.append(f"competitor:{f['competitor']}")
            flag_reasons.append(f"competitor:{f['competitor']}: {f['fund']} appears in this person's company/bio and is listed as a "
                                f"{f['competitor']} investor ({f.get('round') or 'round unknown'}).")
            flag_details.append({"flag": f"competitor:{f['competitor']}", "fund": f["fund"], "competitor": f["competitor"],
                                 "round": f.get("round")} | ({"source_url": f["source_url"]} if f.get("source_url") else {}))
    tp = ctx.get("tie_people", {}).get((cand.get("handle") or "").strip().lstrip("@").lower()) if cand.get("handle") else None
    if tp:
        comp, kind = tp["competitor"], tp.get("kind") or "tie"
        flags.append(f"competitor:{comp}")
        flag_reasons.append(f"competitor:{comp}: {tp.get('name') or cand.get('handle')} is a {kind} of {comp} "
                            f"(config/competitor_ties.yaml{', ' + tp['source_url'] if tp.get('source_url') else ''}).")
        flag_details.append({"flag": f"competitor:{comp}", "competitor": comp, "kind": kind, "source_url": tp.get("source_url")})
    us = cand.get("us_based")
    if us is None and cls.get("us_based") in (True, False):
        us = 1 if cls.get("us_based") else 0
    if us == 0:
        flags.append("not_us")
        flag_reasons.append("not_us: " + (cls.get("us_based_evidence") or "not US-based per classification") + ".")
    elif us is None:
        unknowns.append("US base unknown")
    auth = cls.get("handle_authenticity") if isinstance(cls.get("handle_authenticity"), dict) else {}
    hv = cand.get("handle_verified") or auth.get("value") or "unknown"
    if hv not in ("verified", "likely_real"):
        flags.append("unverified_handle")
        why = auth.get("reasons") or ("handle looks suspicious" if hv == "suspicious" else "handle not verified yet")
        created = _date((xu or {}).get("created_at"))
        if created and (ref - created).days < 365:
            why += f"; account created {created.isoformat()}"
        flag_reasons.append(f"unverified_handle ({hv}): {why}.")
        if hv == "unknown":
            unknowns.append("handle not verified")
    seen: set[str] = set()
    flags = [f for f in flags if not (f in seen or seen.add(f))]
    factors["flags"] = {"value": flags, "reasons": flag_reasons, "details": flag_details,
                        "evidence_ids": sorted(set(ev_ids["competitor"] + ev_ids["us"] + ev_ids["authenticity"])),
                        "event_ids": sorted(e["event_id"] for e in active_joined)}

    # ---- confidence and total
    backed_n = sum(1 for k in ("p_accept", "p_wear", "p_love") if factors[k]["backed"])
    confidence = round(backed_n / 3.0, P_DECIMALS)
    # default weights: 100 x p_accept x p_wear x p_love x (0.3 + 0.7 x impact); config/weights.yaml `default:` reweights it
    w_total, w_source = ctx.get("weights") or weights.DEFAULT_WEIGHTS, ctx.get("weights_source")
    tf = {"p_accept": p_accept, "p_wear": p_wear, "p_love": p_love, "impact": impact, "reach": reach,
          "affinity": affinity, "engagement": eng_value, "category": category}
    total = round(_clamp(weights.weighted_total(tf, w_total), 0.0, 100.0), TOTAL_DECIMALS)
    factors["confidence"] = {"value": confidence, "reasons": [
        f"{backed_n} of 3 probabilities are backed by evidence or events ({', '.join(k for k in ('p_accept', 'p_wear', 'p_love') if factors[k]['backed']) or 'none'})."]}
    factors["total"] = {"value": total, "reasons": [weights.formula_text(tf, w_total, total, w_source)]}

    seen_u: set[str] = set()
    unknowns = [u for u in unknowns if not (u in seen_u or seen_u.add(u))]
    return {
        "candidate_id": cid, "name": cand.get("name") or cand.get("handle") or f"candidate {cid}", "handle": cand.get("handle"),
        "category": category, "section": section, "summary": cls.get("summary"),
        "p_accept": p_accept, "p_wear": p_wear, "p_love": p_love, "impact": impact, "reach": reach, "affinity": affinity,
        "engagement": eng_value, "confidence": confidence, "total": total, "rank": None, "flags": flags, "factors": factors, "unknowns": unknowns,
        # what this run saw, so the next run can tell new evidence from re-classified evidence (fetched_at is refreshed on upsert)
        "seen": {"evidence": sorted(e["evidence_id"] for e in cand_ev), "events": sorted(e["event_id"] for e in evs)},
    }


# ------------------------------------------------------------------ change log

def _describe_evidence(e: dict) -> str:
    against = "" if (e.get("polarity") if e.get("polarity") is not None else 1) >= 0 else ", against"
    return f"{e.get('kind') or 'evidence'} '{_short(e.get('text'))}' ({e.get('supports')}{against}, #{e['evidence_id']})"


def _describe_event(e: dict) -> str:
    d = _date(e.get("date"))
    return f"{EVENT_LABELS.get(e.get('kind'), e.get('kind'))} on {d.isoformat() if d else 'unknown date'} (#{e['event_id']})"


def _whats_new(new_ev: list[dict], new_events: list[dict], path_note: str | None) -> str | None:
    bits = []
    if new_ev:
        bits.append("new evidence: " + "; ".join(_describe_evidence(e) for e in new_ev[:4])
                    + (f" (+{len(new_ev) - 4} more)" if len(new_ev) > 4 else ""))
    if new_events:
        bits.append("new events: " + "; ".join(_describe_event(e) for e in new_events[:4]))
    if path_note:
        bits.append(path_note)
    return "; ".join(bits) if bits else None


def _fmt_affinity(v: Any) -> str:
    return "unknown" if v is None else f"{float(v):.2f}"


def _affinity_change_reason(prev_f: dict, cur_f: dict, new_ev: list[dict]) -> str:
    """Why topic affinity moved: topical posts the previous run did not have among the best ones, the topical post count, a new
    classifier verdict, new supports='affinity' evidence; else the fade of the 90-day window."""
    bits = []
    prev_urls = {tp.get("url") for tp in prev_f.get("top_posts") or [] if isinstance(tp, dict)}
    fresh = [tp for tp in cur_f.get("top_posts") or [] if isinstance(tp, dict) and tp.get("url") and tp["url"] not in prev_urls]
    if fresh:
        bits.append("new topical posts: " + "; ".join(f"'{_short(tp.get('text'))}' ({tp.get('date') or 'undated'}, {tp['url']})"
                                                      for tp in fresh))
    n_old, n_new = prev_f.get("topical_posts"), cur_f.get("topical_posts")
    if isinstance(n_new, int) and n_new != n_old:
        bits.append(f"{n_new} topical posts in {AFFINITY_WINDOW_DAYS} days" + (f" (was {n_old})" if isinstance(n_old, int) else "")
                    + (f", last on {cur_f['last_topical']}" if cur_f.get("last_topical") else ""))
    if cur_f.get("verdict") != prev_f.get("verdict"):
        bound = {"floor": f", raised to the {AFFINITY_FLOOR_IF_VOICE:.2f} floor",
                 "cap": f", capped at {AFFINITY_CAP_IF_NOT:.2f}"}.get(cur_f.get("bound") or "", "")
        bits.append(f"classifier verdict: {AFFINITY_VERDICTS.get(prev_f.get('verdict'))} -> "
                    f"{AFFINITY_VERDICTS.get(cur_f.get('verdict'))}{bound}")
    if new_ev:
        bits.append("new classifier evidence: " + "; ".join(_describe_evidence(e) for e in new_ev[:3]))
    if not bits:
        bits.append(f"re-computed from the stored posts (topical posts fade over {AFFINITY_WINDOW_DAYS} days)")
    return "; ".join(bits)


def _write_changes(conn: sqlite3.Connection, run_id: int, results: dict[int, dict], ctx: dict) -> tuple[int, int]:
    prev_id = db.previous_scored_run_id(conn, run_id)
    prev_run = db.one(conn, "SELECT * FROM runs WHERE run_id=?", (prev_id,)) if prev_id else None
    cutoff = (prev_run.get("finished_at") or prev_run.get("started_at")) if prev_run else None
    prev_scores = {r["candidate_id"]: r for r in db.rows(conn, "SELECT * FROM scores WHERE run_id=?", (prev_id,))} if prev_id else {}
    prev_reasons = {cid: db.jloads(r.get("reasons_json"), {}) or {} for cid, r in prev_scores.items()}
    prev_paths: dict[int, dict] = {}
    if prev_id:
        for p in db.rows(conn, "SELECT candidate_id, how, via_name, strength FROM paths WHERE run_id=? ORDER BY rank, strength DESC", (prev_id,)):
            prev_paths.setdefault(p["candidate_id"], p)

    conn.execute("DELETE FROM changes WHERE run_id=?", (run_id,))
    n_changes = n_new = 0

    def add(cid: int, field: str, old: Any, new: Any, reason: str) -> None:
        nonlocal n_changes
        conn.execute("INSERT INTO changes (run_id, candidate_id, field, old_value, new_value, reason) VALUES (?,?,?,?,?,?)",
                     (run_id, cid, field, None if old is None else str(old), None if new is None else str(new), reason))
        n_changes += 1

    for cid, r in results.items():
        prev = prev_scores.get(cid)
        if prev is None:
            n_new += 1
            why = r["summary"] or next((x for x in r["factors"]["p_wear"]["reasons"] if "Evidence" in x), None) or "no evidence yet, priors only"
            add(cid, "new_candidate", None, r["total"],
                f"New on the list: {r['name']} ({r['section']}), total {r['total']:.1f}, rank {r['rank']}. {why}")
            continue

        # New = evidence/events the previous run did not see. fetched_at cannot tell: classify re-upserts every row it
        # re-classifies, which refreshes fetched_at. Rows scored before `seen` existed fall back to fetched_at.
        seen = prev_reasons.get(cid, {}).get("seen")
        new_ev: list[dict] = []
        new_events: list[dict] = []
        if isinstance(seen, dict):
            seen_ev, seen_events = set(seen.get("evidence") or []), set(seen.get("events") or [])
            new_ev = [e for e in ctx["evidence"].get(cid, []) if e["evidence_id"] not in seen_ev]
            new_events = sorted((e for e in ctx["events"].get(cid, []) if e["event_id"] not in seen_events), key=lambda e: e["event_id"])
        elif cutoff:
            new_ev = db.rows(conn, "SELECT * FROM evidence WHERE candidate_id=? AND fetched_at > ? ORDER BY evidence_id", (cid, cutoff))
            new_events = db.rows(conn, "SELECT * FROM events WHERE candidate_id=? AND fetched_at > ? ORDER BY event_id", (cid, cutoff))
        cur_path = r["factors"]["reach"].get("path")
        prev_reach = (prev_reasons.get(cid, {}).get("factors") or {}).get("reach")
        # the path the previous run actually used (possibly a stale one from an older run), else that run's paths rows
        old_path = prev_reach.get("path") if isinstance(prev_reach, dict) else prev_paths.get(cid)
        path_note = None
        if (cur_path or {}).get("how") != (old_path or {}).get("how") or (cur_path or {}).get("via_name") != (old_path or {}).get("via_name"):
            def lab(p: dict | None) -> str:
                if not p or not p.get("how") or p.get("how") == "none":
                    return "none"
                return f"{p['how']}" + (f" via {p['via_name']}" if p.get("via_name") else "") + f" ({float(p.get('strength') or 0):.2f})"
            path_note = f"path: {lab(old_path)} -> {lab(cur_path)}"
        general = _whats_new(new_ev, new_events, path_note)
        # topic affinity: unknown counts as 0 for the size of the move (it adds nothing to the probabilities). When it moved,
        # the other rows (p_love, p_accept, total) name it too, instead of "re-scored with same evidence".
        old_a, new_a = prev.get("affinity"), r["affinity"]
        aff_moved = round(abs(float(new_a or 0.0) - float(old_a or 0.0)), P_DECIMALS) >= AFFINITY_CHANGE_THRESHOLD
        if aff_moved:
            aff_note = f"topic affinity {_fmt_affinity(old_a)} -> {_fmt_affinity(new_a)}"
            general = f"{general}; {aff_note}" if general else aff_note
        # community engagement: a new sample of their latest posts (or the benchmark) moved it; the total and rank rows name it
        old_e, new_e = prev.get("engagement"), r["engagement"]
        eng_moved = (old_e is None) != (new_e is None) or (
            new_e is not None and round(abs(float(new_e) - float(old_e)), P_DECIMALS) >= CHANGE_THRESHOLD)
        eng_why = None
        if eng_moved:
            prev_ef = (prev_reasons.get(cid, {}).get("factors") or {}).get("engagement")
            old_s = ((prev_ef.get("kpis") or {}) if isinstance(prev_ef, dict) else {}).get("fetched_at")
            new_s = (r["factors"]["engagement"].get("kpis") or {}).get("fetched_at")
            eng_why = (f"new sample of their latest posts ({str(new_s)[:10]})" if new_s and new_s != old_s
                       else "benchmark of accounts the same size moved")
            eng_note = f"community engagement {_fmt_affinity(old_e)} -> {_fmt_affinity(new_e)}"
            general = f"{general}; {eng_note}" if general else eng_note
        fallback = "re-scored with same evidence" + (" (dated events fade over 90 days)" if ctx["events"].get(cid) else "")
        if prev_reasons.get(cid, {}).get("version") != SCORING_VERSION:
            fallback = SCORING_VERSION_NOTE
            general = f"{general}; {SCORING_VERSION_NOTE}" if general else None

        def factor_reason(field: str) -> str:
            sup = _FACTOR_SUPPORTS.get(field, set())
            ev = [e for e in new_ev if e["supports"] in sup]
            key = _FACTOR_EVENT_KEY.get(field)
            evs = [e for e in new_events if key and key in (EVENT_EFFECTS.get(e["kind"]) or {})]
            note = path_note if field == "reach" else None
            return _whats_new(ev, evs, note) or general or fallback

        for field in ("p_accept", "p_wear", "p_love", "impact", "reach"):
            old, new = prev.get(field), r[field]
            if old is None or abs(float(new) - float(old)) >= CHANGE_THRESHOLD:
                add(cid, field, old, new, f"{field} {float(old or 0):.2f} -> {float(new):.2f}: {factor_reason(field)}.")
        if eng_moved:
            add(cid, "engagement", old_e, new_e,
                f"community engagement {_fmt_affinity(old_e)} -> {_fmt_affinity(new_e)}: {eng_why}.")
        if aff_moved:
            prev_af = (prev_reasons.get(cid, {}).get("factors") or {}).get("affinity")
            why = _affinity_change_reason(prev_af if isinstance(prev_af, dict) else {}, r["factors"]["affinity"],
                                          [e for e in new_ev if e["supports"] == "affinity"])
            add(cid, "affinity", old_a, new_a, f"topic affinity {_fmt_affinity(old_a)} -> {_fmt_affinity(new_a)}: {why}.")
        old_t, new_t = prev.get("total"), r["total"]
        total_moved = old_t is None or abs(float(new_t) - float(old_t)) >= TOTAL_CHANGE_THRESHOLD
        if total_moved:
            add(cid, "total", old_t, new_t, f"Total {float(old_t or 0):.1f} -> {float(new_t):.1f}: {general or fallback}.")
        if prev.get("rank") != r["rank"]:
            why = (general or fallback) if total_moved else "other candidates moved; re-scored with same evidence"
            add(cid, "rank", prev.get("rank"), r["rank"], f"Rank {prev.get('rank')} -> {r['rank']}: {why}.")
        old_flags = [f for f in db.jloads(prev.get("flags"), []) or [] if f != "no_path"]   # no longer a flag: not a change
        if set(old_flags) != set(r["flags"]):
            added = [f for f in r["flags"] if f not in old_flags]
            removed = [f for f in old_flags if f not in r["flags"]]
            detail = "; ".join(x for x in r["factors"]["flags"]["reasons"] if any(x.startswith(a) for a in added))
            parts = []
            if added:
                parts.append("added " + ", ".join(added) + (f" ({detail})" if detail else ""))
            if removed:
                parts.append("removed " + ", ".join(removed))
            add(cid, "flags", db.jdumps(old_flags), db.jdumps(r["flags"]), "Flags: " + "; ".join(parts) + ".")
    return n_changes, n_new


# ------------------------------------------------------------------ public API

def run_score(conn: sqlite3.Connection, run_id: int) -> dict:
    """Score every candidate with a classification, evidence or map link for `run_id`; write scores and changes. Idempotent."""
    ctx = _load(conn, run_id)
    ctx["weights"], ctx["weights_source"] = weights.scheduled_weights(WEIGHTS_FILE)
    log.info("score: run %s, %d candidates, reference date %s, weights %s", run_id, len(ctx["candidates"]), ctx["ref_date"],
             ctx["weights_source"] or "built-in defaults")
    results = {c["candidate_id"]: _score_one(ctx, c) for c in ctx["candidates"]}
    # People rejected by hand (status 'rejected') are still scored, so their evidence stays visible, but rank after everyone
    # else: the top 100 is the top 100 of people still on the list.
    rejected = {r["candidate_id"] for r in db.rows(conn, "SELECT candidate_id FROM status WHERE status='rejected'")}
    ranked = sorted(results.values(), key=lambda r: (r["candidate_id"] in rejected, -r["total"], -r["confidence"], r["candidate_id"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    existing = {row["candidate_id"] for row in db.rows(conn, "SELECT candidate_id FROM scores WHERE run_id=?", (run_id,))}
    for r in ranked:
        db.upsert(conn, "scores", {
            "run_id": run_id, "candidate_id": r["candidate_id"], "p_accept": r["p_accept"], "p_wear": r["p_wear"], "p_love": r["p_love"],
            "impact": r["impact"], "reach": r["reach"], "affinity": r["affinity"], "engagement": r["engagement"],
            "confidence": r["confidence"], "total": r["total"], "rank": r["rank"],
            "flags": db.jdumps(r["flags"]),
            "reasons_json": db.jdumps({"category": r["category"], "section": r["section"], "summary": r["summary"],
                                       "factors": r["factors"], "unknowns": r["unknowns"], "seen": r["seen"],
                                       "version": SCORING_VERSION}),
        }, keys=("run_id", "candidate_id"))
        log.debug("score: #%d %s total=%.2f flags=%s", r["rank"], r["name"], r["total"], r["flags"])
    for stale in existing - set(results):
        conn.execute("DELETE FROM scores WHERE run_id=? AND candidate_id=?", (run_id, stale))
    n_changes, n_new = _write_changes(conn, run_id, results, ctx)
    conn.commit()
    counts = {
        "run_id": run_id, "scored": len(results), "changes": n_changes, "new_candidates": n_new,
        "flagged": sum(1 for r in results.values() if r["flags"]),
        "competitor_flagged": sum(1 for r in results.values() if any(f.startswith("competitor:") for f in r["flags"])),
        "no_path": sum(1 for r in results.values() if r["factors"]["reach"]["no_path"]),   # information, not a flag
        "previous_run_id": db.previous_scored_run_id(conn, run_id),
    }
    log.info("score: %s", counts)
    return counts


def explain(conn: sqlite3.Connection, run_id: int, candidate_id: int) -> dict:
    """Per-candidate explanation for the dashboard. Reads the stored score; computes live when the candidate is not scored yet."""
    row = db.one(conn, "SELECT * FROM scores WHERE run_id=? AND candidate_id=?", (run_id, candidate_id))
    if row:
        stored = db.jloads(row["reasons_json"], {}) or {}
        factors = stored.get("factors") or {}
        values = {k: row.get(k) for k in ("p_accept", "p_wear", "p_love", "impact", "reach", "affinity", "engagement",
                                           "confidence", "total", "rank")}
        flags = db.jloads(row["flags"], []) or []
        unknowns = stored.get("unknowns") or []
        meta = {"category": stored.get("category"), "section": stored.get("section"), "summary": stored.get("summary")}
    else:
        ctx = _load(conn, run_id)
        cand = next((c for c in ctx["candidates"] if c["candidate_id"] == candidate_id), None)
        if cand is None:
            return {"candidate_id": candidate_id, "run_id": run_id, "factors": {}, "reasons": ["Not scored: no classification, evidence or map link."],
                    "evidence_ids": [], "event_ids": [], "flags": [], "unknowns": [], "per_factor": {}}
        r = _score_one(ctx, cand)
        factors = r["factors"]
        values = {k: r[k] for k in ("p_accept", "p_wear", "p_love", "impact", "reach", "affinity", "engagement",
                                    "confidence", "total", "rank")}
        flags, unknowns = r["flags"], r["unknowns"]
        meta = {"category": r["category"], "section": r["section"], "summary": r["summary"]}
    reasons: list[str] = []
    evidence_ids: set[int] = set()
    event_ids: set[int] = set()
    for name in ("p_accept", "p_wear", "p_love", "affinity", "impact", "engagement", "reach", "flags", "confidence", "total"):
        f = factors.get(name) or {}
        reasons.extend(f"{name}: {s}" for s in f.get("reasons", []))
        evidence_ids.update(f.get("evidence_ids", []))
        event_ids.update(f.get("event_ids", []))
    return {
        "candidate_id": candidate_id, "run_id": run_id, **meta, "factors": values, "reasons": reasons,
        "evidence_ids": sorted(evidence_ids), "event_ids": sorted(event_ids), "flags": flags, "unknowns": unknowns,
        "per_factor": factors,
    }


def score_candidate(conn: sqlite3.Connection, candidate_id: int, run_id: int | None, *, affinity: dict | None = None,
                    path: dict | None = None) -> dict:
    """One candidate scored with the current code against run `run_id`'s context (reference date, launch wave, funds,
    scheduled weights), as run_score would, WITHOUT writing anything: the dashboard's 'Check an account' (wearable_wishlist.check).
    Their latest classification counts even when it is newer than the run (a check classifies them after it). `affinity`
    (a topic_affinity entry) and `path` (the best path: how, strength, via_name) replace what the context holds for them.
    Returns _score_one's dict (rank None). KeyError when the candidate does not exist."""
    cand = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,))
    if cand is None:
        raise KeyError(candidate_id)
    ctx = _load(conn, run_id if run_id is not None else -1)
    ctx["weights"], ctx["weights_source"] = weights.scheduled_weights(WEIGHTS_FILE)
    latest = db.one(conn, "SELECT json FROM classifications WHERE candidate_id=? ORDER BY run_id DESC LIMIT 1", (candidate_id,))
    if latest:
        ctx["classifications"][candidate_id] = db.jloads(latest["json"], {}) or {}
    if affinity is not None:
        ctx["affinity"][candidate_id] = affinity
    if path is not None:
        ctx["paths"][candidate_id] = {"candidate_id": candidate_id, "run_id": ctx["run_id"], "rank": 1, **path}
    return _score_one(ctx, cand)


def _main() -> None:
    ap = argparse.ArgumentParser(description="Score candidates and write the change log.")
    ap.add_argument("--run-id", type=int, default=None, help="score an existing run (default: create a new 'score' run)")
    ap.add_argument("--explain", type=int, default=None, help="print the explanation for one candidate_id and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    if args.explain is not None:
        rid = args.run_id or db.latest_run_id(conn, with_scores=True)
        print(db.jdumps(explain(conn, rid, args.explain)))
        return
    run_id = args.run_id or db.new_run(conn, "score")
    counts = run_score(conn, run_id)
    if args.run_id is None:
        db.finish_run(conn, run_id)
    print(db.jdumps(counts))


if __name__ == "__main__":
    _main()
