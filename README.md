# tattoo

a daily digest that watches a curated set of feeds, acquires the full content behind each new item, uses an llm to separate substance from filler, and publishes a briefing to the home tailnet with a pushover notification. named for the evening bugle call — the end-of-day counterpart to [reveille](https://github.com/pid1/reveille).

the unit of subscription is a feed. youtube channels are one kind of feed among several, handled by a source adapter.

**status: all milestones (M0–M6) implemented.** feed polling with conditional requests, per-source daily caps, and a first-poll backfill limit (a new source contributes its newest few items, never its back catalogue); article acquisition (feed body → trafilatura extraction → degraded summary fallback); the youtube adapter (data api enrichment, rate-limited transcript fetching with the full failure taxonomy); the two-pass llm gate (haiku triage against per-source criteria, sonnet extraction with citation locators), shipped in shadow mode by default; dual-view rendering (briefing page + pushover digest with real content inline); and the settings interface at `/settings` — source lifecycle with a paste-anything resolver and confirmation card, write-only secrets with last-4 hints, versioned prompts with rollback, run-now/reprocess, toml export/import, and retention pruning. awaiting real-world calibration before the gate goes live (flip shadow mode off in settings).

## security posture

**tailnet-only. no authentication, no csrf protection, no https.** the service assumes wireguard encrypts the transport and the tailnet bounds the audience. do not expose it publicly without adding all three.

## how it works

- a single python process: uvicorn serving fastapi, with the scheduler as an in-process daemon thread. the scheduler re-reads its schedule from the database each cycle, so settings changes apply without a restart.
- the briefing pages are pre-rendered static files on a volume — a dated page per run under `/archive/<date>/`, byte-copied to `/dashboard/`. only the settings surface (M6) is dynamic.
- configuration and state live in one sqlite database. content is cached permanently so prompt tuning is a reprocess, not a refetch. secrets can live in the db (settings ui, M6) or in env vars (`ANTHROPIC_API_KEY`, `PUSHOVER_API_KEY`, `PUSHOVER_USER_KEY`, `YOUTUBE_API_KEY`), and env wins.
- structured json logs on stdout for the alloy/loki stack. no healthcheck endpoint, no heartbeat — by decision, the logs are the observability surface.

## http policy

the http layer is deliberately stdlib-only (`urllib.request` behind `sources/base.py`), inheriting reveille's policy. planned exceptions, documented here on purpose: `feedparser` for rss/atom parsing, and `trafilatura` (M2) for article extraction — extracting readable text from arbitrary publisher html is a genuinely hard problem not worth hand-rolling.

## development

with [devenv](https://devenv.sh): `devenv shell`, then `setup`, `test`, `dev`. see AGENTS.md for the full command table.

without devenv:

```bash
uv sync
PYTHONPATH=src uv run pytest
PYTHONPATH=src uv run uvicorn tattoo.main:app --reload --port 8000
PYTHONPATH=src uv run python -m tattoo.pipeline   # one-shot run
```

## deploy

ghcr image built by `.github/workflows/container.yml`. on the nas:

```bash
docker run -d --name tattoo --restart unless-stopped \
  -p 6868:8000 \
  -v /mnt/user/appdata/tattoo/data:/data \
  -v /mnt/user/appdata/tattoo/dist:/dist \
  --env-file /mnt/user/appdata/tattoo/.env \
  ghcr.io/pid1/tattoo:latest
```

include `/mnt/user/appdata/tattoo` in the appdata backup schedule. the app also snapshots the database into `/data/backups/` before every migration (`TATTOO_BACKUP_KEEP`, default 10).
