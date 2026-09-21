# Own Trend Radar — Universe & Momentum (2026-09-21)

## Entry (LOCKED)

Daily TF GC entry is unchanged:

- `dual_cross_up = close > upper and prev_close <= prev_upper`
- `dual_cross_down_filter = close < filt and prev_close >= prev_filt`
- Filter / Upper / Lower Gaussian Channel math untouched
- Screener HOT signals are **not** used for entry

## Universe expansion

Default `MAX_SYMBOLS` raised **150 → 280** (`--max` still overrides).

Liquidity floors (in `scan_gc_radar.py`):

| Floor | Value | Notes |
|-------|-------|-------|
| `dayNtlVlm` | **≥ $75,000** | Mid of $50k–$100k; excludes dust |
| `openInterest` | **> 0** (soft) | When OI present in assetCtxs |
| Delisted / zero vol | excluded | Always |

If the `$75k` floor yields too few names, scanner falls back to top-N by volume (`vol>0`) then pads. Does **not** scan raw full HL with no floors.

**HL reality (2026-09-21 probe):** ~178 non-delisted perps; ~167 pass `$75k`. A `--max 250` / default-280 run effectively scans the full liquid set (~160–180).

Payload meta includes `universe_source`, `universe_floors`, `n_scanned`.

## Momentum layer (observe / rank only)

Computed from already-fetched candles (same bars as GC). **Does not change which rows get `dual_cross_up=true`.**

| Field | Meaning |
|-------|---------|
| `day_ntl_vlm` | HL `dayNtlVlm` from metaAndAssetCtxs |
| `open_interest` | HL OI when available |
| `rvol` | last closed bar volume / median(prior 14 bars) |
| `vol_accel` | alias of `rvol` |
| `vol_change_pct` | `(v_today - v_yday) / v_yday * 100` |
| `mom_score` | `log1p(rvol)` (+ small tanh tilt if vol rising) |
| `mom_rank` | 1 = highest `mom_score` in the scan |

**Sort priority for display/candidates:** `dual_cross_up` first, then `mom_score` desc, then symbol. Ranking only.

## Smoke results (2026-09-21 HKT)

- `--max 80` → n=75 in ~14s (fields OK)
- `--max 250` → **n=166** in ~91s; `dual_cross_up=[]`; top mom: AVAX, HMSTR, S, NIL, AR, …
- Before this change: n≈137 / MAX=150 / no mom fields
- Railway sync: OK (`out/gc_radar_1d.json` pushed)

```bash
python scan_gc_radar.py --tf 1d --max 80    # quick
python scan_gc_radar.py --tf 1d --max 250   # full liquid set
```


## Liquidity floor clarification

The **$75k** universe floor is Hyperliquid **`dayNtlVlm`** = **24h day notional trading volume in USD**
(not open interest, not market cap).

## Size tier + category (row labels)

Each radar row includes:

| Field | Source |
|-------|--------|
| `tier` | Circulating mcap → `mega` (≥$50B) / `large` ($2B–<$50B) / `small` ($200M–<$2B) / `tiny` (<$200M or unknown). See `mcap_tiers.py`. |
| `mcap_usd` / `mcap_known` | From `data/mcap_cache.json` (+ `out/` override) |
| `category` | Primary sleeve: **Narrative** (watchlist) → else **Cemetery** (`drop_from_ath_pct` ≥ 70) → else **Price** |
| `categories` | Optional multi-label array |

UI Radar table shows sortable **tier** and **category** columns.
