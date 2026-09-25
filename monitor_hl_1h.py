#!/usr/bin/env python3
"""
Legacy 1H Upper-loss monitor (Tiny-tier primary style).

Prefer `monitor_hl_exit.py` (LOCKED 2026-09-21 mcap-tier exits + Hard SL=4H Lower).
This script remains for ad-hoc 1H Upper-loss checks. DRY_RUN only.
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_gc_radar import fetch_candles, compute_gc, hl_post, TF_CONFIG

# scan_gc_radar dropped 1h from TF_CONFIG (universe scan uses 2h/4h/1d),
# but this monitor still SoT-checks 1H exits — reinject fetch config.
if "1h" not in TF_CONFIG:
    TF_CONFIG["1h"] = {
        "interval": "1h",
        "bar_ms": 3600 * 1000,
        "n_bars": 400,
        "out_stem": "gc_radar_1h",
    }
else:
    TF_CONFIG["1h"]["n_bars"] = 400
PER = 72
CFG = Path(__file__).parent / "hl_monitor_config.json"
OUT = Path(__file__).parent / "out" / "monitor_hl_1h.json"

def load_addr() -> str:
    env = os.environ.get("HL_ADDRESS") or os.environ.get("HYPERLIQUID_ADDRESS")
    if env:
        return env.strip()
    if CFG.is_file():
        d = json.loads(CFG.read_text(encoding="utf-8"))
        return str(d.get("address") or "").strip()
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
        coin = pos.get("coin") or ""
        entry = float(pos.get("entryPx") or 0)
        uPnl = float(pos.get("unrealizedPnl") or 0)
        side = "LONG" if szi > 0 else "SHORT"
        rows.append({
            "symbol": coin,
            "side": side,
            "size": abs(szi),
            "entry": entry,
            "unrealizedPnl": uPnl,
            "positionValue": float(pos.get("positionValue") or 0),
        })
    return rows

def exit_signal(coin: str, side: str, entry: float) -> dict:
    bars = fetch_candles(coin, "1h")
    if len(bars) < PER + 30:
        return {"ok": False, "error": "not_enough_bars"}
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=PER)
    i = len(bars) - 2  # closed
    # arm: any recent closed bar above upper (look back 14d of hours)
    look = max(0, i - 24 * 14)
    armed = any(c[j] > gc[j]["upper"] for j in range(look, i + 1)) if side == "LONG" else any(
        c[j] < gc[j]["filter"] for j in range(look, i + 1)
    )
    close, upper, filt = c[i], gc[i]["upper"], gc[i]["filter"]
    signal = None
    if side == "LONG" and armed and close < upper:
        signal = "EXIT_LONG_1H_UPPER_LOSS"
    elif side == "SHORT" and close > filt:
        signal = "EXIT_SHORT_1H_FILTER_STOP"  # informational; SoT shorts still daily
    roe = (close - entry) / entry if side == "LONG" and entry else (
        (entry - close) / entry if entry else 0
    )
    return {
        "ok": True,
        "bar_utc": datetime.fromtimestamp(bars[i]["t"] / 1000, tz=timezone.utc).isoformat(),
        "close": round(close, 8),
        "upper": round(upper, 8),
        "filter": round(filt, 8),
        "armed": armed,
        "signal": signal,
        "roe_px": round(roe * 100, 3),
        "gc": f"1h/{PER}",
    }

def main():
    addr = load_addr()
    if not addr.startswith("0x") or len(addr) < 10:
        print("Need HL wallet address. Set HL_ADDRESS or write hl_monitor_config.json {\"address\":\"0x...\"}")
        sys.exit(2)
    state = clearinghouse(addr)
    margin = state.get("marginSummary") or {}
    positions = parse_positions(state)
    watched = []
    for p in positions:
        sig = exit_signal(p["symbol"], p["side"], p["entry"])
        watched.append({**p, "exit": sig})
        time.sleep(0.25)
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "address": addr[:6] + "…" + addr[-4:],
        "accountValue": margin.get("accountValue"),
        "totalMarginUsed": margin.get("totalMarginUsed"),
        "sot": "daily entry / 1H upper-loss exit (long); monitor only",
        "positions": watched,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"NAV≈{out['accountValue']}  positions={len(watched)}")
    for w in watched:
        e = w.get("exit") or {}
        flag = e.get("signal") or ("HOLD" if e.get("armed") else "FLAT_RULE")
        print(f"  {w['symbol']} {w['side']} entry={w['entry']} uPnl={w['unrealizedPnl']:.4f} → {flag} "
              f"close={e.get('close')} upper={e.get('upper')} roe≈{e.get('roe_px')}%")
    print(f"wrote {OUT}")

if __name__ == "__main__":
    main()
