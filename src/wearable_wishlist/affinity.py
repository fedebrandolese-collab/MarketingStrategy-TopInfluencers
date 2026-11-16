"""Topic affinity: does a person keep discussing personal agents (and the wearable's other themes), and does a community answer?

Computed from the person's own stored posts only (x_posts.x_id = candidates.x_id, 'latest_status' rows included). No LLM,
no network, no writes: score.py combines it with the classifier's `topic_voice` verdict (docs/ARCHITECTURE.md, "Topic
affinity").

    topic_affinity(conn, candidate_ids=None, now=None, window_days=90) -> {candidate_id: {...}}

One query and one pass over x_posts (joined to candidates), so all ~2,600 candidates cost well under a second.

What counts as a topical post (`post_topics`)
---------------------------------------------
Four themes, the same keys as the classifier's `topic_voice.topics`:
  personal_agents (weight 1.0)  "personal agent", "AI agent(s)", "agentic", "voice agent", "agent mode", "multi-agent",
                                "AI assistant/companion", named agent products (OpenClaw / Clawdbot / Moltbot, Tasklet,
                                TinyNature, Hermes Agent, Manus AI, OpenAI Operator, ChatGPT agent). Ambiguous words count
                                only next to an AI word in the same post (AI_CONTEXT): a bare "agents", "assistant that",
                                "personal/virtual assistant", hermes, manus. "my/your agent" also counts next to an agent's
                                chore (AGENT_TASK_CONTEXT: books, inbox, calendar, phone calls...); Muse, Instinct and Hermes
                                are case-sensitive and also count next to the word "agent". People called agents
                                (HUMAN_AGENT: "ICE agents", "real estate agent", "free agent") are removed first, so "my
                                agent called about the role" and "ICE agents used an AI app" are not topical.
  voice (0.7)                   voice interfaces and dictation: "voice-first", "voice interface/AI/assistant/mode/input",
                                "dictation", "speech-to-text", "talking to my computer", Wispr Flow, Superwhisper. Not "voice
                                commands/control": a line in every car and appliance spec sheet.
  wearables (0.7)               AI wearables, smart wearables, pendants, pins, earbuds, glasses: "smart wearable", "wearable(s)",
                                "AI pendant/pin/glasses/device", "Oura wearable", "Humane Pin", "Rabbit R1", "Limitless pendant",
                                "Meta Ray-Ban", "ambient computing", "post-smartphone". A bare "earbuds"/"pendant" needs an
                                AI or device word nearby.
  screen_free (0.7)             screen-free life, screen time, dumbphones: "screen-free", "screenless", "screen time",
                                "dumbphone", "Light Phone", "digital minimalism/detox", "doomscrolling", "phone addiction".
A post's weight is its heaviest theme (an agents post is 1.0 even if it also mentions voice). Matching is
case-insensitive on word boundaries, after dropping URLs and the leading @handles of a reply. A general-purpose brand
alone is never topical: "I asked ChatGPT for a recipe", "my Oura sleep score" and "Siri is useless" need a topic phrase.
A product whose only purpose is a theme (OpenClaw, Wispr Flow, Rabbit R1, Humane Pin, Light Phone) is itself the phrase.
Retweets ("RT @...", discover.is_retweet) are not the person's words and are dropped before anything is counted; a
person whose only stored posts are retweets is treated like one with no posts. discover.post_is_topical is not reused:
it counts every post a discovery query surfaced, and its keyword list has bare brands ("oura", "humane", "sesame").

The score (0..1, 3 decimals)
----------------------------
Only topical posts dated inside the window (now - window_days .. now) count; undated or future posts do not.
  volume     log-scaled weighted count: log(1 + w) / log(1 + VOLUME_FULL_POSTS); 1 agent post 0.29, 3 -> 0.58, 10 -> 1.0.
  community  how the audience answers: engagement per topic post = likes + reposts + 2 x replies + quotes (replies are
             conversation, so they count double), on score.py's log scale log10(1 + e) / 4 (10 -> 0.26, 100 -> 0.50,
             1k -> 0.75, 10k -> 1.0), blended COMMUNITY_WEIGHTS (0.7 / 0.3) with the lift over the person's usual post
             (lift 1 -> 0.5, LIFT_FULL x -> 1.0, 1/LIFT_FULL x -> 0, log scale). lift = geometric mean of (1 + engagement)
             over the topic posts / the same over all their own posts in the window: a typical post, so one viral meme
             does not make every topic post look ignored. It is therefore not topic_eng_avg / overall_eng_avg, which are
             plain averages (the "~N engagements each" of the reason). The lift needs a baseline of at least
             LIFT_MIN_OTHER_POSTS non-topical posts in the window; without one (lift None) community is the engagement
             term alone.
  recency    1.0 when the latest topical post is at most RECENCY_FULL_DAYS old, fading linearly to 0 at window_days.
  support    community and recency are multiplied by min(1, weighted posts / SUPPORT_FULL_POSTS): one post is an anecdote,
             not a community, so a one-off mention cannot score like a voice (1 voice post with 100 engagements, 3 days
             old: 0.19 instead of 0.43; 3 agent posts like it: 0.61). The components reported are after this discount.
  score      SCORE_WEIGHTS: 0.5 volume + 0.35 community + 0.15 recency.
None when the candidate has no stored posts at all (or no X account); 0.0 when they have posts but none topical in the
window. `last_topical` is the latest topical post ever stored, so an old one is still reported.

Output per candidate (contract), plus `topics` ({theme: topical posts in the window}) used by the reason sentence:
  score, topical_posts, agent_posts, weighted_posts, topic_eng_avg, overall_eng_avg, lift, replies_per_topic_post,
  last_topical, top_posts (best 3 by engagement: url, date, text, engagement, replies), components, reason.

CLI (read-only; opens the database with mode=ro and never writes):
  python -m wearable_wishlist.affinity [--top 15] [--db data/wishlist.db] [--sample 30]
"""
from __future__ import annotations

import datetime as dt
import math
import re
import sqlite3
from collections import Counter

from . import db
from .discover import is_retweet
from .sources.base import XPost

# ---------------------------------------------------------------------------------------------------------------------
# Constants (judgement, calibrated by reading the live posts; see the module docstring)
# ---------------------------------------------------------------------------------------------------------------------
WINDOW_DAYS = 90                      # default look-back
THEMES = ("personal_agents", "voice", "wearables", "screen_free")
THEME_WEIGHTS = {"personal_agents": 1.0, "voice": 0.7, "wearables": 0.7, "screen_free": 0.7}
THEME_LABELS = {"personal_agents": "personal agents", "voice": "voice", "wearables": "AI wearables",
                "screen_free": "screen-free life"}
VOLUME_FULL_POSTS = 10.0              # weighted topical posts that make volume 1.0
REPLY_WEIGHT = 2                      # a reply counts double in engagement: replies are conversation
ENGAGEMENT_DECADES = 4.0              # log10(1 + e) / 4: the same engagement scale as score.py
LIFT_FULL = 4.0                       # lift at which the lift term is 1.0 (1x -> 0.5, 1/4x -> 0.0)
LIFT_MIN_OTHER_POSTS = 2              # non-topical posts in the window needed for a baseline
COMMUNITY_WEIGHTS = {"engagement": 0.7, "lift": 0.3}
RECENCY_FULL_DAYS = 30                # latest topical post this recent -> recency 1.0
SUPPORT_FULL_POSTS = 3.0              # weighted topical posts below which community and recency are discounted pro rata
SCORE_WEIGHTS = {"volume": 0.5, "community": 0.35, "recency": 0.15}
TOP_POSTS = 3

# ---------------------------------------------------------------------------------------------------------------------
# Topic phrases. STRONG phrases count alone; CONTEXT phrases count only when AI_CONTEXT (or DEVICE_CONTEXT) is also in
# the post. Patterns are regex fragments; _rx() wraps them in word boundaries.
# ---------------------------------------------------------------------------------------------------------------------
_AGENT_KINDS = r"(?:ai|a\.i\.|llm|gpt|autonomous|coding|browser|computer[- ]use|voice|phone|email|inbox|research|background|personal)"
STRONG = {
    "personal_agents": [
        r"personal (?:ai|a\.i\.) (?:agents?|assistants?|companions?)",
        rf"{_AGENT_KINDS}[- ]agents?",          # "AI agents", "personal agent", "coding agent", "voice agent"...
        r"agentic",
        r"agent[- ](?:mode|swarms?|frameworks?|harness(?:es)?|sdk|loops?|workflows?|builder|orchestrat\w*|platforms?|infra\w*|economy|memory|skills?|os|stack|to[- ]agent)",
        r"multi[- ]?agents?", r"sub[- ]?agents?",
        r"ai (?:assistants?|assts?|companions?|employees?|butlers?|chiefs? of staff|secretar(?:y|ies))",
        r"(?:openclaw|clawdbot|moltbot|open claw)",
        r"tasklet(?:ai)?", r"tinynature",
        r"hermes agents?", r"manus (?:ai|agents?)", r"(?:openai|chatgpt)(?:'s)? operator", r"chatgpt agents?",
        r"(?:claude|chatgpt|gemini)(?:'s)? agent(?: mode)?",
    ],
    "voice": [
        # not "voice commands/control/activated": spec-sheet lines of cars and appliances, not a voice interface theme
        r"voice[- ](?:first|interfaces?|uis?|ai|assistants?|agents?|mode|input|computing|typing|dictation|tech(?:nology)?|"
        r"os|layer|apps?|computers?|bots?|notes? app|memos? app)",
        r"dictation", r"speech[- ]to[- ]text", r"text[- ]to[- ]speech", r"speech recognition",
        r"conversational (?:ai|interfaces?|agents?|voice|computing)",
        r"talk(?:ing|ed|s)? (?:to|with) (?:my|your|the|our|an?) (?:computer|phone|laptop|mac|devices?|ai|assistant|agents?|glasses|wearable|earbuds|wrist)",
        r"(?:wispr|whisper) ?flow", r"superwhisper", r"willow voice",
    ],
    "wearables": [
        r"smart[- ]?wearables?", r"wearables?", r"hearables?",
        r"(?:ai|a\.i\.|voice)[- ](?:wearables?|pendants?|pins?|earbuds|glasses|necklaces?|wearables?|devices?|hardware|gadgets?|jewelry|jewellery|recorders?)",
        r"smart[- ]?(?:glasses|earbuds|pendants?|jewelry|jewellery|necklaces?)", r"(?:ar|xr) glasses",
        r"(?:oura|ultrahuman|ringconn|circular|galaxy|samsung|evie|movano|wearable_wishlist|sandbar|stream|oasis|amazfit|whoop) (?:smart )?(?:wearables?|bands?)",
        r"humane (?:ai )?pins?", r"rabbit r1", r"limitless (?:ai )?pendants?", r"friend (?:ai )?pendants?",
        r"plaud (?:note(?:pin)?|pin)", r"notepin", r"omi (?:pendants?|necklaces?|devices?|wearables?)",
        r"bee (?:bracelets?|pendants?|wearables?|pioneer)", r"humanpods", r"muse charm",
        r"ray-?bans? meta", r"meta ray-?bans?", r"meta (?:ai |display )?glasses",
        r"ambient (?:computing|ai|intelligence|devices?|interfaces?)", r"post-?smartphone",
        r"(?:beyond|after|replace|replaces|replacing|kill|kills|killing) (?:the )?smart ?phones?",
        r"always[- ](?:on|listening) (?:ai|devices?|wearables?|recording|mics?|microphones?|assistants?)",
    ],
    "screen_free": [
        r"screen[- ]?(?:free|less)", r"screen[- ]?time", r"dumb[- ]?phones?", r"light phone",
        r"digital (?:minimalism|detox|wellbeing|well-being|wellness)", r"phone[- ]free", r"doom[- ]?scroll\w*",
        r"(?:phone|smartphone|screen|social media) addiction", r"addicted to (?:my|our|their|your|the) (?:smart)?(?:phones?|screens?)",
        r"(?:no|fewer|less|without|zero) screens?",
        r"(?:put|putting|puts|keep|keeping|leave|leaving|ditch|ditched|ditching) (?:down )?(?:the|my|your|our|their) (?:smart)?phones? (?:down|away|at home|in (?:a|the|my) drawer)",
        r"(?:get|getting|got|stay|staying|go|going|went|time) off (?:my|your|the|our|their) (?:smart)?phones?",
        r"look(?:ing)? up from (?:your|our|my|the|their) (?:phones?|screens?)",
    ],
}
CONTEXT = {   # count only with AI_CONTEXT (agents) / DEVICE_CONTEXT (the rest) in the same post
    "personal_agents": [r"agents?", r"assistants? (?:that|which|who)", r"(?:personal|virtual|digital) assistants?",
                        r"hermes", r"manus"],
    "voice": [r"dictat(?:e|es|ed|ing)", r"voice (?:notes?|memos?)", r"(?:by|with|using|use) (?:my|your|their) voice",
              r"voice (?:is|feature|features|support|conversations?|chats?|version|capabilities|experience|replies)",
              r"talk(?:ing|s)? (?:to|with) (?:grok|chatgpt|claude|gemini|siri|alexa|(?:your|my|an|the) ai|ai|it)"],
    "wearables": [r"earbuds", r"pendants?", r"necklaces?", r"pins?"],
    "screen_free": [],
}
# "my agent" / "your agent" also count next to what an agent does for a person ("your agent makes phone calls now").
OWN_AGENT = r"(?:my|your|our|their|his|her) (?:own )?(?:ai )?agents?"
AGENT_TASK_CONTEXT = (r"books?|booking|handles?|handled|schedul\w*|inbox|emails?|calendar|errands?|remembers?|tasks?|"
                      r"to-?dos?|on its own|for you|for me|in the background|reminders?|phone calls?|makes? the calls|"
                      r"shops?|checks? out|workflows?|mailbox|iphone|mac|desktop|laptop|computer|browser|terminal|apps?|repos?")
# Agent products that are also common words: case-sensitive, and only next to an AI word or the word "agent".
CASE_SENSITIVE_CONTEXT = {"personal_agents": [r"Instinct", r"Muse", r"Hermes"]}
AI_CONTEXT = (r"ai|a\.i\.|llms?|gpt[-\w.]*|chatgpt|claude|gemini|openai|anthropic|grok|copilot|chatbots?|bots?|agentic|prompts?|"
              r"prompting|codex|mcp|apis?|automat\w*|workflows?|openclaw|nous|language models?|machine learning|"
              r"tokens? a week|cursor|perplexity|siri|alexa")
DEVICE_CONTEXT = AI_CONTEXT + (r"|devices?|wearables?|gadgets?|hardware|app|apps|phone|computer|laptop|mac|keyboard|typing|"
                               r"transcri\w*|recorders?|recording|mics?|microphones?|smart|voice|sensors?|battery|camera")
_AGENT_WORD = r"agents?"
# "ICE agents", "real estate agent", "free agent" ... are people, removed before the bare-"agent" context match.
HUMAN_AGENT = (r"(?:ice|fbi|cia|dea|atf|dhs|federal|border patrol|patrol|immigration|secret|special|undercover|double|free|"
               r"real estate|estate|travel|insurance|talent|sports|literary|booking|leasing|rental|gate|ticket|customer service|"
               r"service|support|sales|claims|shipping|field|government|nerve|chemical|cleaning|sleeper|foreign|"
               r"enforcement|of change) agents?")


def _rx(fragments: list[str], flags: int = re.I) -> re.Pattern | None:
    if not fragments:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(fragments) + r")(?![a-z0-9])", flags)


_STRONG_RE = {t: _rx(STRONG[t]) for t in THEMES}
_CONTEXT_RE = {t: _rx(CONTEXT[t]) for t in THEMES}
_CASE_RE = {t: _rx(CASE_SENSITIVE_CONTEXT.get(t, []), 0) for t in THEMES}
_AI_CONTEXT_RE = _rx([AI_CONTEXT])
_DEVICE_CONTEXT_RE = _rx([DEVICE_CONTEXT])
_HUMAN_AGENT_RE = _rx([HUMAN_AGENT])
_OWN_AGENT_RE = _rx([OWN_AGENT])
_AGENT_TASK_RE = _rx([AGENT_TASK_CONTEXT])
_AGENT_WORD_RE = _rx([_AGENT_WORD])
_URL_RE = re.compile(r"https?://\S+")
_LEADING_MENTIONS_RE = re.compile(r"^(?:\s*@\w+)+")


def _clean(text: str) -> str:
    t = _URL_RE.sub(" ", text or "")
    t = _LEADING_MENTIONS_RE.sub(" ", t)          # "@a @b thanks!" addresses people; the words after are the content
    return t.replace("’", "'").replace("&amp;", "&")


def post_topics(text: str | None) -> set[str]:
    """Themes a post is about (empty set: not topical). Pure function of the text."""
    if not text:
        return set()
    raw = _clean(text)
    low = raw.lower()
    ai_ctx = bool(_AI_CONTEXT_RE.search(low))
    dev_ctx = ai_ctx or bool(_DEVICE_CONTEXT_RE.search(low))
    found = set()
    for t in THEMES:
        if _STRONG_RE[t].search(low):
            found.add(t)
        elif t == "personal_agents":
            if _agent_in_context(raw, low, ai_ctx):
                found.add(t)
        elif dev_ctx and _CONTEXT_RE[t] and _CONTEXT_RE[t].search(low):
            found.add(t)
    return found


def _agent_in_context(raw: str, low: str, ai_ctx: bool) -> bool:
    probe = _HUMAN_AGENT_RE.sub(" ", low)                 # "ICE agents", "real estate agent" are people
    if ai_ctx and _CONTEXT_RE["personal_agents"].search(probe):
        return True
    if _OWN_AGENT_RE.search(probe) and (ai_ctx or _AGENT_TASK_RE.search(probe)):
        return True
    return bool(_CASE_RE["personal_agents"].search(raw) and (ai_ctx or _AGENT_WORD_RE.search(probe)))


def post_weight(topics: set[str]) -> float:
    return max((THEME_WEIGHTS[t] for t in topics), default=0.0)


def engagement(likes, reposts, replies, quotes) -> int:
    """likes + reposts + REPLY_WEIGHT x replies + quotes (a different mix from discover._engagement on purpose: here a
    reply is the community answering, reposts are reach)."""
    return (likes or 0) + (reposts or 0) + REPLY_WEIGHT * (replies or 0) + (quotes or 0)


# ---------------------------------------------------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------------------------------------------------

def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            d = dt.datetime.strptime(s.strip()[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def volume_component(weighted_posts: float) -> float:
    return min(1.0, math.log1p(max(weighted_posts, 0.0)) / math.log1p(VOLUME_FULL_POSTS))


def support_factor(weighted_posts: float) -> float:
    """One topical post is an anecdote, not a community: community and recency count in full from SUPPORT_FULL_POSTS."""
    return min(1.0, max(weighted_posts, 0.0) / SUPPORT_FULL_POSTS)


def engagement_scale(e: float) -> float:
    return min(1.0, max(0.0, math.log10(1.0 + max(e, 0.0)) / ENGAGEMENT_DECADES))


def lift_scale(lift: float) -> float:
    if lift <= 0:
        return 0.0
    return min(1.0, max(0.0, 0.5 + 0.5 * math.log(lift) / math.log(LIFT_FULL)))


def community_component(topic_eng_avg: float | None, lift: float | None) -> float:
    if topic_eng_avg is None:
        return 0.0
    e = engagement_scale(topic_eng_avg)
    if lift is None:
        return e
    w = COMMUNITY_WEIGHTS
    return (w["engagement"] * e + w["lift"] * lift_scale(lift)) / (w["engagement"] + w["lift"])


def recency_component(days_since: float | None, window_days: int) -> float:
    if days_since is None or days_since > window_days:
        return 0.0
    if days_since <= RECENCY_FULL_DAYS or window_days <= RECENCY_FULL_DAYS:
        return 1.0
    return max(0.0, 1.0 - (days_since - RECENCY_FULL_DAYS) / (window_days - RECENCY_FULL_DAYS))


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _fmt_eng(x: float) -> str:
    return f"~{x / 1000:.1f}k" if x >= 1000 else f"~{round(x)}"


def _ago(days: float) -> str:
    d = int(days)
    return "today" if d <= 0 else ("1 day ago" if d == 1 else f"{d} days ago")


# ---------------------------------------------------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------------------------------------------------

def _empty(reason: str) -> dict:
    return {"score": None, "topical_posts": 0, "agent_posts": 0, "weighted_posts": 0.0, "topic_eng_avg": None,
            "overall_eng_avg": None, "lift": None, "replies_per_topic_post": None, "last_topical": None, "top_posts": [],
            "components": {"volume": 0.0, "community": 0.0, "recency": 0.0}, "topics": {}, "reason": reason}


def topic_affinity(conn: sqlite3.Connection, candidate_ids: list[int] | None = None, now: str | None = None,
                   window_days: int = WINDOW_DAYS) -> dict[int, dict]:
    """{candidate_id: affinity dict} for the given candidates (all candidates when None). See the module docstring."""
    now_dt = _parse_ts(now) or _parse_ts(db.utcnow())
    start_dt = now_dt - dt.timedelta(days=window_days)
    wanted = None if candidate_ids is None else {int(c) for c in candidate_ids}

    cand_sql = "SELECT candidate_id, x_id FROM candidates"
    post_sql = ("SELECT c.candidate_id, p.post_id, p.x_id, p.handle, p.created_at, p.text, p.likes, p.reposts, p.replies, "
                "p.quotes, p.url FROM x_posts p JOIN candidates c ON c.x_id = p.x_id")
    params: tuple = ()
    if wanted is not None and len(wanted) <= 500:      # small sets: let SQLite filter; large ones are filtered below
        marks = ",".join("?" for _ in wanted)
        cand_sql += f" WHERE candidate_id IN ({marks})"
        post_sql += f" WHERE c.candidate_id IN ({marks})"
        params = tuple(sorted(wanted))

    cands = {int(r[0]): r[1] for r in conn.execute(cand_sql, params) if wanted is None or int(r[0]) in wanted}
    acc: dict[int, dict] = {cid: {"stored": 0, "own": 0, "window_eng": [], "topical": [], "last_topical": None}
                            for cid in cands}

    for r in conn.execute(post_sql, params):          # the single pass over x_posts
        cid = int(r[0])
        a = acc.get(cid)
        if a is None:
            continue
        a["stored"] += 1
        text = r[5] or ""
        if is_retweet(XPost(post_id=r[1], x_id=r[2], text=text, handle=r[3])):
            continue
        a["own"] += 1
        ts = _parse_ts(r[4])
        if ts is not None and ts > now_dt:            # posted after `now` (a replay of an earlier day): not there yet
            continue
        topics = post_topics(text)
        if topics and ts is not None and (a["last_topical"] is None or ts > a["last_topical"]):
            a["last_topical"] = ts
        if ts is None or ts < start_dt:
            continue
        eng = engagement(r[6], r[7], r[8], r[9])
        a["window_eng"].append((eng, bool(topics)))
        if topics:
            a["topical"].append({"topics": topics, "ts": ts, "eng": eng, "replies": r[8] or 0, "url": r[10], "text": text})

    out: dict[int, dict] = {}
    for cid, x_id in cands.items():
        a = acc[cid]
        if not x_id:
            out[cid] = _empty("no X account known")
            continue
        if a["own"] == 0:
            out[cid] = _empty("no posts stored" if a["stored"] == 0 else "only retweets stored")
            continue
        out[cid] = _summarise(a, now_dt, window_days)
    return out


def _summarise(a: dict, now_dt: dt.datetime, window_days: int) -> dict:
    topical = a["topical"]
    n_window = len(a["window_eng"])
    overall = (sum(e for e, _ in a["window_eng"]) / n_window) if n_window else None
    last_all = a["last_topical"]
    res = _empty("")
    res["overall_eng_avg"] = round(overall, 1) if overall is not None else None
    res["last_topical"] = last_all.strftime("%Y-%m-%d") if last_all else None
    if not topical:
        res["score"] = 0.0
        s = (f"{a['own']} post{'s' if a['own'] != 1 else ''} stored, none about personal agents, voice, AI wearables or "
             f"screen-free life in the last {window_days} days")
        if last_all:
            s += f" (latest topical one {res['last_topical']})"
        res["reason"] = s
        return res

    n = len(topical)
    weighted = sum(post_weight(p["topics"]) for p in topical)
    counts = Counter(t for p in topical for t in p["topics"])
    topic_avg = sum(p["eng"] for p in topical) / n
    n_other = n_window - n
    lift = None
    if n_other >= LIFT_MIN_OTHER_POSTS:
        log_topic = sum(math.log1p(p["eng"]) for p in topical) / n
        log_all = sum(math.log1p(e) for e, _ in a["window_eng"]) / n_window
        lift = math.exp(log_topic - log_all)
    latest = max(p["ts"] for p in topical)
    days = max(0.0, (now_dt - latest).total_seconds() / 86400)
    support = support_factor(weighted)
    comps = {"volume": volume_component(weighted), "community": support * community_component(topic_avg, lift),
             "recency": support * recency_component(days, window_days)}
    score = sum(SCORE_WEIGHTS[k] * v for k, v in comps.items())
    best = sorted(topical, key=lambda p: (p["eng"], p["ts"]), reverse=True)[:TOP_POSTS]

    themes = [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], THEMES.index(kv[0])))]
    s = f"{n} post{'s' if n != 1 else ''} about {_join([THEME_LABELS[t] for t in themes])} in {window_days} days"
    s += f", {_fmt_eng(topic_avg)} engagements {'each' if n > 1 else 'on it'}"
    if lift is not None:
        s += f" ({lift:.1f}x their usual)"
    s += f", latest {_ago(days)}"

    res.update({
        "score": round(score, 3), "topical_posts": n, "agent_posts": counts.get("personal_agents", 0),
        "weighted_posts": round(weighted, 2), "topic_eng_avg": round(topic_avg, 1),
        "lift": round(lift, 2) if lift is not None else None,
        "replies_per_topic_post": round(sum(p["replies"] for p in topical) / n, 2),
        "top_posts": [{"url": p["url"], "date": p["ts"].strftime("%Y-%m-%d"), "text": p["text"], "engagement": p["eng"],
                       "replies": p["replies"]} for p in best],
        "components": {k: round(v, 3) for k, v in comps.items()},
        "topics": {t: counts[t] for t in themes},
        "reason": s,
    })
    return res


# ---------------------------------------------------------------------------------------------------------------------
# CLI: read-only look at a database (calibration)
# ---------------------------------------------------------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> None:
    import argparse
    import random
    from . import config

    ap = argparse.ArgumentParser(description="Topic affinity on a database, read-only")
    ap.add_argument("--db", default=str(config.db_path()))
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--now", default=None)
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    ap.add_argument("--sample", type=int, default=0, help="also print N random topical and N random non-topical posts")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    res = topic_affinity(conn, now=args.now, window_days=args.window)
    handles = {int(r[0]): (r[1], r[2]) for r in conn.execute("SELECT candidate_id, handle, name FROM candidates")}
    scored = [(cid, r) for cid, r in res.items() if r["score"] is not None]
    dist = Counter("none" if r["score"] is None else ("0" if r["score"] == 0 else f"{min(int(r['score'] * 10), 9) / 10:.1f}+")
                   for r in res.values())
    print(f"{len(res)} candidates, {len(scored)} with posts; distribution: {dict(sorted(dist.items()))}")
    for i, (cid, r) in enumerate(sorted(scored, key=lambda kv: -kv[1]["score"])[:args.top], 1):
        h, name = handles.get(cid, (None, None))
        print(f"{i:2}. {r['score']:.3f} @{h} ({name}) #{cid} {r['components']}\n    {r['reason']}")
    if args.sample:
        rows = [r[0] for r in conn.execute("SELECT p.text FROM x_posts p JOIN candidates c ON c.x_id = p.x_id "
                                           "WHERE p.text NOT LIKE 'RT @%'")]
        rng = random.Random(7)
        hit = [t for t in rows if post_topics(t)]
        miss = [t for t in rows if not post_topics(t)]
        print(f"\n{len(hit)} topical of {len(rows)} own posts. Random topical:")
        for t in rng.sample(hit, min(args.sample, len(hit))):
            print(f"  + {sorted(post_topics(t))} {t[:200]!r}")
        print("Random non-topical:")
        for t in rng.sample(miss, min(args.sample, len(miss))):
            print(f"  - {t[:200]!r}")


if __name__ == "__main__":
    _main()
