#!/usr/bin/env python3
"""Joint monitor: HL positions + tier-aware primary exits + Hard SL=4H Filter (mid).

LOCKED 2026-09-21 (see HARBOR_AUTOTRADE_PROMPT_v1.md / SIZE_TIER_EXIT_LOCKED.md).
DRY_RUN — never places orders.
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_gc_radar import fetch_candles, compute_gc, hl_post, TF_CONFIG, gc_period_for_tf
from mcap_tiers import tier_for, PRIMARY_RULE, HARD_SL_RULE

CFG = Path(__file__).parent / "hl_monitor_config.json"
OUT = Path(__file__).parent / "out" / "monitor_hl_exit.json"
SHORT_EXIT_TF = "1d"  # shorts: daily close above Filter = COVER
LOOKBACK_BARS = 24 * 10  # armed lookback (~10d on 1H; ~40d on 4H)

# Ensure 1h fetch config (Small/Tiny primary)
if "1h" not in TF_CONFIG:
    TF_CONFIG["1h"] = {
        "interval": "1h",
        "bar_ms": 3600 * 1000,
        "n_bars": 400,
        "period": 48,
        "out_stem": "gc_radar_1h",
    }


def load_addr() -> str:
    env = os.environ.get("HL_ADDRESS") or os.environ.get("HYPERLIQUID_ADDRESS")
    if env:
        return env.strip()
    if CFG.is_file():
        return str(json.loads(CFG.read_text(encoding="utf-8")).get("address") or "").strip()
    return ""


def clearinghouse(user: str) -> dict:
    return hl_post({"type": "clearinghouseState", "user": user})


def parse_positions(state: dict) -> list:
    rows = []
    for a in state.get("assetPositions") or []:
        pos = a.get("position") or {}
        szi = float(pos.get("szi") or 0)
        if abs(szi) < 1e-12:
            continue
        rows.append({
            "symbol": pos.get("coin") or "",
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "entry": float(pos.get("entryPx") or 0),
            "unrealizedPnl": float(pos.get("unrealizedPnl") or 0),
            "positionValue": float(pos.get("positionValue") or 0),
        })
    return rows


def _gc_closed(coin: str, tf: str) -> Dict[str, Any]:
    """Last fully closed bar GC levels + prev close/filter/upper for cross detects."""
    period = gc_period_for_tf(tf)
    bars = fetch_candles(coin, tf)
    min_bars = period + 20
    if len(bars) < min_bars:
        return {"ok": False, "tf": tf, "error": "not_enough_bars", "period": period}
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=period)
    i = len(bars) - 2  # last closed
    if i < 1:
        return {"ok": False, "tf": tf, "error": "no_prev_bar", "period": period}
    look = max(0, i - LOOKBACK_BARS)
    armed_upper = any(c[j] > gc[j]["upper"] for j in range(look, i + 1))
    return {
        "ok": True,
        "tf": tf,
        "period": period,
        "bar_utc": datetime.fromtimestamp(bars[i]["t"] / 1000, tz=timezone.utc).isoformat(),
        "close": float(c[i]),
        "prev_close": float(c[i - 1]),
        "upper": float(gc[i]["upper"]),
        "prev_upper": float(gc[i - 1]["upper"]),
        "filter": float(gc[i]["filter"]),
        "prev_filter": float(gc[i - 1]["filter"]),
        "lower": float(gc[i]["lower"]),
        "prev_lower": float(gc[i - 1]["lower"]),
        "armed": armed_upper,
        "lookback_bars": i - look + 1,
    }


def _round_levels(ex: dict) -> dict:
    out = dict(ex)
    for k in ("close", "prev_close", "upper", "prev_upper", "filter", "prev_filter", "lower", "prev_lower"):
        if k in out and isinstance(out[k], float):
            out[k] = round(out[k], 8)
    return out


def long_primary(coin: str, tier: str, entry: float) -> Dict[str, Any]:
    """Tier-aware long primary exit + always compute 4H Filter hard SL."""
    primary_rule = PRIMARY_RULE[tier]
    # Always need 4H for hard SL (and Mega/Large primary)
    g4 = _gc_closed(coin, "4h")
    hard_sl_px = round(g4["filter"], 8) if g4.get("ok") else None

    g1: Optional[dict] = None
    if tier in ("small", "tiny"):
        g1 = _gc_closed(coin, "1h")

    signal = None
    armed = None
    detail: Dict[str, Any] = {"4h": _round_levels(g4) if g4.get("ok") else g4}

    if tier in ("mega", "large"):
        # 4H Filter cross-down — no Armed (Mega == Large LOCKED 2026-09-21)
        if g4.get("ok"):
            cross = g4["close"] < g4["filter"] and g4["prev_close"] >= g4["prev_filter"]
            if cross:
                signal = "EXIT_LONG_4H_FILTER_CROSS"
            armed = False
            detail["cross_down_filter"] = bool(cross)
        primary_tf = "4h"
        primary_ex = g4

    elif tier in ("small", "tiny"):
        # 1H Lower cross-down = out of channel (Small == Tiny LOCKED 2026-09-21)
        detail["1h"] = _round_levels(g1) if g1 and g1.get("ok") else g1
        if g1 and g1.get("ok"):
            prev_lo = g1.get("prev_lower", g1["lower"])
            cross = g1["close"] < g1["lower"] and g1["prev_close"] >= prev_lo
            if cross:
                signal = "EXIT_LONG_1H_LOWER_CROSS"
            armed = False
            detail["cross_down_lower"] = bool(cross)
        primary_tf = "1h"
        primary_ex = g1 or {"ok": False, "error": "no_1h"}

    else:
        primary_tf = "4h"
        primary_ex = {"ok": False, "error": f"unknown_tier:{tier}"}

    roe = 0.0
    if primary_ex and primary_ex.get("ok") and entry:
        close = primary_ex["close"]
        roe = (close - entry) / entry

    return {
        "ok": bool(primary_ex and primary_ex.get("ok")),
        "tier": tier,
        "primary_rule": primary_rule,
        "primary_tf": primary_tf,
        "hard_sl_rule": HARD_SL_RULE,
        "hard_sl_px": hard_sl_px,
        "armed": armed,
        "signal": signal,
        "roe_px": round(roe * 100, 3),
        "bar_utc": (primary_ex or {}).get("bar_utc"),
        "close": round((primary_ex or {}).get("close") or 0, 8) if (primary_ex or {}).get("ok") else None,
        "upper": round((primary_ex or {}).get("upper") or 0, 8) if (primary_ex or {}).get("ok") else None,
        "filter": round((primary_ex or {}).get("filter") or 0, 8) if (primary_ex or {}).get("ok") else None,
        "filter_4h_sl": hard_sl_px,  # hard SL = 4H Filter (mid)
        "detail": detail,
        "error": None if (primary_ex or {}).get("ok") else (primary_ex or {}).get("error"),
    }


def short_exit(coin: str, entry: float) -> Dict[str, Any]:
    """Shorts unchanged: 1D Filter stop (cover when close > Filter)."""
    g = _gc_closed(coin, SHORT_EXIT_TF)
    if not g.get("ok"):
        return {"ok": False, "tf": SHORT_EXIT_TF, "error": g.get("error")}
    close, filt = g["close"], g["filter"]
    signal = "EXIT_SHORT_FILTER_STOP" if close > filt else None
    # hard SL N/A for shorts under this SoT lock (cover = Filter)
    roe = (entry - close) / entry if entry else 0
    return {
        "ok": True,
        "tf": SHORT_EXIT_TF,
        "tier": None,
        "primary_rule": "1d_filter_cover",
        "hard_sl_rule": None,
        "hard_sl_px": None,
        "bar_utc": g["bar_utc"],
        "close": round(close, 8),
        "upper": round(g["upper"], 8),
        "filter": round(filt, 8),
        "lower": round(g["lower"], 8),
        "armed": None,
        "signal": signal,
        "roe_px": round(roe * 100, 3),
    }


def main():
    addr = load_addr()
    if not addr.startswith("0x"):
        print("Need HL address in hl_monitor_config.json")
        sys.exit(2)
    state = clearinghouse(addr)
    margin = state.get("marginSummary") or {}
    positions = parse_positions(state)
    watched = []
    for p in positions:
        coin = p["symbol"]
        if p["side"] == "LONG":
            tier = tier_for(coin)
            ex = long_primary(coin, tier, p["entry"])
            signal = ex.get("signal")
            watched.append({
                **p,
                "tier": tier,
                "primary_rule": ex.get("primary_rule"),
                "hard_sl_rule": HARD_SL_RULE,
                "hard_sl_px": ex.get("hard_sl_px"),
                "armed": ex.get("armed"),
                "exit": ex,
                "exits": {ex.get("primary_tf") or "primary": ex},
                "signal": signal,
            })
            time.sleep(0.2)
        else:
            ex = short_exit(coin, p["entry"])
            signal = None
            if ex.get("signal") == "EXIT_SHORT_FILTER_STOP":
                signal = "COVER_SHORT_1D_FILTER_STOP"
            watched.append({
                **p,
                "tier": None,
                "primary_rule": "1d_filter_cover",
                "hard_sl_rule": None,
                "hard_sl_px": None,
                "exit": ex,
                "exits": {SHORT_EXIT_TF: ex},
                "signal": signal,
            })
            time.sleep(0.2)

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "address": addr[:6] + "…" + addr[-4:],
        "accountValue": margin.get("accountValue"),
        "sot": "2026-09-21 mcap-tier primary exits; Hard SL=4H Filter (mid)(72); shorts 1D Filter cover",
        "gc": "hlc3/4 per-TF period (1d=144,4h=72,1h=48) lag=0 fast=0",
        "tiers": PRIMARY_RULE,
        "hard_sl_rule": HARD_SL_RULE,
        "positions": watched,
        "dry_run": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"NAV≈{out['accountValue']} positions={len(watched)}")
    for w in watched:
        sig = w.get("signal") or "HOLD"
        tier = w.get("tier") or "—"
        print(
            f"  {w['symbol']} {w['side']} tier={tier} rule={w.get('primary_rule')} "
            f"→ {sig} hard_sl={w.get('hard_sl_px')}"
        )
        ex = w.get("exit") or {}
        if ex.get("ok"):
            print(
                f"    tf={ex.get('primary_tf') or ex.get('tf')} armed={ex.get('armed')} "
                f"close={ex.get('close')} filter={ex.get('filter')} upper={ex.get('upper')}"
            )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
