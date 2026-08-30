# tattoo — production setup runbook

A plan for an agent to bring tattoo into production on the Unraid NAS. The code is merged to `main` in `pid1/tattoo`; the container image is published by CI to `ghcr.io/pid1/tattoo:latest`. This runbook takes it from "image exists" to "calibrated daily briefing." Work through the phases in order; each has an explicit exit criterion.

Context you need before starting:

- tattoo is a daily feed digest: polls RSS/Atom and YouTube feeds, caches full content, runs a two-pass LLM usefulness gate (Haiku triage → Sonnet extraction), publishes a static briefing plus a Pushover digest. Full design doc: `~/workdir/jro/tattoo-plan.md`. Repo conventions: `AGENTS.md` in the repo.
- Target host: the Unraid NAS (`rosedale-nas`), already on the tailnet at `rosedale-nas.tail117cd.ts.net`, with an existing Alloy → Loki → Grafana logging stack that scrapes container stdout.
- **Security posture is tailnet-only: no auth, no CSRF, no HTTPS.** Never expose the port beyond the tailnet/LAN.
- The service is a single container: uvicorn + an in-process scheduler thread. It has **no healthcheck endpoint by design** — structured JSON logs are the observability surface.
- jon must supply the secrets (Anthropic API key, Pushover app token + user key, YouTube Data API key). Ask for them at Phase 3; do not proceed past it without at least the Anthropic key.

---

## Phase 1 — verify the image is pullable — ✅ DONE 2026-08-06

Mostly pre-verified on 2026-08-06: the `container` workflow succeeded and `ghcr.io/pid1/tattoo` is **public** with tags `latest` and `sha-86b4fbb`, confirmed anonymously pullable from GHCR.

1. Sanity-check nothing has regressed since: `gh run list --repo pid1/tattoo` shows the latest `container` run green.
2. On the NAS: `docker pull ghcr.io/pid1/tattoo:latest` succeeds (no credentials needed).

Exit: the image pulls on the NAS.

**Result:** latest `container` run green (id 31126348963). Pulled anonymously on the NAS,
digest `sha256:49ae0c62…`, 225MB.

## Phase 2 — container deployment on Unraid — ✅ DONE 2026-08-06

> **Port changed: 6868 → 6565.** 6868 was already published by the `rally` container.
> 6565 was free and keeps the existing convention (puffin 6767, rally 6868, freshrss 6969).
> Every URL below and in later phases uses **6565**.

1. Confirm external port **6565** is free: `netstat -tlnp | grep 6565` (or check the Unraid Docker page for conflicts). If taken, pick another and adjust every URL below.
2. Create the appdata directories:
   ```
   mkdir -p /mnt/user/appdata/tattoo/data /mnt/user/appdata/tattoo/dist
   ```
3. Create the container. Via the Unraid UI (preferred so it shows in the Docker tab with an icon and update tracking) or the equivalent CLI:
   ```bash
   docker run -d --name tattoo --restart unless-stopped \
     -p 6565:8000 \
     -v /mnt/user/appdata/tattoo/data:/data \
     -v /mnt/user/appdata/tattoo/dist:/dist \
     --env-file /mnt/user/appdata/tattoo/.env \
     -e TZ=America/Chicago \
     ghcr.io/pid1/tattoo:latest
   ```
   Created via CLI, plus a matching Unraid template at
   `/boot/config/plugins/dockerMan/templates-user/my-tattoo.xml` (modelled on `my-rally.xml`)
   so the container still gets the Docker-tab entry, WebUI link and update tracking.
   The `--env-file` flag lives in the template's `<ExtraParams>` so Unraid's
   update/recreate button preserves it.
   Secrets can go in an `--env-file /mnt/user/appdata/tattoo/.env` (keys: `ANTHROPIC_API_KEY`, `PUSHOVER_API_KEY`, `PUSHOVER_USER_KEY`, `YOUTUBE_API_KEY`) — env overrides win over the database and never touch appdata backups. Alternatively enter them in the settings UI in Phase 3 (they are then at rest in SQLite inside appdata; accepted in the plan with the write-only/last-4 mitigations). Either works; the `.env` route is preferred for the Anthropic key.
4. Verify startup in `docker logs tattoo`:
   - `[migrations] ok: 001_initial` and `[migrations] ok: 002_token_columns`
   - two `"msg": "prompt seeded"` lines (subsystem `judge`)
   - `"msg": "startup complete"` then a `scheduler` `"waiting"` line with `next_run` at 21:00 local
5. Verify serving: `http://rosedale-nas.tail117cd.ts.net:6565/` redirects to `/dashboard/` (placeholder page until the first run), and `/settings` loads with all sections populated.
6. **Add `/mnt/user/appdata/tattoo` to the existing Unraid appdata backup schedule.** The app also snapshots the DB into `/data/backups/` before every migration (`TATTOO_BACKUP_KEEP`, default 10).

Exit: container Up, logs clean, dashboard and settings reachable over the tailnet.

**Result:** container Up on 6565. All expected log lines present (both migrations, two
`prompt seeded`, `startup complete`, scheduler `waiting` with `next_run` 21:00 −05:00).
`/` → 307 → `/dashboard/`; `/settings` renders all eight sections.

Two deviations:

- **`/dashboard/` 404s on a fresh install.** The runbook expects a placeholder, but the app
  never writes one — `/dashboard` is a `StaticFiles(html=True)` mount over an empty dir until
  the first run. Wrote a static `dist/dashboard/index.html` placeholder; the first run
  overwrites it. Cosmetic only.
- **Step 6 dropped: there is no appdata backup schedule on this NAS, and jon does not want
  one** (decided 2026-08-06). No CA Appdata Backup plugin is installed and no backup cron job
  exists. What remains: tattoo snapshots the DB into `/data/backups/` before every migration
  (`TATTOO_BACKUP_KEEP`, default 10), which covers a bad migration but **not disk loss**.
  Everything tattoo holds is reconstructible — sources and settings via
  `Settings → Download TOML`, content by refetching — except cached transcripts older than
  the retry window and the judgment history used for calibration.

## Phase 3 — configuration via /settings — ✅ DONE 2026-08-06

> **Secrets live in the SQLite settings table, not `.env`** (jon's call, 2026-08-06). Loaded via
> `PUT /api/settings`, all four with `env_override=false`. The NAS `.env` is deliberately left
> with empty values and a comment explaining that populating a key there would silently override
> the UI. Note `docker restart` does **not** re-read an `--env-file`; the container must be
> recreated. Both test buttons returned green:
> `connected to claude-haiku-4-5` and `test notification sent`.

All of this is done in the settings UI (mobile-friendly; jon can do it from a phone). Ask jon for the secrets now.

1. **Schedule section**: timezone `America/Chicago`; daily run time `21:00` (the name argues for evening — confirm with jon); page URL `http://rosedale-nas.tail117cd.ts.net:6565` (this becomes the Pushover tap-through link, with a daily cache-buster appended automatically). — ✅ pre-applied via `POST /api/import/config`, along with `run_token_budget=300000`, `retention_days=90`, `shadow_mode=true`. Run time left at the 21:00 default, **still needs jon's confirmation**.
2. **LLM section**: Anthropic API key (skip if provided via `.env` — the field shows "env override active"); models default to `claude-haiku-4-5` (triage) and `claude-sonnet-5` (extraction), leave as-is; run token budget default `300000` — leave, revisit in Phase 6; **shadow mode ON** (default — do not turn it off yet). Press **Test connection** → expect "✓ connected to claude-haiku-4-5".
3. **Pushover section**: create an application named `tattoo` at pushover.net/apps if one doesn't exist, enter the app token and user key, press **Send test notification** → confirm it arrives on jon's phone.
4. **YouTube section**: Data API key. If one must be created: Google Cloud console → new or existing project → enable "YouTube Data API v3" → Credentials → API key (restrict it to that API). The free 10k units/day quota is ample (~1 unit per 50 videos enriched). Without this key, @handle/watch-link resolution fails and Shorts/premieres are not filtered — it is effectively required.
5. Retention: 90 days (default).

Exit: both test buttons green, settings saved.

## Phase 4 — sources and criteria

Add via the **Sources** box (paste → Resolve → review confirmation card → Add). The card shows posting frequency and content situation — sanity-check both before adding. Confirmed seeds first, then the shadow-trial candidates (decided 2026-08-06, plan §2):

| Source                        | Paste                                                                                                                        | Cap |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | --- |
| Mike Tango Whiskey            | `https://www.youtube.com/@Mike_Tango_Whiskey`                                                                                | 5   |
| S2 Underground                | `https://www.youtube.com/channel/UCTq1zHztiV69Ur8t6jco4CQ`                                                                   | 5   |
| Civil Defense Engineer        | `https://www.youtube.com/@CivilDefenseEngineer`                                                                              | 5   |
| CDC Health Alert Network      | `https://tools.cdc.gov/api/v2/resources/media/285676.rss` — if resolution fails, find the current HAN RSS URL at cdc.gov/han | 10  |
| The Provident Prepper (trial) | `https://www.youtube.com/@TheProvidentPrepper`                                                                               | 5   |
| MedCram (trial)               | `https://www.youtube.com/@MedCram` (verify handle on the card)                                                               | 5   |
| City Prepping (trial)         | `https://www.youtube.com/c/CityPrepping` (or search the handle)                                                              | 5   |
| Forward Observer (trial)      | find their channel/feed; overlaps S2's Wire — pass-rate decides which survives                                               | 5   |
| SouthernPrepper1 (trial)      | `https://www.youtube.com/@southernprepper1`                                                                                  | 5   |

Excluded by decision: food/product recall feeds, Canadian Prepper, anything overlapping reveille (local weather, ERCOT, Highland Village police/fire).

**Resolved 2026-08-06 — 8 of 9 added (ids 1–8), all enabled, threshold 5, cap 5.**
Corrections to the table above:

| Runbook said                               | Reality                                                                                                                                  |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| City Prepping `/c/CityPrepping`            | 422 "could not work out a channel" — use `https://www.youtube.com/@CityPrepping` (`UCmb2QRAjdnkse21CtxAQ-cA`)                            |
| Forward Observer "find their channel/feed" | `https://www.youtube.com/@ForwardObserver` (`UCSqrUtY5pAE86yDVCk4OOnw`), 30/mo — same cadence as S2's Wire, so the overlap trial is live |
| MedCram "verify handle"                    | Confirmed, resolves to "MedCram - Medical Lectures Explained CLEARLY" (`UCG-iSMVtWbbwDDXgXXypARQ`), 5.9/mo                               |
| CDC HAN `285676.rss`                       | **Not HAN.** See below.                                                                                                                  |

**⚠️ CDC HAN dropped — no live feed exists.** The runbook's URL resolves to "CDC Outbreaks

- US Based", a _food-outbreak_ feed (Salmonella jalapeños/turtles/shell eggs, Cyclospora
  lettuce) — the exact category excluded above. It fails silently rather than erroring.
  Feed liveness check on 2026-08-06:

| Feed                                                         | Items | Newest         | Verdict                           |
| ------------------------------------------------------------ | ----- | -------------- | --------------------------------- |
| `createrss.asp?c=177` — genuine "Health Alert Network (HAN)" | 124   | **2023-09-01** | frozen archive                    |
| `413690.rss` — "HAN Managed Feed"                            | **0** | —              | empty, `lastBuildDate` 2025-03-31 |
| `285676.rss` — "CDC Outbreaks - US Based"                    | 10    | 2026-08-06     | live, but excluded content        |
| `132608.rss` — "CDC Online Newsroom"                         | 1836  | 2026-08-05     | live, broad CDC press             |

jon's decision: **skip the CDC slot** rather than substitute excluded content. Revisit if CDC
republishes a real HAN feed. The `web` source path and the 10-item cap are therefore
**untested in production** — the first web source added will be the first exercise of them.

Then open each source's **edit** panel and set a starting criteria block (threshold 5 everywhere to begin). Starter criteria — tune freely, they are the shadow phase's main lever:

- **Mike Tango Whiskey / The Provident Prepper / SouthernPrepper1 / City Prepping**: `signal is actionable family preparedness: specific gear with model numbers and prices, storage quantities and shelf lives, procedures with concrete steps, tested configurations, named failure modes. general encouragement, mindset talk, and current-events commentary without a testable claim are filler.`
- **S2 Underground / Forward Observer**: `signal is a concrete, dated event or capability with location and named actors, an explicit change in threat posture, or a specific technical/comms procedure (frequencies, equipment, configurations). commentary, forecasting without evidence, and restatements of mainstream headlines are filler.`
- **Civil Defense Engineer**: `signal is engineering specifics: shelter/shielding numbers, dose rates, materials and thicknesses, standards citations, equipment models, cost figures. historical narrative without an actionable takeaway is filler.`
- **CDC HAN**: `official health advisories are near-always signal: score by concreteness of the recommendation (case definitions, exposure criteria, specific actions for the public). score low only for administrative notices with no household relevance.`
- **MedCram**: `signal is clinical/epidemiological specifics: transmission data, case counts with sources, named interventions with efficacy figures, testable mechanisms. speculation and general wellness content are filler.`

Exit: all sources added and enabled, criteria saved, list view shows them.

## Phase 5 — first runs and YouTube validation

1. Press **Run now**. Follow `docker logs -f tattoo`.
2. Expect per-source `poll` lines, then `acquire` lines. Watch for:
   - `method` distribution — CDC HAN should be `feed_body`/`extracted`; YouTube should be `transcript`. Log the acquisition method per item is the designed early-warning for bad extraction.
   - **`youtube blocking detected, halting transcript fetches`** — the IP-protection abort. If it fires on the first run, do NOT immediately re-run; wait a few hours (unfetched items are retried automatically for 3 days). Repeated blocking means transcript volume must come down (lower caps) — this is the plan's known residential-IP risk and it affects the whole household.
   - `judge` lines with scores, then `pages written`, `run finished status: ok`, and a Pushover digest (or NSTR skip).
3. Open the dashboard: every item should render with its score badge and, for rejected items, the one-line reason (shadow mode). Spot-check that YouTube findings link to `watch?v=…&t=NNNs` and actually land near the claim.
4. **Auto-caption quality check** (plan risk): for two or three technical videos, skim the judged transcripts' findings against the actual videos. If auto-captions are mangling the jargon badly enough that extraction is producing garbage specifics, note which channels are affected — that informs their criteria/threshold.
5. Check `runs` telemetry after a couple of runs: `docker exec tattoo python -c "import sqlite3,json; c=sqlite3.connect('/data/tattoo.db'); [print(dict(r)) for r in c.execute('select id,status,items_seen,items_judged,items_passed,token_usage from runs order by id desc limit 5').fetchall()]"` (set row factory or just eyeball the tuples). Confirm token spend per run is sane against the 300k budget.

Exit: two consecutive clean scheduled runs (check the next two evenings), transcripts fetching without blocking, digest arriving on the phone.

**Run 1 (manual, 2026-08-06 20:07 CDT): `status: ok`, seen 40, judged 21, passed 15.**
Pushover digest `sent ok`. Dashboard renders real content (58KB) with `7/10`-style badges,
`item rejected` classes for shadow-mode rejections, and 71 `watch?v=…&t=NNNs` locator links.
Acquisition method mix: 20 `transcript`, 1 `summary_fallback`.

Two things came out of it:

- **⚠️ YouTube IP blocking fired on the first run**, at 20:09 CDT after ~17 transcripts
  (`IpBlocked` on `weKIaQ9DnXI`). This is the plan's known residential-IP risk. 19 of the 40
  items were never acquired; they retry automatically for 3 days. Per the runbook, do **not**
  force re-runs. If it recurs on consecutive evenings, lower the caps — it affects the whole
  household's IP.
- **⚠️ The 300k token budget is undersized for the backfill.** Run 1 spent **261,860 tokens
  — 87% of the ceiling — while judging only 21 of 40 items** (haiku 125,694 in / 8,877 out;
  sonnet 106,755 in / 20,534 out). Had acquisition not been cut short by the IP block, the run
  would very likely have hit `aborted_budget`. Roughly 12.5k tokens/item ⇒ a full 40-item day
  is ~500k. Note the first run flushed a backlog; steady state is far lower (combined posting
  rate across the 8 sources is ~3–4 items/day, not 40). Failure mode is safe — the run aborts,
  it does not overspend — so this was left at 300000 for jon to decide. Revisit in Phase 6.

### Post-run-1 fix: extraction token ceilings (2026-08-06)

Run 1 stored **3 of 15 passing items as blank cards** — 20% of the briefing missing, with
no error and no log line. Two causes through one silent path:

- `EXTRACT_MAX_TOKENS = 2000` truncated the densest items mid-JSON (both recorded _exactly_
  2000 output tokens).
- `_extract_json_object`'s brace fallback then skipped the unbalanced outer object and
  returned the first inner `findings` element — a dict with none of the keys the caller
  reads — so an empty row was written silently.

Fixed in PRs #1 and #2 (both merged, image redeployed):

- `triage_max_tokens` / `extract_max_tokens` settings keys, default 1000/2000 (unchanged
  behaviour for other deployments). **0 = the model's own ceiling** — this install runs 0/0,
  resolving to haiku-4-5 → 64,000 and sonnet-5 → 128,000. `run_token_budget` is also 0
  (disabled). The API requires `max_tokens` on every call so there is no literal unlimited;
  billing is on tokens produced, not the cap.
- The parser rejects a candidate lacking all expected keys, strips ```json fences, repairs
invalid JSON escapes (`\'` is not legal JSON), and distinguishes truncated from malformed.
- `call_llm` raises on `stop_reason == "max_tokens"`, previously ignored entirely.
- `extract_item` refuses to store an extraction with neither bluf nor findings.

**Result across reprocesses of the same cached content:**

| run          | passed | missing | silently blank |
| ------------ | ------ | ------- | -------------- |
| 1 (before)   | 15     | 0       | **3**          |
| 3 (after #1) | 16     | 1       | 0              |
| 4 (after #2) | 15     | 2       | 0              |

Silent data loss is gone. The residual 1–2 per run are **loud, logged skips** from the model
emitting invalid JSON, plus one transient `HTTP 520` from the API. The durable fix for that
class is structured outputs (`output_config.format` with a json_schema), which the API
enforces — **not yet implemented; recommended next.**

## Phase 6 — shadow-mode calibration (1–2 weeks)

The gate ships in shadow mode: everything renders with scores, nothing is suppressed. The calibration loop:

1. Each evening, jon reads the briefing and judges the judge: items scored high that are filler, items scored low that mattered.
2. Adjust the offending source's criteria (and/or threshold) in settings, then press **Reprocess** — it re-scores today's cached content against the new prompts in seconds, no refetching. Iterate until the day's scores look right.
3. Watch the per-source list view: pass rate tells you a source isn't worth its tokens; tokens-30d tells you which one is eating the budget. Prune or cap accordingly. This is where the Forward Observer vs S2 Underground overlap gets decided.
4. Global prompt changes (the triage system prompt itself) are versioned — save creates a new version, and rollback is one tap if a change makes things worse. Every judgment records the prompt version that produced it, so comparisons stay honest.
5. After 1–2 weeks of stable, agreeable scoring: **flip shadow mode off** in settings. Rejections become invisible; quiet days send nothing.
6. Right-size `run_token_budget` from observed usage (aim ~2× a typical day so a burst doesn't abort a legitimate run).

Exit: shadow mode off, thresholds trusted, budget right-sized.

**Shadow mode was turned off on 2026-08-06**, at jon's direction, without waiting out the
1–2 week calibration. The gate is live: rejected items are now invisible and a quiet day sends
nothing. Immediately after the flip the briefing went from 21 cards to 15 — 6 rejections
suppressed.

Two consequences to keep in mind:

- **Calibration is now blind from the page.** The score badge was also removed from the
  briefing, so neither scores nor rejection reasons are visible there. Scores, reasons and the
  prompt version behind each judgment are all still in `/data/tattoo.db`; tuning means querying
  it (or turning shadow mode back on for a day) rather than skimming the briefing.
- **A false negative is now silent.** With the gate live, a source whose criteria are too
  strict simply stops appearing, and that looks identical to a quiet source.

## Phase 7 — observability wiring — ❌ NOT DOING (decided 2026-08-06)

> **The premise was wrong: there is no Alloy → Loki → Grafana stack on this NAS.** Verified
> 2026-08-06 — the running containers are tattoo, rally, puffin, freshrss, seerr, sabnzbd,
> radarr, Dozzle, sonarr, Plex, netdata. Nothing listens on 3000/3100/12345. The `otel-plugin`
> on 127.0.0.1:4317 belongs to **netdata**, not to a log pipeline.
>
> **jon's decision: skip it.** Dozzle already gives a searchable view of container
> stdout/stderr, which is the whole observability surface tattoo was designed around. Standing
> up Alloy + Loki + Grafana for one container and one reader is not worth the machinery, and
> the analytics it would provide (score trends, acquisition-method mix) are two SQLite queries
> against `/data/tattoo.db`.
>
> `docs/grafana-tattoo.json` stays in the repo unused, for whoever wants it later.

**Residual risk, accepted:** a dead container looks like a quiet day — no digest arrives, which
is indistinguishable from nothing scoring above threshold. Mitigations already in place:
`--restart unless-stopped` handles crashes, run failures send a priority −2 Pushover, and the
Unraid Docker tab plus netdata both show container state. The uncovered case is narrow: the
scheduler thread dying while uvicorn stays up. If that ever bites, the sanctioned fix is an
alert on the absence of a `run finished` line — **do not add a healthcheck endpoint to the
app** (plan §9).

## Phase 8 — ongoing operations (document, then done)

Leave a short ops note in the NAS documentation (wherever jon keeps it) covering:

- **Updates**: CI publishes `latest` on every merge to main. Update = pull + recreate the container (Unraid's built-in update button does this). Migrations run automatically at startup, preceded by an automatic DB snapshot into `/data/backups/`.
- **Config backup**: `Settings → Download TOML` exports sources/settings/prompts (secrets excluded) — worth committing to a private notes repo after calibration stabilizes. Restore via `POST /api/import/config`.
- **Adding sources later**: paste into settings from the phone share sheet; use disable (reversible) not purge; purge deletes cached content that cannot be refetched.
- **Failure modes**: run failures arrive as priority −2 Pushover notices; `aborted_budget` status means the token ceiling fired — check for a runaway source before raising it. A dead container looks like a quiet day by design — the Docker tab and Grafana are the checks.

---

## Final acceptance checklist

- [x] Image public on GHCR and pullable from the NAS
- [x] Container Up at 6565 — backups deliberately not configured (see Phase 2)
- [x] LLM and Pushover test buttons green; YouTube key set
- [x] All Phase-4 sources added with criteria; caps youtube 5 — ⚠️ CDC dropped (no live HAN feed), so no web source exists and the web/10 path is untested
- [ ] Two clean scheduled runs; transcripts fetching without IP blocking — ⚠️ run 1 hit YouTube IP blocking after ~17 transcripts
- [x] Locator links land on the right video timestamps — 71 links emitted; **spot-check by hand still outstanding** (Phase 5.3/5.4)
- [x] Shadow mode off (2026-08-06) — flipped early at jon's direction, without the 1–2 week calibration (see Phase 6)
- [x] ~~Grafana dashboard live~~ — Phase 7 dropped; Dozzle covers stdout/stderr (see Phase 7)
- [x] Structured logs — 100% of container stdout is JSON (app, uvicorn access/error, migrations), filterable in Dozzle
- [x] Failed extractions labelled "no summary" rather than rendering as bare cards
- [ ] Ops note written
