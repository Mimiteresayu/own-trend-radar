#!/usr/bin/env python3
"""Propose Hard SL align to 4H GC Filter mid (period 72) for open HL longs.

LOCKED 2026-09-21: Hard SL = 4H Filter (not Lower).
Default: DRY_RUN — writes out/hard_sl_plan.json. No signing / no place.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from scan_gc_radar import fetch_candles, compute_gc, hl_post, gc_period_for_tf  # noqa: E402
from mcap_tiers import tier_for, HARD_SL_RULE  # noqa: E402

CFG = ROOT / "hl_monitor_config.json"
OUT = ROOT / "out" / "hard_sl_plan.json"
DRIFT_PCT = 0.3


def load_addr() -> str:
    env = os.environ.get("HL_ADDRESS") or os.environ.get("HYPERLIQUID_ADDRESS")
    if env:
        return env.strip()
    if CFG.is_file():
        return str(json.loads(CFG.read_text(encoding="utf-8")).get("address") or "").strip()
    return ""


def clearinghouse(user: str) -> dict:
    return hl_post({"type": "clearinghouseState", "user": user})


def open_orders(user: str) -> list:
    try:
        return hl_post({"type": "openOrders", "user": user}) or []
    except Exception:
        return []


def closed_4h_filter(coin: str) -> Dict[str, Any]:
    period = gc_period_for_tf("4h")
    bars = fetch_candles(coin, "4h")
    if len(bars) < period + 20:
        return {"ok": False, "error": "not_enough_bars"}
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=period)
    i = len(bars) - 2
    return {
        "ok": True,
        "period": period,
        "filter": round(float(gc[i]["filter"]), 8),
        "lower": round(float(gc[i]["lower"]), 8),
        "upper": round(float(gc[i]["upper"]), 8),
    }


def round_trigger(px: float) -> float:
    if px >= 1000:
        return round(px, 1)
    if px >= 100:
        return round(px, 2)
    if px >= 1:
        return round(px, 4)
    if px >= 0.01:
        return round(px, 6)
    return round(px, 8)


def current_stop_trigger(orders: list, coin: str) -> Optional[float]:
    best = None
    for o in orders:
        if (o.get("coin") or o.get("symbol")) != coin:
            continue
        if not o.get("isTrigger") and "stop" not in str(o.get("orderType") or "").lower():
            continue
        try:
            t = float(o.get("triggerPx") or o.get("limitPx") or 0)
        except (TypeError, ValueError):
            continue
        if t > 0:
            best = t
    return best


def main() -> None:
    addr = load_addr()
    if not addr.startswith("0x"):
        print("Need HL address")
        sys.exit(2)
    state = clearinghouse(addr)
    orders = open_orders(addr)
    rows: List[dict] = []
    for a in state.get("assetPositions") or []:
        pos = a.get("position") or {}
        szi = float(pos.get("szi") or 0)
        if szi <= 0:
            continue
        coin = pos.get("coin") or ""
        tier = tier_for(coin)
        g = closed_4h_filter(coin)
        if not g.get("ok"):
            rows.append({"coin": coin, "tier": tier, "action": "skip_no_gc", "error": g.get("error")})
            continue
        filt = float(g["filter"])
        trigger = round_trigger(filt)
        cur = current_stop_trigger(orders, coin)
        drift = abs(cur - filt) / filt * 100.0 if cur and filt else None
        need = cur is None or (drift is not None and drift >= DRIFT_PCT)
        rows.append({
            "coin": coin,
            "tier": tier,
            "hard_sl_rule": HARD_SL_RULE,
            "filter_4h": filt,
            "lower_4h": float(g["lower"]),
            "proposed_trigger": trigger,
            "current_trigger": cur,
            "drift_pct_vs_filter": round(drift, 4) if drift is not None else None,
            "action": "place_or_update" if need else "aligned_skip",
        })
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "sot": "Hard SL = 4H Filter mid (period 72); LOCKED 2026-09-21",
        "hard_sl_rule": HARD_SL_RULE,
        "dry_run": True,
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")
    for r in rows:
        print(f"{r.get('coin'):<8} {r.get('tier'):<6} {r.get('action'):<16} filt={r.get('filter_4h')} cur={r.get('current_trigger')} prop={r.get('proposed_trigger')}")


if __name__ == "__main__":
    main()
