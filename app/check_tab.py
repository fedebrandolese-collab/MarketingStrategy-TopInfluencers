"""Dashboard tab "Check an account": would an account the operator has in mind make the wishlist, and where would it rank?

Type an X handle (with or without @, or an x.com / twitter.com link) and press Preview cost: the itemised estimate comes
from the adapters' own estimators and nothing is paid before "Accept $X.XX and run". The check then runs step by step in
a status box; src/wearable_wishlist/check.py does the work with the pipeline's own functions. A known account with recent posts and a
classification can be shown for free from stored data instead.

The answer reads in ten seconds: "@handle would rank #n of N" under the list's weights (weights.effective: the manual
sliders when switched on, else the scheduled default) and hard filters (tf_* keys, else weights.DEFAULT_FILTERS), the rank
under the default weights when they differ, the neighbours, the three chances with their strongest quote, impact, topic
affinity, community engagement, flags, how to contact them (a warm path, open X DMs, a researched public route, an email
in the X bio, or none) and what would move them up. Past checks reopen for free (ranked again under the current weights,
with today's contact routes). An account below the brief's bar (weights.hidden_by, the list's own filter source) is told
which part fails, not a person account, no evidence of wearing a device or using a voice tool, or no way to contact
them, and still placed.
"Keep off the wishlist" sets status rejected through data.set_status.

Entry point: render_check_tab(conn, db_path). engagement_block is the Wishlist person panel's too.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from wearable_wishlist import check, config, weights
from app import data

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~$])")
PAST_SHOWN = 12
FILTER_WORDS = {"us_only": "US only", "hide_competitor": "hide competitor-tied", "hide_rejected": "hide rejected",
                "bar": "the brief's bar"}
HIDDEN_WORDS = {"us_only": "not US-based, and the list shows US only",
                "hide_competitor": "tied to a competitor, and the list hides competitor-tied people",
                "hide_rejected": "rejected by hand, and the list hides rejected people",
                "bar": "{reason}, so below the brief's bar (a person who wears a device or uses a voice tool, and "
                       "can be contacted)"}


# ---------------------------------------------------------------- text helpers (as in dashboard.py)

def md(text) -> str:
    return "" if text is None else _MD_SPECIAL.sub(r"\\\1", " ".join(str(text).split()))


def link(label: str, url: str | None) -> str:
    if not url or not str(url).startswith(("http://", "https://")):
        return md(label)
    return f"[{md(label)}]({str(url).replace(' ', '%20').replace('(', '%28').replace(')', '%29')})"


def short(text, n: int = 220) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def utc(ts: str | None) -> str:
    return "unknown" if not ts else str(ts).replace("T", " ")[:16] + " UTC"


def usd(v: float | None, nd: int = 4) -> str:
    return f"\\${float(v or 0.0):,.{nd}f}"   # escaped: a bare $ pair renders as LaTeX


def who(nb: dict | None) -> str:
    if not nb:
        return ""
    label = f"@{nb['handle']}" if nb.get("handle") else (nb.get("name") or f"candidate {nb['candidate_id']}")
    return f"{md(label)} (#{nb['rank']})"


# ---------------------------------------------------------------- state

def list_filters() -> dict:
    """The list's hard filters from the session (Wishlist's tf_* keys), else the defaults."""
    s, d = st.session_state, weights.DEFAULT_FILTERS
    return {**{k: bool(s.get(f"tf_{k}", v)) for k, v in d.items() if k != "n"}, "n": int(s.get("tf_n", d["n"]) or d["n"])}


def _stamp(conn: sqlite3.Connection) -> str:
    """Changes whenever a status is saved, so the cached table follows 'hide rejected'."""
    r = conn.execute("SELECT COALESCE(MAX(updated_at), '') || COUNT(*) FROM status").fetchone()
    return str(r[0]) if r else ""


@st.cache_resource(show_spinner=False, max_entries=6)
def run_table(db_path: str, run_id: int, stamp: str) -> pd.DataFrame:
    """The compared run's people with their stored factors (unrounded), read once per status change."""
    conn = data.open_db(db_path)
    try:
        return data.ranked_table(conn, run_id, precise=True)
    finally:
        conn.close()


def _go(handle: str, mode: str) -> None:
    st.session_state["ck_go"] = {"handle": handle, "mode": mode}


def _open(check_id: int | None) -> None:
    st.session_state["ck_open"] = check_id


def _pick_past(key: str, ids: list[int]) -> None:
    rows = st.session_state[key].selection.rows if key in st.session_state else []
    if rows:
        st.session_state["ck_open"] = ids[rows[0]]


def _keep_off(db_path: str, cid: int) -> None:
    conn = data.open_db(db_path)
    try:
        data.set_status(conn, cid, "rejected", note=check.KEEP_OFF_NOTE)
    except (ValueError, sqlite3.Error) as e:
        st.session_state["ck_error"] = f"Status not saved: {e}"
        return
    finally:
        conn.close()
    st.cache_resource.clear()   # every cached table reads status ('hide rejected')
    st.toast("Kept off the wishlist: status rejected.", icon=":material/block:")


# ---------------------------------------------------------------- preview (nothing spent)

def preview(conn: sqlite3.Connection, handle: str, busy: bool) -> None:
    est = check.estimate(conn, handle)
    k = check.known(conn, handle)
    total = est["total_usd"]
    st.markdown(f"##### Checking @{md(handle)} costs about {usd(total, 3)}")
    st.dataframe(pd.DataFrame([{"Step": i["what"], "Source": i["provider"], "Up to": i["units"], "Cost": f"${i['usd']:.4f}",
                                "Note": i["note"]} for i in est["items"]]),
                 hide_index=True, width="stretch")
    note = ("Nothing is paid before you accept. Every paid call goes through the budget guards into the cost ledger, "
            "as its own run. It takes under a minute (the Claude call is the slow part): stay on this tab while it runs.")
    if est["dry_run"]:
        note = f"DRY_RUN=1: fixtures, nothing is billed. Live, this check costs about {usd(est['live_total_usd'], 3)}. " + note
    st.caption(note)
    accept = f"Accept ${total:.2f} and run"
    if k and k["fresh"]:
        st.info(f"@{md(handle)} is already known: {k['posts']} posts fetched by {k['last_fetch']}, classified "
                f"{(k['classified_at'] or '')[:10]}. Show it now for free from stored data, or re-check (paid).",
                icon=":material/inventory_2:")
        b1, b2, _ = st.columns([1, 1, 1])
        b1.button("Show from stored data (free)", type="primary", on_click=_go, args=(handle, "show"), disabled=busy,
                  icon=":material/visibility:", width="stretch")
        b2.button(f"Re-check: {accept.lower()}", on_click=_go, args=(handle, "paid"), disabled=busy,
                  icon=":material/payments:", width="stretch")
        return
    if k:
        why = "no classification yet" if not k["classified_at"] else f"posts last fetched {k['last_fetch'] or 'never'}"
        st.caption(f"Already a candidate (#{k['candidate_id']}), but {why}: the check reads their posts again.")
    st.button(accept, type="primary", on_click=_go, args=(handle, "paid"), disabled=busy, icon=":material/payments:")


def run(conn: sqlite3.Connection, db_path: Path, table: pd.DataFrame, go: dict, w: dict, w_label: str, base: dict,
        filters: dict) -> None:
    handle, mode = go["handle"], go["mode"]
    title = f"Checking @{handle}" if mode == "paid" else f"@{handle} from stored data"
    with st.status(title + "…", expanded=True) as box:
        def say(msg: str) -> None:
            box.write(md(msg))
        try:
            if mode == "show":
                ans = check.show_stored(conn, handle, table=table, w=w, filters=filters, baseline=base,
                                        weights_label=w_label, say=say)
            else:
                ans = check.run_check(conn, handle, table=table, w=w, filters=filters, baseline=base,
                                      weights_label=w_label, say=say)
        except (ValueError, sqlite3.Error) as e:
            box.update(label=f"@{handle}: {e}", state="error")
            return
        spent = f"{float(ans.get('cost_usd') or 0.0):.4f}"
        if ans.get("ok"):
            box.update(label=f"@{handle} checked · ${spent} spent", state="complete", expanded=False)
        else:
            box.update(label=f"@{handle}: stopped · ${spent} spent", state="error", expanded=True)
    st.session_state["ck_open"] = ans["check_id"]


# ---------------------------------------------------------------- the answer

def engagement_block(e: dict, heading: bool = False) -> None:
    """Community engagement (data.engagement_view): the one-cell summary and the score factor, the average post against
    accounts of their size, the arithmetic and their pace; one line when unknown. Shared with the Wishlist's person panel
    (heading=True: an h5 like the panel's other blocks)."""
    if not e["known"]:
        st.caption(md(e["unknown"]))
        return
    st.markdown(("##### Community engagement\n\n" if heading else "**Community engagement** · ") + md(e["headline"]))
    if e.get("table") is not None:
        st.table(e["table"], width="content")
    lines = [md(t) for t in (e.get("reasons") or []) or ([e["pace"]] if e.get("pace") else [])]   # reasons end with the pace
    if lines:
        st.caption("  \n".join(lines))


def _because_line(b: dict) -> str:
    head = f"- **{b['label']}** · {b['value'] * 100:.0f}% · "
    q = b.get("quote")
    if q:
        src = link(f"{(q.get('kind') or 'post').replace('_', ' ')}, {q.get('date') or 'undated'}", q.get("url"))
        more = f" ({md(b['note'])})" if b["note"].endswith("quotes") else ""
        return head + f"“{md(short(q.get('text')))}” ({src}){more}"
    e = b.get("event")
    if e:
        return head + f"{md(short(e.get('text') or e.get('kind')))} ({link(e.get('date') or 'event', e.get('url'))})"
    line = head + md(b["note"])
    a = b.get("against")
    if a:
        line += f"; against: “{md(short(a.get('text'), 140))}” ({link(a.get('date') or 'source', a.get('url'))})"
    return line


def _contact_lines(ans: dict, contact: dict) -> str:
    """How to contact them: the best route (a warm path with its strength and hops), then the others with their source;
    or none, and that it fails the brief's bar on reach."""
    if not contact.get("contactable"):
        return (f"**How to contact** · {md(contact.get('contact_reason') or weights.BAR_NO_CONTACT)}. "
                "They fail the brief's bar on reach: everyone on the list needs a real way to be contacted.")
    routes = contact.get("routes") or []
    text = f"**How to contact** · {md(contact.get('best'))}"
    warm = next((p for p in ans.get("paths") or [] if p.get("how") in ("direct", "via")), None)
    if routes and routes[0]["kind"] == "warm" and warm:   # a warm way in: how strong, and each hop in words
        row = {"how": warm.get("how"), "strength": warm.get("strength"), "via_name": warm.get("via"),
               "you_rel": warm.get("you"), "target_rel": warm.get("target"), "json_obj": warm}
        pv = data.path_view(row, ans.get("name"))
        text += f" · {md(pv['strength_word'])}" + "".join(f"  \n{md(lab)}" for lab in pv["labels"][:2])
    for r in routes[1:4]:
        text += f"  \nalso: {md(r['label'])}" + (f" ({link('source', r['source_url'])})" if r.get("source_url") else "")
    return text


def answer(conn: sqlite3.Connection, db_path: Path, check_id: int, w: dict, w_label: str, base: dict, filters: dict) -> None:
    ans = check.load_check(conn, check_id)
    if not ans:
        return
    st.divider()
    if not ans.get("ok"):
        st.error(f"**@{md(ans['handle'])}**: {md(ans.get('reason') or 'the check stopped')}", icon=":material/person_off:")
        st.caption(f"Checked {utc(ans.get('checked_at'))} · cost {usd(ans.get('cost_usd'))}")
        return
    cid = int(ans["candidate_id"])
    contact = check.contact_now(conn, ans)   # today's routes: research that landed after the check counts
    person = check.with_bar({**ans["person"], "status": data.get_status(conn, cid)["status"]}, ans.get("because"), contact)
    compared = ans.get("compared_run_id")
    table = run_table(str(db_path), int(compared), _stamp(conn)) if compared is not None else None
    rv = check.rank_view(table, person, w, filters, base) if table is not None and len(table) else ans.get("rank_at_check")
    if not rv:
        st.warning("No scored run to rank against.")
        return
    handle_md = link(f"@{ans['handle']}", ans.get("x_url"))
    st.markdown(f"### {handle_md} would rank #{rv['rank']} of {rv['n']}")
    active = [FILTER_WORDS[k] for k in FILTER_WORDS if rv["filters"].get(k)]
    how = "from stored data, free" if ans.get("mode") == "stored" else f"check cost {usd(ans.get('cost_usd'))}"
    st.caption(f"{md(ans['name'])} · {data.category_label(person.get('category'))} · {md(w_label)} · filters: "
               f"{', '.join(active) or 'none'} · against run {compared} ({ans.get('compared_run_date') or '?'}) · "
               f"checked {utc(ans.get('checked_at'))} · {how}")
    lines = []
    if rv.get("weights_differ"):
        lines.append(f"#{rv['rank_default']} of {rv['n']} under the default weights.")
    if rv["above"] and rv["below"]:
        lines.append(f"Between {who(rv['above'])} and {who(rv['below'])}.")
    elif rv["below"]:
        lines.append(f"Top of the list, ahead of {who(rv['below'])}.")
    elif rv["above"]:
        lines.append(f"Last of {rv['n']}, after {who(rv['above'])}.")
    if not rv["hidden_by"] and rv["rank"] > rv["list_size"]:
        lines.append(f"Outside the top {rv['list_size']} the list shows.")
    st.markdown("  \n".join(lines))
    if rv["hidden_by"]:
        reason = person.get("bar_reason") or weights.BAR_NO_EVIDENCE
        st.warning("Filtered out of the list: " + "; ".join(HIDDEN_WORDS[h].format(reason=reason) for h in rv["hidden_by"])
                   + ". The rank above is where they would stand with that filter off.", icon=":material/filter_alt_off:")
    latest = data.latest_scored_run(conn)
    if latest and compared is not None and latest["run_id"] > compared:
        now = conn.execute("SELECT rank FROM scores WHERE run_id=? AND candidate_id=?", (latest["run_id"], cid)).fetchone()
        st.caption(f"Run {latest['run_id']} has been scored since" + (f": it ranks them #{now[0]}." if now else "."))
    if ans.get("summary"):
        st.caption(md(ans["summary"]))

    st.markdown("**Because**  \n" + "\n".join(_because_line(b) for b in ans["because"]))
    imp, aff = ans.get("impact") or {}, ans.get("affinity") or {}
    bits = [f"{int(imp['followers']):,} followers on X" if imp.get("followers") is not None else "followers unknown",
            f"{imp['engagement']:,.0f} engagements per post" if imp.get("engagement") is not None else None,
            f"{imp.get('posts') or 0} post{'' if imp.get('posts') == 1 else 's'} read"]
    iv, av = imp.get("value"), aff.get("value")
    iv_txt = "unknown" if iv is None else f"{float(iv):.2f}"
    av_txt = "unknown" if av is None else f"{float(av):.2f}"
    st.markdown(f"**Impact if they love it** · {iv_txt} · " + " · ".join(md(b) for b in bits if b) + "  \n"
                + f"**Topic affinity** · {av_txt}" + (f" · {md(aff['reason'])}" if aff.get("reason") else ""))
    # the factor as score.py stores it (reasons_json "engagement"), the value as the person row carries it
    engagement_block(data.engagement_view(person.get("engagement"), ans.get("engagement")))
    for f in ans.get("flags") or []:
        if f in data.TABLE_HIDDEN_FLAGS:
            continue
        text = f"**{md(data.flag_short(f))}**: {md(data.flag_explanation(f, ans.get('candidate'), ans.get('x_user')))}"
        (st.warning if f.startswith(("competitor", "unverified_handle")) else st.info)(text, icon=":material/flag:")
    st.markdown(_contact_lines(ans, contact))
    st.markdown(f"**What would move them up** · {md(ans.get('moves'))}")
    if "ck_error" in st.session_state:
        st.error(st.session_state.pop("ck_error"))
    if person["status"] == "rejected":
        note = data.get_status(conn, cid).get("note")
        st.caption("Kept off the wishlist (status rejected" + (f": {md(note)}" if note else "") + "). Change it in the "
                   "Wishlist's person panel.")
    else:
        st.button("Keep off the wishlist", on_click=_keep_off, args=(str(db_path), cid), icon=":material/block:",
                  help="Sets their status to rejected with the note “manual check: not for the list”. Scheduled runs "
                       "still score them, and rank them last.")


def past(conn: sqlite3.Connection) -> None:
    rows = check.past_checks(conn, PAST_SHOWN)
    if not rows:
        return
    st.markdown("##### Past checks")
    st.caption("Select one to reopen it, free: it is ranked again under the current weights.")
    df = pd.DataFrame([{
        "When": utc(r["created_at"]), "Account": f"@{r['handle']}",
        "Rank then": f"#{r['rank_current']} of {r['n']}" if r.get("rank_current") else ("not found" if not r.get("candidate_id") else "-"),
        "Cost": f"${float(r.get('cost_usd') or 0.0):.4f}", "Kind": "stored, free" if r.get("run_id") is None else "paid check",
        "Against run": r.get("compared_run_id"),
    } for r in rows])
    ids = [int(r["check_id"]) for r in rows]
    key = f"ck_past_{ids[0]}"   # a new check gets a fresh table, with no stale selection
    st.dataframe(df, hide_index=True, width="stretch", key=key, on_select=lambda: _pick_past(key, ids),
                 selection_mode="single-row")


# ---------------------------------------------------------------- the tab

def render_check_tab(conn: sqlite3.Connection, db_path: Path) -> None:
    run_info = data.latest_scored_run(conn)
    if run_info is None:
        st.info("Nothing is scored yet: run the pipeline first, then check accounts against the list.")
        return
    st.caption("Would an account you have in mind make the list? Type its X handle and preview the cost: nothing is paid "
               f"before you accept. The answer ranks it against run {run_info['run_id']} ({run_info['date']}) with the "
               "list's weights and filters, and says why.")
    w, w_label = weights.effective(st.session_state)
    base, _ = weights.effective({})
    filters = list_filters()
    go = st.session_state.pop("ck_go", None)
    with st.form("ck_form", border=False):
        c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
        c1.text_input("X handle or profile link", key="ck_input", placeholder="@icobrando02 or https://x.com/icobrando02")
        c2.form_submit_button("Preview cost", icon=":material/request_quote:", width="stretch")
    raw = st.session_state.get("ck_input") or ""
    handle = check.parse_handle(raw)
    if raw.strip() and not handle:
        st.error("Not an X handle: 1 to 15 letters, digits or underscores, with or without @, or an x.com link.")
    if handle:
        preview(conn, handle, busy=go is not None)
    if go:
        table = run_table(str(db_path), int(run_info["run_id"]), _stamp(conn))
        run(conn, db_path, table, go, w, w_label, base, filters)
    if st.session_state.get("ck_open"):
        answer(conn, db_path, int(st.session_state["ck_open"]), w, w_label, base, filters)
    past(conn)
    if config.dry_run():
        st.caption("DRY_RUN=1: checks use the offline fixtures.")
