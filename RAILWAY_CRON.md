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

| TF | When (UTC) | HKT |
|----|------------|-----|
| 1D | daily **00:05** | 08:05 |
| 4H | **00:05, 04:05, 08:05, 12:05, 16:05, 20:05** | +8h |
| 1H | every hour at **:05** | +8h |

Logic lives in `serve.py` (`_scheduler_loop`). Enabled when `RAILWAY=1` or
`OTR_SCHEDULER=1`.

Env knobs:

- `OTR_SCHEDULER=1|0` — force on/off
- `OTR_SCAN_MAX` — default 280 (1d/4h)
- `OTR_SCAN_MAX_1H` — default 200 (lighter hourly)
- `OTR_SCAN_CONCURRENCY` — default 2

On boot, if any `gc_radar_*.json` is missing, one background scan runs after ~15s.

## Image contents

Dockerfile bakes:

- `serve.py`, `ui.html`, `scan_gc_radar.py`
- `data/own_radar_candidates_base_v0.json` (optional secondary filter)
- `data/hl_volume_top100.json` (fallback if live dayNtlVlm is empty)

Universe primary source remains live Hyperliquid `metaAndAssetCtxs` (stdlib urllib).

## Harbor `/api/sync`

Still supported for desk_daily / narrative / optional radar override.
**Merge-only:** only paths present in the POST body are written; omitted
`gc_radar_*.json` files are **not** deleted. Scheduler and Harbor can coexist.

## Manual rescan

`POST /api/rescan` (password / Bearer same as UI):

```json
{"tf": "4h", "max": 280}
```

Returns 409 if a scan is already running.

## Narrative

**Narrative stays on Grok.ai / Harbor** — this cron does **not** refresh narrative
overlays. Only GC radar JSON is self-refreshed on Railway.

## Deploy checklist

1. Volume mounted at `/app/out`
2. Push to `main` (Dockerfile builder via `railway.json`)
3. Confirm `/health` → `{"scheduler": true, "scanner": true}`
4. After next `:05` UTC (or boot missing-file scan), check `out/gc_radar_*.json` mtimes
