#!/usr/bin/env python3
"""Observe-only suggested exits for open HL positions. NEVER places/cancels/modifies orders.

For each open position computes four exit levels:
  h1   — 1H GC Filter
  h4   — 4H GC Filter
  nbar — min(low of last N fully closed 4H bars)  [SHORT: max high]
  mom  — 4H Upper (structure / 止贏); label mom_4h_upper

Optional: mom_fail_1h_filter (1H Filter) when mid > entry (long hint).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scan_gc_radar import (  # noqa: E402
    GC_PERIOD,
    TF_CONFIG,
    compute_gc,
    fetch_candles,
    hl_post,
)

DESK = ROOT / "out" / "desk_daily.json"
OUT = ROOT / "out" / "suggested_exits.json"
CFG = ROOT / "hl_monitor_config.json"
WALLET_DEFAULT = "0xcFCda0F8576a268BaA17935368081F4e687dB122"
NBAR_N = 2
NBAR_TF = "4h"
HKT = timezone(timedelta(hours=8))


def _addr() -> str:
    env = (os.environ.get("HL_ADDRESS") or os.environ.get("HYPERLIQUID_ADDRESS") or "").strip()
    if env.startswith("0x"):
        return env
    if CFG.is_file():
        a = str(json.loads(CFG.read_text(encoding="utf-8")).get("address") or "").strip()
        if a.startswith("0x"):
            return a
    return WALLET_DEFAULT


def _ts_hkt() -> str:
    return datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")


def side_sign(side: str) -> int:
    return 1 if str(side).upper() == "LONG" else -1


def exit_metrics(exit_px: float, entry: float, sz: float, side: str) -> Dict[str, Any]:
    ss = side_sign(side)
    ntl = entry * sz
    pnl_usd = (exit_px - entry) * sz * ss
    pnl_pct = (pnl_usd / ntl) if ntl else 0.0
    above = (exit_px > entry) if ss > 0 else (exit_px < entry)
    return {
        "px": round(exit_px, 8),
        "pnl_usd": round(pnl_usd, 6),
        "pnl_pct": round(pnl_pct, 6),
        "pnl_pct_ntl": round(pnl_pct, 6),
        "above_entry": bool(above),
    }


def closed_gc_levels(bars: List[dict], period: int = GC_PERIOD) -> Optional[Dict[str, Any]]:
    """Last fully closed bar GC filter/upper (bars[-1] treated as forming)."""
    min_bars = period + 20
    if len(bars) < min_bars:
        return None
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=period)
    i = len(bars) - 2  # last closed
    if i < 0:
        return None
    return {
        "filter": float(gc[i]["filter"]),
        "upper": float(gc[i]["upper"]),
        "close": float(c[i]),
        "bar_t": int(bars[i]["t"]),
        "i": i,
    }


def nbar_level(bars: List[dict], n: int, side: str) -> Optional[Tuple[float, List[float]]]:
    """Break of prior N fully closed bars: LONG=min(lows), SHORT=max(highs)."""
    # Forming = bars[-1]; last N closed = bars[-(n+1) : -1]
    if len(bars) < n + 1:
        return None
    closed = bars[-(n + 1) : -1]
    if len(closed) < n:
        return None
    if str(side).upper() == "LONG":
        vals = [float(b["low"]) for b in closed]
        return min(vals), vals
    vals = [float(b["high"]) for b in closed]
    return max(vals), vals




def mom_vol_fade(bars: List[dict], entry: float, side: str, recent_n: int = 6) -> Optional[Dict[str, Any]]:
    """Exchange-only volume fade: recent closed-bar vol << spike since entry (4H).

    Spike = max volume among closed 4H bars whose close is on the profitable side of entry
    (or simply max vol of closed bars in lookback). Fade when mean(recent) < 0.35 * spike.
    """
    if len(bars) < recent_n + 2:
        return None
    closed = bars[:-1]  # drop forming
    if len(closed) < recent_n + 2:
        return None
    vols = [float(b.get("volume") or 0) for b in closed]
    recent = vols[-recent_n:]
    look = vols[-(recent_n * 8) :] if len(vols) >= recent_n * 8 else vols
    spike = max(look) if look else 0.0
    recent_mean = (sum(recent) / len(recent)) if recent else 0.0
    if spike <= 0:
        return {"fade": False, "recent_mean": round(recent_mean, 4), "spike": 0.0, "ratio": None}
    ratio = recent_mean / spike
    fade = ratio < 0.35
    return {
        "fade": bool(fade),
        "recent_mean": round(recent_mean, 4),
        "spike": round(spike, 4),
        "ratio": round(ratio, 4),
        "recent_n": recent_n,
    }

def load_live_positions(user: str) -> Tuple[List[dict], Dict[str, float]]:
    state = hl_post({"type": "clearinghouseState", "user": user})
    mids_raw = hl_post({"type": "allMids"})
    mids: Dict[str, float] = {}
    if isinstance(mids_raw, dict):
        for k, v in mids_raw.items():
            try:
                mids[str(k)] = float(v)
            except (TypeError, ValueError):
                pass
    rows = []
    for a in state.get("assetPositions") or []:
        pos = a.get("position") or {}
        szi = float(pos.get("szi") or 0)
        if abs(szi) < 1e-12:
            continue
        coin = pos.get("coin") or ""
        entry = float(pos.get("entryPx") or 0)
        sz = abs(szi)
        side = "LONG" if szi > 0 else "SHORT"
        mid = mids.get(coin)
        u_pnl = float(pos.get("unrealizedPnl") or 0)
        rows.append(
            {
                "coin": coin,
                "side": side,
                "sz": sz,
                "size": sz,
                "entry": entry,
                "mid": mid,
                "uPnl": u_pnl,
                "positionValue": float(pos.get("positionValue") or 0),
                "leverage": (pos.get("leverage") or {}).get("value")
                if isinstance(pos.get("leverage"), dict)
                else pos.get("leverage"),
                "liq": pos.get("liquidationPx"),
            }
        )
    return rows, mids


def load_desk_positions() -> List[dict]:
    if not DESK.is_file():
        return []
    d = json.loads(DESK.read_text(encoding="utf-8"))
    out = []
    for p in d.get("positions") or []:
        out.append(
            {
                "coin": p.get("coin"),
                "side": p.get("side") or "LONG",
                "sz": float(p.get("size") or p.get("sz") or 0),
                "size": float(p.get("size") or p.get("sz") or 0),
                "entry": float(p.get("entry") or 0),
                "mid": p.get("mid"),
                "uPnl": p.get("uPnl"),
                "positionValue": p.get("positionValue"),
                "leverage": p.get("leverage"),
                "liq": p.get("liq"),
                "_desk": p,
            }
        )
    return out


def merge_positions(live: List[dict], desk: List[dict]) -> List[dict]:
    """Prefer live HL; fall back to desk if live empty."""
    if live:
        by_desk = {p["coin"]: p for p in desk if p.get("coin")}
        for p in live:
            d = by_desk.get(p["coin"]) or {}
            # keep desk extras (armed, h4_*, etc.) on _desk for patch
            p["_desk"] = d.get("_desk") or d
        return live
    return desk


def compute_for_position(p: dict) -> dict:
    coin = p["coin"]
    side = p["side"]
    entry = float(p["entry"])
    sz = float(p["sz"])
    mid = p.get("mid")
    mid_f = float(mid) if mid is not None else None

    # 4H candles once → filter, upper, nbar
    bars4 = fetch_candles(coin, "4h")
    gc4 = closed_gc_levels(bars4, period=GC_PERIOD)
    nb = nbar_level(bars4, NBAR_N, side)

    # 1H candles → filter
    bars1 = fetch_candles(coin, "1h")
    gc1 = closed_gc_levels(bars1, period=GC_PERIOD)

    exits: Dict[str, Any] = {}
    err: Dict[str, str] = {}

    if gc1:
        m = exit_metrics(gc1["filter"], entry, sz, side)
        m["label"] = "1h_filter"
        m["bar_utc"] = datetime.fromtimestamp(gc1["bar_t"] / 1000, tz=timezone.utc).isoformat()
        exits["h1"] = m
    else:
        err["h1"] = "no_1h_gc"
        exits["h1"] = None

    if gc4:
        m = exit_metrics(gc4["filter"], entry, sz, side)
        m["label"] = "4h_filter"
        m["bar_utc"] = datetime.fromtimestamp(gc4["bar_t"] / 1000, tz=timezone.utc).isoformat()
        exits["h4"] = m
    else:
        err["h4"] = "no_4h_gc"
        exits["h4"] = None

    if nb:
        px, lows = nb
        m = exit_metrics(px, entry, sz, side)
        m["label"] = f"nbar_{NBAR_N}_{NBAR_TF}"
        m["n"] = NBAR_N
        m["component_lows" if side_sign(side) > 0 else "component_highs"] = [round(x, 8) for x in lows]
        exits["nbar"] = m
    else:
        err["nbar"] = "no_nbar"
        exits["nbar"] = None

    if gc4:
        m = exit_metrics(gc4["upper"], entry, sz, side)
        m["label"] = "mom_4h_upper"
        m["bar_utc"] = datetime.fromtimestamp(gc4["bar_t"] / 1000, tz=timezone.utc).isoformat()
        vf = mom_vol_fade(bars4, entry, side)
        if vf is not None:
            m["mom_vol_fade"] = bool(vf.get("fade"))
            m["vol_fade_detail"] = vf
        exits["mom"] = m
    else:
        err["mom"] = "no_4h_upper"
        exits["mom"] = None

    # Optional mom-fail hint: 1H Filter when mid > entry (long)
    if gc1 and mid_f is not None and side_sign(side) > 0 and mid_f > entry:
        m = exit_metrics(gc1["filter"], entry, sz, side)
        m["label"] = "mom_fail_1h_filter"
        exits["mom_fail_1h_filter"] = m

    row = {
        "coin": coin,
        "side": side,
        "entry": entry,
        "sz": sz,
        "mid": mid_f,
        "uPnl": p.get("uPnl"),
        "exits": exits,
    }
    if err:
        row["errors"] = err
    return row


def patch_desk(suggested: dict) -> None:
    if not DESK.is_file():
        print(f"[warn] desk missing, skip patch: {DESK}", file=sys.stderr)
        return
    d = json.loads(DESK.read_text(encoding="utf-8"))
    by = {r["coin"]: r for r in suggested.get("positions") or []}
    for p in d.get("positions") or []:
        coin = p.get("coin")
        r = by.get(coin)
        if not r:
            continue
        # nest suggested_exits on each position
        se = {
            "h1": r["exits"].get("h1"),
            "h4": r["exits"].get("h4"),
            "nbar": r["exits"].get("nbar"),
            "mom": r["exits"].get("mom"),
        }
        if r["exits"].get("mom_fail_1h_filter"):
            se["mom_fail_1h_filter"] = r["exits"]["mom_fail_1h_filter"]
        p["suggested_exits"] = se
        # refresh mid/uPnl from live if present
        if r.get("mid") is not None:
            p["mid"] = r["mid"]
        if r.get("uPnl") is not None:
            p["uPnl"] = r["uPnl"]
    d["suggested_exits_meta"] = {
        "ts_hkt": suggested.get("ts_hkt"),
        "params": suggested.get("params"),
        "observe_only": True,
        "banner": "suggested only — no order changes",
    }
    DESK.write_text(json.dumps(d, indent=2), encoding="utf-8")
    print(f"patched {DESK}")


def main() -> int:
    assert "1h" in TF_CONFIG, "1h must be in TF_CONFIG"
    addr = _addr()
    live: List[dict] = []
    try:
        live, _ = load_live_positions(addr)
        print(f"[info] live HL positions={len(live)} wallet={addr[:6]}…{addr[-4:]}")
    except Exception as e:
        print(f"[warn] live HL failed: {e}", file=sys.stderr)
    desk = load_desk_positions()
    positions = merge_positions(live, desk)
    if not positions:
        print("no open positions", file=sys.stderr)
        return 1

    rows = []
    for i, p in enumerate(positions):
        print(f"[{i+1}/{len(positions)}] {p['coin']} {p['side']} …")
        try:
            rows.append(compute_for_position(p))
        except Exception as e:
            rows.append(
                {
                    "coin": p.get("coin"),
                    "side": p.get("side"),
                    "entry": p.get("entry"),
                    "sz": p.get("sz"),
                    "mid": p.get("mid"),
                    "uPnl": p.get("uPnl"),
                    "exits": {"h1": None, "h4": None, "nbar": None, "mom": None},
                    "errors": {"all": str(e)},
                }
            )
            print(f"  ERR {e}", file=sys.stderr)
        time.sleep(0.15)

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_hkt": _ts_hkt(),
        "observe_only": True,
        "banner": "suggested only — no order changes",
        "wallet": addr[:6] + "…" + addr[-4:],
        "params": {
            "n": NBAR_N,
            "nbar_tf": NBAR_TF,
            "mom": "4h_upper",
            "gc": f"hlc3/4/{GC_PERIOD}/1.414 lag=0 fast=0",
            "h1": "1h_filter",
            "h4": "4h_filter",
        },
        "positions": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
    patch_desk(out)

    # compact table
    print()
    print(f"{'coin':<8} {'side':<5} {'1h_$':>10} {'4h_$':>10} {'nbar_$':>10} {'mom_$':>10}")
    for r in rows:
        def _pnl(k):
            e = (r.get("exits") or {}).get(k) or {}
            v = e.get("pnl_usd")
            return f"{v:+.2f}" if v is not None else "—"
        print(f"{r.get('coin',''):<8} {r.get('side',''):<5} {_pnl('h1'):>10} {_pnl('h4'):>10} {_pnl('nbar'):>10} {_pnl('mom'):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
