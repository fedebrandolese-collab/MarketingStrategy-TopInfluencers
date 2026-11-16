
## A (ingest_map)
* The export lists id 167 (Andrew Yeung) in BOTH groups (sector `angel` in capital, `connector` in press). `map_people.id` is
  the primary key, so he is stored once, under `capital` (first group seen), with the tags of both records unioned. The
  press-group record stays in `data/raw/map_press_people.json`. Consequences: 319 person records -> 318 rows
  (`group_name`: 131 capital, 187 press). `run_ingest` returns `people_capital=131`, `people_press=188` (records
  processed), `people=318` (rows) and `people_in_both_groups=[167]`. If anyone needs "is in the press graph", use the
  `press` tag or the raw file, not `group_name`.
* `competitor_funds` is served identically with both groups; deduped on `(fund, competitor)` -> 12 rows.
* Beyond the contract signatures (all backwards compatible): `fetch_map(passphrase, url, session=None, timeout=30)`,
  `save_raw(payload, raw_dir=None)`, `load_raw(raw_dir=None)`, `raw_available()`, `ingest_payload(conn, payload, run_id)`
  (ingest an in-memory payload; what `run_ingest` calls), `summary_line(counts)`, `main(argv)`.
* `run_ingest(from_raw=True)` falls back to a live fetch only when `data/raw` is incomplete AND `MAP_PASSPHRASE` is set;
  otherwise it raises with instructions. `from_raw=False` requires `MAP_PASSPHRASE` (RuntimeError otherwise).
* People that disappear from a later export are counted in `stale_people` but never deleted (candidates may point at them).
* The standalone CLI opens its own `runs` row (stage `ingest`) and finishes it; the pipeline should call
  `run_ingest(conn, run_id)` with its own run_id instead of shelling out.
* Timelines currently carry only `linkedin` (events/enrich/summary are empty for all 318 people); nothing else is stored.
* No new dependencies. Tests read `data/raw` directly: if the lead refreshes the export, the exact counts in
  `tests/test_ingest_map.py::EXPECTED` and `test_stats` must be updated.
* `fetch_map` refuses to open a real HTTP session under `DRY_RUN=1` unless a session object is injected, so no test or
  dry run can reach network.ri.fo even with `MAP_PASSPHRASE` in `.env`. `save_raw` writes integral floats as ints
  (the API serves `"warmth": 0.0`, the browser export printed `0`), so a real `--refresh` only changes `exported_at`
  plus whatever actually moved in the map.

## B (x_api adapter) — notes for the lead, 2026-09-29

* `XApiSource(conn=None, run_id=None, session=None)` in `src/ring_wishlist/sources/x_api.py`; `session` is test injection only.
  Errors: `XApiError`, `XApiAuthError` (401/403), `XApiCreditsError` (402, credits empty), `XApiRateLimited` (429 after 3 waits),
  plus `costs.BudgetExceeded` from the guard. All are `RuntimeError` subclasses. `RuntimeError("X_BEARER_TOKEN missing ...")` on first use.
* Endpoint facts verified on docs.x.com (OpenAPI v2.168): page minimums are real -- recent search asks for >=10 posts, a user
  timeline >=5, so `search_posts(q, max_results=3)` still requests (and may pay for) 10. Full-archive `search/all` is used
  when `since` is older than 7 days (the recent endpoint rejects older `start_time`); if the token's access level does not
  include the archive it raises `XApiAuthError` with the 403 detail.
* `GET /2/users/search` is documented with user-context auth only (no BearerToken in its security list). The adapter tries
  the app-only bearer, and uses `X_USER_ACCESS_TOKEN` (an OAuth 2.0 user token with read scopes only) for that endpoint if
  present. Please add `X_USER_ACCESS_TOKEN=` to `.env.example` (config.py is frozen; the adapter reads it via `config.env`).
  If neither works, F's `run_resolve_handles` should fall back to `SOURCE=monid` for user search.
* Billing decision (please confirm after the first paid run): the pricing page bills "per resource returned"; whether
  objects returned through `expansions=author_id` are billed as user reads ($0.010) is not stated. `BILL_EXPANSIONS=True`
  counts them (projection: one author per post as an upper bound, so 100 posts project $1.50 not $0.50; ledger rows
  `<endpoint>#author_id` hold the distinct user objects actually returned). Compare the Developer Console with
  `costs.summary(conn)` after the first run and flip the constant if X billed posts only. D's budget maths should use
  `x_api.estimate("search_posts", n)` rather than `n * X_PRICES["post_read"]`.
* Caps: `.env` currently has `BUDGET_X_USD=5`. A single `get_following(h, max_users=1000)` projects $10.00 and is refused
  before any request; D's `run_crawl` defaults (2000 + 2000) need `BUDGET_X_USD>=40` or smaller `max_following/max_followers`.
  `search_users(name, max_results=10)` is $0.10 per map person (150 people = $15). `python -m ring_wishlist.sources.x_api` prints the table.
* `can_dm` is always NULL from this adapter (X API v2 does not expose DM permission to app-only auth), so F's `open_dm`
  path can only come from monid data. `get_user_posts` excludes retweets (`exclude=retweets`) but keeps replies, which
  paths needs for "replied to Carlo" (`in_reply_to_x_id` = the user id replied to, `mentions` = handles).
* Cache: user lookups are served from `x_users` when `fetched_at` is within 7 days (`CACHE_DAYS`); follows and posts are
  always fetched (X deduplicates repeat resources within a UTC day, so re-runs on the same day are free on their side).
* No new dependencies. Tests: `DRY_RUN=1 .venv/bin/python -m pytest -q tests/test_x_api.py` (23 tests, no network).

## G (dashboard + scheduler), notes and requests for the lead, 2026-09-29

> Superseded on 30 Sep for the scheduler: `src/ring_wishlist/ops.py`, `config/schedule.yaml` and `docs/OPERATIONS.md`
> replace the 07:00 `make run` refresh and the exit codes below (`ops run`: 0 ok, 3 a stage failed or was stopped
> by a budget, 75 locked, 2 crashed).

* Files: `app/dashboard.py` (new), `app/data.py` (extended), `scripts/refresh.sh`, `scripts/install_schedule.sh`,
  `scripts/com.ring-wishlist.wishlist.plist` (new), `scripts/seed_demo_db.py` (path variable renamed; the `p` shadowing was
  not live, it was confusing), `tests/test_dashboard_data.py` (24 tests, 3 of them run the page through Streamlit's AppTest).
* **Request (pipeline.py): exit non-zero when a stage fails.** `pipeline.main` logs a failed or budget-stopped stage
  and returns 0, so `make run` always succeeds. `refresh.sh` works around it by scanning this run's log lines for
  `ERROR ring_wishlist.pipeline: stage` or `{'error':` and then exits 3; a proper exit code would make that grep a fallback only.
* **Request (Makefile or a new `.streamlit/config.toml`, both outside G): serve the dashboard on localhost only.**
  Streamlit listens on all interfaces by default (checked: `Uvicorn server started on :::8765`, plus a "Network URL"
  and an "External URL", which means it looked up the public IP), so anyone on the same Wi-Fi can open Carlo's
  network data. Suggested: `dashboard: .venv/bin/streamlit run app/dashboard.py --server.address localhost --browser.gatherUsageStats false`,
  or `.streamlit/config.toml` with `[server] address = "localhost"` and `[browser] gatherUsageStats = false`.
* Optional Makefile targets: `refresh: bash scripts/refresh.sh` and `demo: $(PY) scripts/seed_demo_db.py`.
* Scheduler behaviour, for the README: `make schedule` copies the plist to `~/Library/LaunchAgents` with the repo path
  filled in and **`Disabled` set to true**, so nothing runs (not even at the next login) until Carlo runs the printed
  `launchctl load -w ~/Library/LaunchAgents/com.ring-wishlist.wishlist.plist` (stop with `launchctl unload -w ...`). Reason:
  with live keys in `.env`, every scheduled `make run` spends up to the budgets. The Linux cron line is at the top of
  `scripts/refresh.sh`. `refresh.sh` exit codes: 0 ok, 1 no `.venv`, 3 a stage logged an error, 75 skipped because
  another refresh holds the lock (`data/logs/.refresh.lock`; prevents double spend), otherwise the `make` exit code.
  It logs to `data/logs/refresh-YYYYMMDD-HHMM.log` (+ `refresh-latest.log` symlink), and when not on a terminal it
  prints one summary line (that is what lands in `data/logs/launchd.log`).
* Under launchd the `claude_cli` LLM backend needs the `claude` binary on PATH (refresh.sh adds `~/.local/bin`,
  `/opt/homebrew/bin`, `/usr/local/bin`) and a logged-in CLI; otherwise classify fails and the run exits 3.
* `app/data.py` additions (all pure, tested): `last_run`, `table_view` (display frame, probabilities as whole
  percents), `flag_short`, `status_label`, `path_view` (per-hop labels for "you → via → target"), `change_headline`,
  `change_row_text`, `NO_FLAG` filter option ("no flags"), `run_cost_summary(...)["since_previous_usd"|"to_date_usd"]`.
  `person_detail` now also reads score.py's stored `unknowns`, `summary` and flag reasons (`flags[i]["scoring"]`).
* Dashboard behaviour worth knowing: it never creates a database (a missing `DB_PATH` file shows the how-to-run
  message), opens one connection per rerun, writes only `status`, and supports a deep link `?person=<candidate_id>`.

## D (discover + classify), notes and requests for the lead, 2026-09-29

* Files: `src/ring_wishlist/discover.py`, `src/ring_wishlist/classify.py`, `src/ring_wishlist/prompts/classify.md`, `tests/test_discover.py`
  (12 tests), `tests/test_classify.py` (9 tests). `DRY_RUN=1 .venv/bin/python -m pytest -q tests/test_discover.py tests/test_classify.py`.
* Signatures (all extras are keyword arguments with defaults, so `pipeline._call` can pass only what it has):
  `run_crawl(conn, run_id, source, seed_handle, max_following=2000, max_followers=2000)`,
  `run_discover(conn, run_id, source, queries_file=config.QUERIES_FILE, budget_usd=None, seed_handle=None)`,
  `run_enrich(conn, run_id, source, top=80, posts_per_user=30, budget_usd=None, *, mode="search", refresh_days=30, seed_handle=None)`,
  `run_classify(conn, run_id, candidate_ids=None, max_candidates=200, seed_handle=None, workers=4, force=False)`,
  `candidate_priorities(conn, candidate_ids=None, seed_handle=None)`, `candidate_signals(conn, candidate_id, seed_handle=None)`.
* Budget semantics (discover and enrich): an explicit `budget_usd` caps that stage's own projected spend; with `None` the
  provider's per-run cap from `.env` applies, minus what the run already spent. The pipeline passes the same `--budget`
  to both, so each stage gets its own cap. Projections are source-aware (`discover._unit_cost`: monid via
  `sources.monid.estimate`, x_api via `sources.x_api.estimate`, fixture 0) and settle on the number of results returned.
* Bug fixed: `run_discover`, `candidate_signals` and `suggest_hubs` shadowed the module function `seed_x_id` with a
  local of the same name and raised `UnboundLocalError` on every call. `run_crawl` now asks the source for follow lists
  by handle (monid maps ids to handles only through `x_users`).
* `x_posts.query` now has three kinds (`discover.query_kind`): a queries.yaml topic query ('discovery'), run_enrich's
  `from:<h> (...)` device/voice search ('evidence') and `from:<h> -is:retweet` ('recent'). Only 'discovery' counts as
  topical by itself (`discover.post_is_topical`). No other module reads `query` today; whoever adds a topical count
  should call `post_is_topical(query, text)` rather than test `query IS NOT NULL`, or enrichment posts will inflate it.
* **Request (resolve_handles.py, F):** `upsert_map_candidate` overwrites `candidates.notes` on every run. discover keeps
  its bio hints, seed relation and the enrichment marker (`enriched:YYYY-MM-DD posts=N`) there as ' | '-separated
  `key:value` segments; overwriting them makes run_enrich pay again for map people it already enriched. Please merge
  instead, e.g. `from ring_wishlist.discover import _merge_notes` and `notes=_merge_notes(existing["notes"], note)` (your
  `map: ...` segment is replaced by key, other segments are kept).
* Classification threading: `llm.complete_json` runs on a `ThreadPoolExecutor` (`workers`, default 4); dossiers are
  built and every SQLite write happens on the calling thread. llm.py receives a `_CostRelay` as `conn`: `costs.guard`'s
  SELECT is answered from the run's spend plus what was relayed, `costs.record`'s INSERT is queued and written by the
  calling thread. **Suggestion (llm.py, H):** a `ledger=` callback in `complete_json` would make the relay unnecessary.
* Classify selection: `discover.candidate_priorities` order (same as enrich). Candidates classified in an earlier run are
  skipped unless their post count grew since (`_meta.posts_in_db` in `classifications.json`); `force=True` reclassifies.
  Classifications made before this change have no `posts_in_db`; for them "a post fetched after the classification" decides.
* Deterministic guards added in classify: an unambiguous non-US location now sets `us_based=0` (only US places -> 1,
  mixed or vague -> NULL, the LLM's evidenced answer only when the location says nothing); the LLM cannot claim
  `verified` for a profile without the badge; parody/fan accounts get category `other`. Re-classifying replaces this
  module's own `profile_signal` evidence rows (follower counts in their text go stale) and keeps the first `fetched_at`
  of post/bio evidence and events, so score's "added since the previous run" sees only new rows.
* Crawl filter (`discover.crawl_verdict`): parody, brand/outlet/bot (business/government badge, "official account", bio
  starting with We/Our), spam/NSFW/promo, no role and no topic word, fewer than 500 followers unless a mutual follow.
  Counts per hint and per skip reason are logged and returned (`by_hint`, `skipped`). Writes are batched (executemany).

## F (resolve_handles, paths, export) — notes for the lead, 2026-09-29

* `run_resolve_handles(conn, run_id, source, only_warm=True, max_people=150, force=False, max_paid_lookups=20,
  seed_handle=None)`. Methods, free first: `map_text` (an @handle written in the map's name/role/company; URLs and
  e-mails ignored; 0.98 when the profile's name is the person, 0.8/0.7 unconfirmed with `x_id` NULL when only the handle
  carries the name), `seed_follow_list` (tagged people on their tagged list), `seed_follow_list+strict` (everyone else on
  both lists: token_set_ratio >= 0.92 + company/role words in the bio, or the exact name with a unique hit; one-word
  names always need the bio), `local_x_users` (other x_users rows; the name gate plus a company/role word is required),
  then the paid fallback (`user_search`, `+company`). Free methods run for all map people; paid calls and new candidate
  rows only for the selected ones. Result keys: `per_method` {map_text, seed_follow_list_tagged,
  seed_follow_list_untagged, local_x_users, paid_fallback}, `unresolved`, `unresolved_selected`, `paid_lookups`,
  `paid_stopped`, `candidates_merged`. `resolved`, `searched` and `skipped_low_confidence` are kept as before.
* Paid calls: at most `max_paid_lookups` per run. `get_user_by_handle` for map_text handles goes first ($0.01 each on
  x_api), then `search_users` (10 users, $0.10). The first XApiAuthError (403 on users/search with the app-only token),
  credits error, BudgetExceeded or a "missing key" error stops paid calls; the stage carries on and says why in
  `paid_stopped`. Without crawled follow lists, X-tagged people are not searched with a paid source
  (`deferred_to_seed_lists`): after the crawl they match free.
* Trial on a copy of data/wishlist.db (DRY_RUN=1, no paid calls): 52 of 318 map people resolved for free: all 25 tagged,
  23 untagged (e.g. Julian Chokkattu, Devindra Hardawar, Chris Welch), 2 map_text (unconfirmed: @jasonhowell,
  @swebster77), 1 local. Sabrina Ortiz, Tripp Mickle and Jason Howell are not in Carlo's lists, so paid lookups are the only
  way to find them. 40 selected map people were linked to rows that discovery had already made (one row each, no duplicates).
* **Request (pipeline, lead):** `_call(run_resolve_handles, conn, run_id, verify)` passes no `seed_handle`; the stage
  falls back to `CARLO_X_HANDLE`. Pass `seed_handle=seed_handle` like paths if `--seed` should reach it too.
* **Request (verify, lead):** map_text rows with `x_id` NULL (e.g. @jasonhowell) are waiting for an id check. One
  `get_users_by_handles` batch confirms them for $0.01 each. resolve does this within `max_paid_lookups` when budget is left.
* Candidates: one row per map person. When discovery already has a row for the same `x_id`, it gets the `map_person_id`;
  a map-only row from an earlier run is merged into it (evidence, events, classifications, scores, changes, paths
  and status move over; a set status beats 'candidate'). `notes` are merged by key (discover's ' | ' `key:value`
  convention): the `map:` segment is replaced and `enriched:`, `hint:` and `seed:` are kept. A test covers this.
* Paths calibration (decision): an X conversation link is worth 0.6 (same as a shared company), up to 0.75 with more
  posts or a two-way exchange. Before, it was 0.5, the same as a bio string match, so a conversation through a top-quartile
  connector (0.26) scored below a bare "follows you" (0.3). Same-firm links now need the full firm name (no one-word
  stems, parentheticals dropped, big employers or platforms such as YouTube/OpenAI/Forbes ignored, "backed by X"
  skipped). On the live copy, false via paths such as "Creator Ventures" matching any bio with "creator" disappeared
  (via best paths 314 -> 88).
* export: rows sorted by rank (n = 1..N, unscored last); path objects always carry how/you/via/target as strings ("" not
  null) and warmth as an int 0..3 for the map renderer.
* Enrichment gates (`discover.enrich_gate`, paid enrichment only; everyone is still classified): `suspicious`,
  `competitor_brand` (@ouraring, @plaudai, a display name equal to a competitor), `org` (`looks_brand_account`: looks_org,
  a bio that speaks only as "we", brand handles like @acmehq/@fooai with a one-word name, outlet bios with news/rumors/
  deals/latest, organisation words in the name such as Ventures/Capital/Labs/Combinator or a trailing "AI"; never when the
  bio has a first person or a personal role), `small_audience` (< ENRICH_MIN_FOLLOWERS = 1500, map people exempt).
  Removed 30 Sep: `competitor_role` (competitor ties are flagged, never removed: classify now sets the flag from a role
  stated in the bio) and `non_us` (non-US people are evaluated like everyone; "US only" is a dashboard filter).
  Counts are returned in `stats["skipped"]` and logged; the `top` slots are filled from the next eligible candidates.
* Prefetch: in search mode, after the budget has trimmed `chosen` (so nothing beyond the cap is bought), run_enrich calls
  `source.prefetch_searches([(query, max_results), ...])` when the source has it, with exactly the strings and sizes the
  loop then passes to `search_posts`; a refused or failed prefetch falls back to searching one by one.
