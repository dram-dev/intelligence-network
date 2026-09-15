# intelligence-network — Claude context

User nickname: **"the intelligence network"** / **"intelnet"**. Third digest
project after **pc-insurance-digest** ("PC Digest") and **macro-ai-digest**
("the macro digest"); both live as siblings under `~/Projects`.

## What this project is

A **decentralized sensor network** (mesonet idea, scaled to people) plus the
classic curated / score-based digest. Anyone on Telegram is a sensor; their
readings are expressed in a **common data language** (topic packs), checked
against neighbours and official feeds (corroboration → trust → events), and
pushed to subscribers by category × area. The daily digest is a **Google Doc
in a Drive folder** (not a local file / not Obsidian) that people subscribe
to. Weather first; **Illinois** ground at county / ZIP / ZIP+4 grain — go
finer, not to more states.

Design rules the user set (2026-09-15):
- Telegram is the primary push AND pull mechanism.
- The digest is a Google location people can subscribe to, not a filesystem.
- The network must work for any topic with a common data language — nothing
  weather-specific in code; all of it in `config/topics/<topic>.yaml`.
- Geo ground = Illinois; granularity = counties + ZIP+4 (not more states).

## Shape

```
Telegram / CLI / JSON ──▶ language.parse (grammar; LLM fallback for prose)
        │                        │
        ▼                        ▼
  contrib.contribute ──▶ network.process: store → corroborate (both ways) → trust
        │                      → judge quality → open/join county event → score
        ▼
  subscriptions.fanout_{report,event}   (dedup per chat via notify_log)

feeds (nws_alerts · iem_lsr · iem_asos) ──▶ same Signal table as reference sensors
        └─▶ subscriptions.fanout_alert (per county, routed by severity)

pipeline (01:10, under digest_core.runlock) ──▶ digest.build → gdrive.publish
notify (08:00) ──▶ subscriptions.fanout_digest
```

## Key facts / decisions

- **Signal** (`models.py`) is the unit: metric, canonical value (or flag=1),
  observed/received, Location (lat/lon, county_fips, zip5, zip9, precision),
  sensor id/kind, quality (`raw → corroborated | flagged | rejected`;
  `reference` for official), corroboration/contradiction counts,
  reference_agreement, group_key (one NWS alert = many county rows),
  evidence/raw JSON, event_id. UNIQUE(source, source_id) dedups re-delivery.
- **Sensor kinds**: human, bot, station (ASOS, trust 0.9), official (LSR per
  WFO, 0.95), authority (NWS alerts, 1.0). Reference kinds skip corroboration
  but settle nearby human readings (forward) and drive events.
- **Trust** = `(K·prior + agreements)/(K + agreements + disagreements)`, K=4,
  prior 0.5 unless `/admin trust` set `trust_prior`. Trusted ≥ 0.8 can push
  an event alone.
- **Event score** = `severity × (1 + 0.5·ln(n_sensors)) × clamp(trust/0.5,
  0.4..2) × {1.25 official agree, 0.7 disagree, 1.0}`; push when score ≥
  `EVENT_PUSH_MIN_SCORE` (1.0) AND verified (≥2 sensors | official | trusted);
  re-push when score grows ×1.4. Idle 6h → closed.
- **Areas**: `il` ⊃ `il.<countyslug>` ⊃ `il.zip.<zip5>` ⊃ `il.zip.<zip9>`.
  County slug = lowercase alnum (`stclair`, `jodaviess`). Digest subscriptions
  are state-wide by construction.
- **ZIP+4** has no free geocode: kept as the finest key, located at the ZCTA5
  centroid unless the sensor shares a Telegram location. Vendored Census
  tables in `config/geo/` (102 counties, 1,396 ZCTAs). Point→county via
  api.weather.gov `/points` (cached in kv by geohash-6), nearest-centroid
  fallback; `GEO_ONLINE_LOOKUP=false` in tests.
- **Topic pack** (`config/topics/weather.yaml`): metrics (aliases, units w/
  factor or `expr` in x — sandboxed AST), `default_unit` (US-typed inches /
  mph / °F), `words` (hail chart), range, tolerance (abs/rel), radius_km,
  window_min, event thresholds+severity, categories, alert_routing,
  alert_support (which active alerts back a metric), lsr_types,
  station_fields. `topics.py` loads all packs; `find_metric` resolves aliases.
- **Personas**: contributor, grower/land manager, spotter/EM, subscriber,
  researcher/analyst, steward — see README "Who it's for".
- **LLM use** (all best-effort, `LLM_ENABLED=false` in tests): free-text
  parse (Ollama `local_qwen`), news triage (Ollama), digest narrative (MLX).
  Same shared servers as the other digests → the daily pipeline holds the
  cross-digest run lock (`pipeline_serialize("intelligence-network")`).
  The bot's per-message LLM parse does NOT take the run lock (interactive).
- **Google Drive**: `drive.file` scope; folder + Latest doc ids in `kv`;
  `publish()` creates/updates the day's doc and rewrites Latest;
  `add_reader(email)` shares the folder (`/digest email` on Telegram);
  `GDRIVE_PUBLIC_LINK` makes the folder anyone-with-link. Fake service in
  tests.
- **Quiet hours** apply only to the digest ping (`notify` 08:00). Warnings /
  events / reports are opt-in and never suppressed.
- **Telegram**: `telegram.Bot(TelegramNotifier)` adds `send_to(chat_id)`.
  New bot token (do not reuse the PC/macro bots). Admin = `TELEGRAM_ADMIN_CHAT_ID`.
  Open registration unless `NETWORK_JOIN_CODE`; auto-join on first reading.
  Rate limit `NETWORK_RATE_LIMIT` per 10 min; `/admin ban`.
- **digest-core** is consumed as an editable path dep from
  `../pc-insurance-digest/packages/digest-core` (like macro). CI checks out
  both repos side by side.

## Schedule (Mac mini launchd; `scripts/install_launchd.sh`)

| job | when |
|---|---|
| `com.dr.intelnet.bot` | KeepAlive (SuccessfulExit=false) |
| `com.dr.intelnet.watch` | every 300 s (alerts + LSR; stations gated to 60 min) |
| `com.dr.intelnet.daily` | 01:10 — third in the queue: macro 01:00 → PC 01:05 → this |
| `com.dr.intelnet.notify` | 08:00 digest ping |

## Topic packs (2026-09-15, wave 2)

Five packs: `weather`, `soil`, `water`, `agriculture`, `air`. `language.parse`
reads ALL packs at once (scope `*`); aliases must be unique across packs
(`tests/test_packs.py` enforces). Bare `events` = `weather.events`;
`soil.events` explicit; `*.events` expands to every pack. Extra mapping
sections in a pack (`usgs_parameters`, `awdb_elements`) land in
`Topic.mappings`. New feeds: `usgs_water` (NWIS IV, hourly gate),
`nrcs_scan` (AWDB, daily; IL has one station), `usdm` (Drought Monitor county
API, daily; aoi = comma-separated county FIPS). Events anchor on the reading's
observed time (`find_open_event(around=…)`), so backfills/late readings join
the right event.

## Site + snapshot

`export.snapshot()` → 10 public JSON docs (anonymised: humans as `s-xxxxx` +
county only). `site/index.fragment.html` (Fraunces / IBM Plex; light+dark
tokens; D3 v7 from cdnjs) is wrapped into `docs/index.html` with the data
inlined; the same fragment publishes as a Claude artifact. `intelnet
demo-seed` builds the sample fortnight; `intelnet export --sample` rebuilds the
site from it. Pipeline stage 5 re-exports after each run; `SITE_AUTO_PUSH`
pushes `docs/`. Pages workflow in `.github/workflows/pages.yml` (needs a public
repo on the free plan).

## Public launch (2026-09-15)

Repo is **public**; GitHub Pages serves `docs/` at https://dram-dev.github.io/intelligence-network/
(+ `privacy.html`, `terms.html` rendered from `site/` with `{{PLACEHOLDERS}}` by
`export.render_static_pages`). `SITE_AUTO_PUSH=true` → the nightly run commits + pushes `docs/`.
Public identity = **ilintelligencenetwork@gmail.com** (`NETWORK_CONTACT_EMAIL`, Drive owner via
`GDRIVE_ACCOUNT`). Privacy rules the code enforces: report pushes, digest and site show a contributor
only as `public_handle()` + ZIP5/county (`Location.describe_public`); `/forget confirm` deletes a
person's sensor, readings, subscriptions, e-mail (+ Drive folder share). Google OAuth app: Branding
values in `secrets/README.md`; after "Publish app", re-run `drive init --remote` once.

## Status (2026-09-15)

Waves 1–2 built and tested. Live-verified feeds: NWS alerts, IEM LSR/ASOS,
USGS IV (278 IL sites), NRCS SCAN (Mason), USDM county stats, Google News.
Bot handle `@intelligence_network_bot` exists. **Turn-on checklist** =
`uv run intelnet setup`: token + admin chat id in `.env`, Google OAuth client
in `secrets/`, `intelnet drive init`, `bash scripts/install_launchd.sh`.

## Next ideas

- A second topic pack (air quality / river gauges) to prove the language.
- Photo evidence: download + attach to the Drive doc (currently file_id only).
- A per-ZIP+4 mesh view once there are enough human sensors to matter.
- Sensor "beats": prompt quiet counties' sensors for a reading when a warning
  is issued for their county (pull-to-push).
- Public read-only web view of the mesh (stdlib server, like PC's `digest web`).

## How to run

```bash
uv sync && cp .env.example .env && uv run intelnet init-db
uv run intelnet watch · signal "rain 0.4in @62704" · near 62704 · digest --html out.html
uv run intelnet drive init · pipeline --run-type manual · health
uv run pytest
```
