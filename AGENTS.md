# tattoo — agent guide

Daily feed digest: polls RSS/Atom and YouTube feeds, acquires full content, runs a two-pass LLM usefulness gate, and publishes a static briefing plus a Pushover digest. Sibling of `pid1/reveille` (pipeline conventions), `pid1/rally` (settings layer), and `pid1/puffin` (operational hygiene). The full design lives outside this repo; this file covers what you need to work here.

## Commands

All operations are devenv scripts. **Always use these — run `lint`, not `ruff check .`; run `test`, not `uv run pytest`.**

| Command | What it does | Blocking |
|---|---|---|
| `setup` | Initialize repo (install deps) | No |
| `dev` | FastAPI dev server on port 8000 | **Yes** |
| `dev-start` / `dev-stop` / `dev-status` / `dev-logs` | Background dev server quartet | No |
| `lint` / `lint-fix` / `format` | ruff | No |
| `test` | pytest | No |
| `check` | lint + format check + tests — run before pushing | No |
| `run-once` | Init the db and run the pipeline once | No |
| `add-source` | Add a source row (`add-source web <feed_url> <name>`) | No |
| `backup` | Snapshot the database | No |

Without devenv: `uv sync`, then `PYTHONPATH=src uv run pytest` etc. Never bare `python` or `pip`.

## For AI agents

1. Always use devenv scripts (table above).
2. Prefer the background dev-server commands; check `dev-status` before starting, `dev-logs` after.
3. Never destructively mutate a local dev database — the pytest harness gives every test an isolated tmp database; use it.
4. Run `check` before pushing.

## Architecture facts you would otherwise get wrong

- **src layout, never installed.** `src/` is put on `PYTHONPATH` (pytest ini, devenv env, Dockerfile). There is no build backend and no console entry point; the server is `uvicorn tattoo.main:app`, one-shots are `python -m tattoo.pipeline` / `python -m tattoo.backup`.
- **`templates/`, `static/`, and `migrations/` live at the repo root**, outside the package, and are copied separately in the Dockerfile.
- **Stdlib `sqlite3`, no ORM, no Alembic.** Migrations are numbered files in `migrations/`, each exporting an idempotent `migrate() -> bool`, ordered by the hand-maintained list in `migrations/run_migrations.py`. They must be idempotent by introspection (no version table), additive, standalone-runnable, and tested by running twice. Migration 001 creates the schema and must not early-return on a missing db file; later migrations should.
- **Startup sequence** (`database.init_db()`): ensure dirs → pre-migration backup → run migrations. Every connection gets `PRAGMA foreign_keys=ON` — sqlite defaults it off and `ON DELETE CASCADE` is inert without it.
- **All outbound HTTP goes through `sources/base.py`** (stdlib `urllib.request`; errors normalized to `RuntimeError` with the shape `HTTP {code} from {url}: ...`). Tests mock at this seam. Do not add `httpx`/`requests`; the only sanctioned parsing/extraction deps are `feedparser` and (from M2) `trafilatura`.
- **Configuration is authoritative in the SQLite `settings` table** (string key/value). Env vars only override paths and secrets (`TATTOO_DB_PATH`, `TATTOO_DIST_PATH`, `TATTOO_BACKUP_KEEP`, `ANTHROPIC_API_KEY`, `PUSHOVER_API_KEY`, `PUSHOVER_USER_KEY`, `YOUTUBE_API_KEY`). Secret env overrides win over the db and are never written back.
- **The scheduler is an in-process daemon thread** (`scheduler.py`), started from the FastAPI lifespan. It re-reads its schedule from the db each cycle and persists `last_run_date` in settings. Tests never start it: the `client` fixture deliberately skips lifespan.
- **A source failure is data, not an exception.** The pipeline only records `status='failed'`; nothing propagates. The notifier never raises. Errors render on the page rather than being hidden.
- **Logs are structured JSON on stdout** via `tattoo.log.log(subsystem, msg, **fields)` — no `logging` module. Every skip path logs a reason. No healthcheck endpoint or heartbeat exists, by decision: logs are the observability surface.
- **Rendered pages are static files** written to the dist volume; the app serves them via `StaticFiles` mounts with `Cache-Control: no-cache` (stable URLs, daily-changing bytes). The dashboard page is a byte-identical plain copy of the dated archive page — never a symlink. Briefing pages inline their CSS to stay self-contained; `static/styles.css` styles `/settings` only.
- **LLM calls go through `judge.call_llm`** — raw Anthropic Messages API over `sources/base.post_json`, no SDK. Static instructions in the `system` block with ephemeral `cache_control`; text read as a type-filtered join over content blocks; JSON parsed with a balanced-object fallback. Token usage feeds `runs.token_usage` plus per-row columns on `judgments`/`extractions`; crossing `run_token_budget` raises `BudgetExceeded` → run status `aborted_budget`.
- **Prompts are versioned, not files at runtime.** `prompts/*.md` only seeds `prompt_history` on first run; after that the DB is authoritative (pointer keys `current_{name}_history_id` in settings). Save = insert + repoint; rollback = repoint only, never insert. Every judgment/extraction records its `prompt_history_id`.
- **Secrets are write-only through the API.** `GET /api/settings` returns `{set, hint, env_override}` metadata, never the value; blank on PUT means keep. Do not add any endpoint that returns a stored secret.
- **Shadow mode** (`shadow_mode` setting, default `"true"`) renders every item with its score and rejection reason; flipping it off makes rejections invisible. Suppression happens in `pipeline._sections_for_today`, nowhere else.

## Style

- Python 3.14, PEP-604 unions, `from __future__ import annotations`.
- Lowercase docstrings and comments; comments record *why* (and the evidence), not what.
- Tests are plain pytest functions; docstrings state the rule being locked in.
