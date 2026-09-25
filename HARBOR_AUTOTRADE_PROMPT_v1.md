# Harbor Autotrade Prompt v1 — LOCKED 2026-09-21 (no Armed)

## GC
- hlc3 / poles 4 / mult 1.414
- **1D = 144 · 4H = 72 · 1H = 48** · Lag/Fast OFF · closed bars only
- Filter = mid · Upper = upper · Lower = out of channel
- **Chase** = 加倉 · **Armed** = unused

## Universe (expanded)
- Own Radar scan: **MAX ≈ 280**, floor **dayNtlVlm ≥ $75k**, OI soft >0
- Plus Narrative watchlist · Cemetery path · Tier labels by mcap
- Momentum columns = rank/observe only (do not gate Base entry)

## Tier (mcap ONLY) — no “組”; Mega shares rules with Large; Small with Tiny
| Tier | Mcap | Primary exit | Hard SL |
|------|------|--------------|---------|
| Mega | ≥ $50B | 4H Filter cross down | **4H Lower** |
| Large | $2B – <$50B | 4H Filter cross down | **4H Lower** |
| Small | $200M – <$2B | 1H Lower cross down | 4H Filter mid |
| Tiny | <$200M | 1H Lower cross down | 4H Filter mid |

Unknown mcap → Tiny.

## Entry (all Tiers)
1. **Base**: 1D Upper dual_cross_up — Green or Red OK · signal **N** → fill **N+1**
2. **Chase**: 1D Green + 4H Green + 4H Upper dual_cross_up
3. **Size (equity %)** + Lev 1–5:
   - **P** ~**4–8%**
   - **P+N** ~**8–12%**
   - **P+CR** ~**8–12%**
   - **P+N+CR** ~**10–15%**
4. Notional ≥ $10 · **check liquidation**
5. **Liq must be &lt; Hard SL** (longs: liq below stop — Mega/Large stop=4H Lower; Small/Tiny stop=4H Filter). Else abort / reduce lev / skip.

## Exits (no Armed)
- Mega / Large → 4H Filter cross down
- Small / Tiny → 1H Lower cross down
- Hard SL: Mega/Large → **4H Lower**; Small/Tiny → 4H Filter mid

## Shorts
- Signum L15 unchanged

## Schedules
- 08:40 HKT daily · every 4h :11 exits + SL align to Filter
