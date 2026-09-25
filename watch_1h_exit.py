#!/usr/bin/env python3
"""Watch 1H GC Upper-loss exits (SoT 2026-09-16). DRY_RUN — no orders."""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path

# reuse radar
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_gc_radar import fetch_candles, compute_gc, TF_CONFIG

TF_CONFIG["1h"]["n_bars"] = 400
PER = 72

# open longs to watch (update as needed)
DEFAULT_POS = [
    {"symbol": "FIL", "side": "LONG", "entry": 0.80820,
     "entry_utc": "2026-09-13T00:36:00+00:00"},
]

def parse_ts(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp() * 1000)

def check(pos: dict) -> dict:
    coin, side, entry = pos["symbol"], pos["side"], float(pos["entry"])
    entry_ms = parse_ts(pos["entry_utc"])
    bars = fetch_candles(coin, "1h")
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=PER)
    i0 = next((i for i, b in enumerate(bars) if b["t"] >= entry_ms), 0)
    armed = False
    for i in range(i0, len(bars) - 1):
        if c[i] > gc[i]["upper"]:
            armed = True
            break
    # last closed bar
    i = len(bars) - 2
    close, upper, filt = c[i], gc[i]["upper"], gc[i]["filter"]
    signal = None
    if side == "LONG" and armed and close < upper:
        signal = "EXIT_LONG_1H_UPPER_LOSS"
    roe = (close - entry) / entry if side == "LONG" else (entry - close) / entry
    return {
        "symbol": coin,
        "side": side,
        "entry": entry,
        "bar_utc": datetime.fromtimestamp(bars[i]["t"] / 1000, tz=timezone.utc).isoformat(),
        "close": round(close, 8),
        "upper": round(upper, 8),
        "filter": round(filt, 8),
        "armed": armed,
        "close_below_upper": close < upper,
        "roe_now": round(roe * 100, 3),
        "signal": signal,
        "gc": f"1h hlc3/4/{PER}/1.414 lag+fast",
        "dry_run": True,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default=str(Path(__file__).parent / "out" / "watch_1h_exit.json"))
    args = ap.parse_args()
    rows = [check(p) for p in DEFAULT_POS]
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    out = {"ts": datetime.now(timezone.utc).isoformat(), "sot": "daily entry / 1h upper-loss exit", "rows": rows}
    Path(args.json_out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    for r in rows:
        flag = r["signal"] or ("HOLD" if r["armed"] else "WAIT_ARM")
        print(f"{r['symbol']} {flag} armed={r['armed']} close={r['close']} upper={r['upper']} ROE={r['roe_now']}%")
    print(f"wrote {args.json_out}")

if __name__ == "__main__":
    main()
