"""Weights on the score: re-rank a scored run from its stored factors, instantly and at no cost (no pipeline, no LLM, no API).

    total = 100 x p_accept^w_accept x p_wear^w_wear x p_love^w_love x (0.3 + 0.7 impact)^w_impact
                x (0.7 + 0.6 engagement)^w_engagement
                x (0.25 + 0.75 reach)^w_connection x (0.3 + 0.7 affinity)^w_affinity x w_<category>

Community engagement (engagement.py) is 0.5 for a community typical of the account's size, so the term is x1.0 there,
x1.3 at four times typical and x0.7 at a quarter; unknown engagement counts as typical (ENGAGEMENT_UNKNOWN).

Defaults are 1, except connection and affinity (0): with them the total is score.py's, exactly. A weight of 0 removes
a factor, 2 doubles its emphasis. Category weights multiply the total of everyone in that category (0 removes them).
Why categories and not attention / network / funding: scores.reasons_json keeps impact as one value plus text, and
score.py has no separate network or funding term (a role's `base` holds both), so priorities cannot be recomputed.

Pure functions, no Streamlit: weighted_total, rerank, movers, category_mix, presets in config/weights.yaml
(`presets:` by name; `default:` sets the weights of scheduled runs, read by score.py through scheduled_weights).

The brief's bar (SCORING.md "The bar") is a hard filter, not a weight: a person category, net positive evidence that
they wear a device or use a voice tool (wear_verdict), and at least one real way to contact them (contactable, from
contacts.py: a warm path, open X DMs, a researched public route or an email in their X bio). hidden_by is the one
filter source for the list, the check tab and the export.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Mapping

from . import config

WEIGHTS_FILE = config.CONFIG_DIR / "weights.yaml"
WEIGHT_MIN, WEIGHT_MAX = 0.0, 3.0
FACTOR_KEYS = ("accept", "wear", "love", "impact", "engagement", "connection", "affinity")
CATEGORY_KEYS = ("x_voice", "media", "vc", "founder_angel", "other")
DEFAULT_WEIGHTS: dict[str, float] = {"accept": 1.0, "wear": 1.0, "love": 1.0, "impact": 1.0, "engagement": 1.0,
                                     "connection": 0.0, "affinity": 0.0, **{c: 1.0 for c in CATEGORY_KEYS}}
IMPACT_BASE, IMPACT_SLOPE = 0.3, 0.7            # score.py's (0.3 + 0.7 x impact): a small audience never erases a lover
CONNECTION_BASE, CONNECTION_SLOPE = 0.25, 0.75  # the reach term the total had until 30 Sep
AFFINITY_BASE, AFFINITY_SLOPE = 0.3, 0.7        # unknown affinity counts as 0 (it adds nothing)
ENGAGEMENT_BASE, ENGAGEMENT_SLOPE = 0.7, 0.6    # x0.7 .. x1.3; typical for their size (0.5) is x1.0
ENGAGEMENT_UNKNOWN = 0.5                        # no sample of their latest posts: counted as typical, neither up nor down
REACH_NONE = 0.05                               # score.py's reach when no path is known
TOTAL_DECIMALS = 2
DEFAULT_FILTERS = {"us_only": True, "hide_competitor": False, "hide_rejected": True, "bar": True, "n": 100}
PERSON_CATEGORIES = ("x_voice", "media", "vc", "founder_angel")   # 'other': outlets, brands, not-a-person accounts
BAR_NOT_PERSON = "not a person account"
BAR_NO_EVIDENCE = "no evidence that they wear a device or use a voice tool"
BAR_AGAINST = "evidence is against wearing"
BAR_NO_CONTACT = "no way to contact found"   # contacts.NO_CONTACT; the full reason names what was looked at

LABELS = {
    "accept": "Accepts a wearable", "wear": "Wears it daily", "love": "Loves it and says so", "impact": "Impact if they love it",
    "engagement": "Community engagement",
    "connection": "Warm connection", "affinity": "Topic affinity (extra)",
    "x_voice": "X voices", "media": "Media", "vc": "VCs", "founder_angel": "Founders & angels", "other": "Other",
}
HELP = {
    "accept": "How much the chance they say yes to a wearable counts. 0 ignores it, 2 counts it twice as hard.",
    "wear": "How much the chance they wear it every day counts.",
    "love": "How much the chance they love it and say so in public counts.",
    "impact": "How much their reach counts if they love it: audience size, relevance and the leverage of their role.",
    "engagement": "How much their audience answers counts: the average post's views, likes, replies and reposts per "
                  "follower, against accounts of the same size. At 1 a typical community leaves the score as it is, the "
                  "most engaged x1.3, the least x0.7; 0 ignores it, 2 doubles it.",
    "connection": "0 = how to reach them is information only (the operator, 29 Sep). Above 0, people with a warm path rise.",
    "affinity": "Already inside 'accepts' and 'loves it'. Above 0 adds extra emphasis on people who talk about agents, "
                "voice, wearables or screen-free life. Unknown counts as none.",
    "x_voice": "Multiplies every X voice's score: 0 removes them, 2 doubles them.",
    "media": "Multiplies every media person's score: 0 removes them, 2 doubles them.",
    "vc": "Multiplies every VC's score: 0 removes them, 2 doubles them.",
    "founder_angel": "Multiplies every founder's and angel's score: 0 removes them, 2 doubles them.",
    "other": "Multiplies the score of everyone outside the four categories: 0 removes them.",
}


# ---------------------------------------------------------------- the formula

def normalize(weights: Mapping[str, Any] | None) -> dict[str, float]:
    """Every known key, as a float in [0, 3]; missing or unreadable values take the default. Unknown keys are dropped."""
    out = dict(DEFAULT_WEIGHTS)
    for k, v in (weights or {}).items():
        if k in out:
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isnan(x):
                out[k] = min(WEIGHT_MAX, max(WEIGHT_MIN, x))
    return out


def is_default(weights: Mapping[str, Any] | None) -> bool:
    return normalize(weights) == DEFAULT_WEIGHTS


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def _category_weight(w: Mapping[str, float], category: Any) -> float:
    c = str(category or "other")
    return w[c] if c in CATEGORY_KEYS else w["other"]


def weighted_total(factors: Mapping[str, Any], weights: Mapping[str, Any] | None = None) -> float:
    """factors: p_accept, p_wear, p_love, impact (0..1), optional reach, affinity and category. Returns 0..100+ points,
    rounded like score.py. With DEFAULT_WEIGHTS it is score.py's total bit for bit (x**1.0 == x, x**0 == 1)."""
    w = normalize(weights)
    pa, pw, pl = (_num(factors.get(k)) or 0.0 for k in ("p_accept", "p_wear", "p_love"))
    impact = _num(factors.get("impact")) or 0.0
    reach = _num(factors.get("reach"))
    reach = REACH_NONE if reach is None else reach
    affinity = _num(factors.get("affinity")) or 0.0
    eng = _num(factors.get("engagement"))
    eng = ENGAGEMENT_UNKNOWN if eng is None else eng
    total = (100.0 * pa ** w["accept"] * pw ** w["wear"] * pl ** w["love"]
             * (IMPACT_BASE + IMPACT_SLOPE * impact) ** w["impact"]
             * (ENGAGEMENT_BASE + ENGAGEMENT_SLOPE * eng) ** w["engagement"]
             * (CONNECTION_BASE + CONNECTION_SLOPE * reach) ** w["connection"]
             * (AFFINITY_BASE + AFFINITY_SLOPE * affinity) ** w["affinity"]
             * _category_weight(w, factors.get("category")))
    return round(max(0.0, total), TOTAL_DECIMALS)


def formula_text(factors: Mapping[str, Any], weights: Mapping[str, Any] | None, total: float, source: str | None = None) -> str:
    """The total's reason line for score.py. Default weights keep the exact text of the unweighted formula."""
    w = normalize(weights)
    pa, pw, pl, impact = (_num(factors.get(k)) or 0.0 for k in ("p_accept", "p_wear", "p_love", "impact"))
    eng = _num(factors.get("engagement"))
    eng_t = (f"(0.7 + 0.6 x engagement {eng:.2f})" if eng is not None
             else f"(0.7 + 0.6 x engagement unknown, counted {ENGAGEMENT_UNKNOWN:.2f})")
    if w == DEFAULT_WEIGHTS:
        return (f"total = 100 x {pa:.2f} x {pw:.2f} x {pl:.2f} x (0.3 + 0.7 x {impact:.2f}) x {eng_t} = {total:.2f} "
                "(reach is not in the total).")
    reach = _num(factors.get("reach"))
    reach = REACH_NONE if reach is None else reach
    affinity = _num(factors.get("affinity")) or 0.0
    parts = {"accept": f"{pa:.2f}", "wear": f"{pw:.2f}", "love": f"{pl:.2f}", "impact": f"(0.3 + 0.7 x {impact:.2f})",
             "engagement": eng_t,
             "connection": f"(0.25 + 0.75 x reach {reach:.2f})", "affinity": f"(0.3 + 0.7 x affinity {affinity:.2f})"}
    terms = [t if w[k] == 1 else f"{t}^{w[k]:g}" for k, t in parts.items() if w[k] > 0]
    category = str(factors.get("category") or "other")
    cw = _category_weight(w, category)
    if cw != 1:
        terms.append(f"{cw:g} ({LABELS.get(category, 'Other')} weight)")
    changed = ", ".join(f"{k} {w[k]:g}" for k in DEFAULT_WEIGHTS if w[k] != DEFAULT_WEIGHTS[k])
    note = f"weights from {source or 'config/weights.yaml'}: {changed}"
    if w["connection"] == 0:
        note += "; reach is not in the total"
    return f"total = 100 x {' x '.join(terms) or '1'} = {total:.2f} ({note})."


# ---------------------------------------------------------------- re-ranking a table of stored factors

def wear_verdict(stored: Any) -> str:
    """The device-or-voice part of the bar, from the stored p_wear factor (reasons_json, parsed or not, or the factor
    itself): 'for' when evidence or an event took p_wear above its prior (voice-tool quotes count, at half weight in the
    score); 'against' when evidence left it below the prior (mocking or abandoning a device); 'none' when nothing backs
    it, it sits at the prior, or the prior is missing (older runs)."""
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = None
    f = stored if isinstance(stored, Mapping) else {}
    if "value" not in f:
        f = (f.get("factors") if isinstance(f.get("factors"), Mapping) else f).get("p_wear") or {}
        f = f if isinstance(f, Mapping) else {}
    value, prior = _num(f.get("value")), _num(f.get("prior"))
    if not f.get("backed") or value is None or prior is None or value == prior:
        return "none"
    return "for" if value > prior else "against"


def bar_reason(category: Any, wear: str | bool | None, reach: str | bool | None = True) -> str:
    """'' when the row meets the brief's bar, else which parts fail, e.g. 'not a person account'. wear: a wear_verdict,
    or the bool wear_evidence (False reads as no evidence). reach: True when they can be contacted, else the contact
    reason ('no way to contact found: X DMs closed, no warm path, ...'); False or None read as BAR_NO_CONTACT."""
    wear = "for" if wear is True else wear
    parts = [] if str(category or "") in PERSON_CATEGORIES else [BAR_NOT_PERSON]
    if wear != "for":
        parts.append(BAR_AGAINST if wear == "against" else BAR_NO_EVIDENCE)
    if reach is not True:
        parts.append(reach if isinstance(reach, str) and reach else BAR_NO_CONTACT)
    return "; ".join(parts)


def _yes(v: Any) -> bool:
    return v is not None and v == v and bool(v)   # missing and NaN are no


def meets_device_bar(row: Mapping[str, Any]) -> bool:
    """The device-or-voice half: a person category and net positive wear evidence (category, wear_evidence)."""
    return str(row.get("category") or "") in PERSON_CATEGORIES and _yes(row.get("wear_evidence"))


def meets_bar(row: Mapping[str, Any]) -> bool:
    """The brief's bar: the device-or-voice half and the reach half, a real way to contact them (row keys category,
    wear_evidence, contactable; missing or NaN fails)."""
    return meets_device_bar(row) and _yes(row.get("contactable"))


def hidden_by(row: Mapping[str, Any], filters: Mapping[str, Any] | None = None) -> list[str]:
    """The hard filters that leave this row out (keys of DEFAULT_FILTERS), in their order. US only drops flag not_us
    (unknown stays); hide_competitor drops competitor:* flags; hide_rejected drops status 'rejected'; bar drops rows
    below the brief's bar (meets_bar: a person, wears a device or uses a voice tool, and can be contacted). The one
    filter source of the list, the check tab and the export."""
    f = {**DEFAULT_FILTERS, **(filters or {})}
    flags = [str(x) for x in (row.get("flags") or [])]
    out = []
    if f["us_only"] and "not_us" in flags:
        out.append("us_only")
    if f["hide_competitor"] and any(x.startswith("competitor") for x in flags):
        out.append("hide_competitor")
    if f["hide_rejected"] and row.get("status") == "rejected":
        out.append("hide_rejected")
    if f["bar"] and not meets_bar(row):
        out.append("bar")
    return out


def filter_rows(df, filters: Mapping[str, Any] | None = None):
    """The rows no hard filter leaves out (hidden_by). Works on data.ranked_table's columns (flags list, status,
    category, wear_evidence, contactable)."""
    f = {**DEFAULT_FILTERS, **(filters or {})}
    if df is None or len(df) == 0:
        return df
    cols = [c for c in ("flags", "status", "category", "wear_evidence", "contactable") if c in df]
    keep = [not hidden_by(r, f) for r in df[cols].to_dict("records")]
    return df[keep]


def _ranks(df, col: str) -> list[int]:
    """1..k by total, then confidence, then candidate_id (score.py's order)."""
    conf = df["confidence"].fillna(0.0) if "confidence" in df else 0.0
    keyed = df.assign(_t=-df[col], _c=-conf).sort_values(["_t", "_c", "candidate_id"], kind="mergesort")
    pos = {cid: i for i, cid in enumerate(keyed["candidate_id"], 1)}
    return [pos[c] for c in df["candidate_id"]]


def change_label(rank: int, rank_default: int, n: int) -> str:
    if rank <= n < rank_default:
        return "new"
    d = int(rank_default) - int(rank)
    return f"↑{d}" if d > 0 else f"↓{-d}" if d < 0 else "="


def rerank(df, weights: Mapping[str, Any] | None = None, filters: Mapping[str, Any] | None = None,
           baseline: Mapping[str, Any] | None = None):
    """Filtered rows sorted by the weighted total. Adds total (new), total_default, rank (new, 1..k within the filtered
    rows), rank_default, rank_change (positive = moved up) and change ('↑12', '↓3', '=', 'new' when outside the default
    top n). Every filtered row is returned, so movers() can tell who entered and who dropped out. `baseline` is what
    "default" means (the list without manual weights, see effective()); None is DEFAULT_WEIGHTS."""
    f = {**DEFAULT_FILTERS, **(filters or {})}
    d = filter_rows(df, f).copy()
    if len(d) == 0:
        for c in ("total_default", "rank_default", "rank_change", "change"):
            d[c] = []
        return d
    records = d.to_dict("records")
    base = DEFAULT_WEIGHTS if baseline is None else normalize(baseline)
    d["total_default"] = [weighted_total(r, base) for r in records]
    d["total"] = [weighted_total(r, weights) for r in records]
    d["rank_default"] = _ranks(d, "total_default")
    d["rank"] = _ranks(d, "total")
    d["rank_change"] = d["rank_default"] - d["rank"]
    n = int(f["n"])
    d["change"] = [change_label(r, rd, n) for r, rd in zip(d["rank"], d["rank_default"])]
    return d.sort_values("rank").reset_index(drop=True)


def movers(ranked, n: int):
    """(entered, dropped): rows in the new top n that were not in the default top n, and the reverse."""
    entered = ranked[(ranked["rank"] <= n) & (ranked["rank_default"] > n)]
    dropped = ranked[(ranked["rank_default"] <= n) & (ranked["rank"] > n)].sort_values("rank_default")
    return entered, dropped


def category_mix(ranked, n: int) -> dict[str, dict[str, int]]:
    """{category: {"now": count in the new top n, "default": count in the default top n}}."""
    out: dict[str, dict[str, int]] = {}
    for col, key in (("rank", "now"), ("rank_default", "default")):
        for c in ranked.loc[ranked[col] <= n, "category"].fillna("other"):
            out.setdefault(str(c), {"now": 0, "default": 0})[key] += 1
    return out


# ---------------------------------------------------------------- presets: config/weights.yaml

def load_file(path: Path | str | None = None) -> dict:
    """{"presets": {name: weights}, "default": weights | None, "default_name": str | None}. Missing file -> empty."""
    import yaml
    p = Path(path or WEIGHTS_FILE)
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except FileNotFoundError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    presets = {str(k): normalize(v) for k, v in (raw.get("presets") or {}).items() if isinstance(v, dict)}
    default = normalize(raw["default"]) if isinstance(raw.get("default"), dict) else None
    return {"presets": presets, "default": default, "default_name": raw.get("default_name")}


def save_preset(name: str, weights: Mapping[str, Any], as_default: bool = False, path: Path | str | None = None) -> dict:
    """Add or replace a named preset; with as_default also write `default:` (the weights of scheduled runs)."""
    import yaml
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("a preset needs a name")
    p = Path(path or WEIGHTS_FILE)
    cur = load_file(p)
    w = normalize(weights)
    cur["presets"][name] = w
    doc: dict[str, Any] = {"presets": cur["presets"]}
    if as_default:
        doc["default_name"], doc["default"] = name, w
    elif cur["default"] is not None:
        doc["default_name"], doc["default"] = cur["default_name"], cur["default"]
    header = ("# Score weights (src/wearable_wishlist/weights.py, SCORING.md 'Tuning the weights live'). Written by the dashboard's\n"
              "# 'Manual weights' panel. `default:` sets the weights of scheduled runs; without it they use the built-in\n"
              f"# defaults (today's formula). Last saved {dt.date.today().isoformat()}.\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return load_file(p)


DEFAULT_LABEL = "default weights (SCORING.md)"
MANUAL_LABEL = "manual weights"


def effective(state: Mapping[str, Any] | None = None, path: Path | str | None = None) -> tuple[dict[str, float], str]:
    """(weights, label): the weights the dashboard ranks with, the one place every view asks. With the 'Manual weights'
    switch on (state['manual_weights'] true, state being st.session_state) they are the sliders' keys tw_<name>;
    otherwise the scheduled default, the same weights score.py uses (config/weights.yaml `default:`, else the built-in
    DEFAULT_WEIGHTS, SCORING.md's formula). Slider values are ignored while the switch is off."""
    state = state if state is not None else {}
    if state.get("manual_weights"):
        return normalize({k: state.get(f"tw_{k}", v) for k, v in DEFAULT_WEIGHTS.items()}), MANUAL_LABEL
    w, source = scheduled_weights(path)
    return w, source or DEFAULT_LABEL


def scheduled_weights(path: Path | str | None = None) -> tuple[dict[str, float], str | None]:
    """(weights, source) for score.py: the file's `default:` or DEFAULT_WEIGHTS (source None). A broken file must not
    stop a scheduled run: it falls back to the defaults."""
    try:
        cur = load_file(path)
    except Exception:  # noqa: BLE001 - unreadable yaml: built-in defaults, today's totals
        return dict(DEFAULT_WEIGHTS), None
    if cur["default"] is None:
        return dict(DEFAULT_WEIGHTS), None
    return cur["default"], f"config/weights.yaml default '{cur['default_name'] or 'unnamed'}'"
