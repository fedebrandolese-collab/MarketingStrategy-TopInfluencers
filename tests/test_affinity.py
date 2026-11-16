"""Topic affinity (src/wearable_wishlist/affinity.py): topical detection, the three components, None / 0.0, ordering."""
import datetime as dt
import json

import pytest

from wearable_wishlist import affinity, config, db
from wearable_wishlist.affinity import engagement, post_topics, topic_affinity

NOW = "2026-09-29T12:00:00Z"
_NOW_DT = dt.datetime(2026, 9, 29, 12, tzinfo=dt.timezone.utc)
CONTRACT_KEYS = {"score", "topical_posts", "agent_posts", "weighted_posts", "topic_eng_avg", "overall_eng_avg", "lift",
                 "replies_per_topic_post", "last_topical", "top_posts", "components", "reason"}


def ago(days: float) -> str:
    return (_NOW_DT - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


_pid = iter(range(10_000, 99_999))


def add_candidate(conn, cid: int, x_id: str | None = None, handle: str | None = None) -> int:
    conn.execute("INSERT INTO candidates (candidate_id, x_id, handle, name) VALUES (?,?,?,?)",
                 (cid, x_id, handle, handle))
    return cid


def add_post(conn, x_id: str, text: str, days: float, likes=0, reposts=0, replies=0, quotes=0, query=None) -> str:
    pid = str(next(_pid))
    db.upsert(conn, "x_posts", {"post_id": pid, "x_id": x_id, "handle": f"h{x_id}", "created_at": ago(days), "text": text,
                                "likes": likes, "reposts": reposts, "replies": replies, "quotes": quotes,
                                "url": f"https://x.com/h{x_id}/status/{pid}", "query": query}, keys=("post_id",))
    return pid


AGENT = "My personal agent booked the dentist and cleared my inbox while I walked the dog."
WEAR = "Thirty days with a smart wearable: the best wearable is the one you forget you are wearing."
OTHER = "Lovely dinner in Brooklyn with old friends tonight."


# ---------------------------------------------------------------------------------------------------------------------
# Topical detection
# ---------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("My personal agent booked the dentist while I was in a meeting.", {"personal_agents"}),
    ("Agentic workflows are the real story of 2026.", {"personal_agents"}),
    ("OpenClaw filed my expense report overnight.", {"personal_agents"}),
    ("Your agent makes phone calls now. The hold music era is ending.", {"personal_agents"}),
    ("An AI assistant that remembers what I said last week is all I want.", {"personal_agents"}),
    ("Voice-first computing is closer than the skeptics think.", {"voice"}),
    ("I switched to dictation for every email. Speech-to-text is finally good.", {"voice"}),
    ("Unboxing my third smart wearable this year.", {"wearables"}),
    ("The Limitless pendant keeps falling off my shirt.", {"wearables"}),
    ("A month with a dumbphone: my screen time dropped by 70%.", {"screen_free"}),
    ("Smart wearables plus a voice agent is the form factor.", {"personal_agents", "voice", "wearables"}),
])
def test_topical_posts(text, expected):
    assert post_topics(text) == expected


@pytest.mark.parametrize("text", [
    "I asked ChatGPT for a recipe and dinner was great.",           # a general brand is not the topic
    "My Oura sleep score is terrible today.",
    "Siri is useless, as usual.",
    "Claude wrote the first draft of my essay.",
    "My agent called about the new role, fingers crossed.",         # a talent agent, no AI anywhere
    "ICE agents used an AI facial recognition app during the stop.",  # people, even next to an AI word
    "Grandma's pendant necklace is the most beautiful thing I own.",
    "RT-free zone: great game last night https://t.co/voice-first",  # words inside a URL do not count
    "",
    None,
])
def test_passing_mentions_are_not_topical(text):
    assert post_topics(text) == set()


def test_leading_reply_handles_are_not_content():
    assert post_topics("@openclaw thanks!") == set()
    assert post_topics("@the operator OpenClaw booked my flights while I slept") == {"personal_agents"}


def test_replies_count_double_in_engagement():
    assert engagement(10, 1, 5, 2) == 10 + 1 + 2 * 5 + 2
    assert engagement(None, None, None, None) == 0


# ---------------------------------------------------------------------------------------------------------------------
# None / 0.0 / contract shape
# ---------------------------------------------------------------------------------------------------------------------

def test_none_without_posts_and_zero_without_topical_posts(conn):
    add_candidate(conn, 1, "100", "noposts")
    add_candidate(conn, 2, None, "mapperson")                         # no X account
    add_candidate(conn, 3, "300", "chatter")
    add_post(conn, "300", "I asked ChatGPT for a recipe and dinner was great.", 3, likes=50)
    add_post(conn, "300", OTHER, 10, likes=20)
    add_candidate(conn, 4, "400", "retweeter")
    add_post(conn, "400", "RT @someone: My personal agent booked the dentist.", 2, likes=0, reposts=40)
    conn.commit()
    res = topic_affinity(conn, now=NOW)
    assert set(res) == {1, 2, 3, 4}
    assert res[1]["score"] is None and res[1]["reason"] == "no posts stored"
    assert res[2]["score"] is None and res[2]["reason"] == "no X account known"
    assert res[4]["score"] is None                                      # retweets are not the person's words
    r = res[3]
    assert r["score"] == 0.0 and r["topical_posts"] == 0 and r["top_posts"] == [] and r["last_topical"] is None
    assert r["overall_eng_avg"] == pytest.approx(35.0)
    assert "none about personal agents" in r["reason"]
    for v in res.values():
        assert CONTRACT_KEYS <= set(v)
        assert set(v["components"]) == {"volume", "community", "recency"}


def test_topical_posts_older_than_the_window_score_zero_but_are_reported(conn):
    add_candidate(conn, 1, "100", "old")
    add_post(conn, "100", AGENT, 120, likes=300)
    add_post(conn, "100", OTHER, 5, likes=10)
    conn.commit()
    r = topic_affinity(conn, now=NOW)[1]
    assert r["score"] == 0.0 and r["topical_posts"] == 0
    assert r["last_topical"] == ago(120)[:10]
    assert r["last_topical"] in r["reason"]


# ---------------------------------------------------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------------------------------------------------

def test_agent_posts_outweigh_other_themes(conn):
    add_candidate(conn, 1, "100", "agents")
    add_candidate(conn, 2, "200", "wearables")
    for d in (2, 9, 20):
        add_post(conn, "100", AGENT, d, likes=80, replies=10)
        add_post(conn, "200", WEAR, d, likes=80, replies=10)
    conn.commit()
    res = topic_affinity(conn, now=NOW)
    a, w = res[1], res[2]
    assert a["agent_posts"] == 3 and w["agent_posts"] == 0
    assert a["weighted_posts"] == pytest.approx(3.0) and w["weighted_posts"] == pytest.approx(2.1)
    assert a["components"]["volume"] > w["components"]["volume"]
    assert a["score"] > w["score"] > 0
    assert "personal agents" in a["reason"] and "AI wearables" in w["reason"]


def test_community_lift_rewards_an_audience_that_cares_about_the_topic(conn):
    add_candidate(conn, 1, "100", "topic_draws")      # topic posts beat their usual
    add_candidate(conn, 2, "200", "topic_ignored")    # the same topic posts, but their other posts do far better
    for d in (3, 10, 17):
        add_post(conn, "100", AGENT, d, likes=100, replies=20)
        add_post(conn, "200", AGENT, d, likes=100, replies=20)
    for d in (4, 11, 18):
        add_post(conn, "100", OTHER, d, likes=10)
        add_post(conn, "200", OTHER, d, likes=2000, replies=100)
    conn.commit()
    res = topic_affinity(conn, now=NOW)
    up, down = res[1], res[2]
    assert up["topic_eng_avg"] == down["topic_eng_avg"] == pytest.approx(140.0)
    assert up["replies_per_topic_post"] == pytest.approx(20.0)
    assert up["lift"] > 1 > down["lift"]
    assert up["components"]["community"] > down["components"]["community"]
    assert up["score"] > down["score"]
    assert "x their usual" in up["reason"]


def test_lift_needs_a_baseline(conn):
    add_candidate(conn, 1, "100", "only_topic")
    for d in (1, 2, 3):
        add_post(conn, "100", AGENT, d, likes=100)
    conn.commit()
    r = topic_affinity(conn, now=NOW)[1]
    assert r["lift"] is None and "their usual" not in r["reason"]
    assert r["components"]["community"] == pytest.approx(affinity.engagement_scale(100), abs=1e-3)


def test_recency_fades_from_30_days_to_the_window_edge(conn):
    for cid, days in ((1, 10), (2, 60), (3, 85), (4, 95)):
        x = str(cid * 100)
        add_candidate(conn, cid, x, f"r{cid}")
        for extra in (0, 0.2, 0.4):                    # three agent posts: full support
            add_post(conn, x, AGENT, days + extra, likes=50)
    conn.commit()
    res = topic_affinity(conn, now=NOW)
    rec = {cid: res[cid]["components"]["recency"] for cid in res}
    assert rec[1] == pytest.approx(1.0)
    assert rec[2] == pytest.approx(0.5, abs=0.01)
    assert 0 < rec[3] < 0.1
    assert res[4]["score"] == 0.0                      # everything outside the 90-day window
    assert res[1]["score"] > res[2]["score"] > res[3]["score"] > res[4]["score"]
    assert res[1]["reason"].endswith("latest 10 days ago")


def test_volume_is_log_scaled_and_saturates():
    assert affinity.volume_component(0) == 0
    assert affinity.volume_component(1) == pytest.approx(0.289, abs=1e-3)
    assert affinity.volume_component(10) == pytest.approx(1.0)
    assert affinity.volume_component(40) == 1.0


# ---------------------------------------------------------------------------------------------------------------------
# Ordering, reason, filters
# ---------------------------------------------------------------------------------------------------------------------

def test_an_active_agent_voice_with_replies_beats_a_one_off_mention(conn):
    add_candidate(conn, 1, "100", "voice")
    for d in (1, 8, 15, 25, 40, 55):
        add_post(conn, "100", AGENT, d, likes=200, reposts=10, replies=40)
    add_post(conn, "100", OTHER, 5, likes=150, replies=5)
    add_post(conn, "100", OTHER, 12, likes=150, replies=5)
    add_candidate(conn, 2, "200", "oneoff")           # one viral agent post, nothing else about it
    add_post(conn, "200", AGENT, 2, likes=5000, reposts=300, replies=100)
    for d in (3, 6, 9):
        add_post(conn, "200", OTHER, d, likes=400)
    conn.commit()
    res = topic_affinity(conn, now=NOW)
    assert res[1]["score"] > 0.7
    assert res[2]["score"] < 0.35                      # stays under the 0.4 a positive topic_voice verdict guarantees
    assert res[1]["score"] > res[2]["score"]


def test_reason_top_posts_and_filters(conn):
    add_candidate(conn, 1, "100", "a")
    add_candidate(conn, 2, "200", "b")
    add_post(conn, "100", AGENT, 3, likes=100, replies=10)
    add_post(conn, "100", "Voice-first beats typing for anything longer than a tweet.", 5, likes=300, replies=30)
    add_post(conn, "100", WEAR, 7, likes=10)
    add_post(conn, "100", "Screen-free Sundays are the best habit I picked up this year.", 9, likes=50)
    add_post(conn, "100", AGENT, -2, likes=9999)       # after `now`: ignored
    add_post(conn, "200", AGENT, 1, likes=5)
    conn.commit()
    res = topic_affinity(conn, candidate_ids=[1], now=NOW)
    assert set(res) == {1}
    r = res[1]
    assert r["topical_posts"] == 4 and r["agent_posts"] == 1
    assert r["topics"] == {"personal_agents": 1, "voice": 1, "wearables": 1, "screen_free": 1}
    assert [p["engagement"] for p in r["top_posts"]] == [360, 120, 50]
    assert set(r["top_posts"][0]) == {"url", "date", "text", "engagement", "replies"}
    assert r["top_posts"][0]["date"] == ago(5)[:10] and r["top_posts"][0]["replies"] == 30
    assert r["last_topical"] == ago(3)[:10]
    assert r["reason"] == ("4 posts about personal agents, voice, AI wearables and screen-free life in 90 days, "
                           "~135 engagements each, latest 3 days ago")
    assert 0 < r["score"] <= 1 and r["score"] == round(r["score"], 3)


def test_large_candidate_lists_are_filtered_in_python(conn):
    for cid in (1, 2, 3):
        add_candidate(conn, cid, str(cid * 100), f"c{cid}")
        add_post(conn, str(cid * 100), AGENT, cid, likes=10 * cid)
    conn.commit()
    small = topic_affinity(conn, candidate_ids=[1, 3], now=NOW)
    big = topic_affinity(conn, candidate_ids=[1, 3] + list(range(1000, 1700)), now=NOW)
    assert set(small) == set(big) == {1, 3}
    assert small == big


def test_window_parameter(conn):
    add_candidate(conn, 1, "100", "w")
    for d in (5, 6, 7):
        add_post(conn, "100", AGENT, d, likes=30)
    add_post(conn, "100", AGENT, 40, likes=30)
    conn.commit()
    r30 = topic_affinity(conn, now=NOW, window_days=30)[1]
    r90 = topic_affinity(conn, now=NOW, window_days=90)[1]
    assert r30["topical_posts"] == 3 and r90["topical_posts"] == 4
    assert "in 30 days" in r30["reason"]


def test_fixture_posts(conn):
    posts = json.loads((config.FIXTURES_DIR / "x_posts.json").read_text())
    handles = {p["x_id"]: p["handle"] for p in posts}
    for i, (x_id, h) in enumerate(sorted(handles.items()), 1):
        add_candidate(conn, i, x_id, h)
    for p in posts:
        db.upsert(conn, "x_posts", {k: p[k] for k in ("post_id", "x_id", "handle", "created_at", "text", "likes", "reposts",
                                                        "replies", "quotes", "views")}, keys=("post_id",))
    conn.commit()
    by_handle = {handles[x]: r for x, r in zip(sorted(handles), [topic_affinity(conn, now=NOW)[i]
                                                                 for i in range(1, len(handles) + 1)])}
    rn = by_handle["wearablenerd_test"]
    assert rn["topical_posts"] == 3 and rn["agent_posts"] == 1         # "Smart wearables plus a voice agent"
    assert by_handle["parody_test"]["score"] == 0.0                     # throwing wearables into volcanoes is not the topic
    assert rn["score"] > by_handle["eu_test"]["score"] > 0              # three posts beat one
