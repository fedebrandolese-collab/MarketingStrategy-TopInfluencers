"""Score weights (src/ring_wishlist/weights.py): the weighted total, re-ranking, filters, presets. Pure, offline."""
import itertools

import pandas as pd
import pytest

from ring_wishlist import export, weights as W


def formula(pa, pw, pl, imp):
    """score.py's total with the built-in weights."""
    return round(min(100.0, max(0.0, 100.0 * pa * pw * pl * (0.3 + 0.7 * imp))), 2)


def person(cid, pa=0.6, pw=0.6, pl=0.6, imp=0.5, reach=None, aff=None, cat="x_voice", flags=(), status="candidate", conf=1.0,
           wear=True, contactable=True):
    return {"candidate_id": cid, "name": f"P{cid}", "handle": f"p{cid}", "handle_verified": "verified", "category": cat,
            "p_accept": pa, "p_wear": pw, "p_love": pl, "impact": imp, "reach": reach, "affinity": aff,
            "confidence": conf, "flags": list(flags), "status": status, "why": "", "best_path": "cold: no warm intro",
            "wear_evidence": wear, "contactable": contactable}


def test_defaults_equal_the_formula_bit_for_bit():
    grid = [0.0, 0.05, 0.123, 0.333, 0.5, 0.777, 0.9, 0.999, 1.0]
    for pa, pw, pl, imp in itertools.product(grid[::2], grid[1::2], grid[::3], grid):
        f = {"p_accept": pa, "p_wear": pw, "p_love": pl, "impact": imp, "reach": 0.8, "affinity": 0.9, "category": "vc"}
        assert W.weighted_total(f) == formula(pa, pw, pl, imp)
        assert W.weighted_total(f, W.DEFAULT_WEIGHTS) == formula(pa, pw, pl, imp)
    assert W.is_default({}) and W.is_default(None) and not W.is_default({"impact": 2})
    assert W.DEFAULT_WEIGHTS["connection"] == 0 and W.DEFAULT_WEIGHTS["affinity"] == 0


def test_weight_zero_removes_a_factor_and_two_doubles_it():
    f = {"p_accept": 0.5, "p_wear": 0.4, "p_love": 0.8, "impact": 0.2, "reach": 0.6, "affinity": 0.5, "category": "media"}
    assert W.weighted_total(f, {"accept": 0}) == round(100 * 0.4 * 0.8 * (0.3 + 0.7 * 0.2), 2)
    assert W.weighted_total(dict(f, p_accept=0.0), {"accept": 0}) == W.weighted_total(f, {"accept": 0})   # 0**0 == 1
    assert W.weighted_total(f, {"wear": 2}) == round(100 * 0.5 * 0.4 ** 2 * 0.8 * (0.3 + 0.7 * 0.2), 2)
    assert W.weighted_total(f, {"connection": 1}) == round(100 * 0.5 * 0.4 * 0.8 * 0.44 * (0.25 + 0.75 * 0.6), 2)
    assert W.weighted_total(f, {"affinity": 1}) == round(100 * 0.5 * 0.4 * 0.8 * 0.44 * (0.3 + 0.7 * 0.5), 2)
    assert W.weighted_total(f, {"media": 0}) == 0.0
    assert W.weighted_total(f, {"media": 2}) == round(2 * 100 * 0.5 * 0.4 * 0.8 * 0.44, 2)
    assert W.weighted_total(f, {"vc": 0}) == W.weighted_total(f)            # another category's weight does not touch it
    # unknown reach is score.py's no-path 0.05; unknown affinity counts as none
    assert W.weighted_total(dict(f, reach=None), {"connection": 1}) == W.weighted_total(dict(f, reach=0.05), {"connection": 1})
    assert W.weighted_total(dict(f, affinity=None), {"affinity": 1}) == W.weighted_total(dict(f, affinity=0.0), {"affinity": 1})
    assert W.normalize({"impact": 9, "love": -1, "bogus": 2, "wear": "x"}) == dict(W.DEFAULT_WEIGHTS, impact=3.0, love=0.0)


def test_monotonic():
    lo = {"p_accept": 0.5, "p_wear": 0.5, "p_love": 0.5, "impact": 0.2, "reach": 0.2, "affinity": 0.2, "category": "vc"}
    hi_impact = dict(lo, impact=0.9, p_love=0.4)
    for w in (None, {"impact": 2.5, "connection": 1, "affinity": 1}, {"accept": 0.3, "love": 3}):
        for k in ("p_accept", "p_wear", "p_love", "impact", "reach", "affinity"):
            assert W.weighted_total(dict(lo, **{k: 0.8}), w) >= W.weighted_total(lo, w), (k, w)
    # more weight on impact moves the high-impact person up against the other one
    ratios = [W.weighted_total(hi_impact, {"impact": x}) / W.weighted_total(lo, {"impact": x}) for x in (0, 1, 2, 3)]
    assert ratios == sorted(ratios) and ratios[0] < 1 < ratios[-1]


def test_formula_text():
    f = {"p_accept": 0.8, "p_wear": 0.5, "p_love": 0.9, "impact": 0.4, "reach": 0.3, "affinity": None, "category": "vc"}
    t = W.weighted_total(f)
    assert W.formula_text(f, None, t) == \
        (f"total = 100 x 0.80 x 0.50 x 0.90 x (0.3 + 0.7 x 0.40) x (0.7 + 0.6 x engagement unknown, counted 0.50) "
         f"= {t:.2f} (reach is not in the total).")
    t2 = W.weighted_total(dict(f, engagement=0.75))
    assert "x (0.7 + 0.6 x engagement 0.75) = " in W.formula_text(dict(f, engagement=0.75), None, t2)
    w = {"impact": 2, "accept": 0, "connection": 1, "vc": 1.5}
    text = W.formula_text(f, w, W.weighted_total(f, w), "config/weights.yaml default 'Carlo'")
    assert "0.80" not in text and "(0.3 + 0.7 x 0.40)^2" in text and "(0.25 + 0.75 x reach 0.30)" in text
    assert "1.5 (VCs weight)" in text and "config/weights.yaml default 'Carlo'" in text and "accept 0" in text
    assert "reach is not in the total" not in text


def frame():
    return pd.DataFrame([
        person(1, pa=0.9, pw=0.9, pl=0.9, imp=0.50, reach=0.9),                  # default #2, smaller audience, warm
        person(2, pa=0.7, pw=0.7, pl=0.7, imp=0.95, cat="media"),                # default #3, big audience
        person(3, pa=0.6, pw=0.6, pl=0.6, imp=0.30, cat="vc"),                   # default #4
        person(4, pa=0.5, pw=0.5, pl=0.5, imp=1.00, cat="media", flags=["not_us"]),
        person(5, pa=0.95, pw=0.95, pl=0.95, imp=0.9, flags=["competitor:Oura"]),
        person(6, pa=0.99, pw=0.99, pl=0.99, imp=1.0, status="rejected"),
        person(7, pa=0.3, pw=0.3, pl=0.3, imp=1.0, cat="founder_angel"),        # last by default
    ])


def test_rerank_default_order_and_rank_change():
    d0 = W.rerank(frame())
    assert list(d0["candidate_id"]) == [5, 1, 2, 3, 7]   # not_us and rejected out by default, competitor kept
    assert (d0["rank"] == d0["rank_default"]).all() and set(d0["change"]) == {"="}
    d = W.rerank(frame(), {"impact": 3}, {"n": 3})
    assert list(d["rank"]) == [1, 2, 3, 4, 5]
    by = d.set_index("candidate_id")
    assert by.loc[2, "rank"] < by.loc[1, "rank"]                          # impact x3: the big audience passes the small one
    assert by.loc[2, "change"] == "↑1" and by.loc[1, "change"] == "↓1"
    assert (d["rank_change"] == d["rank_default"] - d["rank"]).all()
    d = W.rerank(frame(), {"vc": 0, "founder_angel": 3, "impact": 3, "x_voice": 0.3}, {"n": 3})
    by = d.set_index("candidate_id")
    assert by.loc[7, "rank"] <= 3 and by.loc[7, "change"] == "new"      # entered the top 3 from outside it
    entered, dropped = W.movers(d, 3)
    assert 7 in set(entered["candidate_id"]) and len(entered) == len(dropped)
    mix = W.category_mix(d, 3)
    assert sum(v["now"] for v in mix.values()) == 3 == sum(v["default"] for v in mix.values())
    assert mix["founder_angel"] == {"now": 1, "default": 0}


def test_filters():
    df = frame()
    ids = lambda f: set(W.rerank(df, None, f)["candidate_id"])  # noqa: E731
    assert ids({}) == {1, 2, 3, 5, 7}
    assert ids({"us_only": False}) == {1, 2, 3, 4, 5, 7}
    assert ids({"hide_competitor": True}) == {1, 2, 3, 7}
    assert ids({"hide_rejected": False}) == {1, 2, 3, 5, 6, 7}
    d = W.rerank(df, None, {"hide_rejected": False, "n": 2})
    assert list(d["candidate_id"][:2]) == [6, 5] and list(d["rank"]) == [1, 2, 3, 4, 5, 6]
    assert len(W.rerank(df.iloc[0:0], None, None)) == 0


def test_wear_verdict_needs_net_positive_evidence():
    stored = lambda **f: {"factors": {"p_wear": f}}  # noqa: E731
    assert W.wear_verdict(stored(value=0.73, prior=0.25, backed=True)) == "for"
    assert W.wear_verdict(W.json.dumps(stored(value=0.73, prior=0.25, backed=True))) == "for"   # the raw column too
    assert W.wear_verdict({"value": 0.73, "prior": 0.25, "backed": True}) == "for"               # the factor itself
    assert W.wear_verdict(stored(value=0.21, prior=0.30, backed=True)) == "against"   # a humour account mocking glasses
    assert W.wear_verdict(stored(value=0.30, prior=0.30, backed=True)) == "none"      # for and against cancel out
    assert W.wear_verdict(stored(value=0.45, prior=0.30, backed=False)) == "none"     # moved, but no quote or event
    assert W.wear_verdict(stored(value=0.73, backed=True)) == "none"                  # no prior stored: fails the bar
    for empty in (None, "", "not json", {}, {"factors": {}}, {"factors": {"p_wear": None}}):
        assert W.wear_verdict(empty) == "none"
    assert W.bar_reason("media", "for") == "" and W.bar_reason("vc", True) == ""
    assert W.bar_reason("other", "for") == W.BAR_NOT_PERSON == "not a person account"
    assert W.bar_reason("x_voice", "against") == W.BAR_AGAINST
    assert W.bar_reason(None, "none") == f"{W.BAR_NOT_PERSON}; {W.BAR_NO_EVIDENCE}"
    assert W.bar_reason("founder_angel", False) == "no evidence that they wear a device or use a voice tool"


def test_the_bar_is_a_hard_filter_and_ranks_count_after_it():
    df = pd.concat([frame(), pd.DataFrame([
        person(8, pa=0.99, pw=0.99, pl=0.99, imp=1.0, cat="other"),     # an outlet: would be #1 on reach alone
        person(9, pa=0.98, pw=0.98, pl=0.98, imp=1.0, wear=False),      # a big name with no device or voice evidence
        person(10, pa=0.97, pw=0.97, pl=0.97, imp=1.0, wear=None),      # older run: nothing stored
    ])], ignore_index=True)
    assert W.DEFAULT_FILTERS["bar"] is True
    ids = lambda f: list(W.rerank(df, None, f)["candidate_id"])  # noqa: E731
    assert ids({}) == [5, 1, 2, 3, 7]                                # the bar, US only, hide rejected: the default
    d = W.rerank(df, None, {"bar": False})
    assert list(d["candidate_id"][:3]) == [8, 9, 10] and list(d["rank"]) == list(range(1, 9))
    assert list(W.rerank(df, None, {})["rank"]) == [1, 2, 3, 4, 5]  # rank numbers count after the bar
    assert set(W.filter_rows(df, {"bar": True, "us_only": False, "hide_rejected": False})["candidate_id"]) == set(range(1, 8))
    rows = {r["candidate_id"]: r for r in df.to_dict("records")}
    assert W.hidden_by(rows[8]) == ["bar"] and W.hidden_by(rows[9]) == ["bar"] and W.hidden_by(rows[10]) == ["bar"]
    assert W.hidden_by({**rows[4], "category": "other"}) == ["us_only", "bar"] and W.hidden_by(rows[1]) == []
    assert W.hidden_by(rows[8], {"bar": False}) == [] and W.meets_bar({"category": "vc", "wear_evidence": float("nan")}) is False


def test_presets_round_trip_and_scheduled_default(tmp_path):
    p = tmp_path / "weights.yaml"
    assert W.load_file(p) == {"presets": {}, "default": None, "default_name": None}
    assert W.scheduled_weights(p) == (W.DEFAULT_WEIGHTS, None)
    W.save_preset("Attention first", {"impact": 2.0, "media": 1.5}, path=p)
    cur = W.load_file(p)
    assert cur["presets"]["Attention first"] == dict(W.DEFAULT_WEIGHTS, impact=2.0, media=1.5) and cur["default"] is None
    assert W.scheduled_weights(p)[1] is None
    W.save_preset("  Carlo  live ", {"love": 2.0}, as_default=True, path=p)
    W.save_preset("Scratch", {"wear": 0.5}, path=p)             # a later preset keeps the default
    w, source = W.scheduled_weights(p)
    assert w == dict(W.DEFAULT_WEIGHTS, love=2.0) and "Carlo live" in source
    assert set(W.load_file(p)["presets"]) == {"Attention first", "Carlo live", "Scratch"}
    with pytest.raises(ValueError):
        W.save_preset("  ", {}, path=p)
    p.write_text("default: [unclosed")
    assert W.scheduled_weights(p) == (W.DEFAULT_WEIGHTS, None)   # a broken file never stops a scheduled run


def test_repo_weights_file_has_no_default():
    """The tracked config/weights.yaml ships presets only: scheduled totals stay today's until someone saves a default."""
    cur = W.load_file()
    assert cur["default"] is None and cur["presets"]["Today's formula"] == W.DEFAULT_WEIGHTS


def test_export_reorder_keeps_the_wishlist_shape():
    rows = [{"n": i, "candidate_id": c, "rank": i, "score": 10.0 - i, "name": f"P{c}", "paths": []}
            for i, c in enumerate((11, 12, 13), 1)]
    out = export.reorder(rows, [{"candidate_id": 13, "rank": 1, "total": 55.55, "rank_default": 3},
                                {"candidate_id": 99, "rank": 2, "total": 1.0},
                                {"candidate_id": 11, "rank": 3, "total": 4.04}])
    assert [(r["n"], r["candidate_id"], r["rank"], r["score"]) for r in out] == [(1, 13, 1, 55.5), (2, 11, 3, 4.0)]
    assert out[0]["rank_default"] == 3 and rows[2]["n"] == 3   # the cached rows are not mutated


def test_effective_weights_follow_the_manual_switch(tmp_path):
    p = tmp_path / "weights.yaml"
    assert W.effective({}, p) == (W.DEFAULT_WEIGHTS, W.DEFAULT_LABEL)
    sliders = {"tw_wear": 2.0, "tw_vc": 0.5}
    assert W.effective(sliders, p) == (W.DEFAULT_WEIGHTS, W.DEFAULT_LABEL)          # switch off: sliders ignored
    w, label = W.effective({"manual_weights": True, **sliders}, p)
    assert w == dict(W.DEFAULT_WEIGHTS, wear=2.0, vc=0.5) and label == W.MANUAL_LABEL
    W.save_preset("Carlo", {"love": 2.0}, as_default=True, path=p)
    w, label = W.effective({}, p)                                                   # a saved default is the default
    assert w == dict(W.DEFAULT_WEIGHTS, love=2.0) and "Carlo" in label


def test_rerank_baseline_is_what_default_means():
    d = W.rerank(frame(), {"impact": 3}, {"n": 3}, baseline={"impact": 3})
    assert set(d["change"]) == {"="} and (d["rank"] == d["rank_default"]).all()


def test_engagement_term_typical_is_neutral_unknown_counts_as_typical():
    f = {"p_accept": 0.5, "p_wear": 0.4, "p_love": 0.8, "impact": 0.2, "category": "media"}
    base = round(100 * 0.5 * 0.4 * 0.8 * (0.3 + 0.7 * 0.2), 2)
    assert W.weighted_total(f) == W.weighted_total(dict(f, engagement=0.5)) == base   # typical for their size: x1.0
    assert W.weighted_total(dict(f, engagement=1.0)) == round(base * 1.3, 2)
    assert W.weighted_total(dict(f, engagement=0.0)) == round(base * 0.7, 2)
    assert W.weighted_total(dict(f, engagement=1.0), {"engagement": 0}) == base      # weight 0 leaves it out
    assert W.weighted_total(dict(f, engagement=1.0), {"engagement": 2}) == round(base * 1.3 ** 2, 2)
    assert "engagement" in W.FACTOR_KEYS and W.DEFAULT_WEIGHTS["engagement"] == 1.0 and W.LABELS["engagement"]
