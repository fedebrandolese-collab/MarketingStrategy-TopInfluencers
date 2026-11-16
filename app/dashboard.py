"""wearable_wishlist wearable wishlist: the dashboard (Streamlit).

    make dashboard                                                  # data/wishlist.db, or DB_PATH
    DB_PATH=data/demo.db .venv/bin/streamlit run app/dashboard.py   # the demo built by scripts/seed_demo_db.py

Ten-second read. One row per person: rank, name with the handle check, category, flags (⚑ a competitor tie: flagged,
never removed), one plain line on why, the three probabilities, impact, community engagement (ER and x peers), how to
contact them (the best route: a warm intro, open X DMs, a researched public route or an email in their X bio) and
status. The brief's bar is on by default (Filters): a person who wears a device or uses a voice tool, backed by a quote
or an event, and who has at least one real way to be contacted (weights.hidden_by, SCORING.md "The bar"). The count
line under the search says what each filter alone hides: what switching it off adds (data.filter_effects). Click a row
for the quotes with links behind each probability, every contact route with its source, every warm path, their
average post against accounts of their size, why the person moved run by run, and the status editor. Scoring math,
topic affinity, the filters and the data source are tucked away (expanders, one Filters popover, sidebar).

One list, ranked by weights.effective(st.session_state) from the run's stored factors (data.rank_list). By default
that means the default weights (SCORING.md's formula, or config/weights.yaml's `default:`): the stored ranking. The
"Manual weights" switch shows the weights as sliders; Run re-ranks in well under a second, with the move against the
default, the category mix and who entered or dropped out of the top N. Named presets live in config/weights.yaml.
CSV / wishlist.json downloads of the top N either way.

Reads the latest scored run through app/data.py. The only DB write is the `status` table (data.set_status), saved the
moment a status or note changes; "Save weights" writes config/weights.yaml. No network, no LLM, no spend.
Deep link to a person: ?person=<candidate_id>. Tabs: Wishlist, Check an account (app/check_tab.py) and System
(app/system_tab.py: is the daily pipeline alive, what it runs next, what it spent). With DASHBOARD_PASSWORD set, a
password comes before anything else, once per browser session.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wearable_wishlist import config, contacts, costs, export, weights  # noqa: E402
from app import data  # noqa: E402
from app.check_tab import engagement_block, render_check_tab  # noqa: E402
from app.system_tab import render_system_tab  # noqa: E402

DEMO_DB = config.DATA_DIR / "demo.db"
TABLE_ROWS_VISIBLE, ROW_PX, HEADER_PX = 8, 72, 38   # ROW_PX > 64 lets long text wrap inside a cell
QUOTES_SHOWN = 4
VIEWS = ("Table", "Cards")
CARDS_SHOWN = 25
# Flags right after Category, visible without scrolling; "change" goes after rank when the weights differ from the default
TABLE_ORDER = ["rank", "person_link", "category_label", "flags_short", "why", "p_accept", "p_wear", "p_love", "impact",
               "engagement_short", "contact", "status_label"]
WEIGHT_GROUPS = (("The three chances", ("accept", "wear", "love")),
                 ("Impact and extras", ("impact", "engagement", "affinity", "connection")),
                 ("Category mix", weights.CATEGORY_KEYS))
LIST_N_MAX = 300
PROB_LABELS = {"p_accept": "Accepts a wearable", "p_wear": "Wears it daily", "p_love": "Loves it and says so"}
PROB_SUPPORT = {"p_accept": "accept", "p_wear": "wear", "p_love": "love"}
AFFINITY_HELP = ("Topic affinity (0 to 1): discusses personal agents, voice, wearables or screen-free life, "
                 "and their community answers. Blank = not measured.")
BAR_LABEL = "Meets the brief's bar: a person who wears a device or uses a voice tool, and can be contacted"
BAR_HELP = ("The brief: a name nobody can reach, or someone who has never worn a device or used a voice tool, does not "
            "belong on the list however big they are. On: only people (not outlets, brands or other accounts) with a "
            "quote or an event that took their 'wears it daily' above its prior (voice-tool quotes count), and with at "
            "least one real way to contact them: a warm path, open X DMs, a researched public route or an email in "
            "their X bio.")
STRONG_FLAGS = ("competitor", "unverified_handle")   # shown in a warning box; the others in an info box

st.set_page_config(page_title="wearable_wishlist wearable wishlist", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
[data-testid="stMainBlockContainer"], .block-container {padding-top: 2.2rem; padding-bottom: 2rem;
  padding-left: 2rem; padding-right: 2rem;}
@media (max-width: 640px) {[data-testid="stMainBlockContainer"], .block-container {padding-left: 1rem; padding-right: 1rem;}}
h1 {font-size: 2rem !important; padding-bottom: 0 !important;}
</style>""", unsafe_allow_html=True)

COLUMN_CONFIG = {
    "rank": st.column_config.NumberColumn("#", format="%d", width=36, pinned=True,
                                          help="Rank under the weights in use, counted after the hard filters (1 = best)."),
    "change": st.column_config.TextColumn(
        "vs default", width=64, pinned=True,
        help="Move against the default weights: ↑ up, ↓ down, = same place, new = not in the default top N."),
    "person_link": st.column_config.LinkColumn(
        "Person", width=250, pinned=True, display_text=data.PERSON_LINK_TEXT,   # links do not wrap
        help="Name and X handle; click to open their X profile (no handle: an X search for the name). ✓ = handle "
             "confirmed as theirs; otherwise (likely real), (suspicious) or (unchecked)."),
    "category_label": st.column_config.TextColumn("Category", width=72),
    "why": st.column_config.TextColumn("Why", width=388,
                                       help="The case in one line, with the main caveat. Click the row for the quotes."),
    "p_accept": st.column_config.ProgressColumn("Accepts", min_value=0, max_value=100, format="%d%%", width=74,
                                                help="Chance they accept a wearable."),
    "p_wear": st.column_config.ProgressColumn("Wears daily", min_value=0, max_value=100, format="%d%%", width=76,
                                              help="Chance they wear it every day."),
    "p_love": st.column_config.ProgressColumn("Loves it", min_value=0, max_value=100, format="%d%%", width=76,
                                              help="Chance they love it and say so in public."),
    "impact": st.column_config.ProgressColumn(
        "Impact", min_value=0.0, max_value=1.0, format="%.2f", width=70,
        help="Impact if they love it (0 to 1): audience size and relevance, and the leverage of their role."),
    "engagement_short": st.column_config.TextColumn(
        "Engagement", width=140,
        help="ER: likes + replies + reposts of the average post ÷ followers. × peers: against accounts with as many "
             "followers; replies and reposts weigh most. unknown: too few of their latest posts to measure (counts as "
             "typical)."),
    "contact": st.column_config.TextColumn(
        "How to contact", width=190,
        help="The best way to get a message to them: a warm intro through your network, open X DMs, a public route "
             "found by research (email, contact form, press office, agent...), or an email in their X bio. (weak) = "
             "a weak route. Every route, with its source, is in the person panel. It does not change the rank."),
    "status_label": st.column_config.TextColumn("Status", width=76, help="Change it in the person panel (click the row)."),
    "flags_short": st.column_config.TextColumn(
        "Flags", width=120,
        help="⚑ = tied to a direct competitor; also: unverified handle, not US. Competitor ties are flagged, never "
             "removed (the brief). Filters → hide competitor-tied people if you need to."),
}


# ---------------------------------------------------------------- text helpers

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~$])")


def md(text) -> str:
    """Escape stored text (posts, bios, LLM output) for st.markdown: no accidental bold, links, HTML or LaTeX ($)."""
    return "" if text is None else _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


def link(label: str, url: str | None) -> str:
    """Markdown link for http(s) urls only; anything else is shown as plain text."""
    if not url or not str(url).startswith(("http://", "https://")):
        return md(label)
    safe = str(url).replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{md(label)}]({safe})"


def short(text, n: int = 240) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def rel(path: Path) -> str:
    p = Path(path).resolve()
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def utc(ts: str | None) -> str:
    """'2026-09-29T07:00:00Z' -> '2026-09-29 07:00 UTC'."""
    return "unknown" if not ts else str(ts).replace("T", " ")[:16] + " UTC"


def pct(v: float | None) -> str:
    return "unknown" if v is None else f"{v * 100:.0f}%"


# ---------------------------------------------------------------- data source (sidebar) and empty state

def pick_db(box) -> tuple[Path, bool]:
    """Demo toggle when data/demo.db exists (and DB_PATH is not already the demo); else DB_PATH or data/wishlist.db."""
    configured = config.db_path()
    demo_exists = DEMO_DB.exists()
    configured_is_demo = demo_exists and configured.resolve() == DEMO_DB.resolve()
    with box:
        st.subheader("Data")
        use_demo = configured_is_demo
        if demo_exists and not configured_is_demo:
            use_demo = st.toggle("Demo data", value=False, key="use_demo",
                                 help=f"Open {rel(DEMO_DB)} (fixtures, built by scripts/seed_demo_db.py) instead of {rel(configured)}.")
        path = DEMO_DB if use_demo else configured
        st.caption(f"Database: `{rel(path)}`")
        st.caption("The daily run follows `config/schedule.yaml` once `make schedule-on` turns it on; the System tab shows "
                   "whether it is alive. Status and notes save immediately.")
    return path, use_demo


def empty_state(path: Path, last: dict | None, exists: bool) -> None:
    if not exists:
        st.info(f"No database at `{rel(path)}` yet: the pipeline has not run here.", icon=":material/database:")
    elif last:
        st.info(f"`{rel(path)}` has no scored run yet. Last run: #{last['run_id']} (stage {last.get('stage') or '?'}), "
                f"started {utc(last.get('started_at'))}" + ("" if last.get("finished_at") else ", not finished") + ".",
                icon=":material/database:")
    else:
        st.info(f"`{rel(path)}` is empty: no run yet.", icon=":material/database:")
    st.markdown(
        "**To fill it, from the repository root:**\n\n"
        "1. `make ingest`: mirror the operator's network map into SQLite (reads `data/raw`; nothing leaves the machine).\n"
        "2. `make run`: the whole pipeline (map, X crawl and discovery, classification, paths, scoring, export). "
        "Paid stages print their projected cost first and stop at the per-run budgets in `.env`.\n"
        "3. Reload this page. `make schedule` installs the daily run on macOS (`make schedule-on` turns it on); for a "
        "Linux server see `docs/OPERATIONS.md`.\n\n"
        "To look around without spending anything: `.venv/bin/python scripts/seed_demo_db.py`, then switch on "
        "**Demo data** in the sidebar.")


def cost_caption(box, run: dict, cost: dict) -> None:
    providers = " · ".join(f"{p} {costs.fmt(v)}" for p, v in cost["by_provider"].items()) or "nothing billed"
    text = (f"Run #{run['run_id']} started {utc(run.get('started_at'))}"
            + (f", finished {utc(run['finished_at'])}" if run.get("finished_at") else ", not finished")
            + f". Cost of this run: {cost['label']} ({providers}); since the previous scored run "
            f"{costs.fmt(cost['since_previous_usd'])}; all runs {costs.fmt(cost['to_date_usd'])}.")
    with box:
        st.caption(text.replace("$", "\\$"))   # a bare $ pair renders as LaTeX


# ---------------------------------------------------------------- what moved (one line, details on demand)

def run_line(run: dict, changes: dict) -> str:
    n = changes["counts"]
    when = run.get("date") or "unknown date"
    head = f"Run of {when} · {int(run.get('n_candidates') or 0)} people scored"
    if changes["previous_run_id"] is None and not any(n.values()):
        return head + " · first scored run"
    moved = f"{n['up']} up, {n['down']} down, {n['new']} new" + (f", {n['other']} other" if n.get("other") else "")
    return f"{head} · since the last run: {moved} · what moved and why"


def change_expander(run: dict, changes: dict, per_group: int = 12) -> None:
    label = run_line(run, changes)
    with st.expander(label):
        if changes["previous_run_id"] is None and not any(changes["counts"].values()):
            st.write("First scored run: the change log starts with the next run.")
            return
        if not any(changes["counts"].values()):
            st.write("Nothing moved: same people, same scores.")
            return
        for group, title in (("up", "▲ Up"), ("down", "▼ Down"), ("new", "+ New"), ("other", "Other changes")):
            items = changes[group]
            if not items:
                continue
            lines = [f"**{title} ({len(items)})**", ""]
            for e in items[:per_group]:
                lines.append(f"- **{md(data.change_headline(e))}**")
                lines += [f"    - {md(short(data.plain_reason(r), 320))}" for r in e["reasons"][:3]] or ["    - no reason recorded"]
            if len(items) > per_group:
                lines.append(f"- … and {len(items) - per_group} more (open a person for their history)")
            st.markdown("\n".join(lines))


# ---------------------------------------------------------------- search, Manual weights, filters (one popover), list

def control_row(ranked, sig: str) -> dict:
    """Search, the Manual weights switch, Table/Cards and one Filters popover. Its first part decides who is ranked,
    rank numbers count after it (session keys tf_<name>, read by data.rank_list); the rest only hides rows."""
    opts = data.filter_options(ranked)
    cat_n = ranked["section"].value_counts().to_dict()
    flag_n = {f: sum(1 for fl in ranked["flags"] if f in (fl or [])) for f in opts["flags"]}
    flag_n[data.NO_FLAG] = sum(1 for fl in ranked["flags"] if not fl)
    status_n = ranked["status"].value_counts().to_dict()
    k = {name: f"f_{name}_{sig}" for name in ("query", "section", "flag", "status", "conf", "aff")}
    search_col, manual_col, mode_col, filter_col = st.columns([4, 1.5, 1.3, 1], vertical_alignment="bottom")
    with search_col:
        query = st.text_input("Search", key=k["query"], label_visibility="collapsed",
                              placeholder="Search a name, handle, company, path or note")
    with manual_col:
        st.toggle("Manual weights", key="manual_weights",
                  help="Show the weights the list is ranked with, change them and press Run. Off: the default weights.")
    with mode_col:
        st.segmented_control("View", VIEWS, default=VIEWS[0], required=True, key="view_mode", label_visibility="collapsed",
                             width="stretch", help="Cards read better on a phone.")
    with filter_col.popover("Filters", icon=":material/filter_list:", width="stretch"):
        st.markdown("**Who is ranked** · rank numbers count after these")
        st.toggle(data.FILTER_NAMES["us_only"], key="tf_us_only",
                  help="Leave out people flagged not US-based (unknown stays in). Off adds only the non-US people no "
                       "other filter here leaves out: the count line under the search says how many.")
        st.toggle(data.FILTER_NAMES["hide_competitor"], key="tf_hide_competitor",
                  help="Leave out anyone tied to a direct competitor. Off by default: the brief flags competitor ties "
                       "(⚑ in the Flags column), it never removes them.")
        st.toggle(data.FILTER_NAMES["hide_rejected"], key="tf_hide_rejected",
                  help="Leave out people whose status is rejected. Off adds only the rejected people no other filter "
                       "here leaves out.")
        st.toggle(BAR_LABEL, key="tf_bar", help=BAR_HELP)
        st.number_input("List size (top N)", min_value=10, max_value=LIST_N_MAX, step=10, key="tf_n",
                        help="How many people the list shows; also the top N of the category mix and the downloads.")
        st.divider()
        st.markdown("**Narrow the list** · hides rows, ranks stay")
        sections = st.multiselect("Category", opts["sections"], key=k["section"], placeholder="all categories",
                                  format_func=lambda s: f"{s} ({cat_n.get(s, 0)})")
        statuses = st.multiselect("Status", list(config.STATUSES), key=k["status"], placeholder="any status",
                                  format_func=lambda s: f"{data.status_label(s)} ({status_n.get(s, 0)})")
        flags = st.multiselect("Flag", opts["flags"], key=k["flag"], placeholder="any flag or none",
                               format_func=lambda f: f"{data.flag_short(f, full=True)} ({flag_n.get(f, 0)})")
        min_conf = st.slider("Minimum confidence", 0.0, 1.0, 0.0, 0.05, key=k["conf"],
                             help="Share of the three probabilities backed by quotes: 0.33 = one, 0.67 = two, 1.00 = all three.")
        measured = int(ranked["affinity"].notna().sum()) if "affinity" in ranked else 0
        min_aff = st.slider("Minimum topic affinity", 0.0, 1.0, 0.0, 0.05, key=k["aff"], disabled=measured == 0,
                            help=AFFINITY_HELP.replace("Blank = not measured.", "")
                                 + (f"Above 0, people not measured are hidden ({len(ranked) - measured} here)."
                                    if measured else "Not measured in this run."))
    return {"sections": sections, "flags": flags, "statuses": statuses, "min_confidence": min_conf, "query": query,
            "min_affinity": min_aff}


def active_filters(f: dict) -> int:
    return sum(bool(f.get(n)) for n in ("sections", "flags", "statuses")) \
        + sum(float(f.get(n) or 0) > 0 for n in ("min_confidence", "min_affinity"))


def narrowed(f: dict) -> bool:
    """A search or a view filter is on: the list shows every match with its rank, not only the top N."""
    return bool((f.get("query") or "").strip()) or active_filters(f) > 0


def list_rows(lst: dict, f: dict):
    """The rows on show: the top N, or every ranked person matching the search and filters (ranks never renumber)."""
    view = data.apply_filters(lst["ranked"], **f)
    return view if narrowed(f) else view[view["rank"] <= lst["n"]]


def _selected_rows(event) -> list[int]:
    try:
        return [int(r) for r in event["selection"]["rows"]]
    except (KeyError, TypeError, ValueError):
        return []


def count_line(tv, lst: dict, f: dict, hint: str) -> None:
    ranked, n = lst["ranked"], lst["n"]
    bar = bool(lst["filters"].get("bar"))
    people = "people who meet the bar" if bar else "people"
    if narrowed(f):
        n_on = active_filters(f)
        bits = [f"**{len(tv)} of {len(ranked)} {people}**"] + ([f"{n_on} filter{'s' if n_on > 1 else ''} on"] if n_on else [])
        if len(ranked) > n:
            bits.append(f"{int((tv['rank'] <= n).sum()) if len(tv) else 0} of them in the top {n}")
    else:
        bits = [f"**{data.list_count(len(tv), len(ranked), n, bar)}**"]
    hidden = data.hidden_text(lst["effects"])   # what each filter alone hides: what switching it off adds
    st.caption(" · ".join(bits + ([hidden] if hidden else []) + [hint]), help=data.hidden_help(lst["effects"]) or None)
    if bar and 0 < len(ranked) < n and not narrowed(f):
        st.markdown(f"{len(ranked)} people meet the bar: a shorter list with real evidence beats a hundred names.")


def nobody(lst: dict) -> None:
    """The empty list: nobody passes the hard filters (the bar, most likely), or the search and view filters hide all."""
    if len(lst["ranked"]) == 0 and lst["filters"].get("bar") and (
            "below the bar" in lst["hidden"] or data.NO_CONTACT_WORD in lst["hidden"]):
        st.info("Nobody in this run meets the brief's bar: a person who wears a device or uses a voice tool, backed by "
                "a quote or an event, and who can be contacted. Turn the bar off in Filters to see everyone ranked.",
                icon=":material/filter_alt:")
    else:
        st.info("Nobody matches. Clear the search or the filters.")


def _open_person(cid: int | None) -> None:
    if cid is None:
        st.query_params.pop("person", None)
    else:
        st.query_params["person"] = str(cid)


def wanted_person(tv) -> int | None:
    wanted = st.query_params.get("person")
    if wanted and str(wanted).isdigit() and int(wanted) in set(int(c) for c in tv["candidate_id"]):
        return int(wanted)
    return None


def cards_section(view, lst: dict, f: dict, sig: str) -> None:
    """Phone-friendly list: one card per person with the same facts as a table row and an Open button."""
    tv = data.table_view(view)
    count_line(tv, lst, f, "Open a card for the quotes, paths, history and status")
    if len(tv) == 0:
        nobody(lst)
        return
    for _, r in tv.head(CARDS_SHOWN).iterrows():
        cid = int(r["candidate_id"])
        with st.container(border=True):
            move = f" {md(r['change'])}" if lst["differs"] and r["change"] else ""
            person = link(r["person"], r["person_link"].split("#", 1)[0]) if r["person_link"] else md(r["person"])
            flags = f" · **{md(r['flags_short'])}**" if r["flags_short"] else ""   # next to the name, as in the table
            st.markdown(f"**#{int(r['rank'])}{move} · {person}**{flags}  \n{md(r['category_label'])} · "
                        f"{md(r['status_label'])}")
            st.markdown(md(r["why"]))
            probs = " · ".join(f"{lab} {'?' if r[f] != r[f] else int(r[f])}%" for f, lab in
                               (("p_accept", "Accepts"), ("p_wear", "Wears daily"), ("p_love", "Loves it")))
            impact = "?" if r["impact"] != r["impact"] or r["impact"] is None else f"{float(r['impact']):.2f}"
            st.caption(f"{probs} · Impact {impact}  \nEngagement: {md(r['engagement_short'])}  \n"
                       f"How to contact: {md(r['contact'])}")
            st.button("Open", key=f"open_{sig}_{cid}", on_click=_open_person, args=(cid,))
    if len(tv) > CARDS_SHOWN:
        st.caption(f"Showing the first {CARDS_SHOWN} of {len(tv)}: search or filter to find the rest.")


def ranked_section(view, lst: dict, f: dict, sig_base: str) -> int | None:
    """The ranked table; returns the candidate_id of the selected row (or of ?person=<id> when it is in view)."""
    tv = data.table_view(view)
    count_line(tv, lst, f, "tick the box at the left of a row to open the quotes, paths, history and status")
    if len(tv) == 0:
        nobody(lst)
        if "person" in st.query_params:
            del st.query_params["person"]
        return None
    # The key follows the rows on show: a filter change resets the selection instead of pointing at a shifted row.
    sig = hashlib.sha1((sig_base + "|" + ",".join(str(int(c)) for c in tv["candidate_id"])).encode()).hexdigest()[:12]
    default = None
    wanted = st.query_params.get("person")
    if wanted and str(wanted).isdigit():
        hits = tv.index[tv["candidate_id"] == int(wanted)].tolist()
        if hits:
            default = {"selection": {"rows": [int(hits[0])]}}
    event = st.dataframe(
        tv, key=f"ranked_{sig}", on_select="rerun", selection_mode="single-row", selection_default=default,
        hide_index=True, width="stretch", row_height=ROW_PX,
        height=HEADER_PX + ROW_PX * min(len(tv), TABLE_ROWS_VISIBLE) + 2,
        column_order=TABLE_ORDER[:1] + ["change"] * lst["differs"] + TABLE_ORDER[1:], column_config=COLUMN_CONFIG)
    rows = _selected_rows(event)
    if rows and 0 <= rows[0] < len(tv):
        cid = int(tv.iloc[rows[0]]["candidate_id"])
        if st.query_params.get("person") != str(cid):
            st.query_params["person"] = str(cid)
        return cid
    if "person" in st.query_params:
        del st.query_params["person"]
    return None


# ---------------------------------------------------------------- person panel

def save_status(db_path: str, cid: int, k_status: str, k_note: str) -> None:
    """Widget callback: persist status and note together, on its own connection, before the rerun."""
    status = st.session_state.get(k_status) or config.STATUSES[0]
    note = st.session_state.get(k_note)
    try:
        conn = data.open_db(db_path)
        try:
            row = data.set_status(conn, cid, status, note=note)
        finally:
            conn.close()
    except (ValueError, sqlite3.Error) as e:
        st.session_state["_status_error"] = f"Status not saved: {e}"
        return
    st.toast(f"Saved: {data.status_label(row['status'])}" + (" + note" if row.get("note") else ""), icon=":material/check:")
    list_source.clear()   # the list (status column, 'Hide rejected') reads status from the cached table


def status_editor(d: dict, db_path: Path, tag: str) -> None:
    cid = int(d["candidate"]["candidate_id"])
    cur = d["status"]
    k_status, k_note = f"status_{tag}_{cid}", f"note_{tag}_{cid}"
    statuses = list(config.STATUSES)
    args = (str(db_path), cid, k_status, k_note)
    st.selectbox("Status", statuses, index=statuses.index(cur["status"]) if cur.get("status") in statuses else 0,
                 key=k_status, format_func=data.status_label, on_change=save_status, args=args)
    st.text_area("Note", value=cur.get("note") or "", key=k_note, height=100, on_change=save_status, args=args,
                 placeholder="Who asks for the intro, what was said, when the wearable ships…",
                 help="Saved when you click outside the box (or press Cmd/Ctrl+Enter).")
    if cur.get("updated_at"):
        st.caption(f"Saved {utc(cur['updated_at'])}" + (f" by {md(cur['owner'])}" if cur.get("owner") else ""))
    else:
        st.caption("Nothing saved yet (default: candidate).")


def flags_block(flags: list[dict]) -> None:
    for f in flags:
        if f["flag"] in data.TABLE_HIDDEN_FLAGS:   # "no path" is how to approach them, already on the path line
            continue
        text = f"**{md(f['short'])}**: {md(f['explanation'])}"   # the explanation keeps a tie's full text
        for s in f.get("scoring") or []:   # the scorer's reason, without the flag it restates
            text += f"  \n{md(data.plain_reason(str(s).removeprefix(f['flag'] + ': ')))}"
        if f["flag"].startswith(STRONG_FLAGS):
            st.warning(text, icon=":material/warning:")
        else:
            st.info(text, icon=":material/info:")


def quote_line(e: dict) -> str:
    against = "**Against:** " if (e.get("polarity") or 1) < 0 else ""
    src = link(f"{(e.get('kind') or 'source').replace('_', ' ')}, {e.get('date') or 'undated'}", e.get("url"))
    return f"- {against}“{md(short(e.get('text'), 240))}” ({src})"


def quotes(items: list[dict], empty: str) -> None:
    if not items:
        st.caption(empty)
        return
    pos = sorted((e for e in items if (e.get("polarity") or 1) > 0), key=lambda e: str(e.get("date") or ""), reverse=True)
    ordered = pos + [e for e in items if (e.get("polarity") or 1) < 0]   # newest supporting quotes, then the ones against
    st.markdown("\n".join(quote_line(e) for e in ordered[:QUOTES_SHOWN]))
    if len(ordered) > QUOTES_SHOWN:
        with st.expander(f"{len(ordered) - QUOTES_SHOWN} more"):
            st.markdown("\n".join(quote_line(e) for e in ordered[QUOTES_SHOWN:]))


def prob_block(d: dict, field: str, groups: dict) -> None:
    v = d["factors"][field]["value"]
    st.progress(min(max(float(v or 0.0), 0.0), 1.0), text=f"**{PROB_LABELS[field]}** · {pct(v)}")
    quotes(groups.get(PROB_SUPPORT[field], []), "No quote found: the number is the starting guess for the category.")


def impact_block(d: dict, groups: dict) -> None:
    v = d["factors"]["impact"]["value"]
    st.progress(min(max(float(v or 0.0), 0.0), 1.0),
                text=f"**Impact if they love it** · {'unknown' if v is None else f'{v:.2f}'}")
    xu, mp = d.get("x_user") or {}, d.get("map_person") or {}
    bits = [f"{int(xu['followers']):,} followers on X" if xu.get("followers") is not None else None,
            " at ".join(b for b in (mp.get("role"), mp.get("company")) if b) or None]
    if any(bits):
        st.caption(md(" · ".join(b for b in bits if b)))
    # Profile signals restate the follower count and an audience estimate; investments, reviews and articles say more.
    quotes([e for e in groups.get("impact", []) if e.get("kind") != "profile_signal"],
           "Audience size and role only (details under How the numbers were computed).")


def contact_block(d: dict) -> None:
    """Every contact route, best first, with where it was found and a note; or why there is none."""
    ct = d.get("contact") or {}
    st.markdown("##### How to contact them, best first")
    if not ct.get("contactable"):
        st.warning(md(ct.get("contact_reason") or "No way to contact found."), icon=":material/contact_support:")
    lines = []
    for i, r in enumerate(ct.get("routes") or [], 1):
        detail, src = r.get("detail") or "", r.get("source_url")
        bits = [f"**{md(r['label'])}**"]
        if detail.startswith(("http://", "https://")):
            bits.append(link("open", detail))
        elif detail and detail not in r["label"]:
            bits.append(md(detail))
        if src and src != detail:
            bits.append(link("X profile" if r["kind"] in ("dm", "bio_email") else "source", src))
        lines.append(f"{i}. " + " · ".join(bits) + (f"  \n   {md(r['note'])}" if r.get("note") else ""))
    if lines:
        st.markdown("\n".join(lines))


def paths_block(d: dict) -> None:
    """Relationship paths (you know them, an intro, X): open DMs and 'none' are in the contact routes above."""
    st.markdown("##### Warm paths, warmest first")
    paths = [p for p in d["paths"] if p.get("how") not in ("none", "open_dm")]
    if not paths:
        st.caption("No warm intro in your network: the routes above are cold.")
        return
    lines = []
    for i, p in enumerate(paths[:5], 1):
        pv = data.path_view(p, d["name"])
        lines.append(f"{i}. **{md(pv['head'])}** · {md(pv['kind'])} · {md(pv['strength_word'])}")
        lines += [f"    - {md(lab)}" for lab in pv["labels"]]
    st.markdown("\n".join(lines))
    if len(paths) > 5:
        st.caption(f"{len(paths) - 5} weaker paths not shown.")


def history_block(d: dict, run: dict) -> None:
    st.markdown("##### Why it moved")
    if not d["history"]:
        st.caption("No change recorded for this person yet." if run.get("previous_run_id") else "First scored run.")
        return
    for h in d["history"][:5]:
        when = f"Run of {h['when']}" if h.get("when") else f"Run #{h['run_id']}"
        lines = [f"**{md(when)}**"] + [f"- {md(short(t, 300))}" for t in h["lines"][:6]]
        if len(h["lines"]) > 6:
            lines.append(f"- … {len(h['lines']) - 6} more changes")
        st.markdown("\n".join(lines))


def other_evidence(groups: dict) -> None:
    rest = [(k, v) for k, v in groups.items() if k not in ("accept", "wear", "love", "impact")]
    if not rest:
        return
    with st.expander("Other evidence: competitor ties, US base, handle check, topic posts"):
        for k, items in rest:
            st.markdown(f"**{md(data.SUPPORT_LABELS.get(k, k).capitalize())}**\n\n" + "\n".join(quote_line(e) for e in items))


def side_facts(d: dict) -> None:
    st.markdown("##### Unknown (not guessed)")
    if d["unknowns"]:
        st.markdown("\n".join(f"- {md(u)}" for u in d["unknowns"]))
    else:
        st.caption("Nothing recorded as unknown.")
    st.markdown("##### Dated events")
    if d["events"]:
        st.markdown("\n".join(
            f"- {md(e.get('date') or 'undated')} · **{md((e.get('kind') or 'event').replace('_', ' '))}** · "
            f"{md(short(e.get('text'), 200))} · {link('source', e.get('url'))}" for e in d["events"]))
    else:
        st.caption("No dated events.")


def affinity_block(d: dict) -> None:
    """Topic affinity: the value, the one-sentence reason, where it comes from and the best posts as links."""
    a = d.get("affinity") or {}
    v = a.get("value")
    st.markdown(f"**Topic affinity** · {'not measured' if v is None else f'{v:.2f}'}")
    if a.get("reason"):
        st.markdown(md(short(a["reason"], 400)))
    elif v is None:
        st.caption("Not measured for this run: scored before topic affinity existed, or no posts of theirs are stored.")
    else:
        st.caption("No reason stored.")
    bits = []
    if a.get("source_label"):
        bits.append(f"from {a['source_label']}")
    if a.get("topics"):
        bits.append("topics: " + ", ".join(a["topics"]))
    if bits:
        st.caption(md(" · ".join(bits)))
    posts = a.get("top_posts") or []
    if posts:
        lines = []
        for p in posts:
            label = p.get("date") or "post"
            tail = [f"{int(p['replies'])} replies"] if p.get("replies") is not None else []
            text = f" · “{md(short(p['text'], 110))}”" if p.get("text") else ""
            lines.append(f"- {link(label, p.get('url'))}{text}" + (f" · {md(' · '.join(tail))}" if tail else ""))
        st.markdown(("Best posts:\n\n" if not a.get("posts_from_evidence") else "Best posts (classifier evidence):\n\n")
                    + "\n".join(lines))


def math_expander(d: dict, row: dict | None = None, formula: str | None = None) -> None:
    with st.expander("How the numbers were computed"):
        total = d["factors"].get("total") or {}
        # The formula is the scorer's (SCORING.md); show what it stored rather than a copy that can go stale.
        st.markdown("**Score**" + (f" · {d['total']:.1f}" if d.get("total") is not None else ""))
        st.caption(md(" ".join(data.plain_reason(r) for r in total.get("reasons") or [])
                      or "The three probabilities weighted by impact; the method is in SCORING.md."))
        if row and formula:
            st.markdown(f"**With the manual weights** · {row['total']:.1f}, rank #{int(row['rank'])} "
                        f"(#{int(row['rank_default'])} with the default weights)")
            st.caption(md(formula))
        for field in data.SCORE_FIELDS:
            fac = d["factors"][field]
            v = fac["value"]
            shown = pct(v) if field in data.PROB_FIELDS else ("unknown" if v is None else f"{v:.2f}")
            reasons = [data.plain_reason(r) for r in fac["reasons"]] or ["no reason stored: starting guess only"]
            st.markdown(f"**{md(fac['label'].capitalize())}** · {shown}\n\n" + "\n".join(f"- {md(short(r, 400))}" for r in reasons))
        conf = d["factors"]["confidence"]["value"]
        st.markdown(f"**Confidence** · {'unknown' if conf is None else f'{conf:.2f}'} "
                    "(share of the three probabilities backed by quotes rather than starting guesses)")
        affinity_block(d)


def profile_expander(d: dict) -> None:
    cand, xu, mp = d["candidate"], d["x_user"] or {}, d["map_person"] or {}
    with st.expander("Profile and provenance"):
        lines = []
        if xu:
            bits = [f"{int(xu['followers']):,} followers" if xu.get("followers") is not None else "followers unknown",
                    f"location {xu['location']}" if xu.get("location") else "location unknown",
                    f"verified ({xu.get('verified_type') or 'yes'})" if xu.get("verified") else "not blue-verified"]
            if xu.get("created_at"):
                bits.append(f"on X since {str(xu['created_at'])[:10]}")
            lines.append("- **X:** " + md(" · ".join(bits)))
            if xu.get("bio"):
                lines.append(f"- **Bio:** “{md(short(xu['bio'], 300))}”")
        if mp:
            bits = [mp.get("company"), mp.get("role"), f"sector {mp['sector']}" if mp.get("sector") else None,
                    f"{mp['meetings']} meetings" if mp.get("meetings") else None,
                    f"last contact {str(mp['last_contact'])[:10]}" if mp.get("last_contact") else None]
            lines.append("- **In your network map:** " + md(" · ".join(b for b in bits if b)))
        lines.append(f"- **Found through:** {md(cand.get('discovered_via') or 'unknown')}"
                     + (f" · first seen {md(str(cand['first_seen'])[:10])}" if cand.get("first_seen") else ""))
        lines.append(f"- **Handle check:** {md(cand.get('handle_verified') or 'not checked')}"
                     + (f" ({md(cand['handle_verification_method'])})" if cand.get("handle_verification_method") else ""))
        if cand.get("linkedin_url"):
            lines.append(f"- **LinkedIn:** {link(cand['linkedin_url'], cand['linkedin_url'])}")
        if cand.get("notes"):
            lines.append(f"- **Notes:** {md(cand['notes'])}")
        st.markdown("\n".join(lines))


def person_row(lst: dict, cid: int) -> dict | None:
    hit = lst["ranked"][lst["ranked"]["candidate_id"] == cid]
    return hit.iloc[0].to_dict() if len(hit) else None


def detail_panel(conn, run: dict, cid: int, db_path: Path, tag: str, lst: dict | None = None) -> None:
    """lst (data.rank_list): the rank shown is the list's; under manual weights the score and math follow the sliders."""
    d = data.person_detail(conn, run["run_id"], cid)
    if d is None:
        st.warning("This person is not in the database any more.")
        return
    groups = data.evidence_groups(d["evidence"])
    row = person_row(lst, cid) if lst else None
    formula = (weights.formula_text(row, lst["weights"], row["total"], source="the Manual weights sliders")
               if row and lst["manual"] else None)
    rank = int(row["rank"]) if row else d.get("rank")
    with st.container(border=True):
        head, side = st.columns([3, 1], gap="large")
        with head:
            st.subheader((f"#{rank} · " if rank is not None else "") + md(d["name"]))
            meta = [link(d["handle_label"], d["x_url"]) if d["handle"] else "no X handle", md(d["category_label"])]
            if formula:
                meta.append(f"score {row['total']:.1f} with manual weights ({row['total_default']:.1f} by default)")
            elif d.get("total") is not None:
                meta.append(f"score {d['total']:.1f} of 100")
            st.markdown(" · ".join(meta))
            if d["summary"]:
                st.markdown(f"**{md(d['summary'])}**")
            else:
                st.caption(data.NOT_RESEARCHED)
            ct = d.get("contact") or {}
            st.markdown(f"**How to contact:** {md(ct.get('best') or contacts.NONE_LABEL)}"
                        + ("" if ct.get("contactable") else f" ({md(ct.get('contact_reason'))})"))
            flags_block(d["flags"])
        with side:
            status_editor(d, db_path, tag)
        cols = st.columns(4, gap="large")
        for col, field in zip(cols, data.PROB_FIELDS):
            with col:
                prob_block(d, field, groups)
        with cols[3]:
            impact_block(d, groups)
        left, right = st.columns([3, 2], gap="large")
        with left:
            contact_block(d)
            paths_block(d)
            engagement_block(d["engagement"], heading=True)
            other_evidence(groups)
            math_expander(d, row, formula)
        with right:
            history_block(d, run)
            side_facts(d)
            profile_expander(d)


# ---------------------------------------------------------------- weights: one list, the Manual weights panel

@st.cache_resource(ttl=300, show_spinner=False, max_entries=4)
def list_source(db_path: str, run_id: int) -> tuple:
    """Stored factors (unrounded) and the export rows of a scored run, read once: a slider move or Run never re-queries.
    Cleared when a status is saved, re-read after 5 minutes. Treated as read-only (rerank and export.reorder copy)."""
    conn = data.open_db(db_path)
    try:
        return data.ranked_table(conn, run_id, precise=True), export.rows_for_run(conn, run_id)
    finally:
        conn.close()


def _set_weights(w: dict) -> None:
    for k, v in weights.normalize(w).items():
        st.session_state[f"tw_{k}"] = float(v)


def _load_preset(presets: dict) -> None:
    name = st.session_state.get("tune_preset")
    if name in presets:
        _set_weights(presets[name])


def _save_preset() -> None:
    w = {k: st.session_state.get(f"tw_{k}", v) for k, v in weights.DEFAULT_WEIGHTS.items()}
    as_default = bool(st.session_state.get("tune_as_default"))
    try:
        weights.save_preset(st.session_state.get("tune_name") or "", w, as_default=as_default)
    except (ValueError, OSError) as e:
        st.session_state["_tune_error"] = f"Weights not saved: {e}"
        return
    st.toast(f"Saved to config/weights.yaml{' as the default for scheduled runs' if as_default else ''}.",
             icon=":material/check:")


def keep_tune_state() -> None:
    """Streamlit drops a widget's value on a run where it is not drawn (the sliders while Manual weights is off, or the
    whole list while another tab shows): re-assigning the switch, the weights and the filters at the top of every run
    keeps the operator's settings. Other views read them only through weights.effective(st.session_state)."""
    for k in [k for k in st.session_state if str(k).startswith(("tw_", "tf_")) or k == "manual_weights"]:
        st.session_state[k] = st.session_state[k]


def weights_caption(lst: dict, secs: float) -> str:
    """The line under the run line: which weights rank the list."""
    if not lst["manual"]:
        src = ("the default weights (SCORING.md)" if lst["default_label"] == weights.DEFAULT_LABEL
               else f"the {md(lst['default_label'])}")
        return f"Ranked with {src} · **Manual weights** to change them"
    diff = [f"{weights.LABELS[k]} {v:g}" for k, v in lst["weights"].items() if v != lst["default"][k]]
    if not diff:
        return "Manual weights: still the defaults. Move a slider and press **Run**."
    return (f"Ranked with manual weights: {md(', '.join(diff))} (the rest at default) · re-ranked in {secs:.2f} s from "
            "stored factors, no cost · *vs default* is the move against the default weights")


def weights_form(default_w: dict) -> None:
    """The Manual weights panel: presets and Save on top, the three slider groups (pre-filled with the current values,
    the defaults the first time) and Run. Keys tw_<name>; the list re-ranks on Run, or at once on Reset and Load."""
    with st.container(border=True):
        presets = weights.load_file()["presets"]
        b1, b2, b3 = st.columns([1.2, 2, 1.2], vertical_alignment="bottom")
        b1.button("Reset to defaults", on_click=_set_weights, args=(default_w,), width="stretch",
                  help="Every weight back to the default the list uses without manual weights; the list follows at once.")
        if presets:
            with b2:
                p1, p2 = st.columns([3, 1], vertical_alignment="bottom")
                p1.selectbox("Preset", list(presets), key="tune_preset")
                p2.button("Load", on_click=_load_preset, args=(presets,), width="stretch")
        with b3.popover("Save weights", icon=":material/save:", width="stretch"):
            st.text_input("Name", key="tune_name", placeholder="e.g. the operator, attention first")
            st.checkbox("Use as default for scheduled runs", key="tune_as_default",
                        help="Writes `default:` in config/weights.yaml: the daily scoring uses these weights from the next run.")
            st.button("Save", on_click=_save_preset, type="primary")
        with st.form("weights_form", border=False):
            cols = st.columns(3, gap="large")
            for col, (title, keys) in zip(cols, WEIGHT_GROUPS):
                with col:
                    st.markdown(f"**{title}**")
                    for k in keys:
                        if k not in weights.LABELS:   # a weight weights.py does not have (yet): no slider
                            continue
                        st.slider(weights.LABELS[k], 0.0, weights.WEIGHT_MAX, step=0.1, key=f"tw_{k}",
                                  help=weights.HELP.get(k))
            c1, c2 = st.columns([4, 1], vertical_alignment="center")
            c1.caption("0 leaves a factor out, 1 is the default, 2 counts it twice as hard. **Run** re-ranks from the "
                       "stored scores in well under a second: no pipeline, no API, no cost.")
            c2.form_submit_button("Run", type="primary", icon=":material/play_arrow:", width="stretch")


def below_list(lst: dict, rows: list[dict], run: dict) -> None:
    """Under the list: the category mix of the top N (the brief asks for a reasonable mix), who entered or dropped out
    against the default under manual weights, and the downloads of the top N."""
    ranked, top, n, manual = lst["ranked"], lst["top"], lst["n"], lst["manual"]
    if len(top) == 0:
        return
    mix = weights.category_mix(ranked, n)
    st.markdown(f"##### Category mix of the top {len(top)}")
    for col, cat in zip(st.columns(len(mix)), sorted(mix, key=lambda c: (-mix[c]["now"], c))):
        delta = mix[cat]["now"] - mix[cat]["default"]
        col.metric(data.category_label(cat), mix[cat]["now"], delta=delta if manual and delta else None,
                   help=f"{mix[cat]['default']} with the default weights." if manual else None)
    if manual:
        entered, dropped = weights.movers(ranked, n)
        e_col, d_col = st.columns(2, gap="large")
        for col, title, frame, empty in ((e_col, f"Entered the top {n}", entered, "Nobody new."),
                                         (d_col, f"Dropped out of the top {n}", dropped, "Nobody dropped.")):
            with col:
                st.markdown(f"##### {title} ({len(frame)})")
                if len(frame) == 0:
                    st.caption(empty)
                    continue
                st.dataframe(pd.DataFrame({
                    "Person": [data.person_link(a, b, c) for a, b, c in zip(frame["name"], frame["handle"], frame["handle_verified"])],
                    "Category": [data.category_label(c) for c in frame["category"]],
                    "Now": frame["rank"].astype(int).tolist(), "Default": frame["rank_default"].astype(int).tolist(),
                }), hide_index=True, width="stretch", height=HEADER_PX + 35 * min(len(frame), 8) + 2,
                    column_config={"Person": st.column_config.LinkColumn("Person", display_text=data.PERSON_LINK_TEXT)})
    out = export.reorder(rows, top[["candidate_id", "rank", "total", "rank_default"]].to_dict("records"))
    stamp = f"run{run['run_id']}_top{len(out)}" + ("_manual" if lst["differs"] else "")
    d1, d2, _ = st.columns([1, 1, 2])
    d1.download_button("Download CSV", export.csv_text(out), file_name=f"wishlist_{stamp}.csv", mime="text/csv",
                       on_click="ignore", icon=":material/download:", width="stretch")
    d2.download_button("Download wishlist.json", json.dumps(out, ensure_ascii=False, indent=1, default=str),
                       file_name=f"wishlist_{stamp}.json", mime="application/json", on_click="ignore",
                       icon=":material/download:", width="stretch",
                       help="The map's wishlist shape (as data/exports/wishlist.json), the top N as ranked here, "
                            "under the list's filters (the brief's bar and US only by default). Like the pipeline's "
                            "export, it leaves out people rejected by hand.")


# ---------------------------------------------------------------- page

def render(conn, db_path: Path, tag: str, sidebar_box) -> None:
    for key in ("_status_error", "_tune_error"):
        if key in st.session_state:
            st.error(st.session_state.pop(key))
    run = data.latest_scored_run(conn)
    if run is None:
        empty_state(db_path, data.last_run(conn), exists=True)
        return
    changes = data.changes_since_previous(conn, run["run_id"])
    cost_caption(sidebar_box, run, data.run_cost_summary(conn, run["run_id"]))
    change_expander(run, changes)
    default_w, _ = weights.effective({})
    for k, v in default_w.items():              # the sliders start at the default (the first time only)
        st.session_state.setdefault(f"tw_{k}", float(v))
    for k, v in weights.DEFAULT_FILTERS.items():
        st.session_state.setdefault(f"tf_{k}", v)
    t0 = time.perf_counter()
    df, rows = list_source(str(db_path), int(run["run_id"]))
    lst = data.rank_list(df, st.session_state)
    st.session_state["list_now"] = {"run_id": int(run["run_id"]), "text": data.list_count(   # the System tab's count
        len(lst["top"]), len(lst["ranked"]), lst["n"], bool(lst["filters"].get("bar")))}
    st.caption(weights_caption(lst, time.perf_counter() - t0))
    sig = f"{tag}_{run['run_id']}"
    f = control_row(lst["ranked"], sig)
    view = list_rows(lst, f)
    cards = st.session_state.get("view_mode") == "Cards"
    if cards:
        cid = wanted_person(view) if len(view) else None
        if cid is not None:   # on a phone the person replaces the list; Back returns to it
            st.button("← Back to the list", key="back_to_list", on_click=_open_person, args=(None,))
            detail_panel(conn, run, cid, db_path, tag, lst)
            return
    if lst["manual"]:
        weights_form(lst["default"])
    if cards:
        cards_section(view, lst, f, sig)
    else:
        cid = ranked_section(view, lst, f, sig)
        if cid is not None:
            detail_panel(conn, run, cid, db_path, tag, lst)
    below_list(lst, rows, run)


def password_gate() -> None:
    """DASHBOARD_PASSWORD set (the dashboard shared inside a company): nothing renders before the password, asked once
    per browser session. Unset (local use): no gate."""
    want = config.env("DASHBOARD_PASSWORD")
    if not want or st.session_state.get("gate_ok"):
        return
    st.title("wearable_wishlist wearable wishlist")
    given = st.text_input("Password", type="password", key="gate_password")
    if given and hmac.compare_digest(given.encode(), want.encode()):
        st.session_state["gate_ok"] = True
        st.rerun()
    if given:
        st.error("Wrong password.")
    st.stop()


def main() -> None:
    password_gate()
    st.title("wearable_wishlist wearable wishlist")
    st.caption("Who gets one of the 100 prototype wearables, best first: the chance they accept one, wear it every day and "
               "love it out loud, weighted by their impact if they do. Everyone on the list has at least one real way to be "
               "contacted; the best one is shown beside them, not scored.")
    sidebar_box = st.sidebar.container()
    db_path, is_demo = pick_db(sidebar_box)
    if is_demo:
        st.info("Demo data: built from fixtures by scripts/seed_demo_db.py. People, posts and scores are illustrative, "
                "not research.", icon=":material/science:")
    if not db_path.exists():
        empty_state(db_path, None, exists=False)
        return
    tag = hashlib.sha1(str(db_path.resolve()).encode()).hexdigest()[:8]
    try:
        conn = data.open_db(db_path)
    except sqlite3.Error as e:
        st.error(f"Could not open `{rel(db_path)}`: {e}")
        return
    keep_tune_state()
    try:
        # on_change="rerun" makes .open tell which tab shows, so only that one runs (the wishlist reads more of the DB)
        tab_list, tab_check, tab_system = st.tabs(["Wishlist", "Check an account", "System"], key="main_tab",
                                                  on_change="rerun")
        if tab_list.open:
            with tab_list:
                render(conn, db_path, tag, sidebar_box)
        if tab_check.open:
            with tab_check:
                render_check_tab(conn, db_path)
        if tab_system.open:
            with tab_system:
                render_system_tab(conn)
    except sqlite3.Error as e:
        st.error(f"Could not read `{rel(db_path)}`: {e}. If the pipeline is writing right now, reload in a moment.")
    finally:
        conn.close()


main()
