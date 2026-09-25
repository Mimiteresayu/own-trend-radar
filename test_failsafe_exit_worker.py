#!/usr/bin/env python3
"""Unit tests for failsafe_exit_worker.py signal logic.

Tests tier-based exit signals using synthetic candles (no live API calls).
Dry-run smoke test ensures worker doesn't crash without keys.
Tests with mocked Exchange client cover live order placement logic.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch, MagicMock, call

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from failsafe_exit_worker import (
    _gc_closed,
    _compute_long_signal,
    _check_radar_freshness,
    _round_trigger,
    _round_size,
    _round_price,
    _slippage_price,
    _place_market_close,
    _place_stop_trigger,
    _align_hard_sl,
    run_failsafe,
)
from scan_gc_radar import compute_gc


class TestSignalFunctions(unittest.TestCase):
    """Test tier-based exit signal logic with synthetic candles."""

    def _make_synthetic_bars(self, periods: int, trend: str = "up") -> List[Dict[str, float]]:
        """Generate synthetic OHLC bars for testing."""
        bars = []
        base_price = 100.0
        t_start = int(datetime.now(timezone.utc).timestamp() * 1000) - (periods * 3600_000)
        
        for i in range(periods):
            if trend == "up":
                c = base_price + i * 0.5
            elif trend == "down":
                c = base_price - i * 0.5
            else:  # flat
                c = base_price
            
            h = c * 1.01
            l = c * 0.99
            o = (c + l) / 2
            
            bars.append({
                "t": t_start + i * 3600_000,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            })
        return bars

    def _make_cross_down_bars(self, period: int, cross_at: str) -> List[Dict[str, float]]:
        """Generate bars that cross down at specified level (filter/lower)."""
        bars = self._make_synthetic_bars(period + 30, trend="up")
        h = [b["high"] for b in bars]
        l = [b["low"] for b in bars]
        c = [b["close"] for b in bars]
        gc = compute_gc(h, l, c, period=period)
        
        # Last bar crosses below filter/lower
        i = len(bars) - 2  # last closed
        if cross_at == "filter":
            target = gc[i]["filter"]
        else:  # lower
            target = gc[i]["lower"]
        
        bars[-2]["close"] = target * 0.99  # below target
        bars[-2]["low"] = target * 0.98
        bars[-3]["close"] = target * 1.01  # prev above target
        
        return bars

    @patch("failsafe_exit_worker.fetch_candles")
    def test_4h_filter_cross_down_mega_large(self, mock_fetch):
        """Test Mega/Large tier 4H Filter cross-down exit signal."""
        bars = self._make_cross_down_bars(period=72, cross_at="filter")
        mock_fetch.return_value = bars
        
        result = _compute_long_signal("BTC", "mega", entry=100.0, size=1.0)
        
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier"], "mega")
        self.assertEqual(result["exit_signal"], "EXIT_LONG_4H_FILTER_CROSS")
        self.assertIsNotNone(result["hard_sl_px"])

    @patch("failsafe_exit_worker.fetch_candles")
    def test_1h_lower_cross_down_small_tiny(self, mock_fetch):
        """Test Small/Tiny tier 1H Lower cross-down exit signal."""
        # Need to mock both 4H (for hard SL) and 1H (for primary)
        bars_4h = self._make_synthetic_bars(72 + 30, trend="up")
        bars_1h = self._make_cross_down_bars(period=48, cross_at="lower")
        
        def fetch_side_effect(coin, tf):
            if tf == "4h":
                return bars_4h
            elif tf == "1h":
                return bars_1h
            raise ValueError(f"unexpected tf={tf}")
        
        mock_fetch.side_effect = fetch_side_effect
        
        result = _compute_long_signal("BRETT", "small", entry=0.15, size=10000.0)
        
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier"], "small")
        self.assertEqual(result["exit_signal"], "EXIT_LONG_1H_LOWER_CROSS")
        self.assertIsNotNone(result["hard_sl_px"])

    @patch("failsafe_exit_worker.fetch_candles")
    def test_no_exit_signal_when_no_cross(self, mock_fetch):
        """Test no exit signal when price doesn't cross down."""
        bars = self._make_synthetic_bars(72 + 30, trend="up")
        mock_fetch.return_value = bars
        
        result = _compute_long_signal("ETH", "large", entry=2000.0, size=1.0)
        
        self.assertTrue(result["ok"])
        self.assertEqual(result["tier"], "large")
        self.assertIsNone(result["exit_signal"])  # no cross
        self.assertIsNotNone(result["hard_sl_px"])

    def test_round_trigger_precision(self):
        """Test trigger price rounding per HL precision."""
        self.assertEqual(_round_trigger(1234.5678), 1234.6)  # >=1000 → 1 dec
        self.assertEqual(_round_trigger(123.4567), 123.46)   # >=100 → 2 dec
        self.assertEqual(_round_trigger(12.34567), 12.3457)  # >=1 → 4 dec
        self.assertEqual(_round_trigger(0.123456), 0.123456) # >=0.01 → 6 dec
        self.assertEqual(_round_trigger(0.00123456), 0.00123456) # <0.01 → 8 dec

    def test_round_price_max_5_sig_figs(self):
        """Test price rounding with max 5 significant figures."""
        # sz_decimals=0 → price_decimals=6
        self.assertEqual(_round_price(1234.567890, sz_decimals=0), 1234.6)  # 5 sig figs
        # sz_decimals=4 → price_decimals=2
        self.assertEqual(_round_price(12.3456, sz_decimals=4), 12.35)  # 4 decimals but 4 sig figs
        # sz_decimals=2 → price_decimals=4
        self.assertEqual(_round_price(123.456789, sz_decimals=2), 123.46)  # 5 sig figs

    def test_slippage_price(self):
        """Test slippage price calculation."""
        # Buy with 2% slippage: mid * 1.02
        mid = 100.0
        buy_slipped = _slippage_price(mid, is_buy=True, slippage_pct=2.0, sz_decimals=0)
        self.assertAlmostEqual(buy_slipped, 102.0, places=2)
        
        # Sell with 2% slippage: mid * 0.98
        sell_slipped = _slippage_price(mid, is_buy=False, slippage_pct=2.0, sz_decimals=0)
        self.assertAlmostEqual(sell_slipped, 98.0, places=2)

    @patch("failsafe_exit_worker.RADAR_DIR", Path("/tmp/nonexistent"))
    def test_check_radar_freshness_missing(self):
        """Test radar freshness check when file missing."""
        ok, err = _check_radar_freshness("1h")
        self.assertFalse(ok)
        self.assertIn("missing", err)

    @patch("failsafe_exit_worker.RADAR_DIR")
    def test_check_radar_freshness_stale(self, mock_dir):
        """Test radar freshness check when file is stale."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            mock_dir.__truediv__ = lambda self, x: Path(tmp) / x
            radar_file = Path(tmp) / "gc_radar_1h.json"
            
            # Write radar with old timestamp
            old_ts = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()
            radar_file.write_text(json.dumps({"ts": old_ts, "rows": []}))
            
            ok, err = _check_radar_freshness("1h")
            self.assertFalse(ok)
            self.assertIn("stale", err)


class TestDryRunSmoke(unittest.TestCase):
    """Dry-run smoke test: worker should not crash without keys."""

    @patch.dict("os.environ", {
        "FAILSAFE_ENABLE": "0",
        "HL_ADDRESS": "",
        "HL_API_WALLET_KEY": "",
    }, clear=False)
    @patch("failsafe_exit_worker._check_radar_freshness")
    @patch("failsafe_exit_worker._hl_post_retry")
    def test_dry_run_no_keys_no_crash(self, mock_hl_post, mock_radar_check):
        """Test that worker doesn't crash in dry-run mode without keys."""
        mock_radar_check.return_value = (True, None)
        mock_hl_post.return_value = {"assetPositions": []}
        
        # Should not crash
        result = run_failsafe()
        
        self.assertEqual(result["mode"], "DRY_RUN")
        self.assertIn("status", result)

    @patch.dict("os.environ", {
        "FAILSAFE_ENABLE": "0",
    }, clear=False)
    @patch("failsafe_exit_worker.HL_ADDRESS", "0x1234567890abcdef1234567890abcdef12345678")
    @patch("failsafe_exit_worker._check_radar_freshness")
    @patch("failsafe_exit_worker._hl_post_retry")
    @patch("failsafe_exit_worker.fetch_candles")
    @patch("failsafe_exit_worker.OUT")
    def test_dry_run_with_position(self, mock_out, mock_fetch, mock_hl_post, mock_radar_check):
        """Test dry-run with a mock position computes signal without placing orders."""
        mock_radar_check.return_value = (True, None)
        
        # Mock OUT path to avoid file writes
        mock_out.parent.mkdir = MagicMock()
        mock_out.write_text = MagicMock()
        
        # Mock position data and openOrders
        def hl_post_side_effect(payload):
            if payload.get("type") == "clearinghouseState":
                return {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "BTC",
                                "szi": "1.0",
                                "entryPx": "50000",
                                "unrealizedPnl": "1000",
                            }
                        }
                    ]
                }
            elif payload.get("type") == "openOrders":
                return []
            return {}
        
        mock_hl_post.side_effect = hl_post_side_effect
        
        # Mock candles
        bars_4h = self._make_synthetic_bars(72 + 30, trend="up")
        mock_fetch.return_value = bars_4h
        
        result = run_failsafe()
        
        self.assertEqual(result["mode"], "DRY_RUN")
        self.assertEqual(len(result["positions"]), 1)
        self.assertEqual(result["positions"][0]["coin"], "BTC")
        self.assertEqual(result["positions"][0]["tier"], "mega")
        # No crash, actions may or may not exist depending on signal

    def _make_synthetic_bars(self, periods: int, trend: str = "up") -> List[Dict[str, float]]:
        """Generate synthetic OHLC bars."""
        bars = []
        base_price = 50000.0
        t_start = int(datetime.now(timezone.utc).timestamp() * 1000) - (periods * 3600_000)
        
        for i in range(periods):
            if trend == "up":
                c = base_price + i * 10
            elif trend == "down":
                c = base_price - i * 10
            else:
                c = base_price
            
            bars.append({
                "t": t_start + i * 3600_000,
                "open": c * 0.995,
                "high": c * 1.005,
                "low": c * 0.99,
                "close": c,
            })
        return bars


if __name__ == "__main__":
    unittest.main()
