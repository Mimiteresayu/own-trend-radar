# Railway self-refresh (no Harbor / Grok Bot credit)

Cockpit runs **one web service** (`cockpit`) that serves the UI and a background
scheduler. Scans write `out/gc_radar_{1d,4h,1h}.json` on the container filesystem.

## Volume (required)

Mount a persistent volume at **`/app/out`** on the `cockpit` service.

Without it, every redeploy wipes radar JSON. Harbor `/api/sync` also writes under
`/app/out` — same volume keeps desk/narrative and radar together.

Railway UI: Project → cockpit → Settings → Volumes → Add volume → Mount path `/app/out`.

Or via Railway MCP: `create-volume` with `mountPath=/app/out` and `serviceId=cockpit`.

## Schedules (UTC; HKT = UTC+8)

| TF | When (UTC) | HKT | Notes |
|----|------------|-----|-------|
| **1H + 4H** | every **15 minutes** (`:00/:15/:30/:45`) | +8h | Exit radar — replaces old hourly / 4h-bar schedules |
| **1D** | daily **00:05** | 08:05 | Daily bars; not on the 15m tick |

Logic lives in `serve.py` (`_scheduler_loop`). Enabled when `RAILWAY=1` or
`OTR_SCHEDULER=1`.

Env knobs:

- `OTR_SCHEDULER=1|0` — force on/off
- `OTR_SCAN_INTERVAL_MIN` — default **15** (1h+4h cadence)
- `OTR_SCAN_MAX` — default 280 (4h / 1d)
- `OTR_SCAN_MAX_1H` — default 200 (lighter on 15m ticks)
- `OTR_SCAN_CONCURRENCY` — default **2** (keep modest to avoid HL 429s)

On boot, if any `gc_radar_*.json` is missing, one background scan runs after ~15s.

**Why not all three every 15m?** 1d candles do not need sub-hour refresh; scanning
1h+4h @15m already covers exit monitoring. 1d stays daily @00:05 UTC.

## Image contents

Dockerfile bakes:

- `serve.py`, `ui.html`, `scan_gc_radar.py`, `mcap_tiers.py`
- `data/own_radar_candidates_base_v0.json` (optional secondary filter)
- `data/hl_volume_top100.json` (fallback if live dayNtlVlm is empty)
- `data/mcap_cache.json` (circulating mcap USD → Mega/Large/Small/Tiny)
- `data/narrative_watchlist.json` + `narrative/watchlist.json` (Narrative category)

Universe primary source remains live Hyperliquid `metaAndAssetCtxs` (stdlib urllib).

Row fields added each scan: `tier`, `mcap_usd`, `mcap_known`, `category`, `categories`.

## Harbor `/api/sync`

Still supported for desk_daily / narrative / optional radar override.
**Merge-only:** only paths present in the POST body are written; omitted
`gc_radar_*.json` files are **not** deleted. Scheduler and Harbor can coexist.

## Manual rescan

`POST /api/rescan` (password / Bearer same as UI):

```json
{"tf": "1d", "max": 280}
```

Returns 409 if a scan is already running.

## Narrative

**Narrative stays on Grok.ai / Harbor** — this cron does **not** refresh narrative
overlays. Only GC radar JSON is self-refreshed on Railway. Category labeling uses
the baked / synced watchlist when present.

## Deploy checklist

1. Volume mounted at `/app/out`
2. Push to `main` (Dockerfile builder via `railway.json`)
3. Confirm `/health` → `{"scheduler": true, "scanner": true}`
4. After next 15m tick (or boot missing-file scan), check `out/gc_radar_*.json` mtimes
   and that rows include `tier` + `category`

## Narrative prompt

Paste-ready Grok.ai automation prompt: [`GROK_AI_NARRATIVE_PROMPT.md`](./GROK_AI_NARRATIVE_PROMPT.md).
Suggested daily **07:30 HKT**. Output → `narrative/watchlist.json` → Harbor `/api/sync`.
