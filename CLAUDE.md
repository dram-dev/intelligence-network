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

## Status (2026-09-15)

Wave 1 built and tested (107 tests). Live-verified against the real feeds
(30 IL alert rows, 56 stations, LSR) and Google News RSS. **Not yet live**:
needs a BotFather token + admin chat id in `.env`, the Google OAuth client in
`secrets/`, `intelnet drive init`, then `bash scripts/install_launchd.sh`.

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
