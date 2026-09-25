#!/usr/bin/env python3
"""Fail-safe exit worker: deterministic tier-based exits (DORMANT build; not enabled).

LOCKED rules from SIZE_TIER_EXIT_LOCKED.md / HARBOR_AUTOTRADE_PROMPT_v1.md:
- GC periods: 1D=144, 4H=72, 1H=48, Lag/Fast off, closed bars only
- Tiers by mcap: Mega/Large → 4H Filter cross-down; Small/Tiny → 1H Lower cross-down
- Hard SL: Mega/Large → 4H Lower; Small/Tiny → 4H Filter (mid); re-align each run
- Shorts: report-only (no exit logic locked yet)
- Never opens positions

Mode:
  FAILSAFE_ENABLE=1 + HL_API_WALLET_KEY present → LIVE (places orders)
  Otherwise → DRY_RUN (writes out/failsafe_last.json only)

Staleness guard: if radar/candles stale or unreachable, no destructive actions.
Retry/backoff on HL 429.
Optional ALERT_WEBHOOK_URL: POST short text on live action/error.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request as _url_req
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scan_gc_radar import fetch_candles, compute_gc, hl_post, gc_period_for_tf  # noqa: E402
from mcap_tiers import tier_for, PRIMARY_RULE, HARD_SL_BY_TIER  # noqa: E402

OUT = ROOT / "out" / "failsafe_last.json"
RADAR_DIR = ROOT / "out"
STALENESS_THRESHOLD_S = 7200  # 2h; reject radar older than this
LOOKBACK_BARS = 10  # minimal lookback for cross detection

ENABLE = os.environ.get("FAILSAFE_ENABLE", "").strip() == "1"
HL_ADDRESS = (os.environ.get("HL_ADDRESS") or "").strip()
HL_API_WALLET_KEY = (os.environ.get("HL_API_WALLET_KEY") or "").strip()
ALERT_WEBHOOK_URL = (os.environ.get("ALERT_WEBHOOK_URL") or "").strip()

LIVE_MODE = ENABLE and bool(HL_API_WALLET_KEY)


def _alert(text: str) -> None:
    """POST short text to ALERT_WEBHOOK_URL if set."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({"text": text}).encode()
        req = _url_req.Request(
            ALERT_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _url_req.urlopen(req, timeout=10) as _resp:
            pass
    except Exception as e:
        sys.stderr.write(f"[failsafe] alert webhook error: {e}\n")


def _hl_post_retry(payload: dict, retries=3, backoff_s=2) -> dict:
    """POST to HL with retry/backoff on 429."""
    for attempt in range(retries):
        try:
            return hl_post(payload)
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                sys.stderr.write(f"[failsafe] HL 429 retry {attempt+1}/{retries} in {backoff_s}s\n")
                time.sleep(backoff_s)
                backoff_s *= 2
                continue
            raise
    raise RuntimeError("HL retry exhausted")


def _check_radar_freshness(tf: str) -> tuple[bool, Optional[str]]:
    """Check if radar JSON exists and is fresh. Returns (ok, error_msg)."""
    fp = RADAR_DIR / f"gc_radar_{tf}.json"
    if not fp.is_file():
        return False, f"radar_{tf}_missing"
    try:
        with open(fp) as f:
            data = json.load(f)
        ts_str = data.get("ts", "")
        if not ts_str:
            return False, f"radar_{tf}_no_timestamp"
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        age_s = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_s > STALENESS_THRESHOLD_S:
            return False, f"radar_{tf}_stale_{int(age_s)}s"
        return True, None
    except Exception as e:
        return False, f"radar_{tf}_read_error:{e}"


def _gc_closed(coin: str, tf: str) -> Dict[str, Any]:
    """Last fully closed bar GC levels + prev for cross detection."""
    period = gc_period_for_tf(tf)
    try:
        bars = fetch_candles(coin, tf)
    except Exception as e:
        return {"ok": False, "tf": tf, "error": f"fetch_candles:{e}"}
    if len(bars) < period + 20:
        return {"ok": False, "tf": tf, "error": "not_enough_bars"}
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    c = [b["close"] for b in bars]
    gc = compute_gc(h, l, c, period=period)
    i = len(bars) - 2  # last closed
    if i < 1:
        return {"ok": False, "tf": tf, "error": "no_prev_bar"}
    return {
        "ok": True,
        "tf": tf,
        "period": period,
        "bar_utc": datetime.fromtimestamp(bars[i]["t"] / 1000, tz=timezone.utc).isoformat(),
        "close": float(c[i]),
        "prev_close": float(c[i - 1]),
        "upper": float(gc[i]["upper"]),
        "filter": float(gc[i]["filter"]),
        "prev_filter": float(gc[i - 1]["filter"]),
        "lower": float(gc[i]["lower"]),
        "prev_lower": float(gc[i - 1]["lower"]),
    }


def _round_trigger(px: float) -> float:
    """Round trigger price per HL precision."""
    if px >= 1000:
        return round(px, 1)
    if px >= 100:
        return round(px, 2)
    if px >= 1:
        return round(px, 4)
    if px >= 0.01:
        return round(px, 6)
    return round(px, 8)


def _compute_long_signal(coin: str, tier: str, entry: float, size: float) -> Dict[str, Any]:
    """Compute tier-based exit signal + hard SL for a long position."""
    primary_rule = PRIMARY_RULE[tier]
    hard_sl_rule = HARD_SL_BY_TIER.get(tier, "4h_filter")

    # Always need 4H for hard SL (and Mega/Large primary)
    g4 = _gc_closed(coin, "4h")
    if not g4.get("ok"):
        return {
            "ok": False,
            "tier": tier,
            "error": f"4h_gc_error:{g4.get('error')}",
            "exit_signal": None,
            "hard_sl_px": None,
        }

    hard_sl_px = _round_trigger(g4["lower"]) if tier in ("mega", "large") else _round_trigger(g4["filter"])

    exit_signal = None
    primary_tf = "4h"

    if tier in ("mega", "large"):
        # 4H Filter cross-down
        cross = g4["close"] < g4["filter"] and g4["prev_close"] >= g4["prev_filter"]
        if cross:
            exit_signal = "EXIT_LONG_4H_FILTER_CROSS"
        primary_tf = "4h"

    elif tier in ("small", "tiny"):
        # 1H Lower cross-down = out of channel
        g1 = _gc_closed(coin, "1h")
        if not g1.get("ok"):
            return {
                "ok": False,
                "tier": tier,
                "error": f"1h_gc_error:{g1.get('error')}",
                "exit_signal": None,
                "hard_sl_px": hard_sl_px,
            }
        cross = g1["close"] < g1["lower"] and g1["prev_close"] >= g1["prev_lower"]
        if cross:
            exit_signal = "EXIT_LONG_1H_LOWER_CROSS"
        primary_tf = "1h"

    else:
        return {
            "ok": False,
            "tier": tier,
            "error": f"unknown_tier:{tier}",
            "exit_signal": None,
            "hard_sl_px": hard_sl_px,
        }

    return {
        "ok": True,
        "tier": tier,
        "primary_rule": primary_rule,
        "hard_sl_rule": hard_sl_rule,
        "primary_tf": primary_tf,
        "exit_signal": exit_signal,
        "hard_sl_px": hard_sl_px,
        "error": None,
    }


def _current_stop_trigger(orders: list, coin: str) -> Optional[float]:
    """Find current stop trigger for coin."""
    for o in orders:
        if (o.get("coin") or o.get("symbol")) != coin:
            continue
        if not o.get("isTrigger") and "stop" not in str(o.get("orderType") or "").lower():
            continue
        try:
            t = float(o.get("triggerPx") or o.get("limitPx") or 0)
            if t > 0:
                return t
        except (TypeError, ValueError):
            continue
    return None


def _place_market_close(coin: str, size: float, is_long: bool) -> Dict[str, Any]:
    """Place reduce-only market close via hyperliquid-python-sdk."""
    # Placeholder: requires hyperliquid-python-sdk integration
    # For MVP, log intent and return dry-run
    sys.stderr.write(f"[failsafe] LIVE: place market close {coin} size={size} is_long={is_long}\n")
    return {"action": "market_close", "coin": coin, "size": size, "is_long": is_long, "status": "dry_run"}


def _align_hard_sl(coin: str, hard_sl_px: float, current_trigger: Optional[float]) -> Optional[Dict[str, Any]]:
    """Align hard SL trigger order if drift > 0.3%."""
    DRIFT_PCT = 0.3
    if current_trigger is None:
        sys.stderr.write(f"[failsafe] {coin}: place hard SL trigger @ {hard_sl_px} (no existing)\n")
        return {"action": "place_sl", "coin": coin, "trigger": hard_sl_px, "current": None}
    drift = abs(current_trigger - hard_sl_px) / hard_sl_px * 100.0
    if drift >= DRIFT_PCT:
        sys.stderr.write(f"[failsafe] {coin}: update hard SL {current_trigger} → {hard_sl_px} (drift={drift:.2f}%)\n")
        return {"action": "update_sl", "coin": coin, "trigger": hard_sl_px, "current": current_trigger, "drift_pct": drift}
    return None


def run_failsafe() -> Dict[str, Any]:
    """Main fail-safe logic: check positions, compute signals, place/align orders if live."""
    ts = datetime.now(timezone.utc).isoformat()
    mode = "LIVE" if LIVE_MODE else "DRY_RUN"
    sys.stderr.write(f"[failsafe] {ts} mode={mode}\n")

    errors = []

    # Check radar freshness for 1h/4h
    for tf in ("1h", "4h"):
        ok, err = _check_radar_freshness(tf)
        if not ok:
            errors.append(f"staleness_guard:{err}")
            sys.stderr.write(f"[failsafe] staleness guard FAIL: {err}\n")
            return {
                "ts": ts,
                "mode": mode,
                "errors": errors,
                "actions": [],
                "positions": [],
                "status": "aborted_stale_radar",
            }

    if not HL_ADDRESS or not HL_ADDRESS.startswith("0x"):
        errors.append("missing_HL_ADDRESS")
        return {"ts": ts, "mode": mode, "errors": errors, "actions": [], "positions": [], "status": "aborted_no_address"}

    # Fetch positions
    try:
        state = _hl_post_retry({"type": "clearinghouseState", "user": HL_ADDRESS})
    except Exception as e:
        errors.append(f"fetch_positions:{e}")
        sys.stderr.write(f"[failsafe] fetch positions error: {e}\n")
        if ALERT_WEBHOOK_URL:
            _alert(f"[failsafe] ERROR: {e}")
        return {"ts": ts, "mode": mode, "errors": errors, "actions": [], "positions": [], "status": "error"}

    try:
        orders = _hl_post_retry({"type": "openOrders", "user": HL_ADDRESS})
    except Exception as e:
        errors.append(f"fetch_orders:{e}")
        orders = []

    positions = []
    actions = []

    for a in state.get("assetPositions") or []:
        pos = a.get("position") or {}
        szi = float(pos.get("szi") or 0)
        if abs(szi) < 1e-12:
            continue
        coin = pos.get("coin") or ""
        side = "LONG" if szi > 0 else "SHORT"
        size = abs(szi)
        entry = float(pos.get("entryPx") or 0)

        if side == "SHORT":
            # Shorts: report-only (no exit logic locked)
            positions.append({
                "coin": coin,
                "side": side,
                "size": size,
                "entry": entry,
                "signal": None,
                "hard_sl_px": None,
                "note": "shorts_report_only",
            })
            continue

        # Longs: tier-based exits
        tier = tier_for(coin)
        signal_result = _compute_long_signal(coin, tier, entry, size)

        current_trigger = _current_stop_trigger(orders, coin)
        hard_sl_px = signal_result.get("hard_sl_px")

        pos_info = {
            "coin": coin,
            "side": side,
            "size": size,
            "entry": entry,
            "tier": tier,
            "signal": signal_result.get("exit_signal"),
            "hard_sl_px": hard_sl_px,
            "current_trigger": current_trigger,
            "error": signal_result.get("error"),
        }
        positions.append(pos_info)

        if not signal_result.get("ok"):
            errors.append(f"{coin}:{signal_result.get('error')}")
            continue

        # Exit signal → market close
        if signal_result.get("exit_signal"):
            if LIVE_MODE:
                action = _place_market_close(coin, size, is_long=True)
                actions.append(action)
                _alert(f"[failsafe] EXIT {coin} {signal_result['exit_signal']} size={size}")
            else:
                actions.append({
                    "action": "market_close",
                    "coin": coin,
                    "size": size,
                    "signal": signal_result["exit_signal"],
                    "mode": "dry_run",
                })

        # Align hard SL
        if hard_sl_px:
            sl_action = _align_hard_sl(coin, hard_sl_px, current_trigger)
            if sl_action:
                actions.append(sl_action)
                if LIVE_MODE:
                    _alert(f"[failsafe] SL align {coin} → {hard_sl_px}")

    result = {
        "ts": ts,
        "mode": mode,
        "errors": errors,
        "actions": actions,
        "positions": positions,
        "status": "ok" if not errors else "partial_error",
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    sys.stderr.write(f"[failsafe] wrote {OUT}\n")

    return result


def main() -> None:
    result = run_failsafe()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["status"] in ("ok", "partial_error") else 1)


if __name__ == "__main__":
    main()
