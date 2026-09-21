# Own Trend Radar — Multi-TF GC Scan

Lean DRY_RUN scanner for Hyperliquid perps using DonovanWall Gaussian Channel
math (Signum Strategy v3.3 params). Supports **1h / 4h / 1d**.

## GC params (locked)

| Param | Value |
|-------|-------|
| Source | hlc3 |
| Poles | 4 |
| Period | 144 |
| Mult | 1.414 |
| Reduced Lag | ON |
| Fast Response | ON |

Ported from `/workspace/signum-compat-gc/gc.ts`.

## Universe & momentum

See **[UNIVERSE.md](UNIVERSE.md)**: default ~280 liquid HL names (`dayNtlVlm ≥ $75k`, soft OI>0).
Momentum columns (`rvol` / `vol_accel` / `mom_score`) are **observe/rank only** — GC `dual_cross_up` entry math is unchanged.
Each row also gets **tier** (mcap Mega/Large/Small/Tiny) and **category** (Narrative/Cemetery/Price). `$75k` floor = HL `dayNtlVlm` (24h notional volume USD). See [UNIVERSE.md](UNIVERSE.md) / [RAILWAY_CRON.md](RAILWAY_CRON.md) (1h+4h every 15m).

## Run scan

```bash
cd /workspace/own-trend-radar

# All three timeframes (default)
python scan_gc_radar.py

# Single TF
python scan_gc_radar.py --tf 1d
python scan_gc_radar.py --tf 4h
python scan_gc_radar.py --tf 1h

# Subset / smaller universe
python scan_gc_radar.py --tf 1h,4h --max 80

# Back-compat daily-only wrapper
python scan_daily_gc.py
```

No API keys. Uses public HL `info` endpoint only. No orders.

HL `candleSnapshot` intervals used: `1h`, `4h`, `1d` (all natively supported).

### Bar / warmup windows

| TF | Bars fetched | Notes |
|----|--------------|-------|
| 1d | ~280 | existing daily window |
| 4h | ~450 | period=144 × 4h ≈ 24d minimum |
| 1h | ~550 | 500–600 bar warmup |

Concurrency default **4**; TFs run **sequentially** in one command to avoid hammering HL.

## Live-tonight UI

```bash
cd /workspace/own-trend-radar
python serve.py
# open http://127.0.0.1:8787/
```

- TF tabs: **1H | 4H | 1D** — loads `out/gc_radar_{tf}.json`
- Auto-refresh every 60s
- Optional rescan: `POST /api/rescan` runs `scan_gc_radar.py --tf all` (30 min timeout)
- Manual rescan: `python scan_gc_radar.py` then refresh the page

### Offline snapshot (no server)

Open `ui_snapshot.html` in a browser (`file://`). It inlines available TF JSON
(preferably all three; at least 1d). Missing TFs need the live server.

```bash
python scan_gc_radar.py
python build_snapshot.py
```

## Universe (Signum-scale Top 100–150)

1. **Primary:** HL `metaAndAssetCtxs` ranked by `dayNtlVlm` (24h notional) — top **150**
2. **Fallback if vols are zero:** `/workspace/hl_volume_top100.json`, then pad from meta universe names to 150
3. **Secondary only:** `own_radar_candidates_base_v0.json` may filter/reorder; never the primary cap

Same universe is shared across all TFs in one run.

## Outputs

| File | Description |
|------|-------------|
| `out/gc_radar_1h.json` / `.csv` | 1h scan |
| `out/gc_radar_4h.json` / `.csv` | 4h scan |
| `out/gc_radar_1d.json` / `.csv` | 1d scan |
| `out/daily_gc_radar.json` / `.csv` | **alias copy of 1d** (back-compat) |
| `ui.html` | live desk UI (needs `serve.py`) |
| `ui_snapshot.html` | self-contained snapshot for `file://` |

Each JSON includes `tf`, `ts`, `breadth`, `rows` (same row schema as before).

### Per-row fields

- `symbol`, `close`, `filter`, `upper`, `lower`
- `trend` — Green if filter > filter[1], else Red
- `above_upper` — close > upper
- `dual_cross_up` — long signal: close > upper and prev_close ≤ prev_upper
- `dual_cross_down_filter` — short hedge proxy: close crosses down through filter

### Breadth

`green_count`, `red_count`, `green_pct` over successfully scanned symbols (per TF).

## Dependencies

Python 3 stdlib + `requests` if available (falls back to `urllib`).

## Railway (phone / app)

Env: `COCKPIT_PASSWORD`, `PORT=8080`, `RAILWAY=1`.

Sync from Harbor after desk write:
```bash
curl -X POST "$URL/api/sync" \
  -H "Authorization: Bearer $COCKPIT_PASSWORD" \
  -H "Content-Type: application/json" \
  -d "{"files":{"out/desk_daily.json":$(jq -c . out/desk_daily.json | jq -Rs .)}}"
```
