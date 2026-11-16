"""Orchestrator. Usage:
  python -m ring_wishlist.pipeline run --stage all|ingest|crawl|resolve|discover|expand|enrich|classify|verify|paths|sample|score|export
  python -m ring_wishlist.pipeline run --stage discover --query-groups evidence    # only some queries.yaml groups (discover.QUERY_GROUPS)
  python -m ring_wishlist.pipeline run --stage enrich,classify --new-since 31      # only people first seen since run 31 began
  python -m ring_wishlist.pipeline costs          # print the cost ledger summary and rewrite COSTS.md
Order matters: crawl before resolve (handles are matched against Carlo's follow lists), classify before verify
(verification covers the people headed for the list), paths before score (reach comes from the best path), sample
between them (it samples the wishlist, whose bar needs a way to contact them; the engagement KPIs read its samples).
Two X sources: SOURCE_BULK (default monid: crawl, topic search, evidence posts, engagement samples) and SOURCE_VERIFY
(default x_api: official id checks). DRY_RUN=1 swaps both for the offline fixtures.
Every stage of a run is a run_stages row: 'running' at its start, then 'ok' or 'failed' with its seconds, cost and stats;
stages the caller chose not to run (ops.py: not due, or a monthly cap) and those a stop left unreached are 'skipped',
with the reason in `error`. schedule.py (what is due), ops.py (health) and the dashboard's System tab read them."""
from __future__ import annotations

import argparse
import inspect
import logging
import sys
import time

from . import config, costs, db

log = logging.getLogger("ring_wishlist.pipeline")
STAGES = ["ingest", "crawl", "resolve", "discover", "expand", "enrich", "classify", "verify", "paths", "sample", "score",
          "export"]
FATAL = {"ingest", "classify", "score"}


def _call(fn, *args, **kwargs):
    """Call a stage passing only the keyword arguments its signature accepts (stages evolve independently)."""
    params = inspect.signature(fn).parameters
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params and v is not None})


def _sources(conn, run_id):
    from .sources import get_source
    if config.dry_run():
        f = get_source("fixture")
        return f, f
    bulk = get_source(config.env("SOURCE_BULK", "monid"), conn=conn, run_id=run_id)
    verify = get_source(config.env("SOURCE_VERIFY", "x_api"), conn=conn, run_id=run_id)
    return bulk, verify


def _since(conn, new_since: str | None) -> str | None:
    """--new-since: a run id (that run's start) or an ISO time."""
    if not new_since:
        return None
    if str(new_since).isdigit():
        row = db.one(conn, "SELECT started_at FROM runs WHERE run_id=?", (int(new_since),))
        if not row:
            raise ValueError(f"--new-since: run {new_since} not found")
        return row["started_at"]
    return str(new_since)


def _new_enriched_ids(conn, since: str) -> list[int]:
    """Candidates first seen since `since` that enrichment has read, highest priority first (classify only these)."""
    from .discover import candidate_priorities
    fresh = {r["candidate_id"] for r in db.rows(conn, "SELECT candidate_id FROM candidates WHERE first_seen >= ?", (since,))}
    return [s["candidate_id"] for s in candidate_priorities(conn) if s["candidate_id"] in fresh and s["enriched_on"]]


def stage_row(conn, run_id: int, stage: str, status: str, **cols) -> None:
    """Write (or update) this run's run_stages row for `stage`: status plus any of started_at, finished_at, seconds,
    cost_usd, result_json, error."""
    db.upsert(conn, "run_stages", {"run_id": run_id, "stage": stage, "status": status, **cols}, keys=("run_id", "stage"))
    conn.commit()


def _run_spend(conn, run_id: int) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM costs WHERE run_id=?", (run_id,)).fetchone()[0] or 0)


def _stage_done(conn, run_id: int, st: str, result, t0: float, spent0: float) -> None:
    err = result.get("error") if isinstance(result, dict) else None
    stage_row(conn, run_id, st, "failed" if err else "ok", finished_at=db.utcnow(), seconds=round(time.time() - t0, 2),
              cost_usd=round(_run_spend(conn, run_id) - spent0, 6), result_json=db.jdumps(result), error=err)


def run(stage: str = "all", seed: str | None = None, max_candidates: int = 200, budget: float | None = None,
        from_raw: bool = True, max_following: int | None = None, max_followers: int = 2000, enrich_top: int = 80,
        posts_per_user: int = 30, verify_top: int = 100, workers: int = 4, expand_seeds: int = 12,
        query_groups: list[str] | None = None, new_since: str | None = None, sample_top: int = 0,
        sample_posts: int = 30, notes: str = "", skipped: dict[str, str] | None = None) -> dict:
    """Run `stage` (all, one, or a comma list) as one runs row with `notes`. `skipped` ({stage: why}): stages the caller
    decided not to run, recorded as 'skipped' run_stages rows."""
    conn = db.connect()
    run_id = db.new_run(conn, stage, notes)
    stages = STAGES if stage == "all" else [s.strip() for s in stage.split(",")]
    for st, why in (skipped or {}).items():
        stage_row(conn, run_id, st, "skipped", error=why)
    results: dict = {}
    bulk = verify = None
    seed_handle = seed or config.carlo_handle()
    log.info("run %s stages=%s bulk=%s verify=%s llm=%s dry_run=%s", run_id, ",".join(stages),
             config.env("SOURCE_BULK", "monid"), config.env("SOURCE_VERIFY", "x_api"), config.llm_backend(), config.dry_run())
    for i, st in enumerate(stages):
        t0, spent0 = time.time(), _run_spend(conn, run_id)
        stage_row(conn, run_id, st, "running", started_at=db.utcnow())
        stop = None
        try:
            if st in ("crawl", "resolve", "discover", "expand", "enrich", "verify", "sample") and bulk is None:
                bulk, verify = _sources(conn, run_id)
            if st == "ingest":
                from .ingest_map import run_ingest
                results[st] = _call(run_ingest, conn, run_id, from_raw=from_raw)
            elif st == "crawl":
                from .discover import run_crawl
                if not seed_handle:
                    raise RuntimeError("CARLO_X_HANDLE missing in .env (or pass --seed)")
                results[st] = _call(run_crawl, conn, run_id, bulk, seed_handle, max_following=max_following,
                                    max_followers=max_followers)
            elif st == "resolve":
                from .resolve_handles import run_resolve_handles
                results[st] = _call(run_resolve_handles, conn, run_id, verify, seed_handle=seed_handle)
            elif st == "discover":
                from .discover import run_discover
                results[st] = _call(run_discover, conn, run_id, bulk, budget_usd=budget, seed_handle=seed_handle,
                                    groups=tuple(query_groups) if query_groups else None)
            elif st == "expand":
                from .expand import run_expand
                results[st] = _call(run_expand, conn, run_id, bulk, seeds=expand_seeds, max_following=max_following,
                                    budget_usd=budget, seed_handle=seed_handle)
            elif st == "enrich":
                from .discover import run_enrich
                results[st] = _call(run_enrich, conn, run_id, bulk, top=enrich_top, posts_per_user=posts_per_user,
                                    budget_usd=budget, first_seen_since=_since(conn, new_since))
            elif st == "classify":
                from .classify import run_classify
                since = _since(conn, new_since)
                ids = _new_enriched_ids(conn, since) if since else None
                results[st] = _call(run_classify, conn, run_id, candidate_ids=ids, max_candidates=max_candidates,
                                    workers=workers)
            elif st == "verify":
                from .verify import run_verify
                results[st] = _call(run_verify, conn, run_id, verify, top=verify_top)
            elif st == "paths":
                from .paths import run_paths
                results[st] = _call(run_paths, conn, run_id, seed_handle=seed_handle)
            elif st == "sample":
                from .sample import run_sample
                results[st] = _call(run_sample, conn, run_id, bulk, top=sample_top, posts=sample_posts, budget_usd=budget)
            elif st == "score":
                from .score import run_score
                results[st] = _call(run_score, conn, run_id)
            elif st == "export":
                from .export import run_export
                out_dir = config.EXPORTS_DIR / "dry_run" if config.dry_run() else config.EXPORTS_DIR
                results[st] = _call(run_export, conn, run_id, out_dir=out_dir)
            else:
                raise ValueError(f"unknown stage {st}")
            log.info("stage %-8s ok in %5.1fs -> %s", st, time.time() - t0, results[st])
        except costs.BudgetExceeded as e:
            log.error("stage %s stopped by budget guard: %s", st, e)
            results[st] = {"error": str(e), "budget_stop": True}
            stop = "was stopped by the budget guard"
        except (ImportError, AttributeError) as e:
            log.error("stage %s not available yet (%s)", st, e)
            results[st] = {"error": f"not available: {e}"}
        except Exception as e:
            log.exception("stage %s failed: %s", st, e)
            results[st] = {"error": str(e)}
            if st in FATAL:
                stop = "failed"
        except BaseException as e:   # Ctrl-C, or SIGTERM under `ops serve`: drop the stage's open writes, close the run
            conn.rollback()
            results[st] = {"error": f"interrupted: {str(e) or type(e).__name__}"}
            _stage_done(conn, run_id, st, results[st], t0, spent0)
            for rest in stages[i + 1:]:
                stage_row(conn, run_id, rest, "skipped", error=f"not run: the run was interrupted during {st}")
            db.finish_run(conn, run_id)
            raise
        _stage_done(conn, run_id, st, results[st], t0, spent0)
        if stop:
            for rest in stages[i + 1:]:
                stage_row(conn, run_id, rest, "skipped", error=f"not run: {st} {stop}")
            break
    db.finish_run(conn, run_id)
    results["run_id"] = run_id
    results["cost_usd"] = db.one(conn, "SELECT cost_usd FROM runs WHERE run_id=?", (run_id,))["cost_usd"]
    write_costs_md(conn)
    return results


def write_costs_md(conn) -> None:
    if config.dry_run():
        return  # fixture runs never overwrite the real spend report
    rows = costs.summary(conn)
    runs = db.rows(conn, "SELECT run_id, started_at, stage, cost_usd FROM runs ORDER BY run_id DESC LIMIT 20")
    total = sum(r["cost_usd"] or 0 for r in rows)
    lines = ["# Costs", "", "Generated by `python -m ring_wishlist.pipeline costs`. Every paid call is a row in the `costs` table;",
             "budgets per provider and per run are set in `.env` (`BUDGET_X_API_USD`, `BUDGET_MONID_USD`, `BUDGET_ANTHROPIC_USD`).", "",
             f"Total spent so far: **{costs.fmt(total)}**", "", "| provider | endpoint | calls | units | cost |", "|---|---|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['provider']} | {r['endpoint']} | {r['calls']} | {r['units'] or 0} | {costs.fmt(r['cost_usd'] or 0)} |")
    lines += ["", "## Runs", "", "| run | started | stage | cost |", "|---|---|---|---:|"]
    for r in runs:
        lines.append(f"| {r['run_id']} | {r['started_at']} | {r['stage']} | {costs.fmt(r['cost_usd'] or 0)} |")
    lines += ["", "## Unit prices used", "", "| item | price |", "|---|---|"]
    for k, v in costs.X_PRICES.items():
        lines.append(f"| X API {k} | ${v} per resource |")
    lines.append("| monid / Apify actors | read from `inspect` at run time, cached in `data/cache/monid_endpoints.json` |")
    (config.ROOT / "COSTS.md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ring_wishlist.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--stage", default="all", help="all, one stage, or a comma list: " + ",".join(STAGES))
    r.add_argument("--seed", help="X handle to crawl from (default CARLO_X_HANDLE)")
    r.add_argument("--max-candidates", type=int, default=200, help="classify at most N candidates per run")
    r.add_argument("--budget", type=float, help="USD cap for discover/enrich/sample (on top of the per-provider caps in .env)")
    r.add_argument("--max-following", type=int, default=None, help="accounts per following list (crawl 2000, expand 1000)")
    r.add_argument("--expand-seeds", type=int, default=12, help="expand: how many top-ranked people seed the co-follow count")
    r.add_argument("--query-groups", help="discover: only these queries.yaml groups (comma list of topics, competitor_products, "
                                          "ring_wishlist, authors, evidence)")
    r.add_argument("--new-since", help="enrich/classify: only people first seen since this run id (or ISO time)")
    r.add_argument("--max-followers", type=int, default=2000)
    r.add_argument("--enrich-top", type=int, default=80)
    r.add_argument("--posts-per-user", type=int, default=30)
    r.add_argument("--verify-top", type=int, default=100)
    r.add_argument("--sample-top", type=int, default=0, help="sample: best-scored people sampled besides the wishlist (default none)")
    r.add_argument("--sample-posts", type=int, default=30, help="sample: latest original posts per person")
    r.add_argument("--workers", type=int, default=4, help="concurrent LLM calls in classify")
    r.add_argument("--refresh-map", action="store_true", help="re-fetch the map from network.ri.fo (needs MAP_PASSPHRASE)")
    r.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("costs")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(a, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    if a.cmd == "costs":
        conn = db.connect()
        write_costs_md(conn)
        for row in costs.summary(conn):
            print(row)
        return 0
    bad = [s for s in a.stage.split(",") if s != "all" and s not in STAGES]
    if bad:
        ap.error(f"unknown stage(s): {bad}")
    res = run(a.stage, seed=a.seed, max_candidates=a.max_candidates, budget=a.budget, from_raw=not a.refresh_map,
              max_following=a.max_following, max_followers=a.max_followers, enrich_top=a.enrich_top,
              posts_per_user=a.posts_per_user, verify_top=a.verify_top, workers=a.workers, expand_seeds=a.expand_seeds,
              query_groups=[g.strip() for g in a.query_groups.split(",") if g.strip()] if a.query_groups else None,
              new_since=a.new_since, sample_top=a.sample_top, sample_posts=a.sample_posts)
    print("\n== run", res.pop("run_id"), "cost", costs.fmt(res.pop("cost_usd") or 0))
    failed = [k for k, v in res.items() if isinstance(v, dict) and v.get("error")]
    for k, v in res.items():
        print(f"  {k:9s} {v}")
    if failed:
        print(f"\n!! stages with errors: {', '.join(failed)}")
    return 3 if failed else 0   # 3: a stage logged an error (the same code as `python -m ring_wishlist.ops run`)


if __name__ == "__main__":
    sys.exit(main())
