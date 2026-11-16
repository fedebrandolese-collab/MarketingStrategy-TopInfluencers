#!/usr/bin/env python
"""Build a demo database from data/fixtures so the dashboard can be shown before real data exists.

    python scripts/seed_demo_db.py            # -> data/demo.db (rebuilt from scratch)
    python scripts/seed_demo_db.py /tmp/x.db  # any path

What it seeds (all offline, all from data/fixtures): the map mirror (map_* tables, same loader pattern as
tests/conftest.py), x_users / x_posts / x_follows from x_users.json and x_posts.json, 8 candidates across the four
categories (one flagged competitor, one not US, one unverified handle, one map person reached directly), evidence rows
with urls, events, classifications json in the contract shape, two scored runs with a change log (2 up, 3 down, 1 new),
paths in every contract shape (direct, via, x_direct, open_dm, none), a couple of status rows and a cost ledger.
Topic affinity (scores.affinity + a reasons_json "affinity" factor with its best posts) is set on the latest run only:
run 1 stands for a run scored before affinity existed (NULL), and Adrian has no stored posts (value None).
The scores follow the contract formula: total = 100 * p_accept * p_wear * p_love * (0.3 + 0.7*impact) * (0.25 + 0.75*reach).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from wearable_wishlist import config, db  # noqa: E402
from wearable_wishlist.score import PRIORS  # noqa: E402
from wearable_wishlist.sources.base import FixtureSource, persist_follows, persist_posts, persist_users  # noqa: E402

DEFAULT_PATH = config.DATA_DIR / "demo.db"

RUN1 = {"started_at": "2026-09-15T07:00:00Z", "finished_at": "2026-09-15T07:14:20Z", "notes": "demo seed: previous run"}
RUN2 = {"started_at": "2026-09-29T07:00:00Z", "finished_at": "2026-09-29T07:09:05Z", "notes": "demo seed: latest run"}
T1, T2 = "2026-09-15T07:05:00Z", "2026-09-29T07:04:00Z"   # fetched_at of things seen by run 1 / run 2

POST_URL = "https://x.com/{handle}/status/{post_id}"

# ---------------------------------------------------------------- candidates (key -> row)
CANDIDATES: dict[str, dict] = {
    "rina": dict(x_id="2", handle="wearablenerd_test", name="Rina Wearable", category="x_voice", us_based=1, map_person_id=None,
                 handle_verified="verified", handle_verification_method="x_verified+bio_link", competitor_flag=0, competitor_detail=None,
                 discovered_via='query:"smart wearable" (AI OR voice OR assistant OR agent) -is:retweet lang:en', first_seen=T1, last_seen=T2,
                 notes=None),
    "tom": dict(x_id="3", handle="techwriter_test", name="Tom Writer", category="media", us_based=1, map_person_id=None,
                handle_verified="verified", handle_verification_method="x_verified+outlet_bio", competitor_flag=0, competitor_detail=None,
                discovered_via="seed", first_seen=T1, last_seen=T2, notes=None),
    "vera": dict(x_id="4", handle="vc_test", name="Vera Capital", category="vc", us_based=1, map_person_id=None,
                 handle_verified="verified", handle_verification_method="x_verified+fund_site", competitor_flag=0, competitor_detail=None,
                 discovered_via="seed", first_seen=T1, last_seen=T2, notes=None),
    "frank": dict(x_id="5", handle="founder_test", name="Frank Founder", category="founder_angel", us_based=1, map_person_id=None,
                  handle_verified="likely_real", handle_verification_method="user_search+llm", competitor_flag=0, competitor_detail=None,
                  discovered_via='query:"screen-free" OR "screenless" (AI OR device OR wearable) -is:retweet lang:en', first_seen=T1, last_seen=T2,
                  notes=None),
    "adrian": dict(x_id=None, handle="adrianaoun_test", name="Adrian Aoun", category="founder_angel", us_based=None, map_person_id=51,
                   handle_verified="likely_real", handle_verification_method="user_search+llm", competitor_flag=0, competitor_detail=None,
                   discovered_via="map", first_seen=T1, last_seen=T2, notes="Map person (capital): angel + advisor, LP in ~100 funds. Location not in the map."),
    "cora": dict(x_id="8", handle="competitor_test", name="Cora Competitor", category="other", us_based=1, map_person_id=None,
                 handle_verified="verified", handle_verification_method="x_verified", competitor_flag=1,
                 competitor_detail="Oura: product lead (bio: 'Product lead @ouraring')",
                 discovered_via='query:"Oura" (unboxing OR review OR "day one" OR "weeks with") -is:retweet lang:en', first_seen=T1, last_seen=T2, notes=None),
    "parody": dict(x_id="6", handle="parody_test", name="Rina Wearable (parody)", category="x_voice", us_based=None, map_person_id=None,
                   handle_verified="suspicious", handle_verification_method="llm+created_at",
                   competitor_flag=0, competitor_detail=None, discovered_via="following:wearablenerd_test", first_seen=T1, last_seen=T2,
                   notes="Bio says PARODY; account created 2025-11; 900 followers."),
    "elke": dict(x_id="7", handle="eu_test", name="Elke Uropa", category="x_voice", us_based=0, map_person_id=None,
                 handle_verified="likely_real", handle_verification_method="user_search+llm", competitor_flag=0, competitor_detail=None,
                 discovered_via='query:"smart wearable" (AI OR voice OR assistant OR agent) -is:retweet lang:en', first_seen=T2, last_seen=T2, notes=None),
}
ONLY_IN_RUN2 = {"elke"}

# ---------------------------------------------------------------- evidence (key -> rows); "ref" names a row for reasons_json
def _post(handle: str, post_id: str) -> str:
    return POST_URL.format(handle=handle, post_id=post_id)


EVIDENCE: dict[str, list[dict]] = {
    "rina": [
        dict(ref="rina_unbox", kind="post", supports="wear", polarity=1, weight=0.9, url=_post("wearablenerd_test", "101"), date="2026-09-20",
             text="Unboxing the new Oura Wearable 5. Day one impressions: thinner, and the voice notes feature is finally usable.", fetched_at=T2),
        dict(ref="rina_voice", kind="post", supports="love", polarity=1, weight=0.8, url=_post("wearablenerd_test", "102"), date="2026-08-02",
             text="Hot take: the next great wearable has no screen. Smart wearables plus a voice agent is the form factor.", fetched_at=T1),
        dict(ref="rina_limitless", kind="post", supports="love", polarity=1, weight=0.7, url=_post("wearablenerd_test", "103"), date="2026-07-11",
             text="Three weeks with the Limitless pendant. Great transcripts, but I keep forgetting to wear it. Wearables win on that.", fetched_at=T1),
        dict(ref="rina_bio", kind="bio", supports="wear", polarity=1, weight=0.6, url="https://x.com/wearablenerd_test", date="2026-09-15",
             text="Reviewing wearables since 2019. Oura on one hand, Ultrahuman on the other. Voice-first believer.", fetched_at=T1),
        dict(ref="rina_accept", kind="profile_signal", supports="accept", polarity=1, weight=0.5, url="https://x.com/wearablenerd_test", date="2026-09-15",
             text="Bio links to a review site (wearablenerd.example): accepts devices to review.", fetched_at=T1),
        dict(ref="rina_impact", kind="profile_signal", supports="impact", polarity=1, weight=0.7, url="https://x.com/wearablenerd_test", date="2026-09-15",
             text="48k followers; 1.2k likes on the voice-agent post.", fetched_at=T1),
        dict(ref="rina_us", kind="profile_signal", supports="us", polarity=1, weight=0.8, url="https://x.com/wearablenerd_test", date="2026-09-15",
             text="location: Austin, TX", fetched_at=T1),
        dict(ref="rina_auth", kind="profile_signal", supports="authenticity", polarity=1, weight=0.9, url="https://x.com/wearablenerd_test", date="2026-09-15",
             text="blue verified since 2015, 48k followers, bio links to review site", fetched_at=T1),
    ],
    "tom": [
        dict(ref="tom_review", kind="post", supports="love", polarity=1, weight=0.7, url=_post("techwriter_test", "201"), date="2026-09-10",
             text="Reviewed the Humane Pin successor for GadgetDaily. Voice-first computing is closer than the skeptics think.", fetched_at=T1),
        dict(ref="tom_ring", kind="post", supports="wear", polarity=1, weight=0.6, url=_post("techwriter_test", "202"), date="2026-06-01",
             text="I tried a screen-free week. Kept the Oura wearable and AirPods, ditched the phone after 6pm. Writing it up.", fetched_at=T1),
        dict(ref="tom_bio", kind="bio", supports="impact", polarity=1, weight=0.9, url="https://x.com/techwriter_test", date="2026-09-15",
             text="Senior editor @GadgetDaily. Covers AI hardware, wearables, and the post-smartphone era. 120k followers.", fetched_at=T1),
        dict(ref="tom_us", kind="profile_signal", supports="us", polarity=1, weight=0.8, url="https://x.com/techwriter_test", date="2026-09-15",
             text="location: New York, NY", fetched_at=T1),
    ],
    "vera": [
        dict(ref="vera_fund", kind="post", supports="impact", polarity=1, weight=0.8, url=_post("vc_test", "301"), date="2026-09-01",
             text="We just closed Fund IV. Doubling down on consumer hardware and personal AI agents. Founders building ambient devices, DMs open.", fetched_at=T1),
        dict(ref="vera_ring", kind="post", supports="wear", polarity=1, weight=0.7, url=_post("vc_test", "302"), date="2026-05-15",
             text="Wearing a smart wearable changed how I think about notifications. Fewer screens, more presence.", fetched_at=T1),
        dict(ref="vera_bio", kind="bio", supports="love", polarity=1, weight=0.5, url="https://x.com/vc_test", date="2026-09-15",
             text="Partner @ExampleVentures. Consumer hardware, voice, agents. Wears a wearable, talks to her AI.", fetched_at=T1),
        dict(ref="vera_accept", kind="profile_signal", supports="accept", polarity=1, weight=0.5, url=_post("vc_test", "301"), date="2026-09-01",
             text="'Founders building ambient devices, DMs open' - invites exactly this kind of approach.", fetched_at=T1),
        dict(ref="vera_us", kind="profile_signal", supports="us", polarity=1, weight=0.8, url="https://x.com/vc_test", date="2026-09-15",
             text="location: Palo Alto, CA", fetched_at=T1),
    ],
    "frank": [
        dict(ref="frank_angel", kind="post", supports="accept", polarity=1, weight=0.8, url=_post("founder_test", "401"), date="2026-09-18",
             text="Angel-invested in a voice-first AI earbuds startup. The future is human, not artificial. Screens are the bug.", fetched_at=T2),
        dict(ref="frank_love", kind="post", supports="love", polarity=1, weight=0.7, url=_post("founder_test", "401"), date="2026-09-18",
             text="'The future is human, not artificial' - wearable_wishlist's own thesis, in his words.", fetched_at=T2),
        dict(ref="frank_bio", kind="bio", supports="accept", polarity=1, weight=0.5, url="https://x.com/founder_test", date="2026-09-15",
             text="Founded VoiceThing (acq. 2024). Angel in 30 companies. Screen-free evangelist.", fetched_at=T1),
        dict(ref="frank_us", kind="profile_signal", supports="us", polarity=1, weight=0.8, url="https://x.com/founder_test", date="2026-09-15",
             text="location: Boulder, CO", fetched_at=T1),
    ],
    "adrian": [
        dict(ref="adrian_map", kind="map", supports="accept", polarity=1, weight=0.9, url="https://network.ri.fo/?id=51", date="2026-09-17",
             text="Map: angel + advisor (LP in ~100 funds), 3 meetings, 84 interactions, last contact 12 days ago.", fetched_at=T1),
        dict(ref="adrian_reach", kind="map", supports="reach", polarity=1, weight=0.9, url="https://network.ri.fo/?id=51", date="2026-09-17",
             text="Warm direct relationship (warmth rank top quartile; tags advisor, angel, x_following).", fetched_at=T1),
        dict(ref="adrian_impact", kind="investment", supports="impact", polarity=1, weight=0.7, url="https://www.linkedin.com/in/adrianaoun", date="2026-02-19",
             text="Co-founder, Foundational Industries; LP in ~100 funds: opens funding conversations.", fetched_at=T1),
    ],
    "cora": [
        dict(ref="cora_post", kind="post", supports="competitor", polarity=1, weight=0.9, url=_post("competitor_test", "701"), date="2026-09-15",
             text="Proud of what the Oura team shipped this quarter. Wearables are the wearable people actually keep on.", fetched_at=T2),
        dict(ref="cora_bio", kind="bio", supports="competitor", polarity=1, weight=1.0, url="https://x.com/competitor_test", date="2026-09-15",
             text="Product lead @ouraring. Opinions my own.", fetched_at=T2),
        dict(ref="cora_wear", kind="post", supports="wear", polarity=1, weight=0.8, url=_post("competitor_test", "701"), date="2026-09-15",
             text="Wears wearables daily (Oura product lead).", fetched_at=T2),
        dict(ref="cora_us", kind="profile_signal", supports="us", polarity=1, weight=0.8, url="https://x.com/competitor_test", date="2026-09-15",
             text="location: San Francisco, CA", fetched_at=T1),
    ],
    "parody": [
        dict(ref="parody_bio", kind="bio", supports="authenticity", polarity=-1, weight=1.0, url="https://x.com/parody_test", date="2026-09-15",
             text="PARODY account. Not affiliated with anyone. Created 2025-11, 900 followers.", fetched_at=T2),
    ],
    "elke": [
        dict(ref="elke_ring", kind="post", supports="wear", polarity=1, weight=0.8, url=_post("eu_test", "601"), date="2026-09-12",
             text="Smart wearable number six arrived in Berlin today. Comparing AI wearables all month.", fetched_at=T2),
        dict(ref="elke_bio", kind="bio", supports="love", polarity=1, weight=0.5, url="https://x.com/eu_test", date="2026-09-29",
             text="AI wearables enthusiast. Smart wearable collector.", fetched_at=T2),
        dict(ref="elke_us", kind="profile_signal", supports="us", polarity=-1, weight=0.9, url="https://x.com/eu_test", date="2026-09-29",
             text="location: Berlin, Germany", fetched_at=T2),
    ],
}

EVENTS: dict[str, list[dict]] = {
    "rina": [dict(kind="wearable_unboxing", date="2026-09-20", url=_post("wearablenerd_test", "101"),
                  text="Unboxed the Oura Wearable 5 (voice notes 'finally usable').", source="fixture", fetched_at=T2),
             dict(kind="device_review", date="2026-07-11", url=_post("wearablenerd_test", "103"),
                  text="Three weeks with the Limitless pendant.", source="fixture", fetched_at=T1)],
    "tom": [dict(kind="device_review", date="2026-09-10", url=_post("techwriter_test", "201"),
                 text="Reviewed the Humane Pin successor for GadgetDaily.", source="fixture", fetched_at=T1)],
    "vera": [dict(kind="fund_raised", date="2026-09-01", url=_post("vc_test", "301"),
                  text="Closed Fund IV: consumer hardware and personal AI agents.", source="fixture", fetched_at=T1)],
    "frank": [dict(kind="other", date="2026-09-18", url=_post("founder_test", "401"),
                   text="Angel-invested in a voice-first AI earbuds startup.", source="fixture", fetched_at=T2)],
    "cora": [dict(kind="joined_competitor", date="2026-09-15", url=_post("competitor_test", "701"),
                  text="Bio and post confirm product lead at Oura.", source="fixture", fetched_at=T2)],
    "adrian": [], "parody": [], "elke": [],
}

# ---------------------------------------------------------------- classifications (contract JSON, stored verbatim)
def _ev(url: str, quote: str, date: str) -> dict:
    return {"url": url, "quote": quote, "date": date}


def _cls(category, us, us_ev, wear, voice, reviews, comp, relevance, auth, auth_reasons, events, summary, unknowns) -> dict:
    return {"category": category, "us_based": us, "us_based_evidence": us_ev,
            "wearable_user": wear, "voice_tool_user": voice, "reviews_products_publicly": reviews,
            "competitor_tie": comp, "audience_relevance": relevance,
            "handle_authenticity": {"value": auth, "reasons": auth_reasons},
            "events": events, "summary": summary, "unknowns": unknowns}


NO_TIE = {"value": False, "which": None, "kind": None, "evidence": []}
UNKNOWN = {"value": None, "evidence": []}

CLASSIFICATIONS: dict[str, dict] = {
    "rina": _cls("x_voice", True, "location says Austin, TX",
                 {"value": True, "evidence": [_ev(_post("wearablenerd_test", "101"), "Unboxing the new Oura Wearable 5", "2026-09-20")]},
                 {"value": True, "evidence": [_ev(_post("wearablenerd_test", "102"), "Smart wearables plus a voice agent is the form factor", "2026-08-02")]},
                 {"value": True, "evidence": [_ev(_post("wearablenerd_test", "103"), "Three weeks with the Limitless pendant", "2026-07-11")]},
                 NO_TIE, 0.9, "verified", "blue verified since 2015, 48k followers, bio links to review site",
                 [{"kind": "wearable_unboxing", "date": "2026-09-20", "url": _post("wearablenerd_test", "101"), "text": "Unboxing the new Oura Wearable 5"}],
                 "Wearables reviewer who already wears two wearables and argues for screen-free voice devices.",
                 ["whether they accept free devices"]),
    "tom": _cls("media", True, "location says New York, NY",
                {"value": True, "evidence": [_ev(_post("techwriter_test", "202"), "Kept the Oura wearable and AirPods", "2026-06-01")]},
                UNKNOWN,
                {"value": True, "evidence": [_ev(_post("techwriter_test", "201"), "Reviewed the Humane Pin successor for GadgetDaily", "2026-09-10")]},
                NO_TIE, 0.85, "verified", "blue verified, outlet named in bio, 120k followers",
                [{"kind": "device_review", "date": "2026-09-10", "url": _post("techwriter_test", "201"), "text": "Reviewed the Humane Pin successor"}],
                "Senior editor covering AI hardware who wore an Oura wearable through a screen-free week.",
                ["whether GadgetDaily accepts pre-release units", "no voice-tool post found"]),
    "vera": _cls("vc", True, "location says Palo Alto, CA",
                 {"value": True, "evidence": [_ev(_post("vc_test", "302"), "Wearing a smart wearable changed how I think about notifications", "2026-05-15")]},
                 {"value": True, "evidence": [_ev("https://x.com/vc_test", "Wears a wearable, talks to her AI", "2026-09-15")]},
                 UNKNOWN, NO_TIE, 0.8, "verified", "blue verified, fund site links back",
                 [{"kind": "fund_raised", "date": "2026-09-01", "url": _post("vc_test", "301"), "text": "We just closed Fund IV"}],
                 "Consumer-hardware VC who wears a wearable, talks to her AI and just raised Fund IV for ambient devices.",
                 ["whether she posts product opinions publicly"]),
    "frank": _cls("founder_angel", True, "location says Boulder, CO",
                  UNKNOWN,
                  {"value": True, "evidence": [_ev("https://x.com/founder_test", "Founded VoiceThing (acq. 2024)", "2026-09-15")]},
                  UNKNOWN, NO_TIE, 0.6, "likely_real", "not blue verified; name and company match press coverage of the VoiceThing acquisition",
                  [{"kind": "other", "date": "2026-09-18", "url": _post("founder_test", "401"), "text": "Angel-invested in a voice-first AI earbuds startup"}],
                  "Voice-tech founder turned angel who says 'the future is human' and backs screen-free devices.",
                  ["whether he wears any device today"]),
    "adrian": _cls("founder_angel", None, "map has no location field", UNKNOWN, UNKNOWN, UNKNOWN, NO_TIE, 0.5,
                   "likely_real", "handle resolved by user search + bio match (Foundational Industries)", [],
                   "Warm angel/advisor in the operator's map (3 meetings, LP in ~100 funds); no device evidence yet.",
                   ["location", "whether he wears a wearable", "whether he uses voice tools"]),
    "cora": _cls("other", True, "location says San Francisco, CA",
                 {"value": True, "evidence": [_ev(_post("competitor_test", "701"), "Wearables are the wearable people actually keep on", "2026-09-15")]},
                 UNKNOWN, UNKNOWN,
                 {"value": True, "which": "Oura", "kind": "employee", "evidence": [_ev("https://x.com/competitor_test", "Product lead @ouraring", "2026-09-15")]},
                 0.5, "verified", "blue verified, employer in bio",
                 [{"kind": "joined_competitor", "date": "2026-09-15", "url": _post("competitor_test", "701"), "text": "Proud of what the Oura team shipped"}],
                 "Oura product lead: wears wearables daily but is a direct competitor employee.",
                 []),
    "parody": _cls("x_voice", None, "no location", UNKNOWN, UNKNOWN, UNKNOWN, NO_TIE, 0.05,
                   "suspicious", "bio says PARODY; account created 2025-11; 900 followers; impersonates wearablenerd_test", [],
                   "Parody of Rina Wearable; not a real person to send a wearable to.",
                   ["who runs the account"]),
    "elke": _cls("x_voice", False, "location says Berlin, Germany",
                 {"value": True, "evidence": [_ev(_post("eu_test", "601"), "Smart wearable number six arrived in Berlin today", "2026-09-12")]},
                 UNKNOWN, {"value": None, "evidence": []}, NO_TIE, 0.6, "likely_real", "consistent bio and posting history since 2016", [],
                 "Berlin-based smart-wearable collector comparing AI wearables; not US-based.",
                 ["whether she reviews publicly or just collects", "reach: nobody in the map knows her"]),
}

# The classifier's topic verdict (classify.py "topic_voice"), latest classification only.
TOPIC_VOICE: dict[str, dict] = {
    "rina": {"value": True, "topics": ["voice", "wearables", "personal_agents"],
             "evidence": [_ev(_post("wearablenerd_test", "102"), "Smart wearables plus a voice agent is the form factor", "2026-08-02")]},
    "frank": {"value": True, "topics": ["screen_free", "voice"],
              "evidence": [_ev(_post("founder_test", "401"), "Screens are the bug", "2026-09-18")]},
    "vera": {"value": None, "topics": [], "evidence": []},
    "cora": {"value": False, "topics": [], "evidence": [_ev(_post("competitor_test", "701"), "Proud of what the Oura team shipped", "2026-09-15")]},
}

# ---------------------------------------------------------------- scores per run: (p_accept, p_wear, p_love, impact, reach, confidence, flags)
SCORES_RUN1: dict[str, tuple] = {
    "rina": (0.70, 0.70, 0.60, 0.70, 0.55, 0.67, []),
    "vera": (0.60, 0.65, 0.60, 0.80, 0.60, 1.00, []),
    "adrian": (0.80, 0.45, 0.45, 0.75, 0.85, 0.33, []),
    "tom": (0.50, 0.65, 0.60, 0.90, 0.35, 0.67, []),
    "frank": (0.55, 0.50, 0.50, 0.50, 0.50, 0.33, []),
    "cora": (0.35, 0.80, 0.30, 0.50, 0.40, 0.67, []),
    "parody": (0.40, 0.30, 0.30, 0.05, 0.15, 0.33, []),
}
SCORES_RUN2: dict[str, tuple] = {
    "rina": (0.70, 0.85, 0.70, 0.70, 0.55, 1.00, []),
    "vera": (0.60, 0.65, 0.60, 0.80, 0.60, 1.00, []),
    "adrian": (0.80, 0.45, 0.45, 0.75, 0.85, 0.33, []),
    "frank": (0.70, 0.50, 0.65, 0.50, 0.50, 0.67, []),
    "tom": (0.50, 0.55, 0.60, 0.90, 0.35, 0.67, []),
    "elke": (0.60, 0.75, 0.50, 0.35, 0.15, 0.67, ["not_us"]),
    "cora": (0.20, 0.80, 0.30, 0.50, 0.40, 0.67, ["competitor:Oura"]),
    "parody": (0.30, 0.20, 0.20, 0.05, 0.15, 0.33, ["unverified_handle"]),
}

# Topic affinity, latest run only: key -> (value | None, source | None, reason, [(post_id, replies)] best posts first).
# Shape of score.py's reasons_json factor "affinity": {"value", "source", "reason", "top_posts": [{"url", "date", "text", ...}]}.
AFFINITY_RUN2: dict[str, tuple] = {
    "rina": (0.82, "computed+classifier", "3 of her posts since July argue for wearables plus a voice agent; they draw 2.4x her usual "
             "engagement and 90+ replies each.", [("102", 140), ("103", 90), ("101", 35)]),
    "frank": (0.71, "computed+classifier", "Screen-free evangelist: backs voice-first devices in public and his replies fill up "
              "with founders asking how.", [("401", 60)]),
    "vera": (0.66, "computed", "Fund IV thesis is personal AI agents and ambient devices; founders answer in the thread.",
             [("301", 200), ("302", 50)]),
    "tom": (0.58, "computed", "Writes about voice-first computing and a screen-free week; readers argue back in the replies.",
            [("202", 300), ("201", 110)]),
    "elke": (0.52, "computed", "Compares AI wearables and smart wearables all month; replies are mostly other collectors.",
             [("601", 25)]),
    "cora": (0.30, "computed+classifier", "Posts about Oura's roadmap, not about agents or voice: the classifier says no, capped at 0.30.",
             [("701", 80)]),
    "parody": (0.05, "computed", "No topical post of its own: one joke about wearables, 2 replies.", [("501", 2)]),
    "adrian": (None, None, "No posts of his are stored and the classifier had nothing to judge: unknown, not guessed.", []),
}

# Per-factor reasons (key -> field -> (reasons, evidence refs)); priors are stated when nothing is known.
REASONS: dict[str, dict[str, tuple[list[str], list[str]]]] = {
    "rina": {
        "p_accept": (["Bio links to a review site: accepts devices to review"], ["rina_accept"]),
        "p_wear": (["Unboxed the Oura Wearable 5 on 2026-09-20", "Wears Oura and Ultrahuman wearables (bio)"], ["rina_unbox", "rina_bio"]),
        "p_love": (["Argues wearables + voice agent is the form factor (1.2k likes)", "Reviewed the Limitless pendant publicly"], ["rina_voice", "rina_limitless"]),
        "impact": (["48k followers, high engagement on wearable posts"], ["rina_impact"]),
        "reach": (["Best path: via Marwan Refaat (replied to each other on X)"], []),
    },
    "tom": {
        "p_accept": (["Prior for media (0.5): no explicit signal about accepting units"], []),
        "p_wear": (["Kept the Oura wearable through a screen-free week (2026-06-01, fading with 90-day decay)"], ["tom_ring"]),
        "p_love": (["Reviews devices publicly for GadgetDaily"], ["tom_review"]),
        "impact": (["Senior editor at GadgetDaily, 120k followers"], ["tom_bio"]),
        "reach": (["Best path: via Tammy Rogers (press, weak)"], []),
    },
    "vera": {
        "p_accept": (["'Founders building ambient devices, DMs open'"], ["vera_accept"]),
        "p_wear": (["Wears a smart wearable (post 2026-05-15, bio)"], ["vera_ring", "vera_bio"]),
        "p_love": (["'Talks to her AI' (bio); fund thesis is personal AI agents"], ["vera_bio"]),
        "impact": (["Closed Fund IV for consumer hardware and personal AI agents"], ["vera_fund"]),
        "reach": (["Best path: via Adrian Aoun (X conversation) and mutual follow with the operator"], []),
    },
    "frank": {
        "p_accept": (["Angel-invested in a voice-first AI earbuds startup (2026-09-18)", "Screen-free evangelist (bio)"], ["frank_angel", "frank_bio"]),
        "p_wear": (["Prior for founder_angel (0.5): no device evidence yet"], []),
        "p_love": (["'The future is human, not artificial' (2026-09-18)"], ["frank_love"]),
        "impact": (["15k followers; angel in 30 companies"], []),
        "reach": (["Best path: via Max Marchione (X conversation)"], []),
    },
    "adrian": {
        "p_accept": (["Warm map relationship: 3 meetings, 84 interactions, last contact 12 days ago"], ["adrian_map"]),
        "p_wear": (["Prior (0.45): no wearable evidence"], []),
        "p_love": (["Prior (0.45): no public device opinions found"], []),
        "impact": (["LP in ~100 funds; co-founder Foundational Industries"], ["adrian_impact"]),
        "reach": (["Direct: the operator knows him"], ["adrian_reach"]),
    },
    "cora": {
        "p_accept": (["Oura employee: unlikely to accept a competitor's wearable"], ["cora_bio"]),
        "p_wear": (["Wears wearables daily (Oura product lead)"], ["cora_wear"]),
        "p_love": (["Prior lowered: competitor tie"], ["cora_post"]),
        "impact": (["18k followers, strong engagement inside the wearable community"], []),
        "reach": (["Mutual follow with the operator on X"], []),
    },
    "parody": {
        "p_accept": (["Prior (0.3): parody account"], ["parody_bio"]),
        "p_wear": (["Prior (0.2): no real person behind the handle"], []),
        "p_love": (["Prior (0.2)"], []),
        "impact": (["900 followers, 12 likes"], []),
        "reach": (["DMs open only"], []),
    },
    "elke": {
        "p_accept": (["Collects smart wearables (sixth wearable, 2026-09-12)"], ["elke_ring"]),
        "p_wear": (["Smart wearable collector (bio and post)"], ["elke_ring", "elke_bio"]),
        "p_love": (["Prior (0.5): compares AI wearables publicly, no review found yet"], []),
        "impact": (["22k followers; not US-based"], []),
        "reach": (["No path: DMs open only"], []),
    },
}

CHANGE_REASONS: dict[str, str] = {
    "rina": "Posted an Oura Wearable 5 unboxing on 2026-09-20 ('voice notes finally usable'): wear and love evidence added",
    "frank": "Angel-invested in a voice-first AI earbuds startup on 2026-09-18 ('the future is human')",
    "tom": "No wearable post since 2026-06-01: the screen-free-week evidence is fading (90-day decay)",
    "cora": "Bio and 2026-09-15 post confirm product lead at Oura: competitor flag set",
    "parody": "Handle classified suspicious: bio says PARODY, account created 2025-11, 900 followers",
    "elke": 'New candidate from query "smart wearable" (AI OR voice ...): sixth smart wearable arrived in Berlin on 2026-09-12',
}

# ---------------------------------------------------------------- paths (key -> list in rank order), contract shape
PATHS: dict[str, list[dict]] = {
    "rina": [
        dict(how="via", strength=0.55, via_person_id=254, via_name="Marwan Refaat", you_rel="met · 16 intros made · warm · last 103d ago",
             target_rel="replied to each other on X (3 posts)", evidence="x_posts: 3 replies between marwanrefaat and wearablenerd_test in 2026"),
        dict(how="x_direct", strength=0.45, via_person_id=None, via_name=None, you_rel="follow each other", target_rel="follow each other on X",
             evidence="x_follows: 1->2 and 2->1"),
        dict(how="open_dm", strength=0.15, via_person_id=None, via_name=None, you_rel=None, target_rel="DMs open", evidence="x_users.can_dm = 1"),
    ],
    "tom": [
        dict(how="via", strength=0.35, via_person_id=272, via_name="Tammy Rogers", you_rel="met · 1 meeting · cool · last 431d ago",
             target_rel="replied to each other on X (2 posts)", evidence="x_posts: 2 replies between tammyrogers and techwriter_test"),
        dict(how="none", strength=0.05, via_person_id=None, via_name=None, you_rel=None, target_rel=None, evidence="can_dm = 0, no mutual follow"),
    ],
    "vera": [
        dict(how="via", strength=0.60, via_person_id=51, via_name="Adrian Aoun", you_rel="met · 3 meetings · warm · last 12d ago",
             target_rel="replied to each other on X (4 posts)", evidence="x_posts: 4 replies between adrianaoun_test and vc_test"),
        dict(how="x_direct", strength=0.50, via_person_id=None, via_name=None, you_rel="follow each other", target_rel="follow each other on X",
             evidence="x_follows: 1->4 and 4->1"),
        dict(how="open_dm", strength=0.15, via_person_id=None, via_name=None, you_rel=None, target_rel="DMs open", evidence="x_users.can_dm = 1"),
    ],
    "frank": [
        dict(how="via", strength=0.50, via_person_id=83, via_name="Max Marchione", you_rel="met · 2 meetings · 67 intros made · last 153d ago",
             target_rel="replied to each other on X (2 posts)", evidence="x_posts: 2 replies between maxmarchione and founder_test"),
        dict(how="open_dm", strength=0.15, via_person_id=None, via_name=None, you_rel=None, target_rel="DMs open", evidence="x_users.can_dm = 1"),
    ],
    "adrian": [
        dict(how="direct", strength=0.85, via_person_id=None, via_name=None, you_rel="met · 3 meetings · 84 touches · last 12d ago",
             target_rel=None, evidence="map_people 51: warmth 208.5 (top quartile), channels gmail, meeting, imessage"),
    ],
    "cora": [
        dict(how="x_direct", strength=0.40, via_person_id=None, via_name=None, you_rel="follow each other", target_rel="follow each other on X",
             evidence="x_follows: 1->8 and 8->1"),
        dict(how="none", strength=0.05, via_person_id=None, via_name=None, you_rel=None, target_rel=None, evidence="can_dm = 0"),
    ],
    "parody": [
        dict(how="open_dm", strength=0.15, via_person_id=None, via_name=None, you_rel=None, target_rel="DMs open", evidence="x_users.can_dm = 1"),
    ],
    "elke": [
        dict(how="open_dm", strength=0.15, via_person_id=None, via_name=None, you_rel=None, target_rel="DMs open", evidence="x_users.can_dm = 1"),
        dict(how="none", strength=0.05, via_person_id=None, via_name=None, you_rel=None, target_rel=None, evidence="nobody in the map reaches her"),
    ],
}

MAP_X_HANDLES = [
    dict(person_id=254, handle="marwanrefaat", x_id=None, confidence=0.9, method="tag", resolved_at=T1),
    dict(person_id=83, handle="maxmarchione", x_id=None, confidence=0.9, method="tag", resolved_at=T1),
    dict(person_id=51, handle="adrianaoun_test", x_id=None, confidence=0.85, method="user_search+llm", resolved_at=T1),
    dict(person_id=272, handle="tammyrogers", x_id=None, confidence=0.7, method="user_search+llm", resolved_at=T1),
]

STATUS_ROWS = [
    dict(key="rina", status="approved", owner="the operator", note="Send with the launch batch; ask Marwan for the intro.", updated_at="2026-09-28T16:20:00Z"),
    dict(key="adrian", status="intro_asked", owner="the operator", note="Mentioned at Thursday's meeting; he is in.", updated_at="2026-09-26T10:00:00Z"),
]

COSTS = {
    1: [("x_api", "users/search", 40, 0.010, "resolve handles for 40 map people"),
        ("x_api", "tweets/search/recent", 300, 0.005, "19 discovery queries, 300 posts"),
        ("x_api", "users/:id/following", 500, 0.010, "seed crawl: the operator's following"),
        ("anthropic", "messages", 7, 0.012, "classify 7 dossiers (~4k in / 1k out tokens each, claude-sonnet-5-5)")],
    2: [("x_api", "users/search", 12, 0.010, "resolve 12 new map handles"),
        ("x_api", "tweets/search/recent", 140, 0.005, "19 discovery queries, 140 new posts"),
        ("anthropic", "messages", 8, 0.012, "classify 8 dossiers (~4k in / 1k out tokens each, claude-sonnet-5-5)")],
}


# ---------------------------------------------------------------- helpers

def total_score(pa: float, pw: float, pl: float, impact: float, reach: float) -> float:
    return round(100 * pa * pw * pl * (0.3 + 0.7 * impact) * (0.25 + 0.75 * reach), 2)


def load_map_fixtures(conn) -> None:
    """Same loader pattern as tests/conftest.py (copied on purpose: scripts must not import test modules)."""
    fx = config.FIXTURES_DIR
    now = db.utcnow()
    for p in json.loads((fx / "map_people.json").read_text()):
        db.upsert(conn, "map_people", {
            "id": p["id"], "group_name": p["group_name"], "name": p["name"], "company": p.get("company"), "role": p.get("role"),
            "sector": p.get("sector"), "tags": p.get("tags"), "warmth": p.get("warmth"), "intros_made": p.get("intros_made"),
            "intros_got": p.get("intros_got"), "interactions": p.get("interactions"), "sent": p.get("sent"), "received": p.get("received"),
            "meetings": p.get("meetings"), "channels": p.get("channels"), "first_contact": p.get("first_contact"),
            "last_contact": p.get("last_contact"), "imported_at": now}, keys=("id",))
    for e in json.loads((fx / "map_edges.json").read_text()):
        db.upsert(conn, "map_edges", {"from_id": e["from_id"], "to_id": e["to_id"], "source": e["source"], "group_name": e["group_name"]},
                  keys=("from_id", "to_id", "source", "group_name"))
    for f in json.loads((fx / "map_competitor_funds.json").read_text()):
        db.upsert(conn, "map_competitor_funds", {"fund": f["fund"], "competitor": f["competitor"], "round": f.get("round")}, keys=("fund", "competitor"))
    for pid, li in json.loads((fx / "map_linkedin.json").read_text()).items():
        db.upsert(conn, "map_linkedin", {"person_id": int(pid), "company": li.get("company"), "position": li.get("position"),
                                         "connected_on": li.get("connected_on"), "profile_url": li.get("profile_url")}, keys=("person_id",))
    for h in MAP_X_HANDLES:
        db.upsert(conn, "map_x_handles", h, keys=("person_id",))
    conn.commit()


def load_x_fixtures(conn) -> None:
    src = FixtureSource()
    users = list(src._users.values())
    persist_users(conn, users, "fixture")
    persist_posts(conn, src._posts, "fixture")
    for u in users:
        persist_follows(conn, u.x_id, src.get_following(u.x_id), "following", "fixture")


def _warmth_bucket(strength: float | None) -> int:
    s = strength or 0
    return 3 if s >= 0.6 else 2 if s >= 0.4 else 1 if s >= 0.2 else 0


def seed(path: str | Path | None = None, quiet: bool = False) -> dict:
    """Rebuild the demo DB at `path` (default data/demo.db). Returns row counts per table."""
    db_file = Path(path) if path else DEFAULT_PATH
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(db_file) + suffix)
        if stale.exists():
            stale.unlink()
    conn = db.connect(db_file)
    load_map_fixtures(conn)
    load_x_fixtures(conn)

    # runs
    run_ids = {}
    for n, r in ((1, RUN1), (2, RUN2)):
        cur = conn.execute("INSERT INTO runs (started_at, finished_at, stage, notes, cost_usd) VALUES (?,?,?,?,0)",
                           (r["started_at"], r["finished_at"], "all", r["notes"]))
        run_ids[n] = int(cur.lastrowid)

    # candidates
    cids: dict[str, int] = {}
    for key, row in CANDIDATES.items():
        cur = conn.execute(
            "INSERT INTO candidates (x_id, handle, name, category, us_based, map_person_id, linkedin_url, handle_verified, "
            "handle_verification_method, competitor_flag, competitor_detail, discovered_via, first_seen, last_seen, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["x_id"], row["handle"], row["name"], row["category"], row["us_based"], row["map_person_id"],
             "https://www.linkedin.com/in/adrianaoun" if key == "adrian" else None,
             row["handle_verified"], row["handle_verification_method"], row["competitor_flag"], row["competitor_detail"],
             row["discovered_via"], row["first_seen"], row["last_seen"], row["notes"]))
        cids[key] = int(cur.lastrowid)

    # evidence (+ ref -> evidence_id for reasons_json)
    ev_ids: dict[str, int] = {}
    for key, rows in EVIDENCE.items():
        for e in rows:
            cur = conn.execute(
                "INSERT INTO evidence (candidate_id, kind, supports, polarity, weight, url, date, text, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cids[key], e["kind"], e["supports"], e["polarity"], e["weight"], e["url"], e["date"], e["text"], "fixture", e["fetched_at"]))
            ev_ids[e["ref"]] = int(cur.lastrowid)

    # events
    for key, rows in EVENTS.items():
        for ev in rows:
            conn.execute("INSERT INTO events (candidate_id, kind, date, url, text, source, fetched_at) VALUES (?,?,?,?,?,?,?)",
                         (cids[key], ev["kind"], ev["date"], ev["url"], ev["text"], ev["source"], ev["fetched_at"]))

    # classifications: run 1 for the candidates that existed then, run 2 for all
    for n, keys in ((1, [k for k in CANDIDATES if k not in ONLY_IN_RUN2]), (2, list(CANDIDATES))):
        for key in keys:
            j = dict(CLASSIFICATIONS[key])
            if n == 1 and key == "parody":
                j = {**j, "handle_authenticity": {"value": "unknown", "reasons": "not checked yet"}}
            if n == 1 and key == "cora":
                j = {**j, "competitor_tie": {"value": None, "which": None, "kind": None, "evidence": []}}
            if n == 2 and key in TOPIC_VOICE:
                j = {**j, "topic_voice": TOPIC_VOICE[key]}
            conn.execute("INSERT INTO classifications (candidate_id, run_id, json, model, created_at) VALUES (?,?,?,?,?)",
                         (cids[key], run_ids[n], db.jdumps(j), "fixture", T1 if n == 1 else T2))

    # scores per run (+ ranks), reasons_json in the contract shape
    posts_by_id = {pid: r for pid, r in ((str(r["post_id"]), r) for r in db.rows(conn, "SELECT post_id, handle, created_at, text FROM x_posts"))}
    ranked: dict[int, list[tuple[str, dict]]] = {}
    for n, table in ((1, SCORES_RUN1), (2, SCORES_RUN2)):
        rows = []
        for key, (pa, pw, pl, impact, reach, conf, flags) in table.items():
            rows.append((key, {"p_accept": pa, "p_wear": pw, "p_love": pl, "impact": impact, "reach": reach,
                               "confidence": conf, "flags": flags, "total": total_score(pa, pw, pl, impact, reach)}))
        rows.sort(key=lambda kv: -kv[1]["total"])
        for rank, (key, s) in enumerate(rows, start=1):
            s["rank"] = rank
            factors = {}
            all_ids: list[int] = []
            overall: list[str] = []
            for f in ("p_accept", "p_wear", "p_love", "impact", "reach"):
                reasons, refs = REASONS[key].get(f, ([], []))
                ids = [ev_ids[r] for r in refs if r in ev_ids]
                factors[f] = {"value": s[f], "reasons": reasons, "evidence_ids": ids}
                if f == "p_wear":   # score.py stores the prior and whether evidence backs it: the brief's bar reads both
                    factors[f].update(prior=PRIORS[CANDIDATES[key]["category"]]["wear"], backed=bool(ids))
                all_ids += ids
                overall += reasons[:1]
            affinity = None
            if n == 2 and key in AFFINITY_RUN2:   # run 1 predates topic affinity: no column value, no factor
                affinity, source, reason, best = AFFINITY_RUN2[key]
                top_posts = []
                for pid, replies in best:
                    post = posts_by_id.get(pid)
                    if post:
                        top_posts.append({"url": _post(post["handle"], pid), "date": (post["created_at"] or "")[:10],
                                          "text": post["text"], "replies": replies})
                factors["affinity"] = {"value": affinity, "source": source, "reason": reason, "top_posts": top_posts}
            reasons_json = {"factors": factors, "reasons": overall[:4], "evidence_ids": sorted(set(all_ids)),
                            "formula": "100 * p_accept * p_wear * p_love * (0.3 + 0.7*impact) * (0.25 + 0.75*reach)"}
            conn.execute(
                "INSERT INTO scores (run_id, candidate_id, p_accept, p_wear, p_love, affinity, impact, reach, confidence, total, rank, "
                "flags, reasons_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_ids[n], cids[key], s["p_accept"], s["p_wear"], s["p_love"], affinity, s["impact"], s["reach"], s["confidence"],
                 s["total"], rank, db.jdumps(s["flags"]), db.jdumps(reasons_json)))
        ranked[n] = rows

    # changes for run 2 vs run 1: one row per changed field, plus new_candidate rows
    prev = {key: s for key, s in ranked[1]}
    for key, s in ranked[2]:
        base = CHANGE_REASONS.get(key, "no new evidence; moved because others changed")
        if key not in prev:
            conn.execute("INSERT INTO changes (run_id, candidate_id, field, old_value, new_value, reason) VALUES (?,?,?,?,?,?)",
                         (run_ids[2], cids[key], "new_candidate", None, str(s["total"]), base))
            continue
        o = prev[key]
        for f in ("total", "rank", "p_accept", "p_wear", "p_love", "impact", "reach", "confidence"):
            if abs(float(o[f]) - float(s[f])) > 1e-9:
                reason = base if f != "rank" else f"Rank {o[f]} -> {s[f]}: {base}"
                conn.execute("INSERT INTO changes (run_id, candidate_id, field, old_value, new_value, reason) VALUES (?,?,?,?,?,?)",
                             (run_ids[2], cids[key], f, str(o[f]), str(s[f]), reason))
        if o["flags"] != s["flags"]:
            conn.execute("INSERT INTO changes (run_id, candidate_id, field, old_value, new_value, reason) VALUES (?,?,?,?,?,?)",
                         (run_ids[2], cids[key], "flags", db.jdumps(o["flags"]), db.jdumps(s["flags"]), base))

    # paths for both runs (same relationships), json in the map's wishlist shape
    for n in (1, 2):
        for key, plist in PATHS.items():
            if n == 1 and key in ONLY_IN_RUN2:
                continue
            for rank, pt in enumerate(plist, start=1):
                obj = {"how": pt["how"], "you": pt["you_rel"], "via": pt["via_name"], "warmth": _warmth_bucket(pt["strength"]),
                       "target": pt["target_rel"], "strength": pt["strength"]}
                conn.execute(
                    "INSERT INTO paths (run_id, candidate_id, rank, how, strength, via_person_id, via_name, you_rel, target_rel, evidence, json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run_ids[n], cids[key], rank, pt["how"], pt["strength"], pt["via_person_id"], pt["via_name"], pt["you_rel"],
                     pt["target_rel"], pt["evidence"], db.jdumps(obj)))

    # status (the dashboard's table)
    for s in STATUS_ROWS:
        conn.execute("INSERT INTO status (candidate_id, status, owner, note, updated_at) VALUES (?,?,?,?,?)",
                     (cids[s["key"]], s["status"], s["owner"], s["note"], s["updated_at"]))

    # cost ledger + runs.cost_usd (what db.finish_run would have written)
    for n, items in COSTS.items():
        ts = RUN1["started_at"] if n == 1 else RUN2["started_at"]
        for provider, endpoint, units, unit_cost, note in items:
            conn.execute("INSERT INTO costs (ts, run_id, provider, endpoint, units, unit_cost_usd, cost_usd, note) VALUES (?,?,?,?,?,?,?,?)",
                         (ts, run_ids[n], provider, endpoint, units, unit_cost, round(units * unit_cost, 6), note))
        conn.execute("UPDATE runs SET cost_usd=(SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE run_id=?) WHERE run_id=?",
                     (run_ids[n], run_ids[n]))
    conn.commit()

    counts = {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in
              ("runs", "map_people", "map_edges", "map_competitor_funds", "map_linkedin", "map_x_handles", "x_users", "x_posts",
               "x_follows", "candidates", "evidence", "events", "classifications", "scores", "changes", "paths", "status", "costs")}
    conn.close()
    if not quiet:
        print(f"seeded {db_file}")
        for t, n in counts.items():
            print(f"  {t:22s} {n}")
    return counts


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else None)
