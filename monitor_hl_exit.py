#!/usr/bin/env python3
"""Joint monitor: HL positions + 4H GC Upper-loss (long) / 1D Filter (short). DRY_RUN."""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_gc_radar import fetch_candles, compute_gc, hl_post, TF_CONFIG, GC_PERIOD, gc_period_for_tf

CFG = Path(__file__).parent / "hl_monitor_config.json"
OUT = Path(__file__).parent / "out" / "monitor_hl_exit.json"
EXIT_TFS = ("4h",)  # SoT 2026-09-19: long exit 4H only (not 2H)
SHORT_EXIT_TF = "1d"  # shorts: daily close above Filter = COVER

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

def tf_exit(coin: str, side: str, entry: float, tf: str) -> dict:
    period = gc_period_for_tf(tf)
    bars = fetch_candles(coin, tf)
    min_bars = period + 20
    if len(bars) < min_bars:
        return {"ok": False, "tf": tf, "error": "not_enough_bars"}
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=period)  # Lag/Fast OFF; 4h=72 1d=144
    i = len(bars) - 2
    look = max(0, i - 24 * 10)
    close, upper, filt = c[i], gc[i]["upper"], gc[i]["filter"]
    if side == "LONG":
        armed = any(c[j] > gc[j]["upper"] for j in range(look, i + 1))
        signal = "EXIT_LONG_UPPER_LOSS" if armed and close < upper else None
    else:
        armed = any(c[j] < gc[j]["filter"] for j in range(look, i + 1))
        signal = "EXIT_SHORT_FILTER_STOP" if close > filt else None
    roe = (close - entry) / entry if side == "LONG" and entry else (
        (entry - close) / entry if entry else 0)
    return {
        "ok": True, "tf": tf,
        "bar_utc": datetime.fromtimestamp(bars[i]["t"] / 1000, tz=timezone.utc).isoformat(),
        "close": round(close, 8), "upper": round(upper, 8), "filter": round(filt, 8),
        "armed": armed, "signal": signal, "roe_px": round(roe * 100, 3),
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
        exits = {}
        signal = None
        if p["side"] == "LONG":
            for tf in EXIT_TFS:
                if tf not in TF_CONFIG:
                    continue
                ex = tf_exit(p["symbol"], p["side"], p["entry"], tf)
                exits[tf] = ex
                if ex.get("signal") == "EXIT_LONG_UPPER_LOSS":
                    signal = "EXIT_LONG_4H_UPPER_LOSS"
                time.sleep(0.2)
        else:
            # Shorts: daily Filter stop only (do not spam 1H/4H filter hints)
            tf = SHORT_EXIT_TF
            if tf in TF_CONFIG:
                ex = tf_exit(p["symbol"], p["side"], p["entry"], tf)
                exits[tf] = ex
                if ex.get("signal") == "EXIT_SHORT_FILTER_STOP":
                    signal = "COVER_SHORT_1D_FILTER_STOP"
                time.sleep(0.2)
        watched.append({**p, "exits": exits, "signal": signal})
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "address": addr[:6] + "…" + addr[-4:],
        "accountValue": margin.get("accountValue"),
        "sot": "1D entry / 4H upper-loss long exit; 1D Filter stop short; GC lag+fast OFF",
        "gc": "hlc3/4 per-TF period (1d=144,4h=72,1h=48) lag=0 fast=0",
        "positions": watched,
        "dry_run": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"NAV≈{out['accountValue']} positions={len(watched)}")
    for w in watched:
        print(f"  {w['symbol']} {w['side']} → {w.get('signal') or 'HOLD'}")
        for tf, ex in (w.get("exits") or {}).items():
            if ex.get("ok"):
                print(f"    {tf}: armed={ex.get('armed')} close={ex.get('close')} upper={ex.get('upper')} sig={ex.get('signal')}")
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
