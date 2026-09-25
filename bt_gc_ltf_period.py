#!/usr/bin/env python3
"""Lean GC LTF Period BT: config A (144) vs C candidates for Harbor exit monitoring.

FIXED: poles=4, mult=1.414, source=hlc3, Reduced Lag OFF, Fast Response OFF.
Entry SoT: 1D dual_cross_up with period=144 (unchanged).
Exits compared on 4H / 1H Filter-stop and Upper-loss across Period variants.
DRY_RUN — no orders.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from scan_gc_radar import compute_gc, hl_post  # noqa: E402

HKT = timezone(timedelta(hours=8))
OUT_DIR = os.path.join(ROOT, "out")
COINS = ["NIL", "TRX", "AVNT"]
HL_INFO = "https://api.hyperliquid.xyz/info"

POLES = 4
MULT = 1.414
ENTRY_PERIOD = 144  # 1D SoT — do not change
REDUCED_LAG = False
FAST_RESPONSE = False

# Config A + C candidates
PERIODS_4H = [144, 72, 96]       # A=144; C={72,96}
PERIODS_1H = [144, 48, 72, 96]   # A=144; C={48,72,96}

TF_FETCH = {
    "1d": {"interval": "1d", "bar_ms": 86400_000, "n_bars": 450},
    "4h": {"interval": "4h", "bar_ms": 4 * 3600_000, "n_bars": 900},
    "1h": {"interval": "1h", "bar_ms": 3600_000, "n_bars": 2000},
}


def fetch_candles(coin: str, tf: str) -> List[dict]:
    cfg = TF_FETCH[tf]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(cfg["n_bars"]) * int(cfg["bar_ms"]) - int(cfg["bar_ms"])
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": cfg["interval"],
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    time.sleep(0.25)
    raw = hl_post(body)
    if not isinstance(raw, list):
        return []
    bars: List[dict] = []
    for c in raw:
        try:
            bars.append(
                {
                    "t": int(c["t"]),
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c.get("v") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b["t"])
    return bars


def gc_series(bars: List[dict], period: int) -> List[Dict[str, float]]:
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    return compute_gc(
        h, l, c,
        poles=POLES,
        period=period,
        mult=MULT,
        reduced_lag=REDUCED_LAG,
        fast_response=FAST_RESPONSE,
    )


def find_dual_cross_up_entries(bars_1d: List[dict], gc_1d: List[Dict[str, float]]) -> List[dict]:
    """Synthetic entries when 1D close crosses above Upper (period=144). Entry at next bar open if available else close."""
    entries = []
    n = len(bars_1d)
    # skip forming bar: use up to n-2 as signal bar, enter at n-1 open conceptually;
    # for historical BT use closed bars only: signal on i, fill at i close (conservative) or i+1 open.
    warmup = ENTRY_PERIOD + 10
    for i in range(max(warmup, 1), n - 1):  # leave last as open/forming; need i+1 for fill
        prev_c = bars_1d[i - 1]["close"]
        prev_u = gc_1d[i - 1]["upper"]
        c = bars_1d[i]["close"]
        u = gc_1d[i]["upper"]
        if c > u and prev_c <= prev_u:
            fill_i = i + 1  # next daily open
            if fill_i >= n:
                continue
            entries.append(
                {
                    "signal_i": i,
                    "signal_t": bars_1d[i]["t"],
                    "entry_i": fill_i,
                    "entry_t": bars_1d[fill_i]["t"],
                    "entry_px": bars_1d[fill_i]["open"],
                }
            )
    return entries


def ltf_index_at_or_after(bars: List[dict], t_ms: int) -> Optional[int]:
    for i, b in enumerate(bars):
        if b["t"] >= t_ms:
            return i
    return None


def simulate_trade(
    entry_px: float,
    entry_t: int,
    bars_ltf: List[dict],
    gc_ltf: List[Dict[str, float]],
    exit_mode: str,
    bar_ms: int,
    max_hold_bars: Optional[int] = None,
) -> Optional[dict]:
    """Long-only. exit_mode: 'filter' | 'upper_loss'.
    filter: exit when close < Filter
    upper_loss: exit when close < Upper after having been above Upper post-entry
    """
    start = ltf_index_at_or_after(bars_ltf, entry_t)
    if start is None or start >= len(bars_ltf) - 1:
        return None
    # start monitoring from first LTF bar at/after entry; need closed bars
    # skip forming last bar
    end_lim = len(bars_ltf) - 1
    if start >= end_lim:
        return None

    been_above_upper = False
    mfe_pct = 0.0
    mae_pct = 0.0
    filter_at_entry = gc_ltf[start]["filter"]
    # fake-break: Filter still below entry after ~3 calendar days
    three_day_ms = 3 * 86400_000
    filter_below_at_3d: Optional[bool] = None

    exit_i = None
    exit_reason = "open"
    for i in range(start, end_lim):
        close = bars_ltf[i]["close"]
        high = bars_ltf[i]["high"]
        low = bars_ltf[i]["low"]
        filt = gc_ltf[i]["filter"]
        upper = gc_ltf[i]["upper"]

        fav = (high - entry_px) / entry_px * 100.0
        adv = (low - entry_px) / entry_px * 100.0
        if fav > mfe_pct:
            mfe_pct = fav
        if adv < mae_pct:
            mae_pct = adv

        elapsed = bars_ltf[i]["t"] - entry_t
        if filter_below_at_3d is None and elapsed >= three_day_ms:
            filter_below_at_3d = filt < entry_px

        if exit_mode == "filter":
            if close < filt:
                exit_i = i
                exit_reason = "filter_stop"
                break
        elif exit_mode == "upper_loss":
            if close > upper:
                been_above_upper = True
            if been_above_upper and close < upper:
                exit_i = i
                exit_reason = "upper_loss"
                break
            # hard safety: also stop if close < filter (ruin protection)
            if close < filt:
                exit_i = i
                exit_reason = "filter_stop_fallback"
                break
        else:
            raise ValueError(exit_mode)

        if max_hold_bars is not None and (i - start + 1) >= max_hold_bars:
            exit_i = i
            exit_reason = "max_hold"
            break

    if exit_i is None:
        # still open — mark-to-market at last closed
        exit_i = end_lim - 1
        if exit_i < start:
            return None
        exit_reason = "open_mtm"

    # if 3d never reached, evaluate at last available
    if filter_below_at_3d is None:
        filter_below_at_3d = gc_ltf[exit_i]["filter"] < entry_px

    exit_px = bars_ltf[exit_i]["close"]
    pnl_pct = (exit_px - entry_px) / entry_px * 100.0
    hold_bars = exit_i - start + 1
    return {
        "entry_t": entry_t,
        "entry_px": entry_px,
        "exit_t": bars_ltf[exit_i]["t"],
        "exit_px": exit_px,
        "exit_reason": exit_reason,
        "pnl_pct": pnl_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "hold_bars": hold_bars,
        "filter_at_entry": filter_at_entry,
        "filter_below_entry_at_3d": bool(filter_below_at_3d),
        "mfe_minus_pnl": mfe_pct - pnl_pct,
    }


def summarize(trades: List[dict]) -> Dict[str, Any]:
    if not trades:
        return {
            "n": 0,
            "win_rate": None,
            "avg_pnl_pct": None,
            "median_pnl_pct": None,
            "max_dd_trade_pct": None,
            "avg_hold_bars": None,
            "fake_break_rate": None,
            "avg_mfe_pct": None,
            "avg_mfe_minus_pnl": None,
            "closed_n": 0,
        }
    closed = [t for t in trades if t["exit_reason"] != "open_mtm"]
    use = closed if closed else trades
    pnls = [t["pnl_pct"] for t in use]
    wins = sum(1 for p in pnls if p > 0)
    maes = [t["mae_pct"] for t in use]
    return {
        "n": len(trades),
        "closed_n": len(closed),
        "open_n": len(trades) - len(closed),
        "win_rate": round(wins / len(use) * 100, 1) if use else None,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3) if pnls else None,
        "median_pnl_pct": round(float(median(pnls)), 3) if pnls else None,
        "max_dd_trade_pct": round(min(maes), 3) if maes else None,  # worst MAE
        "avg_hold_bars": round(sum(t["hold_bars"] for t in use) / len(use), 1) if use else None,
        "fake_break_rate": round(
            sum(1 for t in use if t["filter_below_entry_at_3d"]) / len(use) * 100, 1
        )
        if use
        else None,
        "avg_mfe_pct": round(sum(t["mfe_pct"] for t in use) / len(use), 3) if use else None,
        "avg_mfe_minus_pnl": round(sum(t["mfe_minus_pnl"] for t in use) / len(use), 3)
        if use
        else None,
    }


def ts_hkt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, HKT).strftime("%Y-%m-%d %H:%M HKT")


def run() -> Dict[str, Any]:
    os.makedirs(OUT_DIR, exist_ok=True)
    results: Dict[str, Any] = {
        "ts_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT"),
        "fixed": {
            "poles": POLES,
            "mult": MULT,
            "source": "hlc3",
            "reduced_lag": False,
            "fast_response": False,
            "entry_sot": "1d dual_cross_up period=144",
        },
        "config_A": {"4h": 144, "1h": 144},
        "config_C_candidates": {"4h": [72, 96], "1h": [48, 72, 96]},
        "coins": {},
        "aggregate": {},
        "recommendation": {},
    }

    # per (tf, period, exit_mode) collect all trades across coins
    agg_bucket: Dict[str, List[dict]] = {}

    for coin in COINS:
        print(f"=== {coin} ===", flush=True)
        bars_1d = fetch_candles(coin, "1d")
        bars_4h = fetch_candles(coin, "4h")
        bars_1h = fetch_candles(coin, "1h")
        print(
            f"  bars 1d={len(bars_1d)} 4h={len(bars_4h)} 1h={len(bars_1h)} "
            f"1d_range={ts_hkt(bars_1d[0]['t']) if bars_1d else '-'} → "
            f"{ts_hkt(bars_1d[-1]['t']) if bars_1d else '-'}",
            flush=True,
        )
        if len(bars_1d) < ENTRY_PERIOD + 30:
            print(f"  [skip] insufficient 1d history", flush=True)
            continue

        gc_1d = gc_series(bars_1d, ENTRY_PERIOD)
        entries = find_dual_cross_up_entries(bars_1d, gc_1d)
        print(f"  1D dual_cross_up entries: {len(entries)}", flush=True)
        for e in entries:
            print(
                f"    entry {ts_hkt(e['entry_t'])} @ {e['entry_px']:.6g}",
                flush=True,
            )

        coin_out: Dict[str, Any] = {
            "bars": {"1d": len(bars_1d), "4h": len(bars_4h), "1h": len(bars_1h)},
            "n_entries": len(entries),
            "entries": [
                {
                    "entry_t_hkt": ts_hkt(e["entry_t"]),
                    "entry_px": e["entry_px"],
                    "signal_t_hkt": ts_hkt(e["signal_t"]),
                }
                for e in entries
            ],
            "variants": {},
        }

        # precompute GC per period
        gc4: Dict[int, List] = {}
        for p in PERIODS_4H:
            if len(bars_4h) >= p + 20:
                gc4[p] = gc_series(bars_4h, p)
            else:
                print(f"  [warn] 4h period={p}: only {len(bars_4h)} bars", flush=True)

        gc1: Dict[int, List] = {}
        for p in PERIODS_1H:
            if len(bars_1h) >= p + 20:
                gc1[p] = gc_series(bars_1h, p)
            else:
                print(f"  [warn] 1h period={p}: only {len(bars_1h)} bars", flush=True)

        for tf, periods, bars, gc_map, bar_ms in [
            ("4h", PERIODS_4H, bars_4h, gc4, TF_FETCH["4h"]["bar_ms"]),
            ("1h", PERIODS_1H, bars_1h, gc1, TF_FETCH["1h"]["bar_ms"]),
        ]:
            for period in periods:
                if period not in gc_map:
                    continue
                gc = gc_map[period]
                for mode in ("filter", "upper_loss"):
                    key = f"{tf}_p{period}_{mode}"
                    trades = []
                    for e in entries:
                        # only use entries where LTF GC is warmed at entry
                        si = ltf_index_at_or_after(bars, e["entry_t"])
                        if si is None or si < period + 10:
                            continue
                        tr = simulate_trade(
                            e["entry_px"], e["entry_t"], bars, gc, mode, bar_ms
                        )
                        if tr:
                            tr["coin"] = coin
                            tr["entry_t_hkt"] = ts_hkt(tr["entry_t"])
                            tr["exit_t_hkt"] = ts_hkt(tr["exit_t"])
                            trades.append(tr)
                    summ = summarize(trades)
                    coin_out["variants"][key] = {
                        "summary": summ,
                        "trades": [
                            {
                                "entry_t_hkt": t["entry_t_hkt"],
                                "exit_t_hkt": t["exit_t_hkt"],
                                "entry_px": round(t["entry_px"], 8),
                                "exit_px": round(t["exit_px"], 8),
                                "pnl_pct": round(t["pnl_pct"], 3),
                                "mfe_pct": round(t["mfe_pct"], 3),
                                "mae_pct": round(t["mae_pct"], 3),
                                "hold_bars": t["hold_bars"],
                                "exit_reason": t["exit_reason"],
                                "filter_below_entry_at_3d": t["filter_below_entry_at_3d"],
                                "mfe_minus_pnl": round(t["mfe_minus_pnl"], 3),
                            }
                            for t in trades
                        ],
                    }
                    agg_bucket.setdefault(key, []).extend(trades)

        results["coins"][coin] = coin_out

    # aggregate summaries
    for key, trades in sorted(agg_bucket.items()):
        results["aggregate"][key] = summarize(trades)

    # Recommendation logic
    def pick_best(tf: str, periods: List[int], mode: str = "filter") -> Dict[str, Any]:
        rows = []
        for p in periods:
            key = f"{tf}_p{p}_{mode}"
            s = results["aggregate"].get(key)
            if not s or not s.get("n"):
                continue
            # score: prefer higher median pnl, then avg pnl, penalize high fake_break_rate
            med = s["median_pnl_pct"] if s["median_pnl_pct"] is not None else -999
            avg = s["avg_pnl_pct"] if s["avg_pnl_pct"] is not None else -999
            fb = s["fake_break_rate"] if s["fake_break_rate"] is not None else 100
            # capture efficiency: lower mfe_minus_pnl is better (less left on table) but only if pnl ok
            score = med * 1.5 + avg - 0.05 * fb
            rows.append({"period": p, "key": key, "summary": s, "score": round(score, 3)})
        rows.sort(key=lambda r: r["score"], reverse=True)
        return {"ranked": rows, "best": rows[0] if rows else None}

    rec_4h_f = pick_best("4h", PERIODS_4H, "filter")
    rec_1h_f = pick_best("1h", PERIODS_1H, "filter")
    rec_4h_u = pick_best("4h", PERIODS_4H, "upper_loss")
    rec_1h_u = pick_best("1h", PERIODS_1H, "upper_loss")

    # NIL spike deep-dive: compare A vs best C on filter exit
    nil_spike = {}
    if "NIL" in results["coins"]:
        for key, v in results["coins"]["NIL"]["variants"].items():
            if "filter" in key:
                nil_spike[key] = {
                    "summary": v["summary"],
                    "trades": v["trades"],
                }

    total_n = sum(results["coins"][c]["n_entries"] for c in results["coins"])
    confidence = "low"
    if total_n >= 15:
        confidence = "medium"
    if total_n >= 40:
        confidence = "moderate-high"
    # still small universe of 3 coins
    if total_n < 25:
        confidence = "low (small sample: 3 coins)"

    a_4h = results["aggregate"].get("4h_p144_filter")
    a_1h = results["aggregate"].get("1h_p144_filter")
    best_4h = rec_4h_f["best"]
    best_1h = rec_1h_f["best"]

    # Prefer C only if clearly better than A on median+avg; else keep A
    def clearly_beats(best, a_summ, margin: float = 0.5) -> bool:
        if not best or not a_summ or a_summ.get("n", 0) == 0:
            return False
        if best["period"] == 144:
            return False
        bs = best["summary"]
        if bs.get("median_pnl_pct") is None or a_summ.get("median_pnl_pct") is None:
            return False
        return (bs["median_pnl_pct"] - a_summ["median_pnl_pct"]) >= margin and (
            (bs["avg_pnl_pct"] or -999) >= (a_summ["avg_pnl_pct"] or -999) - 0.25
        )

    rec_period_4h = 144
    rec_period_1h = 144
    note_4h = "Keep A (144) — C not clearly better"
    note_1h = "Keep A (144) — C not clearly better"
    if best_4h:
        if clearly_beats(best_4h, a_4h):
            rec_period_4h = best_4h["period"]
            note_4h = f"C period={rec_period_4h} clearly beats A on Filter-exit (median/avg)"
        elif best_4h["period"] != 144:
            note_4h = (
                f"Best score C p={best_4h['period']} but NOT clearly better than A — recommend keep A=144"
            )
        else:
            note_4h = "A (144) ranks best on Filter-exit score"
    if best_1h:
        if clearly_beats(best_1h, a_1h):
            rec_period_1h = best_1h["period"]
            note_1h = f"C period={rec_period_1h} clearly beats A on Filter-exit (median/avg)"
        elif best_1h["period"] != 144:
            note_1h = (
                f"Best score C p={best_1h['period']} but NOT clearly better than A — recommend keep A=144"
            )
        else:
            note_1h = "A (144) ranks best on Filter-exit score"

    results["recommendation"] = {
        "period_4h": rec_period_4h,
        "period_1h": rec_period_1h,
        "exit_primary": "filter_stop (SL = LTF Filter)",
        "exit_optional": "upper_loss after above-Upper",
        "note_4h": note_4h,
        "note_1h": note_1h,
        "confidence": confidence,
        "total_1d_entries": total_n,
        "do_not_change_live_defaults": rec_period_4h == 144 and rec_period_1h == 144,
        "ranked_4h_filter": rec_4h_f["ranked"],
        "ranked_1h_filter": rec_1h_f["ranked"],
        "ranked_4h_upper_loss": rec_4h_u["ranked"],
        "ranked_1h_upper_loss": rec_1h_u["ranked"],
        "nil_spike": nil_spike,
    }
    return results


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}%" if isinstance(x, float) and "pnl" in "" else f"{x}"


def write_report(results: Dict[str, Any]) -> Tuple[str, str]:
    md_path = os.path.join(OUT_DIR, "gc_ltf_period_bt.md")
    js_path = os.path.join(OUT_DIR, "gc_ltf_period_bt.json")

    # slim JSON (drop huge ranked nested summaries duplication ok)
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    rec = results["recommendation"]
    lines: List[str] = []
    lines.append("# GC LTF Period BT — Config A vs C (Harbor exits)")
    lines.append("")
    lines.append(f"**Generated:** {results['ts_hkt']}")
    lines.append("")
    lines.append("## Fixed params (unchanged)")
    lines.append("- poles=4, mult=1.414, source=hlc3, Reduced Lag OFF, Fast Response OFF")
    lines.append("- Entry SoT: **1D dual_cross_up period=144** (not varied)")
    lines.append("- Universe: NIL, TRX, AVNT (HL perps); candles via candleSnapshot")
    lines.append("")
    lines.append("## Configs")
    lines.append("- **A (current):** Period=144 on 4H and 1H")
    lines.append("- **C candidates:** 4H ∈ {72, 96}; 1H ∈ {48, 72, 96}")
    lines.append("")
    lines.append("## Exit rules compared")
    lines.append("1. **Filter stop** — exit when LTF close < Filter (primary Harbor SL)")
    lines.append("2. **Upper-loss** — exit when close < Upper after having been above Upper (optional); Filter still acts as ruin stop")
    lines.append("")
    lines.append(f"## Recommendation (confidence: **{rec['confidence']}**)")
    lines.append("")
    lines.append(f"| TF | Recommended Period | Note |")
    lines.append(f"|----|-------------------|------|")
    lines.append(f"| **4H** | **{rec['period_4h']}** | {rec['note_4h']} |")
    lines.append(f"| **1H** | **{rec['period_1h']}** | {rec['note_1h']} |")
    lines.append("")
    lines.append(
        f"- Live scan defaults: **{'DO NOT CHANGE' if rec['do_not_change_live_defaults'] else 'optional trial only — recommend first, do not auto-flip'}**"
    )
    lines.append(f"- Total 1D dual_cross_up entries across 3 coins: **{rec['total_1d_entries']}**")
    lines.append("")

    def table_for(tf: str, periods: List[int], mode: str, title: str):
        lines.append(f"### {title}")
        lines.append("")
        lines.append(
            "| Period | n | closed | win% | avg pnl% | med pnl% | max MAE% | avg hold | fake-break% (Filter<entry @3d) | avg MFE% | MFE−PnL |"
        )
        lines.append(
            "|--------|---|--------|------|----------|----------|----------|----------|--------------------------------|----------|---------|"
        )
        for p in periods:
            key = f"{tf}_p{p}_{mode}"
            s = results["aggregate"].get(key) or {}
            tag = " **A**" if p == 144 else " C"
            lines.append(
                f"| {p}{tag} | {s.get('n', 0)} | {s.get('closed_n', 0)} | "
                f"{s.get('win_rate') if s.get('win_rate') is not None else '—'} | "
                f"{s.get('avg_pnl_pct') if s.get('avg_pnl_pct') is not None else '—'} | "
                f"{s.get('median_pnl_pct') if s.get('median_pnl_pct') is not None else '—'} | "
                f"{s.get('max_dd_trade_pct') if s.get('max_dd_trade_pct') is not None else '—'} | "
                f"{s.get('avg_hold_bars') if s.get('avg_hold_bars') is not None else '—'} | "
                f"{s.get('fake_break_rate') if s.get('fake_break_rate') is not None else '—'} | "
                f"{s.get('avg_mfe_pct') if s.get('avg_mfe_pct') is not None else '—'} | "
                f"{s.get('avg_mfe_minus_pnl') if s.get('avg_mfe_minus_pnl') is not None else '—'} |"
            )
        lines.append("")

    lines.append("## Aggregate metrics (all coins)")
    lines.append("")
    table_for("4h", PERIODS_4H, "filter", "4H Filter-stop")
    table_for("4h", PERIODS_4H, "upper_loss", "4H Upper-loss (+ Filter fallback)")
    table_for("1h", PERIODS_1H, "filter", "1H Filter-stop")
    table_for("1h", PERIODS_1H, "upper_loss", "1H Upper-loss (+ Filter fallback)")

    lines.append("## Per-coin entry count")
    lines.append("")
    lines.append("| Coin | 1D entries | 1d bars | 4h bars | 1h bars |")
    lines.append("|------|------------|---------|---------|---------|")
    for coin, co in results["coins"].items():
        b = co["bars"]
        lines.append(
            f"| {coin} | {co['n_entries']} | {b['1d']} | {b['4h']} | {b['1h']} |"
        )
    lines.append("")

    # NIL spike section
    lines.append("## NIL-like spike: MFE vs Filter-exit PnL (A vs C)")
    lines.append("")
    lines.append(
        "Fake-break risk proxy = Filter still **below entry** at first 3 calendar days "
        "(stop underwater if used as hard SL)."
    )
    lines.append("")
    if "NIL" in results["coins"]:
        lines.append("### NIL Filter-stop trades by period")
        lines.append("")
        for tf, periods in [("4h", PERIODS_4H), ("1h", PERIODS_1H)]:
            lines.append(f"**{tf.upper()}**")
            lines.append("")
            lines.append("| Period | entry | exit | pnl% | MFE% | MFE−PnL | MAE% | hold | reason | FB@3d |")
            lines.append("|--------|-------|------|------|------|---------|------|------|--------|-------|")
            for p in periods:
                key = f"{tf}_p{p}_filter"
                v = results["coins"]["NIL"]["variants"].get(key)
                if not v:
                    continue
                for t in v["trades"]:
                    lines.append(
                        f"| {p} | {t['entry_t_hkt']} | {t['exit_t_hkt']} | "
                        f"{t['pnl_pct']:+.2f} | {t['mfe_pct']:.2f} | {t['mfe_minus_pnl']:.2f} | "
                        f"{t['mae_pct']:.2f} | {t['hold_bars']} | {t['exit_reason']} | "
                        f"{t['filter_below_entry_at_3d']} |"
                    )
            lines.append("")
        # summary nil
        lines.append("### NIL summary Filter-stop")
        lines.append("")
        lines.append("| TF/Period | n | avg pnl% | med pnl% | avg MFE% | avg MFE−PnL | fake-break% |")
        lines.append("|-----------|---|----------|----------|----------|-------------|-------------|")
        for tf, periods in [("4h", PERIODS_4H), ("1h", PERIODS_1H)]:
            for p in periods:
                key = f"{tf}_p{p}_filter"
                s = (results["coins"]["NIL"]["variants"].get(key) or {}).get("summary") or {}
                if not s.get("n"):
                    continue
                lines.append(
                    f"| {tf} p={p} | {s.get('n')} | {s.get('avg_pnl_pct')} | {s.get('median_pnl_pct')} | "
                    f"{s.get('avg_mfe_pct')} | {s.get('avg_mfe_minus_pnl')} | {s.get('fake_break_rate')} |"
                )
        lines.append("")
    else:
        lines.append("_NIL not in results._")
        lines.append("")

    lines.append("## Method notes")
    lines.append("")
    lines.append("- Entries: 1D GC period=144 dual_cross_up (close crosses above Upper); fill = next daily **open**")
    lines.append("- Exits evaluated on closed LTF bars only (forming bar excluded)")
    lines.append("- Small sample — 3 names; treat as directional evidence for Harbor monitoring Period choice, not production retune without more coins")
    lines.append("- Do **not** change live scan defaults unless A clearly wins; this report only **recommends**")
    lines.append("")
    lines.append("## English facts for Harbor")
    lines.append("")
    lines.append(
        f"1. Recommended monitoring Period: **4H={rec['period_4h']}**, **1H={rec['period_1h']}** "
        f"(Filter-stop primary)."
    )
    lines.append(f"2. {rec['note_4h']}")
    lines.append(f"3. {rec['note_1h']}")
    lines.append(
        f"4. Confidence is **{rec['confidence']}** — only {rec['total_1d_entries']} synthetic 1D entries on NIL/TRX/AVNT."
    )
    lines.append(
        "5. Live defaults stay at Period=144 unless ops explicitly trials a C Period; entry SoT unchanged."
    )
    lines.append("")

    text = "\n".join(lines)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    return md_path, js_path


if __name__ == "__main__":
    print("Starting GC LTF Period BT…", flush=True)
    res = run()
    md, js = write_report(res)
    print("Wrote", md, js, flush=True)
    rec = res["recommendation"]
    print(
        f"RECOMMEND 4H={rec['period_4h']} 1H={rec['period_1h']} "
        f"confidence={rec['confidence']} change_live={not rec['do_not_change_live_defaults']}",
        flush=True,
    )
