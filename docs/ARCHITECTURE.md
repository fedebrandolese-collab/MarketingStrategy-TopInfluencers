# Architecture and contracts

One repository, runs on a laptop, re-runs on a schedule. Python 3.12, SQLite, Streamlit. No standalone agents:
scheduled automations that call an LLM where judgement is needed. The X side is read-only by construction
(app-only token, no write scopes): the system can never contact anyone.

## Pipeline

```
ingest -> crawl -> resolve -> discover -> enrich -> classify -> verify -> paths -> score -> export -> dashboard
 map      Carlo's   handles of   topic      recent     Claude:    official  who can   three     wishlist   Streamlit,
 JSON     follows   his map      search ->  posts of   category,  X id      reach     probs x   .json in   status
 mirror   (monid)   contacts     PEOPLE     top 60     evidence,  check     whom      impact x  map shape  edits
                    (matched to  (monid)    (monid)    flags,     (x_api)             reach
                    his lists)                         events
```
Order matters: crawl before resolve (handles are matched against Carlo's own follow lists), classify before verify
(verification covers the people headed for the list), paths before score (reach comes from the best path).
Every stage is `run_<stage>(conn, run_id, ...)` and idempotent. `python -m ring_wishlist.pipeline run --stage all` or a
comma list (`--stage discover,enrich,classify`). Two sources: `SOURCE_BULK` (default monid) for crawl, discover and
enrich; `SOURCE_VERIFY` (default x_api) for handle verification and the bounded fallback search in resolve.

## Ownership (parallel workstreams)

| WS | Deliverable | Files (only touch these) |
|----|-------------|--------------------------|
| A  | Map ingestion | `src/ring_wishlist/ingest_map.py`, `tests/test_ingest_map.py` |
| B  | X API adapter | `src/ring_wishlist/sources/x_api.py`, `tests/test_x_api.py` |
| C  | monid adapter | `src/ring_wishlist/sources/monid.py`, `tests/test_monid.py` |
| D  | Discovery + classification | `src/ring_wishlist/discover.py`, `src/ring_wishlist/classify.py`, `src/ring_wishlist/prompts/*.md`, `tests/test_discover.py`, `tests/test_classify.py` |
| E  | Scoring + change log | `src/ring_wishlist/score.py`, `SCORING.md`, `tests/test_score.py` |
| F  | Paths + handle resolution + export | `src/ring_wishlist/paths.py`, `src/ring_wishlist/resolve_handles.py`, `src/ring_wishlist/export.py`, `tests/test_paths.py` |
| G  | Dashboard + scheduler | `app/dashboard.py`, `scripts/*`, `tests/test_dashboard_data.py` |
| H  | LLM backend | `src/ring_wishlist/llm.py`, `tests/test_llm.py` |
| —  | Orchestrator, monid adapter, verify stage, README, DECISIONS, COSTS | `src/ring_wishlist/pipeline.py`, `src/ring_wishlist/sources/monid.py`, `src/ring_wishlist/verify.py`, `README.md`, `DECISIONS.md`, `COSTS.md` (lead) |

Shared, already written, do not edit (append a note to `docs/INTEGRATION_NOTES.md` if you need a change):
`db/schema.sql`, `src/ring_wishlist/config.py`, `src/ring_wishlist/db.py`, `src/ring_wishlist/costs.py`, `src/ring_wishlist/sources/base.py`,
`tests/conftest.py`, `data/fixtures/*`, `config/queries.yaml` (D may extend queries, not the structure).

## Conventions

* Run everything from the repo root with `.venv/bin/python` (`source .venv/bin/activate`). `PYTHONPATH` is not needed: the package is installed editable.
* Tests: `DRY_RUN=1 .venv/bin/python -m pytest -q tests/test_<yours>.py`. Tests never touch the network or spend money.
  `DRY_RUN=1` makes `get_source()` return `FixtureSource` and `llm_backend()` return `"fixture"`.
* Timestamps: ISO-8601 UTC strings (`db.utcnow()`). Handles: lowercase, no `@` (`norm_handle`). `x_id` is TEXT.
* Unknown is `NULL`, never a guess. If the evidence is not there, store nothing and (where a field exists) say why in `notes`/`unknowns`.
* Every paid call: `costs.guard(conn, provider, projected_usd, run_id)` before, `costs.record(...)` after. Print the projected cost to stdout before a paid batch (`print(f"[cost] {provider} {endpoint}: ~{costs.fmt(x)} for {n} units")`).
* Persist raw responses (`raw_json`) so nothing has to be re-bought. Check the local tables before calling out.
* Logging: `logging.getLogger("ring_wishlist.<module>")`, INFO for stage progress, DEBUG for per-item detail.
* No new dependencies without a note in `docs/INTEGRATION_NOTES.md`. Available: requests, python-dotenv, streamlit, pandas, rapidfuzz, anthropic, pyyaml, pytest.

## Module contracts

### A. `ingest_map.py`
```python
def fetch_map(passphrase: str, url: str = config.MAP_URL) -> dict   # logs in (POST /login, form field p), GETs /api/graph?group=capital|press and /api/timeline?id=<id> for every person; returns {"capital": {...}, "press": {...}, "timelines": {id: {...}}}
def save_raw(payload: dict) -> None                                  # writes data/raw/map_capital_people.json, map_press_people.json, map_graph_meta.json, map_timelines.json (same files/shape as the current ones)
def load_raw() -> dict                                               # reads those files
def run_ingest(conn, run_id: int | None = None, from_raw: bool = True) -> dict   # upserts map_people, map_edges, map_competitor_funds, map_linkedin, map_monthly; returns counts. from_raw=False refreshes from the site first (needs MAP_PASSPHRASE).
```
CLI: `python -m ring_wishlist.ingest_map [--refresh]`. Also seed `candidates` rows for map people? No: F does that in `resolve_handles`.

### B. `sources/x_api.py`  and  C. `sources/monid.py`
Implement `Source` from `sources/base.py` exactly. Constructor signature `XApiSource(conn=None, run_id=None)` / `MonidSource(conn=None, run_id=None)`: `conn` is the SQLite connection for `costs.guard/record` and for checking local caches before buying data (when `conn` is None, only print projected costs). Constructor reads its key from `config`. Raise `RuntimeError("X_BEARER_TOKEN missing")`-style errors early.
* B uses `https://api.x.com/2` with app-only Bearer. Endpoints: `GET /2/users/by/username/:u`, `GET /2/users/by?usernames=`, `GET /2/users/search?query=`, `GET /2/users/:id/following|followers` (max_results 1000, paginate, rate limit 300/15min, honour `x-rate-limit-*` headers and sleep on 429), `GET /2/tweets/search/recent?query=` (and `search/all` when `since` is older than 7 days), `GET /2/users/:id/tweets`. Ask for `user.fields=description,location,url,verified,verified_type,created_at,public_metrics,profile_image_url,entities` and `tweet.fields=created_at,public_metrics,entities,referenced_tweets,author_id` with `expansions=author_id`. Prices: `costs.X_PRICES`.
* C uses `https://api.monid.ai/v1` (`discover`, `inspect`, `run`, `runs/:id`), Bearer `MONID_API_KEY`. Strategy: on first use `discover` the Apify actors (`apidojo/twitter-user-scraper` for profiles/followers/following, `apidojo/tweet-scraper` or `kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest` for posts), `inspect` them to read the price and input schema, cache both in `data/cache/monid_endpoints.json`, and compute the projected cost from the inspected price before every run. Async runs return 202: poll `GET /v1/runs/:runId` until terminal. Map raw actor output to `XUser`/`XPost`. Record `billing.actualCost` (micro-dollars) via `costs.record`.
* Both: tests use `requests` mocked with `monkeypatch`/a fake session and recorded sample payloads you write into `tests/samples/`.

### H. `llm.py`
```python
def complete_json(prompt: str, schema: dict, *, system: str | None = None, model: str | None = None, max_tokens: int = 2000) -> dict
def complete_text(prompt: str, *, system: str | None = None, model: str | None = None, max_tokens: int = 1000) -> str
```
Backends by `config.llm_backend()`: `claude_cli` runs the Claude Code CLI non-interactively (`claude -p <prompt> --output-format json`, plus flags that disable tools and keep it to one turn; verify the exact flags with `claude --help`), `anthropic` uses the SDK (default model `claude-sonnet-5-5`), `fixture` returns `data/fixtures/llm_classification.json` for classification prompts and `{}` otherwise. `schema` is a JSON-schema-like dict; validate that required top-level keys exist, retry once with a repair prompt if the output is not valid JSON. Record token usage to `costs` (provider `anthropic`, unit cost from `costs.ANTHROPIC_PRICES`; CLI usage logs 0 cost with a note).

### D. `discover.py` and `classify.py`
```python
def run_crawl(conn, run_id, source, seed_handle: str, max_following: int = 2000, max_followers: int = 2000) -> dict     # Carlo's following + followers -> x_users/x_follows; candidates for people who look like voices/media/VC/founders (bio heuristics), discovered_via='seed'
def run_discover(conn, run_id, source, queries_file=config.QUERIES_FILE, budget_usd: float | None = None) -> dict       # queries.yaml groups (discover.QUERY_GROUPS: topics + competitor_products + ring_wishlist + authors + evidence; groups= picks some) -> x_posts -> aggregate by author -> upsert candidates (discovered_via='query:<q>'); hub following lists (discovered_via='following:<hub>'); returns counts and cost
def candidate_signals(conn, candidate_id) -> dict                       # counts: topical posts, engagement, bio keywords, followers, verified, can_dm, location, mutual follow with seed
def run_classify(conn, run_id, candidate_ids: list[int] | None = None, max_candidates: int = 200) -> dict   # for each candidate: build a compact dossier (bio, location, metrics, up to 30 topical posts), call llm.complete_json with CLASSIFICATION_SCHEMA, store classifications, update candidates (category, us_based, competitor_flag/detail, handle_verified), write evidence rows and events
```
Aggregation surfaces PEOPLE: a candidate needs at least 2 topical posts or 1 topical post with engagement above `limits.min_likes`, or a bio match. Store why (`discovered_via`).
Classification output (store verbatim in `classifications.json`):
```json
{"category":"x_voice|media|vc|founder_angel|other",
 "us_based":true|false|null, "us_based_evidence":"...",
 "wearable_user":{"value":true|false|null,"evidence":[{"url":"...","quote":"...","date":"YYYY-MM-DD"}]},
 "voice_tool_user":{"value":true|false|null,"evidence":[...]},
 "reviews_products_publicly":{"value":true|false|null,"evidence":[...]},
 "competitor_tie":{"value":true|false|null,"which":"Oura|...|null","kind":"employee|investor|advisor|ambassador|null","evidence":[...]},
 "audience_relevance":0.0-1.0,
 "handle_authenticity":{"value":"verified|likely_real|suspicious|unknown","reasons":"..."},
 "events":[{"kind":"fund_raised|ring_unboxing|joined_competitor|left_competitor|posted_about_product|device_review|other","date":"YYYY-MM-DD","url":"...","text":"..."}],
 "summary":"one sentence Carlo can read in 3 seconds",
 "unknowns":["..."]}
```
Every evidence item must quote a real post or profile field that was in the dossier; the prompt says so and `classify` drops evidence whose url is not in the dossier.
Evidence mapping: wearable_user -> supports 'wear'; voice_tool_user -> 'wear' and 'love'; reviews_products_publicly -> 'love'; competitor_tie -> 'competitor'; us_based -> 'us'; handle_authenticity -> 'authenticity'; audience_relevance and metrics -> 'impact'; can_dm / mutual follow -> 'reach'.

### E. `score.py` and `SCORING.md`
```python
def run_score(conn, run_id) -> dict      # writes scores (all candidates with a classification or map link) and changes (vs previous scored run); returns counts
def explain(conn, run_id, candidate_id) -> dict   # {"factors": {...}, "reasons": [...], "evidence_ids": [...]} used by the dashboard
```
Shape (E documents the exact numbers in SCORING.md and may tune, but keeps this structure):
* `p_accept`, `p_wear`, `p_love` in [0,1]; each starts from a category prior and moves with evidence rows (`supports`, `polarity`, `weight`) and events with 90-day decay. Priors when nothing is known are low and stated: e.g. accept 0.4, wear 0.3, love 0.3.
* `impact` in [0,1]: reach (log followers), audience relevance, role leverage (media and VC weigh more), engagement. Attention > network > funding.
* `reach` in [0,1]: from the best path (F writes `paths.strength`); 0.05 when none. Information on how to approach the person, not in the total (Carlo, 29 Sep: rings may go outside the map; quality over connection). A `direct` or `via` path adds 0.5 x strength to p_accept's log-odds; no path takes nothing away.
* `total = 100 * p_accept * p_wear * p_love * (0.3 + 0.7*impact)` with the default weights (`weights.weighted_total`; a `default:` in config/weights.yaml re-weights it); `rank` by total desc.
* `confidence` = share of the three probabilities backed by at least one evidence row.
* `flags`: `["competitor:<which>"]` when `competitor_flag`, `["not_us"]` when `us_based == 0`, `["unverified_handle"]` (no path is not a flag: `factors.reach.no_path` says it as information). Flags never zero the score; the dashboard shows them.
* `changes`: for each candidate compare with the previous scored run; write one row per changed field with a human reason built from the evidence and event ids the previous run had not seen (each scored run stores the ids it saw in `reasons_json.seen`; `fetched_at` is only a fallback for older rows), plus `new_candidate` rows.

### F. `resolve_handles.py`, `paths.py`, `export.py`
```python
def run_resolve_handles(conn, run_id, source, only_warm: bool = True, max_people: int = 150) -> dict   # for map people (tags x_following/x_follower first, then warmth desc): source.search_users(name [+ company]) -> pick match (name similarity via rapidfuzz + company/role in bio) -> map_x_handles with confidence and method; also upsert a candidates row with map_person_id for map people that are themselves plausible ring wearers (category from sector: vc->vc, angel/founder->founder_angel, press->media)
def run_paths(conn, run_id) -> dict            # for every candidate: compute up to 4 paths, best first, write `paths`
def run_export(conn, run_id, out_dir=config.EXPORTS_DIR) -> dict   # wishlist.json (map shape below) + wishlist.csv + changes.csv
```
Path semantics (a real relationship, not a follow):
* `direct`: the candidate IS a map person (`candidates.map_person_id`). `strength` from warmth rank, meetings, recency of `last_contact` (faded if silent 90+ days). `you_rel` like `"met · 3 meetings · 84 touches · last 12d ago"`.
* `via`: Carlo -> connector (map person with warmth > 0) -> candidate, where the connector-candidate link is one of: map intro edge (`map_edges`), same company (`map_people.company` vs candidate company/bio), X conversation (replies/mentions between the connector's resolved handle and the candidate, 2+ posts), co-listed in `map_competitor_funds` (same fund) or same firm in bio. `target_rel` states which. Mutual X follow between connector and candidate is shown as `"follow each other"` with low strength and never counts as the only path for `reach` above 0.3.
* `x_direct`: Carlo and the candidate follow each other, or the candidate replied to Carlo. `x_follower` tag in the map counts too.
* `open_dm`: `can_dm` is true: reachability floor, strength 0.15.
* `none`: strength 0.05.
Export row (the map's wishlist tab renders `n,name,role,section,paths[{how,you,via,warmth,target}]`; extra keys are ignored):
```json
{"n":1,"name":"Rina Ring","role":"Wearables reviewer · 48k on X","section":"X voices","handle":"ringnerd_test",
 "score":31.2,"p_accept":0.7,"p_wear":0.8,"p_love":0.6,"impact":0.7,"reach":0.6,"confidence":1.0,
 "flags":[],"status":"candidate","summary":"...","reasons":["..."],
 "evidence":[{"kind":"post","supports":"wear","url":"...","date":"...","text":"..."}],
 "paths":[{"how":"via","you":"met · 3 meetings · warm","via":"Marwan Refaat","warmth":3,"target":"introduced them (map)","strength":0.7}]}
```
`warmth` in a path is an integer 0..3 bucket of the connector's warmth rank (3 = top quartile) because the map renderer compares `>= 2`.

### G. `app/dashboard.py` and `scripts/`
Streamlit, reads SQLite (latest scored run), writes only `status`. Ten-second readability: one ranked table (rank, name+handle, section, score, the three probabilities as small bars or numbers, impact, best path in words, flags as chips, status dropdown), a change strip ("since last run: 3 up, 2 down, 4 new" with reasons), filters by section/flag/status, a detail panel per person (summary, evidence with links, all paths, score explanation from `score.explain`, cost of this run). Use `st.data_editor` for status or a selectbox in the detail panel; persist immediately.
Operations (30 Sep): `src/ring_wishlist/ops.py` runs the stages due under `config/schedule.yaml` (`schedule.py`), with a lock,
monthly caps, a backup, per-stage rows in `run_stages`, alerts and a health check. `scripts/refresh.sh` calls it from launchd
(`com.ring-wishlist.wishlist`, at `daily_at`; `com.ring-wishlist.dashboard` keeps the dashboard up), `deploy/systemd/` and
`docker-compose.yml` from a server. The System tab shows it. Runbook: `docs/OPERATIONS.md`.

### Pipeline (lead)
`pipeline.py` wires the stages, creates the run, prints a cost summary at the end and writes `COSTS.md`.

## Integration rules
* Keep to your files. Shared modules are frozen for today; write requests in `docs/INTEGRATION_NOTES.md`.
* Your tests must pass offline with `DRY_RUN=1`. Add small sample payloads under `tests/samples/<ws>/`.
* Report back: what works, what is stubbed, what needs a key or a decision, and the exact command to run your stage.

## Topic affinity (added 2026-09-29)

Goal: someone who keeps discussing personal agents (and the ring's other themes: voice interfaces, AI wearables and smart
rings, screen-free life) and has a community that answers them gets a higher score. Two signals, combined in score.py:

1. **Computed affinity** (`src/ring_wishlist/affinity.py`, workstream AF), from the person's own stored posts, no LLM, no network:
```python
def topic_affinity(conn, candidate_ids: list[int] | None = None, now: str | None = None,
                   window_days: int = 90) -> dict[int, dict]
# {candidate_id: {"score": 0..1, "topical_posts": int, "agent_posts": int, "weighted_posts": float,
#                 "topic_eng_avg": float | None, "overall_eng_avg": float | None, "lift": float | None,
#                 "replies_per_topic_post": float | None, "last_topical": "YYYY-MM-DD" | None,
#                 "top_posts": [{"url", "date", "text", "engagement", "replies"}] (best 3),
#                 "components": {"volume": 0..1, "community": 0..1, "recency": 0..1}, "reason": "one sentence"}}
```
   Only the person's own posts count (x_posts.x_id = candidates.x_id; the 'latest_status' rows count too). A post is
   topical when it is about personal agents (weight 1.0) or about voice interfaces / AI wearables and rings / screen-free
   life (weight 0.7); passing mentions ("I asked ChatGPT") are not topical. Candidates with no posts get score None.
2. **Classifier verdict** (`classify.py`, workstream CL): new field in the classification JSON
```json
"topic_voice": {"value": true|false|null, "topics": ["personal_agents"|"voice"|"wearables"|"screen_free"],
                "evidence": [{"url": "...", "quote": "...", "date": "YYYY-MM-DD"}]}
```
   `true` only when the dossier shows the person repeatedly discussing (explaining, arguing, building in public, hosting
   conversations) at least one of the topics, not just using a product. Evidence rows: supports='affinity', kind='post',
   weight 1.0 (profile/bio quote: kind 'bio', weight 0.8); polarity -1 when value is false.
3. **Scoring** (`score.py`, workstream SC): `affinity` = computed score, raised to at least 0.4 when topic_voice is true,
   capped at 0.3 when topic_voice is false, None when both are unknown. Stored in `scores.affinity`. It adds
   log-odds to p_love (+1.5 x affinity) and p_accept (+0.5 x affinity); in impact, engagement comes from the topic posts
   instead of all posts when there are at least 3 topical posts. Evidence rows with supports='affinity' are cited, not
   summed again. Change log: a move of 0.10 or more writes a row "topic affinity 0.20 -> 0.55: ..." with the new posts.
4. **Dashboard** (`app/`, workstream DB): a "Topic" progress column and, in the detail panel, the affinity sentence with
   its best posts.
