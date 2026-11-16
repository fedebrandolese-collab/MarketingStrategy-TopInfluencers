"""Classification: one LLM call per candidate over a compact dossier, plus deterministic pre-checks that need no LLM.

run_classify(conn, run_id, candidate_ids=None, max_candidates=200, workers=4):
  select (discover.candidate_priorities order: the same people enrichment paid for come first; candidates classified in an
  earlier run are skipped unless new posts arrived since; force=True reclassifies; refresh_field='topic_voice' selects
  only the candidates whose latest classification predates that field) -> dossier (profile, metrics, account age,
  up to 30 posts: run_enrich's device/voice search hits and topic-query hits first, then other on-topic posts, then recent
  context, each with `found_by`, text capped at 400 chars; mutual follow with the seed; map link) -> llm.complete_json(prompt from prompts/classify.md, CLASSIFICATION_SCHEMA) on a thread pool (`workers`
  calls in flight; a claude CLI call takes 10-30 s) -> on the calling thread: drop evidence whose url is not in the dossier
  -> classifications.json (cleaned output + `_meta`) -> candidates (category, us_based, competitor_flag/detail,
  handle_verified, handle_verification_method='llm+signals') -> evidence rows (mapping below) -> events.
  Every SQLite write happens on the calling thread: worker threads only call the LLM; the cost-ledger rows llm.py writes
  are relayed back (_CostRelay) and written by the calling thread. A failing call is logged and skipped.

Evidence mapping (docs/ARCHITECTURE.md): wearable_user -> wear; voice_tool_user -> wear + love; reviews_products_publicly -> love;
competitor_tie -> competitor; us_based -> us; handle_authenticity -> authenticity; audience_relevance + metrics -> impact;
can_dm / mutual follow -> reach; topic_voice -> affinity (post 1.0, bio 0.8). Polarity is -1 when the value is false.
topic_voice 'true' needs evidence from at least two different dossier posts after cleaning, else it is stored as null.
Two extra low-weight rows beyond the contract: wearable_user and reviews_products_publicly also support 'accept'
(weight 0.5): people who wear and review devices accept them.
Re-classifying replaces this module's own profile_signal rows (counts in their text go stale); post/bio evidence keeps its
first fetched_at, so the change log only sees evidence that is really new. A post/bio row the new answer no longer cites is
deleted when the model saw that post again (it is in this dossier); rows on posts outside this dossier are kept.

QA guards (docs/QA_TOP.md, mistakes that repeated in the top 60 of run 14): an organisation, outlet, aggregator or fan page
(not_a_person) gets category 'other' and a wear and an accept row against it; an account selling posts or collabs
(discover.sells_posts on bio and location) gets a love row against it; a wearable/voice quote that shows the device was
dropped ('used it maybe twice', 'returned it') counts against wear, not for it; a non-US place in Carlo's map entry
('Reporter, TechCrunch (India)') sets us_based=false when the location field says nothing. From the run 30 QA: so does an
unhedged clause of the model's own summary ('posts place him in China, not the US'); a summary or bio naming the person as
the builder of a voice-dictation app ('founder of Flit, a Mac dictation app') sets the competitor flag in Wispr Flow's
category; Indian city codes ('sf / blr') count as places in the location pre-check. From the run 39 QA: a profile that
discover.red_flag reads as a crypto persona, an account selling posts or promotion, an AI-news or AI-tools feed, a brand
or shop, or an anonymous account gets category 'other' and a wear and an accept row against it, like not_a_person.
From the 30 Sep rules (competitors are flagged, never removed; enrichment no longer skips their people): a bio that states
a role at a competitor (discover.competitor_role: 'CEO at wisprflow.ai', 'head of design @sesame') sets the competitor
flag whatever the model answered; it adds no row, so the score is the model's.

Deterministic pre-checks (no LLM): parody/fan words -> handle_verified='suspicious' and category 'other' (the LLM cannot
override either: a parody account is not the person); verified badge
+ account older than 2 years + more than 5k followers -> 'likely_real' (the LLM upgrades to 'verified' only with a reason);
an unambiguous US location (state, US city, USA) -> us_based=true, an unambiguous non-US one (country or city) -> false,
else the LLM's evidenced answer, else NULL. The LLM cannot claim 'verified' for a profile without the badge."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import config, costs, db, llm, schedule
from .discover import (_FIRST_PERSON_RE, _PERSON_ROLE_RE, ORG_VERIFIED_TYPES, account_age_years, candidate_priorities,
                       competitor_brand_account, competitor_role, kw_regex, looks_parody, post_is_topical, query_kind,
                       red_flag, seed_x_id, sells_posts)

log = logging.getLogger("ring_wishlist.classify")

PROMPT_FILE = Path(__file__).with_name("prompts") / "classify.md"
MAX_DOSSIER_POSTS = 30
MAX_POST_CHARS = 400          # long posts are cut: quotes must come from what the model saw
MAX_BIO_CHARS = 400
MAX_DOSSIER_CHARS = 20_000    # rendered JSON; lowest-ranked posts are dropped beyond this (~5k tokens)
EST_USD_PER_CALL = 0.02       # ~5k input + ~600 output tokens on Sonnet, for the stdout estimate only; llm.py records the real cost

_EVIDENCE_ITEM = {"type": "object", "required": ["url", "quote"],
                  "properties": {"url": {"type": "string"}, "quote": {"type": "string"}, "date": {"type": ["string", "null"]}}}
_EVIDENCED_BOOL = {"type": "object", "required": ["value", "evidence"],
                   "properties": {"value": {"type": ["boolean", "null"]}, "evidence": {"type": "array", "items": _EVIDENCE_ITEM}}}
AUTHENTICITY_VALUES = ("verified", "likely_real", "suspicious", "unknown")
TOPICS = ("personal_agents", "voice", "wearables", "screen_free")   # personal agents first: the core (TinyNature is one)
_TOPIC_ALIASES = {"agents": "personal_agents", "personal_agent": "personal_agents", "ai_agents": "personal_agents",
                  "voice_interfaces": "voice", "voice_interface": "voice", "voice_ai": "voice", "ai_wearables": "wearables",
                  "wearable": "wearables", "smart_rings": "wearables", "rings": "wearables", "screen_free_life": "screen_free",
                  "screenless": "screen_free"}
MIN_TOPIC_VOICE_POSTS = 2     # topic_voice=true needs quotes from at least this many different posts
TIE_KINDS = ("employee", "investor", "advisor", "ambassador")

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "required": ["category", "us_based", "wearable_user", "voice_tool_user", "reviews_products_publicly", "competitor_tie",
                 "audience_relevance", "topic_voice", "handle_authenticity", "summary", "unknowns"],
    "properties": {
        "category": {"type": "string", "enum": list(config.CATEGORIES)},
        "us_based": {"type": ["boolean", "null"]},
        "us_based_evidence": {"type": ["string", "null"]},
        "wearable_user": _EVIDENCED_BOOL,
        "voice_tool_user": _EVIDENCED_BOOL,
        "reviews_products_publicly": _EVIDENCED_BOOL,
        "competitor_tie": {"type": "object", "required": ["value", "which", "kind", "evidence"],
                           "properties": {"value": {"type": ["boolean", "null"]}, "which": {"type": ["string", "null"]},
                                          "kind": {"type": ["string", "null"], "enum": list(TIE_KINDS) + [None]},
                                          "evidence": {"type": "array", "items": _EVIDENCE_ITEM}}},
        "audience_relevance": {"type": "number", "minimum": 0, "maximum": 1},
        "topic_voice": {"type": "object", "required": ["value", "topics", "evidence"],
                        "properties": {"value": {"type": ["boolean", "null"]},
                                       "topics": {"type": "array", "items": {"type": "string", "enum": list(TOPICS)}},
                                       "evidence": {"type": "array", "items": _EVIDENCE_ITEM}}},
        "handle_authenticity": {"type": "object", "required": ["value", "reasons"],
                                "properties": {"value": {"type": "string", "enum": list(AUTHENTICITY_VALUES)}, "reasons": {"type": "string"}}},
        "events": {"type": "array", "items": {"type": "object", "required": ["kind", "url"],
                                              "properties": {"kind": {"type": "string", "enum": list(config.EVENT_KINDS)},
                                                             "date": {"type": ["string", "null"]}, "url": {"type": "string"},
                                                             "text": {"type": ["string", "null"]}}}},
        "summary": {"type": "string"},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
}

# classification field -> [(supports, weight)] for its evidence items; polarity follows the field's value (+1 true, -1 false)
EVIDENCE_MAP = {
    "wearable_user": [("wear", 1.0), ("accept", 0.5)],
    "voice_tool_user": [("wear", 1.0), ("love", 1.0)],
    "reviews_products_publicly": [("love", 1.0), ("accept", 0.5)],
    "competitor_tie": [("competitor", 1.0)],
    "topic_voice": [("affinity", 1.0)],          # kind 'bio' (profile quote) gets x0.8 like every field
}

# ---------------------------------------------------------------------------------------------------------------------
# Deterministic pre-checks (readable tables)
# ---------------------------------------------------------------------------------------------------------------------

US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida", "hawaii",
    "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    # "georgia" is left out on purpose: also a country
]
# "City, XX" abbreviations. DE (Germany) and IN (India) are left out: too easy to confuse with ISO country codes.
US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "FL", "GA", "HI", "ID", "IL", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
    "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
US_CITIES = [
    "sfo", "jfk",   # airport codes, next to the Indian city codes in NON_US_HINTS ('BOM/SFO' is split, not Mumbai only)
    "san francisco", "sf", "bay area", "silicon valley", "new york", "nyc", "new york city", "brooklyn", "manhattan", "los angeles",
    "san jose", "palo alto", "menlo park", "mountain view", "redwood city", "oakland", "berkeley", "seattle", "portland", "austin",
    "boston", "chicago", "denver", "boulder", "miami", "atlanta", "dallas", "houston", "san diego", "philadelphia", "washington dc",
    "washington, dc", "washington d.c.", "d.c.", "nashville", "las vegas", "phoenix", "minneapolis", "pittsburgh", "detroit",
    "salt lake city", "santa monica", "sunnyvale", "cupertino", "santa clara", "san mateo", "burlingame", "sausalito", "marin",
    "santa barbara", "irvine", "orange county", "pasadena", "sacramento", "raleigh", "durham", "charlotte", "columbus", "cincinnati",
    "cleveland", "kansas city", "st. louis", "st louis", "new orleans", "tampa", "orlando", "jacksonville", "baltimore", "providence",
    "new haven", "hoboken", "jersey city", "ann arbor", "madison", "milwaukee", "indianapolis", "louisville", "memphis", "san antonio",
    "el paso", "albuquerque", "tucson", "boise", "honolulu", "anchorage", "omaha", "tulsa", "oklahoma city", "des moines",
    "charleston", "savannah", "asheville", "santa fe", "aspen", "park city", "jackson hole", "lake tahoe", "big sur", "malibu",
    "hollywood", "beverly hills", "long beach", "silicon beach", "chapel hill", "princeton", "palo alto, ca",
]
US_COUNTRY_WORDS = ["usa", "u.s.a", "u.s.a.", "u.s.", "united states", "united states of america", "🇺🇸"]
# A non-US hint next to a US hint makes the string ambiguous ("London / SF") -> null.
NON_US_HINTS = [
    "uk", "u.k.", "london", "england", "scotland", "germany", "berlin", "munich", "france", "paris", "canada", "toronto", "vancouver",
    "montreal", "india", "bangalore", "bengaluru", "mumbai", "delhi", "australia", "sydney", "melbourne", "singapore", "japan", "tokyo",
    "china", "shanghai", "beijing", "shenzhen", "europe", "eu", "spain", "madrid", "barcelona", "italy", "milan", "rome", "netherlands",
    "amsterdam", "sweden", "stockholm", "israel", "tel aviv", "brazil", "são paulo", "sao paulo", "mexico", "dubai", "uae", "hong kong",
    "korea", "seoul", "ireland", "dublin", "switzerland", "zurich", "portugal", "lisbon", "worldwide", "global", "remote", "earth",
    "everywhere", "internet", "nomad",
    # more countries and cities; names shared with common US places (Cambridge, Athens, Venice, Naples, Florence,
    # Birmingham, Valencia, Lima, Santiago, Oxford, Wellington) are left out on purpose
    "united kingdom", "great britain", "wales", "manchester", "edinburgh", "glasgow", "deutschland", "hamburg", "frankfurt",
    "cologne", "köln", "stuttgart", "düsseldorf", "lyon", "marseille", "denmark", "copenhagen", "aarhus", "norway", "oslo",
    "finland", "helsinki", "iceland", "reykjavik", "poland", "warsaw", "krakow", "kraków", "austria", "vienna", "wien",
    "belgium", "brussels", "antwerp", "luxembourg", "czech republic", "czechia", "prague", "hungary", "budapest", "romania",
    "bucharest", "greece", "estonia", "tallinn", "latvia", "riga", "lithuania", "vilnius", "ukraine", "kyiv", "kiev", "russia",
    "moscow", "türkiye", "turkiye", "turkey", "istanbul", "ankara", "geneva", "lausanne", "basel", "rotterdam", "the hague",
    "utrecht", "eindhoven", "gothenburg", "malmö", "malmo", "porto", "turin", "torino", "bologna", "españa", "italia",
    "ottawa", "calgary", "edmonton", "montréal", "québec", "quebec", "ontario", "british columbia", "alberta", "perth",
    "brisbane", "adelaide", "canberra", "auckland", "new zealand", "hyderabad", "chennai", "pune", "gurgaon", "gurugram",
    "noida", "kolkata", "ahmedabad", "pakistan", "karachi", "lahore", "islamabad", "bangladesh", "dhaka", "sri lanka",
    "nepal", "indonesia", "jakarta", "bali", "vietnam", "hanoi", "ho chi minh", "thailand", "bangkok", "philippines", "manila",
    "malaysia", "kuala lumpur", "taiwan", "taipei", "hangzhou", "guangzhou", "chengdu", "shenzhen", "osaka", "kyoto", "busan",
    "nigeria", "lagos", "abuja", "kenya", "nairobi", "ghana", "accra", "south africa", "cape town", "johannesburg", "egypt",
    "cairo", "morocco", "saudi arabia", "riyadh", "jeddah", "qatar", "doha", "abu dhabi", "bahrain", "kuwait", "jordan",
    "amman", "lebanon", "beirut", "argentina", "buenos aires", "brasil", "rio de janeiro", "chile", "colombia", "bogota",
    "bogotá", "medellin", "medellín", "peru", "méxico", "ciudad de mexico", "cdmx", "guadalajara", "costa rica",
    "🇬🇧", "🇩🇪", "🇫🇷", "🇮🇳", "🇨🇦", "🇪🇺", "🇨🇳", "🇯🇵", "🇹🇷", "🇩🇰", "🇸🇬", "🇭🇰", "🇦🇺", "🇧🇷", "🇮🇱", "🇳🇱", "🇪🇸", "🇮🇹",
    # QA of run 30: Indian city codes as founders write them ('sf / blr' was read as SF only; 'BOM 🏡 BLR 🏢' as nothing).
    # Codes that are also English words or US places (del, sin, ams) are left out.
    "blr", "bom", "ncr", "hyd",
]
VAGUE_LOCATIONS = {"worldwide", "global", "remote", "earth", "everywhere", "internet", "nomad"}
_US_WORDS_RE = kw_regex(US_COUNTRY_WORDS + US_STATES + US_CITIES)
_NON_US_RE = kw_regex(NON_US_HINTS)


def precheck_us_based(location: str | None) -> tuple[bool | None, str]:
    """From the profile's location field: (True, reason) when it names only US places; (False, reason) when it names only
    places outside the US (a country or city, not a vague 'worldwide' / 'remote'); (None, reason) when empty, vague or
    ambiguous ('London / SF'). The location field is evidence the LLM cannot override."""
    loc = (location or "").strip()
    if not loc:
        return None, ""
    low = loc.lower()
    hits = [m.group(0) for m in _US_WORDS_RE.finditer(low)]
    if re.search(r"(?<![A-Za-z])US(?![A-Za-z])", loc):
        hits.append("US")
    m = re.search(r",\s*([A-Z]{2})(?![A-Za-z])", loc)
    if m and m.group(1) in US_STATE_ABBR:
        hits.append(m.group(1))
    non_us = [m.group(0) for m in _NON_US_RE.finditer(low)]
    if hits and non_us:
        return None, f"location {loc!r} is ambiguous ({hits[0]!r} vs {non_us[0]!r})"
    if hits:
        return True, f"location field says {loc!r}"
    places = [w for w in non_us if w not in VAGUE_LOCATIONS]
    if places:
        return False, f"location field says {loc!r} (outside the US)"
    return None, ""


def precheck_handle(user: dict) -> tuple[str | None, str]:
    """('suspicious'|'likely_real'|None, reason) from name/bio words, badge, account age and followers."""
    name, bio = user.get("name"), user.get("bio")
    if looks_parody(name, bio):
        return "suspicious", "parody / fan / unofficial words in the name or bio"
    age = account_age_years(user.get("created_at"))
    followers = user.get("followers") or 0
    if user.get("verified") and age is not None and age >= 2 and followers > 5000:
        return "likely_real", f"verification badge ({user.get('verified_type') or 'type unknown'}), account {age:.1f} years old, {followers:,} followers"
    return None, ""


def merge_authenticity(precheck: str | None, llm_value: str | None, reasons: str | None) -> str:
    """Deterministic checks win: a parody flag cannot be talked away; 'verified' needs a stated reason."""
    if precheck == "suspicious":
        return "suspicious"
    has_reason = bool((reasons or "").strip())
    if llm_value == "verified":
        return "verified" if has_reason else (precheck or "likely_real")
    if llm_value == "suspicious":
        return "suspicious" if has_reason else (precheck or "unknown")
    if llm_value == "likely_real":
        return "likely_real"
    return precheck or "unknown"


# QA guards. The classifier's summary names outlets and brand pages ("it is an outlet account, not a personal user") but the
# schema has no field for it, so 35 of them were filed as media or X voices; these phrases read its own words. The nouns
# count only in the summary's first clause, where it says what the account is ("BGR is a tech publication; ..."), not
# where a person's employer comes up later ("...; writes for a tech publication").
ORG_SUMMARY_LEAD_RE = re.compile(r"(?<![a-z])(outlet account|(news|tech|media) outlet|(tech|news|media|online) publication|"
                                 r"brand account|company account|corporate account|company channel|aggregator|"
                                 r"fan[\w/ -]{0,12}account|an organi[sz]ation|magazine brand)(?![a-z])", re.I)
ORG_SUMMARY_ANY_RE = re.compile(r"(?<![a-z])(not an individual|it is an outlet|it is an organi[sz]ation|it is a (brand|company|corporate)|"
                                r"(brand|company|corporate) account, not)(?![a-z])", re.I)
ORG_BIO_RE = re.compile(r"(?<![a-z])((email|contact|dm|message|follow|join|tag) us|a team of|our team|part of @|"
                        r"independent reporting|real use cases)(?![a-z])", re.I)
# A career in the bio is a person's ("Tech reviews and news @business. Previously @verge"), on top of discover's role words.
PERSON_CUE_RE = re.compile(r"(?<![a-z])(previously|prev\.?|formerly|ex-|reviewer|views (are )?my own|opinions (are )?my own)", re.I)
# A quote showing the device was dropped is evidence against daily wear ("I bought the Rabbit R1 and have used it maybe twice").
ABANDON_RE = re.compile(r"(used it (maybe |only )?(once|twice)|returned (it|mine)|returning (it|mine|my)|gave up on|"
                        r"stopped (wearing|using)|in a drawer|sent (it|mine) back|collecting dust|never (wear|use) (it|mine))", re.I)


def not_a_person(profile: dict, handle: str | None, summary: str | None) -> str | None:
    """Why the account is an organisation, outlet, aggregator or fan page rather than a person who could wear a ring, else
    None. Strongest first: X's business/government badge; a competitor's own brand account; then, only when the bio has no
    first person and no personal role (editor, founder, dad...), an organisation phrase in the bio ('email us', 'a team of',
    'part of @') or the classifier's summary calling it an outlet, publication, brand or company account, aggregator or
    fan account."""
    vt = (profile.get("verified_type") or "").lower()
    if vt in ORG_VERIFIED_TYPES:
        return f"X '{vt}' badge"
    brand = competitor_brand_account(handle, profile.get("name"))
    if brand:
        return f"{brand}'s own brand account"
    low = (profile.get("bio") or "").lower()
    if _FIRST_PERSON_RE.search(low) or _PERSON_ROLE_RE.search(low) or PERSON_CUE_RE.search(low):
        return None
    m = ORG_BIO_RE.search(low)
    if m:
        return f"bio says {m.group(0)!r}"
    summary = summary or ""
    m = ORG_SUMMARY_LEAD_RE.search(summary.split(";")[0]) or ORG_SUMMARY_ANY_RE.search(summary)
    if m:
        return f"classifier calls it {m.group(0).lower()!r}"
    return None


def map_non_us(map_link: dict | None) -> str | None:
    """The map entry's role and company when they name a place outside the US ('Reporter, TechCrunch (India)'), else None.
    Only a non-US answer is taken: US words in a role are often outlet names ('Washington Post'), not where someone lives."""
    if not map_link:
        return None
    text = " · ".join(str(map_link[k]) for k in ("role", "company") if map_link.get(k))
    return text if precheck_us_based(text)[0] is False else None


# QA of run 30. The summary can place a person outside the US while the us_based answer stays empty: "posts place him in
# China, not the US" (@damonchen, who drops his kids at school in Shanghai). An unhedged clause saying so counts as a non-US
# answer when neither the location field nor the map decides. A hedged or split one ("likely India-based", "location split
# across India and SF") stays unknown, and so does a place that describes an employer ("writes for a London-based outlet").
_SUMMARY_PLACES = "|".join(sorted({re.escape(w) for w in NON_US_HINTS if w not in VAGUE_LOCATIONS and w[0].isalpha() and len(w) > 2},
                                  key=len, reverse=True))
SUMMARY_NON_US_RE = re.compile(
    r"(?<![a-z])(not (in |based in )?the us|outside the us|not us-based)(?![a-z])|"
    rf"(?<![a-z])(based in|lives in|living in|located in|places? (him|her|them) in) ({_SUMMARY_PLACES})(?![a-z])|"
    rf"(?<![a-z])({_SUMMARY_PLACES})-based(?! (outlet|publication|newspaper|magazine|company|firm|startup|fund|brand|agency))", re.I)
SUMMARY_HEDGE_RE = re.compile(r"(?<![a-z])(likely|probably|possibly|perhaps|may|might|appears?|seems?|unclear|unknown|"
                              r"split\w*|between|or)(?![a-z])", re.I)


def summary_non_us(summary: str | None) -> str | None:
    """The clause of the classifier's summary that places the person outside the US without hedging, else None. A clause
    that also names a US place ('SF/London-based') is ambiguous, as in the location pre-check."""
    for clause in re.split(r";|\.\s|\(|\)", summary or ""):
        if not SUMMARY_NON_US_RE.search(clause) or SUMMARY_HEDGE_RE.search(clause):
            continue
        if _US_WORDS_RE.search(SUMMARY_NON_US_RE.sub(" ", clause).lower()):
            continue
        return clause.strip().rstrip(".")
    return None


# QA of run 30. Founders of voice-dictation apps (Typeless, TypeVoice, Flit, Aside; VoiceOS in run 14) sell what Wispr Flow
# sells, and none was flagged. The summary names the product ("founder-CEO of Typeless, a voice dictation app"). A user of
# such an app ("dictates daily with Wispr Flow, a dictation app") is not a builder. The flag names Wispr Flow's category and
# never rejects anyone: Carlo decides.
_VOICE_PRODUCT = (r"((voice[- ])?dictation|voice[- ]typing|speech[- ]to[- ]text|voice[- ]to[- ]text|voice keyboard)"
                  r" (app|tool|product|startup|software)s?")
VOICE_RIVAL_RE = re.compile(r"(?<![a-z])(founder|co-?founder|ceo|cto|builds?|building|built|launched|ships?|shipping|maker|creator)"
                            rf"(?![a-z])(?P<gap>(?:[^;.]|\.(?=\w)){{0,60}}?)(?<![a-z]){_VOICE_PRODUCT}(?![a-z])", re.I)
_VOICE_USER_RE = re.compile(r"(?<![a-z])(uses?|using|user|with|via|tried|tries|switched|prefers?|dictates)(?![a-z])", re.I)


def builds_voice_rival(bio: str | None, summary: str | None) -> str | None:
    """The phrase showing the person builds a voice-dictation product (Wispr Flow's category), else None."""
    for text in (summary, bio):
        for m in VOICE_RIVAL_RE.finditer(text or ""):
            if not _VOICE_USER_RE.search(m.group("gap")):
                return m.group(0).strip()
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Dossier
# ---------------------------------------------------------------------------------------------------------------------

def _engagement(p: dict) -> int:
    return (p.get("likes") or 0) + 2 * (p.get("reposts") or 0) + (p.get("replies") or 0) + (p.get("quotes") or 0)


def _cap(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


# keys of the dossier dict that are bookkeeping, not shown to the model
_INTERNAL_KEYS = ("candidate_id", "posts_in_db")
# which search found a post (discover.query_kind -> short label shown in the dossier)
_FOUND_BY = {"discovery": "topic search", "evidence": "device/voice search", "recent": "recent posts", None: "timeline"}


def _prompt_view(dossier: dict) -> dict:
    return {k: v for k, v in dossier.items() if k not in _INTERNAL_KEYS}


def _dossier_json(dossier: dict) -> str:
    return json.dumps(_prompt_view(dossier), ensure_ascii=False, separators=(",", ":"))


def build_dossier(conn: sqlite3.Connection, candidate_id: int, max_posts: int = MAX_DOSSIER_POSTS, seed_handle: str | None = None) -> dict:
    """Everything the LLM is allowed to know about one candidate. Evidence urls must come from here.
    Posts: topical ones first (most engaged first), then the most recent others, at most `max_posts`, text capped at
    MAX_POST_CHARS; the lowest-ranked posts are dropped until the rendered JSON fits MAX_DOSSIER_CHARS."""
    c = db.one(conn, "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,))
    if not c:
        raise KeyError(f"candidate {candidate_id} not found")
    u = (db.one(conn, "SELECT * FROM x_users WHERE x_id=?", (c["x_id"],)) if c["x_id"] else None) or {}
    handle = u.get("handle") or c["handle"]
    profile_url = f"https://x.com/{handle}" if handle else None
    posts = db.rows(conn, "SELECT post_id, text, created_at, likes, reposts, replies, quotes, views, url, query, in_reply_to_x_id "
                          "FROM x_posts WHERE x_id=?", (c["x_id"],)) if c["x_id"] else []
    for p in posts:
        p["kind"] = query_kind(p["query"])
        p["topical"] = post_is_topical(p["query"], p["text"])
    # device/voice evidence search and topic-query hits first, then other on-topic posts (both most engaged first),
    # then recent context
    first = sorted((p for p in posts if p["kind"] in ("evidence", "discovery")), key=lambda p: (_engagement(p), p["created_at"] or ""), reverse=True)
    second = sorted((p for p in posts if p["kind"] not in ("evidence", "discovery") and p["topical"]),
                    key=lambda p: (_engagement(p), p["created_at"] or ""), reverse=True)
    rest = sorted((p for p in posts if p["kind"] not in ("evidence", "discovery") and not p["topical"]),
                  key=lambda p: p["created_at"] or "", reverse=True)
    ranked = (first + second + rest)[:max_posts]
    posts_out = [{
        "url": p["url"] or (f"https://x.com/{handle}/status/{p['post_id']}" if handle else f"https://x.com/i/status/{p['post_id']}"),
        "date": (p["created_at"] or "")[:10] or None, "text": _cap(p["text"], MAX_POST_CHARS), "likes": p["likes"] or 0,
        "reposts": p["reposts"] or 0, "replies": p["replies"] or 0, "topical": p["topical"], "is_reply": bool(p["in_reply_to_x_id"]),
        "found_by": _FOUND_BY.get(p["kind"], "timeline"),
    } for p in ranked]

    seed = None
    sid = seed_x_id(conn, seed_handle)
    if sid and c["x_id"]:
        sf = bool(db.one(conn, "SELECT 1 AS ok FROM x_follows WHERE src_x_id=? AND dst_x_id=?", (sid, c["x_id"])))
        fs = bool(db.one(conn, "SELECT 1 AS ok FROM x_follows WHERE src_x_id=? AND dst_x_id=?", (c["x_id"], sid)))
        srow = db.one(conn, "SELECT handle FROM x_users WHERE x_id=?", (sid,)) or {}
        seed = {"handle": srow.get("handle"), "seed_follows_candidate": sf, "candidate_follows_seed": fs, "mutual_follow": sf and fs}

    map_link = None
    if c["map_person_id"]:
        mp = db.one(conn, "SELECT name, company, role, sector, tags, warmth, meetings, last_contact, group_name FROM map_people WHERE id=?",
                    (c["map_person_id"],))
        if mp:
            map_link = {k: v for k, v in dict(mp).items() if v not in (None, "")}

    dossier = {
        "candidate_id": candidate_id, "handle": handle, "profile_url": profile_url,
        "profile": {"name": u.get("name") or c["name"], "bio": _cap(u.get("bio"), MAX_BIO_CHARS), "location": u.get("location"),
                    "website": u.get("url"), "verified": None if u.get("verified") is None else bool(u.get("verified")),
                    "verified_type": u.get("verified_type"), "created_at": (u.get("created_at") or "")[:10] or None,
                    "account_age_years": account_age_years(u.get("created_at"))},
        "metrics": {"followers": u.get("followers"), "following": u.get("following"), "posts_count": u.get("posts_count"),
                    "can_dm": None if u.get("can_dm") is None else bool(u.get("can_dm"))},
        "seed": seed, "map": map_link, "discovered_via": c["discovered_via"],
        "posts": posts_out, "posts_in_db": len(posts),
    }
    while dossier["posts"] and len(_dossier_json(dossier)) > MAX_DOSSIER_CHARS:
        dossier["posts"].pop()
    return dossier


def dossier_urls(dossier: dict) -> set[str]:
    urls = {p["url"] for p in dossier.get("posts", []) if p.get("url")}
    if dossier.get("profile_url"):
        urls.add(dossier["profile_url"])
    return urls


# ---------------------------------------------------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------------------------------------------------

def load_prompt(path: Path = PROMPT_FILE) -> tuple[str, str]:
    """(system, user template). The markdown file holds '# System' then '# Prompt'."""
    text = path.read_text()
    if "\n# Prompt\n" in text:
        system, _, template = text.partition("\n# Prompt\n")
        return system.replace("# System", "", 1).strip(), template.strip()
    return "", text.strip()


def render_prompt(dossier: dict, template: str | None = None) -> str:
    template = template if template is not None else load_prompt()[1]
    return (template.replace("{{COMPETITORS}}", ", ".join(config.COMPETITORS))
            .replace("{{TODAY}}", db.utcnow()[:10])
            .replace("{{DOSSIER}}", _dossier_json(dossier)))


# ---------------------------------------------------------------------------------------------------------------------
# Cleaning the LLM output: only evidence that points into the dossier survives
# ---------------------------------------------------------------------------------------------------------------------

def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _norm_url(u: str) -> str:
    """Tolerate cosmetic differences (twitter.com vs x.com, trailing slash, query string) when matching dossier urls."""
    u = u.strip().split("?")[0].rstrip("/")
    return re.sub(r"^https?://(www\.|mobile\.)?(twitter|x)\.com", "https://x.com", u, flags=re.I)


def _as_bool(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, str):
        return {"true": True, "false": False}.get(v.strip().lower())
    return None


def _source_text(url: str, dossier: dict) -> str:
    if url == dossier.get("profile_url"):
        pr = dossier.get("profile") or {}
        return " | ".join(str(x) for x in (pr.get("name"), pr.get("bio"), pr.get("location"), pr.get("website"), dossier.get("handle")) if x)
    return next((p["text"] or "" for p in dossier.get("posts", []) if p["url"] == url), "")


def _match_url(url: str, dossier: dict) -> str | None:
    """The dossier url the model meant, or None when it is not in the dossier."""
    urls = dossier_urls(dossier)
    if url in urls:
        return url
    by_norm = {_norm_url(u): u for u in urls}
    return by_norm.get(_norm_url(url))


def _clean_evidence(items, dossier: dict, meta: dict, field: str) -> list[dict]:
    dates = {p["url"]: p.get("date") for p in dossier.get("posts", [])}
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not isinstance(it.get("url"), str):
            meta["dropped_evidence"].append({"field": field, "item": it, "why": "no url"})
            continue
        url = _match_url(it["url"], dossier)
        if url is None:
            meta["dropped_evidence"].append({"field": field, "item": it, "why": "url not in dossier"})
            continue
        quote = str(it.get("quote") or "").strip()
        src = _source_text(url, dossier)
        verbatim = bool(quote) and _norm(quote) in _norm(src)
        if not verbatim:
            meta["paraphrased_evidence"].append({"field": field, "url": url, "quote": quote})
            quote = (src[:280] if src else quote)   # show the real text, not the model's paraphrase
        date = dates.get(url) or it.get("date") or (db.utcnow()[:10] if url == dossier.get("profile_url") else None)
        out.append({"url": url, "quote": quote, "date": date, "verbatim": verbatim})
    return out


def _clean_topics(items, meta: dict) -> list[str]:
    """Known topics only (a few spellings tolerated), deduplicated, in TOPICS order (personal agents first)."""
    out = []
    for t in items if isinstance(items, list) else []:
        key = re.sub(r"[\s\-/]+", "_", str(t).strip().lower())
        key = _TOPIC_ALIASES.get(key, key)
        if key in TOPICS:
            out.append(key)
        else:
            meta["notes"].append(f"topic_voice topic {t!r} not in {TOPICS}; dropped")
    return sorted(set(out), key=TOPICS.index)


def _clean_topic_voice(block, dossier: dict, meta: dict) -> dict:
    """{value, topics, evidence}: 'true' survives only with evidence from MIN_TOPIC_VOICE_POSTS different dossier posts."""
    block = block if isinstance(block, dict) else {}
    value = _as_bool(block.get("value"))
    evidence = _clean_evidence(block.get("evidence"), dossier, meta, "topic_voice")
    if value is True:
        post_urls = {p["url"] for p in dossier.get("posts", [])}
        n = len({e["url"] for e in evidence if e["url"] in post_urls})
        if n < MIN_TOPIC_VOICE_POSTS:
            meta["notes"].append(f"topic_voice true stored as null: evidence from {n} dossier post(s), "
                                 f"the rule needs {MIN_TOPIC_VOICE_POSTS}")
            value = None
    return {"value": value, "topics": _clean_topics(block.get("topics"), meta), "evidence": evidence}


def _canonical_competitor(which) -> str | None:
    if not isinstance(which, str) or not which.strip():
        return None
    w = which.strip().lower()
    for name in config.COMPETITORS:
        n = name.lower()
        if n == w or re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", w):
            return name
    return None


def clean_classification(raw: dict, dossier: dict) -> tuple[dict, dict]:
    """Normalise types, drop evidence/events whose url is not in the dossier. Returns (clean json, meta)."""
    meta = {"dropped_evidence": [], "paraphrased_evidence": [], "dropped_events": [], "notes": []}
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    cat = raw.get("category")
    out["category"] = cat if cat in config.CATEGORIES else None
    if cat not in config.CATEGORIES:
        meta["notes"].append(f"category {cat!r} not in {config.CATEGORIES}; left NULL")
    out["us_based"] = _as_bool(raw.get("us_based"))
    out["us_based_evidence"] = raw.get("us_based_evidence") if isinstance(raw.get("us_based_evidence"), str) else None
    for f in ("wearable_user", "voice_tool_user", "reviews_products_publicly"):
        block = raw.get(f) if isinstance(raw.get(f), dict) else {}
        out[f] = {"value": _as_bool(block.get("value")), "evidence": _clean_evidence(block.get("evidence"), dossier, meta, f)}
    tie = raw.get("competitor_tie") if isinstance(raw.get("competitor_tie"), dict) else {}
    kind = tie.get("kind") if tie.get("kind") in TIE_KINDS else None
    out["competitor_tie"] = {"value": _as_bool(tie.get("value")), "which": tie.get("which") if isinstance(tie.get("which"), str) else None,
                             "which_canonical": _canonical_competitor(tie.get("which")), "kind": kind,
                             "evidence": _clean_evidence(tie.get("evidence"), dossier, meta, "competitor_tie")}
    try:
        rel = float(raw.get("audience_relevance"))
        out["audience_relevance"] = min(1.0, max(0.0, rel)) if not math.isnan(rel) else None
    except (TypeError, ValueError):
        out["audience_relevance"] = None
    if "topic_voice" in raw:   # absent (an answer from before the field existed): left out, so refresh_field finds it
        out["topic_voice"] = _clean_topic_voice(raw.get("topic_voice"), dossier, meta)
    ha = raw.get("handle_authenticity") if isinstance(raw.get("handle_authenticity"), dict) else {}
    out["handle_authenticity"] = {"value": ha.get("value") if ha.get("value") in AUTHENTICITY_VALUES else "unknown",
                                  "reasons": str(ha.get("reasons") or "").strip()}
    dates = {p["url"]: p.get("date") for p in dossier.get("posts", [])}
    events = []
    for ev in raw.get("events") if isinstance(raw.get("events"), list) else []:
        url = _match_url(ev["url"], dossier) if isinstance(ev, dict) and isinstance(ev.get("url"), str) else None
        if url is None:
            meta["dropped_events"].append({"item": ev, "why": "url not in dossier"})
            continue
        events.append({"kind": ev.get("kind") if ev.get("kind") in config.EVENT_KINDS else "other",
                       "date": dates.get(url) or ev.get("date"), "url": url, "text": str(ev.get("text") or "").strip()[:500]})
    out["events"] = events
    out["summary"] = str(raw.get("summary") or "").strip()
    out["unknowns"] = [str(x) for x in raw.get("unknowns") or [] if x]
    return out, meta


# ---------------------------------------------------------------------------------------------------------------------
# Applying a classification: candidates, evidence, events, classifications
# ---------------------------------------------------------------------------------------------------------------------

def _evidence_rows(dossier: dict, cls: dict, final_us: bool | None, us_reason: str, final_auth: str, auth_reason: str, now: str) -> list[dict]:
    post_urls = {p["url"] for p in dossier.get("posts", [])}
    profile_url = dossier.get("profile_url")
    today = now[:10]
    rows: list[dict] = []

    def add(kind, supports, polarity, weight, url, date, text):
        rows.append({"kind": kind, "supports": supports, "polarity": polarity, "weight": round(float(weight), 3),
                     "url": url or profile_url or "", "date": date, "text": (text or "")[:500]})

    for field, targets in EVIDENCE_MAP.items():
        block = cls.get(field) or {}
        value = block.get("value")
        if value is None:
            continue
        polarity = 1 if value else -1
        for ev in block.get("evidence") or []:
            kind = "post" if ev["url"] in post_urls else "bio"
            pol = polarity
            if value and field in ("wearable_user", "voice_tool_user") and ABANDON_RE.search(ev["quote"] or ""):
                pol = -1                             # a dropped device is evidence against daily wear
            for supports, weight in targets:
                w = weight * (1.0 if ev.get("verbatim", True) else 0.6) * (1.0 if kind == "post" else 0.8)
                add(kind, supports, pol, w, ev["url"], ev.get("date"), ev["quote"])

    if cls.get("not_a_person"):
        why = f"not a person ({cls['not_a_person']}): nobody behind the account would wear the ring"
        add("profile_signal", "wear", -1, 1.0, profile_url, today, why)
        add("profile_signal", "accept", -1, 1.0, profile_url, today, why)
    if cls.get("red_flag"):
        why = f"red flag ({cls['red_flag']}): the reviewers reject such accounts (docs/QA_TOP.md)"
        add("profile_signal", "wear", -1, 1.0, profile_url, today, why)
        add("profile_signal", "accept", -1, 1.0, profile_url, today, why)
    if cls.get("sells_posts"):
        add("profile_signal", "love", -1, 1.0, profile_url, today,
            f"sells posts ({cls['sells_posts']!r}): public praise may be paid, not the love the ring needs")

    if final_us is not None:
        add("profile_signal", "us", 1 if final_us else -1, 1.0, profile_url, today, us_reason or cls.get("us_based_evidence") or "")
    if final_auth != "unknown":
        add("profile_signal", "authenticity", -1 if final_auth == "suspicious" else 1,
            {"verified": 1.0, "likely_real": 0.7, "suspicious": 1.0}[final_auth], profile_url, today, f"{final_auth}: {auth_reason}")
    rel = cls.get("audience_relevance")
    if rel is not None:
        add("profile_signal", "impact", 1, rel, profile_url, today, f"audience relevance {rel:.2f} (LLM estimate from topical posts and bio)")
    m = dossier.get("metrics") or {}
    if m.get("followers"):
        w = min(1.0, max(0.0, math.log10(max(m["followers"], 1)) / 6))
        add("profile_signal", "impact", 1, w, profile_url, today,
            f"{m['followers']:,} followers, {m.get('posts_count') or 0:,} posts, verified={dossier.get('profile', {}).get('verified')}")
    if m.get("can_dm"):
        add("profile_signal", "reach", 1, 0.3, profile_url, today, "DMs open (can_dm)")
    seed = dossier.get("seed") or {}
    if seed.get("mutual_follow"):
        add("profile_signal", "reach", 1, 0.6, profile_url, today, f"mutual follow with @{seed.get('handle')}")
    return rows


def apply_classification(conn: sqlite3.Connection, run_id: int, candidate_id: int, dossier: dict, cls: dict, meta: dict,
                         model: str | None, now: str | None = None) -> dict:
    """Merge pre-checks, store the JSON, update the candidate, write evidence and events. Idempotent (upserts)."""
    now = now or db.utcnow()
    user = {**(dossier.get("profile") or {}), **(dossier.get("metrics") or {})}
    pre_auth, pre_auth_reason = precheck_handle(user)
    pre_us, pre_us_reason = precheck_us_based(user.get("location"))
    if pre_us is None:
        where = map_non_us(dossier.get("map"))
        if where:
            pre_us, pre_us_reason = False, f"Carlo's map places them outside the US: {where!r}"
    ha = cls.get("handle_authenticity") or {}
    final_auth = merge_authenticity(pre_auth, ha.get("value"), ha.get("reasons"))
    auth_reason = ha.get("reasons") or pre_auth_reason
    if pre_auth == "suspicious":
        auth_reason = pre_auth_reason + (f"; LLM said {ha.get('value')}: {ha.get('reasons')}" if ha.get("value") not in (None, "suspicious") else "")
    if pre_auth != "suspicious" and final_auth == "verified" and not user.get("verified"):
        final_auth = pre_auth or "likely_real"      # 'verified' needs the badge in the dossier, whatever the model says
        auth_reason = f"no verification badge in the profile; {auth_reason}".strip("; ")
        meta["notes"].append("handle_authenticity 'verified' downgraded: no badge")
    if pre_us is not None:
        final_us, us_reason = pre_us, pre_us_reason
        if cls.get("us_based") is not None and cls.get("us_based") != pre_us:
            meta["notes"].append(f"us_based {cls.get('us_based')} from the LLM overridden by the location field")
    else:
        final_us, us_reason = cls.get("us_based"), cls.get("us_based_evidence") or ""
        if final_us is not None and not us_reason:
            final_us, us_reason = None, ""          # a bare claim without its field or post is a guess: store nothing
            meta["notes"].append("us_based dropped: no us_based_evidence")
        said = summary_non_us(cls.get("summary")) if final_us is None else None
        if said:                                    # the model's own words, unhedged: "posts place him in China, not the US"
            final_us, us_reason = False, f"the classifier's summary places them outside the US: {said!r}"
            meta["notes"].append(f"us_based False from the summary ({said!r})")
            cls = {**cls, "us_based_evidence": us_reason}
    if pre_auth == "suspicious" and cls.get("category") not in (None, "other"):
        meta["notes"].append(f"category {cls.get('category')!r} replaced by 'other': parody / fan account, not the person")
        cls = {**cls, "category": "other"}
    org = not_a_person(dossier.get("profile") or {}, dossier.get("handle"), cls.get("summary"))
    if org:
        meta["notes"].append(f"not a person ({org}): category {cls.get('category')!r} -> 'other'")
        cls = {**cls, "category": "other", "not_a_person": org}
    prof = dossier.get("profile") or {}
    flag = None if org else red_flag(prof.get("name"), dossier.get("handle"), prof.get("bio"), prof.get("location"),
                                     prof.get("website"), in_map=bool(dossier.get("map")))
    if flag:                                        # crypto persona, promo, feed, brand or anonymous: the QA's rejects
        meta["notes"].append(f"red flag ({flag[0]}: {flag[1]!r}): category {cls.get('category')!r} -> 'other'")
        cls = {**cls, "category": "other", "red_flag": f"{flag[0]}: {flag[1]}"}
    promo = sells_posts(user.get("bio"), user.get("location"))
    if promo:
        meta["notes"].append(f"sells posts ({promo!r}): love row against")
        cls = {**cls, "sells_posts": promo}
    tie = cls.get("competitor_tie") or {}
    which = tie.get("which_canonical")
    competitor_flag = 1 if (tie.get("value") is True and which) else 0
    competitor_detail = None
    if tie.get("value") is True:
        competitor_detail = f"{which or tie.get('which') or 'unnamed'} ({tie.get('kind') or 'tie'})" + ("" if which else " - not in the competitor list")
    role = None if competitor_flag else competitor_role(prof.get("bio"))
    if role:                                        # the bio states the tie: flagged whatever the model said, never rejected
        competitor_flag, competitor_detail = 1, f"{role} (role stated in the bio)"
        meta["notes"].append(f"competitor role in the bio ({role}): competitor flag")
        cls = {**cls, "competitor_role": role}
    rival = None if competitor_detail else builds_voice_rival((dossier.get("profile") or {}).get("bio"), cls.get("summary"))
    if rival:                                       # flagged in Wispr Flow's category, never rejected: Carlo decides
        competitor_flag = 1
        competitor_detail = f"Wispr Flow rival: builds a voice-dictation product ({rival!r}), not a company in the brief's list"
        meta["notes"].append(f"voice rival ({rival!r}): competitor flag in Wispr Flow's category")
        cls = {**cls, "voice_rival": rival}
    cls = {**cls, "us_based": final_us, "handle_authenticity": {"value": final_auth, "reasons": auth_reason},
           "_meta": {**meta, "prechecks": {"handle": pre_auth, "handle_reason": pre_auth_reason, "us_based": pre_us, "us_reason": pre_us_reason},
                     "llm_handle_authenticity": ha, "model": model, "run_id": run_id, "classified_at": now,
                     "posts_in_db": dossier.get("posts_in_db"), "dossier_posts": len(dossier.get("posts") or [])}}

    db.upsert(conn, "classifications", {"candidate_id": candidate_id, "run_id": run_id, "json": db.jdumps(cls), "model": model,
                                        "created_at": now}, keys=("candidate_id", "run_id"))
    conn.execute("""UPDATE candidates SET category=?, us_based=?, competitor_flag=?, competitor_detail=?, handle_verified=?,
                    handle_verification_method='llm+signals', name=COALESCE(name, ?) WHERE candidate_id=?""",
                 (cls["category"], None if final_us is None else int(final_us), competitor_flag, competitor_detail, final_auth,
                  (dossier.get("profile") or {}).get("name"), candidate_id))

    src = f"llm:{model}" if model else "llm"
    rows = _evidence_rows(dossier, cls, final_us, us_reason, final_auth, auth_reason, now)
    # this module's profile signals describe the latest classification only: stale ones (old follower counts) are replaced
    keep = {(r["supports"], r["url"], r["text"]) for r in rows if r["kind"] == "profile_signal"}
    for old in db.rows(conn, "SELECT evidence_id, supports, url, text FROM evidence WHERE candidate_id=? AND kind='profile_signal' "
                             "AND source LIKE 'llm%'", (candidate_id,)):
        if (old["supports"], old["url"], old["text"]) not in keep:
            conn.execute("DELETE FROM evidence WHERE evidence_id=?", (old["evidence_id"],))
    # A post the model read again and no longer cites for a field loses that row: in run 14, 84 people carried 232 rows an
    # earlier answer had filed and the latest one dropped (a question, "what is everyone using for dictation?", still counted
    # as daily voice use). Rows on posts outside this dossier stay: the model could not see them.
    seen = {_norm_url(u).lower() for u in dossier_urls(dossier)}
    fresh = {(r["kind"], r["supports"], r["url"], r["text"]) for r in rows}
    pruned = 0
    for old in db.rows(conn, "SELECT evidence_id, kind, supports, url, text FROM evidence WHERE candidate_id=? "
                             "AND kind IN ('post', 'bio') AND source LIKE 'llm%'", (candidate_id,)):
        if _norm_url(old["url"] or "").lower() in seen and (old["kind"], old["supports"], old["url"], old["text"]) not in fresh:
            conn.execute("DELETE FROM evidence WHERE evidence_id=?", (old["evidence_id"],))
            pruned += 1
    for r in rows:   # upsert that keeps the first fetched_at, so the change log only sees evidence that is really new
        conn.execute("""INSERT INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source, fetched_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(candidate_id, kind, supports, url, text) DO UPDATE SET
                        polarity=excluded.polarity, weight=excluded.weight, date=excluded.date, source=excluded.source""",
                     (candidate_id, r["kind"], r["supports"], r["polarity"], r["weight"], r["url"], r["date"], r["text"], src, now))
    n_events = 0
    for ev in cls.get("events") or []:
        conn.execute("""INSERT INTO events (candidate_id, kind, date, url, text, source, fetched_at) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(candidate_id, kind, url) DO UPDATE SET date=excluded.date, text=excluded.text, source=excluded.source""",
                     (candidate_id, ev["kind"], ev.get("date"), ev["url"], ev.get("text"), src, now))
        n_events += 1
    conn.commit()
    return {"evidence": len(rows), "events": n_events, "evidence_dropped": len(meta["dropped_evidence"]),
            "events_dropped": len(meta["dropped_events"]), "handle_verified": final_auth, "us_based": final_us,
            "category": cls["category"], "competitor_flag": competitor_flag,
            "topic_voice": (cls.get("topic_voice") or {}).get("value"), "evidence_pruned": pruned,
            "not_a_person": cls.get("not_a_person"), "sells_posts": cls.get("sells_posts"), "voice_rival": cls.get("voice_rival"),
            "red_flag": cls.get("red_flag"), "competitor_role": cls.get("competitor_role")}


# ---------------------------------------------------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------------------------------------------------

def _last_classifications(conn) -> dict[int, dict]:
    """candidate_id -> {run_id, created_at, posts_in_db, fields} of its most recent classification (fields: its top-level
    JSON keys)."""
    out: dict[int, dict] = {}
    for r in db.rows(conn, """SELECT k.candidate_id, k.run_id, k.created_at, k.json FROM classifications k
                              JOIN (SELECT candidate_id, MAX(run_id) AS r FROM classifications GROUP BY candidate_id) m
                                ON m.candidate_id = k.candidate_id AND m.r = k.run_id"""):
        k = db.jloads(r["json"], {}) or {}
        meta = k.get("_meta") or {}
        out[r["candidate_id"]] = {"run_id": r["run_id"], "created_at": r["created_at"], "posts_in_db": meta.get("posts_in_db"),
                                  "fields": set(k)}
    return out


def _select_candidates(conn, run_id: int, max_candidates: int, seed_handle: str | None = None, force: bool = False,
                       refresh_field: str | None = None) -> tuple[list[int], dict]:
    """Candidates with an X account, in discover.candidate_priorities order. Skipped: classified in this run; classified in
    an earlier run with no new posts since (post count unchanged; older rows without that count: no post fetched after
    the classification), unless force; nothing to read (no bio, no posts). With `refresh_field`, only candidates whose
    latest classification lacks that key are selected (never-classified ones and those that have it are skipped), new
    posts or not. Returns (ids, skip counts)."""
    last = _last_classifications(conn)
    skipped = {"skipped_this_run": 0, "skipped_unchanged": 0, "skipped_no_data": 0, "skipped_has_field": 0,
               "skipped_not_classified": 0}
    ids: list[int] = []
    for s in candidate_priorities(conn, seed_handle=seed_handle):
        k = last.get(s["candidate_id"])
        if k and k["run_id"] == run_id:
            skipped["skipped_this_run"] += 1
            continue
        if refresh_field:
            if not k:
                skipped["skipped_not_classified"] += 1
                continue
            if refresh_field in k["fields"]:
                skipped["skipped_has_field"] += 1
                continue
        elif k and not force:
            if k["posts_in_db"] is not None:
                changed = s["posts"] > int(k["posts_in_db"])
            else:
                changed = (s["last_post_fetched_at"] or "") > (k["created_at"] or "")
            if not changed:
                skipped["skipped_unchanged"] += 1
                continue
        if not s["has_bio"] and not s["posts"]:
            skipped["skipped_no_data"] += 1
            continue
        if len(ids) < max_candidates:
            ids.append(s["candidate_id"])
    return ids, skipped


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _CostRelay:
    """Stands in for the SQLite connection that llm.complete_json receives in worker threads (sqlite3 connections belong
    to the thread that opened them, and every write stays on the calling thread). costs.guard's SELECTs are answered with
    this run's anthropic spend at the start of the stage plus everything relayed since, and for the monthly cap
    (schedule.month_spend) with this month's anthropic spend at the start plus the same; costs.record's INSERT is queued
    and written by the calling thread in flush(). Any other statement raises."""

    _INSERT = ("INSERT INTO costs (ts, run_id, provider, endpoint, units, unit_cost_usd, cost_usd, note) "
               "VALUES (?,?,?,?,?,?,?,?)")

    def __init__(self, conn: sqlite3.Connection, run_id: int | None):
        self._base = costs.spent(conn, llm.PROVIDER, run_id)
        self._month = schedule.month_spend(conn).get(llm.PROVIDER, 0.0)
        self._pending: list[tuple] = []
        self._relayed = 0.0
        self._lock = threading.Lock()

    def execute(self, sql: str, params=()):
        head = " ".join(sql.split()).upper()
        with self._lock:
            if head.startswith("SELECT") and " FROM COSTS" in head and "GROUP BY PROVIDER" in head:
                return _Rows([{"provider": llm.PROVIDER, "usd": self._month + self._relayed}])
            if head.startswith("SELECT") and " FROM COSTS" in head:
                return _Rows([(self._base + self._relayed,)])
            if head.startswith("INSERT INTO COSTS"):
                row = tuple(params)
                self._pending.append(row)
                self._relayed += float(row[6] or 0.0)
                return _Rows([])
        raise RuntimeError(f"classify: worker threads may not touch SQLite ({sql.strip()[:60]!r})")

    def commit(self) -> None:
        pass

    def flush(self, conn: sqlite3.Connection) -> int:
        with self._lock:
            rows, self._pending = self._pending, []
        if rows:
            conn.executemany(self._INSERT, rows)
            conn.commit()
        return len(rows)


def run_classify(conn: sqlite3.Connection, run_id: int, candidate_ids: list[int] | None = None, max_candidates: int = 200,
                 seed_handle: str | None = None, workers: int = 4, force: bool = False, refresh_field: str | None = None) -> dict:
    """Classify candidates (explicit `candidate_ids` are classified regardless of earlier runs). `workers` LLM calls run
    concurrently; dossiers are built and results written on this thread, one candidate at a time, as calls complete.
    `refresh_field` (a CLASSIFICATION_SCHEMA key, e.g. 'topic_voice'): re-classify, once, exactly the candidates whose
    latest classification was made before that field existed; `max_candidates` still caps the batch."""
    if refresh_field is not None and refresh_field not in CLASSIFICATION_SCHEMA["properties"]:
        raise ValueError(f"refresh_field {refresh_field!r} is not a classification field: "
                         f"{', '.join(CLASSIFICATION_SCHEMA['properties'])}")
    now = db.utcnow()
    system, template = load_prompt()
    model = config.llm_model() or config.llm_backend()
    backend = config.llm_backend()
    if candidate_ids is not None:
        ids, sel = list(dict.fromkeys(int(i) for i in candidate_ids))[:max_candidates], {}
    else:
        ids, sel = _select_candidates(conn, run_id, max_candidates, seed_handle=seed_handle, force=force,
                                      refresh_field=refresh_field)
    workers = max(1, int(workers or 1))
    stats = {"selected": len(ids), "classified": 0, "skipped_no_data": sel.get("skipped_no_data", 0),
             "skipped_unchanged": sel.get("skipped_unchanged", 0), "skipped_this_run": sel.get("skipped_this_run", 0),
             "errors": 0, "evidence": 0, "evidence_dropped": 0, "evidence_pruned": 0, "not_a_person": 0, "sells_posts": 0, "red_flag": 0,
             "events": 0, "events_dropped": 0, "suspicious": 0,
             "competitor_flagged": 0, "by_category": {}, "budget_stop": False, "workers": workers, "cost_usd": 0.0,
             "refresh_field": refresh_field, "skipped_has_field": sel.get("skipped_has_field", 0),
             "skipped_not_classified": sel.get("skipped_not_classified", 0),
             "topic_voice": {"true": 0, "false": 0, "null": 0, "missing": 0}}
    if backend == "anthropic":
        print(f"[cost] anthropic classify: ~{costs.fmt(len(ids) * EST_USD_PER_CALL)} for {len(ids)} candidates "
              f"(API, ~{costs.fmt(EST_USD_PER_CALL)} each; guarded by BUDGET_ANTHROPIC_USD)")
    else:
        print(f"[cost] {backend} classify: $0 billed for {len(ids)} candidates ({workers} in parallel)")
    if candidate_ids is None:
        n_no_x = (db.one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE x_id IS NULL") or {}).get("n", 0)
        if n_no_x:
            log.info("classify: %d candidates without an X account are left to the map side (no dossier)", n_no_x)
    log.info("classify: %d selected (%d unchanged since their last classification, %d already done this run, %d without data)",
             len(ids), stats["skipped_unchanged"], stats["skipped_this_run"], stats["skipped_no_data"])
    if refresh_field:
        log.info("classify: refreshing %r: %d classified without it selected (%d already have it, %d never classified)",
                 refresh_field, len(ids), stats["skipped_has_field"], stats["skipped_not_classified"])

    relay = _CostRelay(conn, run_id)

    def ask(prompt: str) -> dict:   # worker thread: the LLM call only, no SQLite
        return llm.complete_json(prompt, CLASSIFICATION_SCHEMA, system=system, model=config.llm_model(), conn=relay, run_id=run_id)

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="classify")
    in_flight: dict = {}
    todo = iter(ids)
    stop = False
    try:
        while True:
            while not stop and len(in_flight) < workers:
                cid = next(todo, None)
                if cid is None:
                    break
                try:
                    dossier = build_dossier(conn, cid, seed_handle=seed_handle)
                except KeyError:
                    log.warning("classify: candidate %s not found; skipped", cid)
                    stats["errors"] += 1
                    continue
                if not (dossier["profile"].get("bio") or dossier["posts"]):
                    log.info("classify: candidate %s (@%s) has no bio and no posts; skipped", cid, dossier.get("handle"))
                    stats["skipped_no_data"] += 1
                    continue
                in_flight[pool.submit(ask, render_prompt(dossier, template))] = (cid, dossier)
            if not in_flight:
                break
            done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            relay.flush(conn)
            for fut in done:
                cid, dossier = in_flight.pop(fut)
                try:
                    raw = fut.result()
                except costs.BudgetExceeded as e:
                    if not stop:
                        log.warning("classify: budget reached, no new calls: %s", e)
                    stop = stats["budget_stop"] = True
                    continue
                except llm.LLMError as e:   # one bad answer must not kill the batch
                    log.error("classify: candidate %s (@%s) failed, skipped: %s", cid, dossier.get("handle"), e)
                    stats["errors"] += 1
                    continue
                except Exception as e:
                    log.exception("classify: candidate %s (@%s) failed unexpectedly, skipped: %s", cid, dossier.get("handle"), e)
                    stats["errors"] += 1
                    continue
                cleaned, meta = clean_classification(raw, dossier)
                r = apply_classification(conn, run_id, cid, dossier, cleaned, meta, model, now)
                stats["classified"] += 1
                stats["evidence"] += r["evidence"]
                stats["evidence_dropped"] += r["evidence_dropped"]
                stats["evidence_pruned"] += r["evidence_pruned"]
                stats["not_a_person"] += bool(r["not_a_person"])
                stats["sells_posts"] += bool(r["sells_posts"])
                stats["red_flag"] += bool(r["red_flag"])
                stats["events"] += r["events"]
                stats["events_dropped"] += r["events_dropped"]
                stats["suspicious"] += r["handle_verified"] == "suspicious"
                stats["competitor_flagged"] += r["competitor_flag"]
                tv = "missing" if "topic_voice" not in cleaned else {True: "true", False: "false"}.get(r["topic_voice"], "null")
                stats["topic_voice"][tv] += 1
                cat = r["category"] or "none"
                stats["by_category"][cat] = stats["by_category"].get(cat, 0) + 1
                log.info("classify: @%s -> %s, us=%s, handle=%s, competitor=%s, %d evidence (%d dropped), %d events",
                         dossier.get("handle"), r["category"], r["us_based"], r["handle_verified"], r["competitor_flag"],
                         r["evidence"], r["evidence_dropped"], r["events"])
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        relay.flush(conn)
        raise
    pool.shutdown(wait=True)
    relay.flush(conn)
    conn.commit()
    stats["cost_usd"] = costs.spent(conn, llm.PROVIDER, run_id)
    log.info("classify done: %d classified, %d errors, by category %s, %d evidence (%d dropped), %d events",
             stats["classified"], stats["errors"], stats["by_category"], stats["evidence"], stats["evidence_dropped"], stats["events"])
    return stats


# Notes apply_classification adds itself: dropped before a re-application so they are not repeated.
_GUARD_NOTE_PREFIXES = ("not a person", "sells posts", "voice rival", "red flag", "competitor role", "category ", "us_based ",
                        "handle_authenticity ")


def _cited_urls(cls: dict) -> set[str]:
    urls = {ev.get("url") for field in EVIDENCE_MAP for ev in ((cls.get(field) or {}).get("evidence") or [])}
    urls |= {ev.get("url") for ev in cls.get("events") or []}
    return {u for u in urls if u}


def reapply_guards(conn: sqlite3.Connection, run_id: int, candidate_ids: list[int] | None = None, now: str | None = None) -> dict:
    """Re-apply the deterministic QA guards to each candidate's latest stored classification, with no LLM call: not_a_person,
    sells_posts, red_flag, a dropped device counting against wear, a non-US map entry or summary, a voice-dictation rival,
    a role at a competitor stated in the bio, and pruning the post/bio rows an earlier answer
    filed that the latest answer, having read those posts, no longer cites. The stored JSON is the model's cleaned answer; the
    dossier is rebuilt for profile, map and seed. Pruning needs the posts the model saw: when that classification's dossier
    held every post in the DB, they are the person's posts fetched before it; otherwise only the posts it cites (nothing the
    model may not have seen is pruned). Stored under `run_id`; the LLM's run, time, category and post counts stay in
    _meta.llm_*, so running it again gives the same result."""
    now = now or db.utcnow()
    stats: Counter = Counter()
    wanted = {int(i) for i in candidate_ids} if candidate_ids is not None else None
    latest = db.rows(conn, """SELECT k.candidate_id, k.run_id, k.created_at, k.json, k.model FROM classifications k
                              JOIN (SELECT candidate_id, MAX(run_id) AS r FROM classifications GROUP BY candidate_id) m
                                ON m.candidate_id = k.candidate_id AND m.r = k.run_id ORDER BY k.candidate_id""")
    for r in latest:
        cid = r["candidate_id"]
        if (wanted is not None and cid not in wanted) or r["run_id"] == run_id:
            continue
        cls = db.jloads(r["json"], {}) or {}
        old = cls.pop("_meta", None) or {}
        was = {"category": cls.get("category"), "not_a_person": cls.pop("not_a_person", None),
               "sells_posts": cls.pop("sells_posts", None), "voice_rival": cls.pop("voice_rival", None),
               "red_flag": cls.pop("red_flag", None), "competitor_role": cls.pop("competitor_role", None)}
        llm = {"llm_run_id": old.get("llm_run_id", r["run_id"]),
               "llm_classified_at": old.get("llm_classified_at") or old.get("classified_at") or r["created_at"],
               "llm_category": old.get("llm_category", cls.get("category")),
               "llm_posts_in_db": old.get("llm_posts_in_db", old.get("posts_in_db")),
               "llm_dossier_posts": old.get("llm_dossier_posts", old.get("dossier_posts"))}
        cls["category"] = llm["llm_category"]
        if old.get("llm_handle_authenticity"):
            cls["handle_authenticity"] = old["llm_handle_authenticity"]    # the model's own answer, merged again below
        meta = {**{k: v for k, v in old.items() if k not in ("prechecks", "llm_handle_authenticity", "model", "run_id",
                                                               "classified_at", "posts_in_db", "dossier_posts")},
                **llm, "reapplied_at": now}
        meta["notes"] = [n for n in old.get("notes") or [] if not str(n).startswith(_GUARD_NOTE_PREFIXES)]
        meta.setdefault("dropped_evidence", [])
        meta.setdefault("dropped_events", [])
        try:
            dossier = build_dossier(conn, cid)
        except KeyError:
            continue
        handle = dossier.get("handle")
        cited = _cited_urls(cls) - {dossier.get("profile_url")}
        seen = set(cited)
        n_db, n_dos = llm["llm_posts_in_db"], llm["llm_dossier_posts"]
        if n_db is not None and n_dos is not None and n_db <= n_dos:
            c = db.one(conn, "SELECT x_id FROM candidates WHERE candidate_id=?", (cid,)) or {}
            for p in db.rows(conn, "SELECT post_id, url FROM x_posts WHERE x_id=? AND fetched_at <= ?",
                             (c.get("x_id"), llm["llm_classified_at"])):
                seen.add(p["url"] or (f"https://x.com/{handle}/status/{p['post_id']}" if handle else f"https://x.com/i/status/{p['post_id']}"))
        else:
            stats["pruning_limited_to_cited"] += 1
        dossier["posts"] = [{"url": u} for u in sorted(seen)]
        res = apply_classification(conn, run_id, cid, dossier, cls, meta, r["model"], now)
        stats["reapplied"] += 1
        stats["evidence_pruned"] += res["evidence_pruned"]
        stats["people_pruned"] += bool(res["evidence_pruned"])
        stats["not_a_person_new"] += bool(res["not_a_person"] and not was["not_a_person"])
        stats["sells_posts_new"] += bool(res["sells_posts"] and not was["sells_posts"])
        stats["voice_rival_new"] += bool(res["voice_rival"] and not was["voice_rival"])
        stats["red_flag_new"] += bool(res["red_flag"] and not was["red_flag"])
        stats["competitor_role_new"] += bool(res["competitor_role"] and not was["competitor_role"])
        stats["category_changed"] += res["category"] != was["category"]
    log.info("reapply_guards: %s", dict(stats))
    return dict(stats)


if __name__ == "__main__":   # python -m ring_wishlist.classify [--max N] [--ids 1,2,3] [--workers 4] [--force] [--refresh-field F]
    import argparse

    ap = argparse.ArgumentParser(description="ring_wishlist classification stage")
    ap.add_argument("--max", type=int, default=200)
    ap.add_argument("--ids", default=None, help="comma-separated candidate ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="reclassify candidates even without new posts")
    ap.add_argument("--refresh-field", default=None, metavar="FIELD",
                    help="re-classify only candidates whose latest classification lacks FIELD (e.g. topic_voice); "
                         "raise --max to cover them all")
    ap.add_argument("--reapply-guards", action="store_true",
                    help="no LLM: re-apply the QA guards to every stored classification (reapply_guards)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    c = db.connect()
    rid = db.new_run(c, "classify")
    if args.reapply_guards:
        print(reapply_guards(c, rid, [int(x) for x in args.ids.split(",")] if args.ids else None))
        db.finish_run(c, rid)
        raise SystemExit(0)
    print(run_classify(c, rid, [int(x) for x in args.ids.split(",")] if args.ids else None, args.max, workers=args.workers,
                       force=args.force, refresh_field=args.refresh_field))
    db.finish_run(c, rid)
