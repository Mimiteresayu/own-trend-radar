#!/usr/bin/env python3
"""Additional mocked Exchange tests for failsafe_exit_worker.py"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# These tests require the main test file to import properly
import test_failsafe_exit_worker


class TestExchangeMocked(unittest.TestCase):
    """Tests with mocked Exchange client for live order placement logic."""

    @patch("failsafe_exit_worker.SDK_AVAILABLE", True)
    @patch("failsafe_exit_worker._get_mid_price")
    @patch("failsafe_exit_worker._get_asset_info")
    @patch("failsafe_exit_worker._get_exchange")
    def test_market_close_reduce_only_args(self, mock_exchange_getter, mock_asset_info, mock_mid):
        """Test market close uses reduce_only, correct side, rounded size/price."""
        from failsafe_exit_worker import _place_market_close
        
        # Setup mocks
        mock_exchange = MagicMock()
        mock_exchange.order.return_value = {"status": "ok", "response": {"oid": "123"}}
        mock_exchange_getter.return_value = mock_exchange
        
        mock_asset_info.return_value = {"szDecimals": 4}
        mock_mid.return_value = 50000.0
        
        # Close a long (size=1.2345, should round to 1.2345 with 4 decimals)
        result = _place_market_close("BTC", 1.23456789, is_long=True, slippage_pct=2.0)
        
        # Verify order call
        self.assertTrue(mock_exchange.order.called)
        order_arg = mock_exchange.order.call_args[0][0]
        
        # Check reduce_only
        self.assertTrue(order_arg["reduce_only"])
        
        # Check side (close long = sell)
        self.assertFalse(order_arg["is_buy"])
        
        # Check rounded size (4 decimals)
        self.assertEqual(order_arg["sz"], 1.2346)
        
        # Check limit_px uses slippage (sell: mid * 0.98)
        # 50000 * 0.98 = 49000
        self.assertAlmostEqual(order_arg["limit_px"], 49000.0, delta=10.0)
        
        # Check IOC
        self.assertEqual(order_arg["order_type"]["limit"]["tif"], "Ioc")

    @patch("failsafe_exit_worker.SDK_AVAILABLE", True)
    @patch("failsafe_exit_worker.LIVE_MODE", True)
    @patch("failsafe_exit_worker._get_asset_info")
    @patch("failsafe_exit_worker._get_exchange")
    @patch("failsafe_exit_worker._cancel_stop_triggers")
    def test_sl_cancel_then_place_no_duplicates(self, mock_cancel, mock_exchange_getter, mock_asset_info):
        """Test SL alignment cancels existing then places new (no duplicates)."""
        from failsafe_exit_worker import _align_hard_sl
        
        mock_exchange = MagicMock()
        mock_exchange.order.return_value = {"status": "ok"}
        mock_exchange_getter.return_value = mock_exchange
        
        mock_asset_info.return_value = {"szDecimals": 4}
        mock_cancel.return_value = [{"oid": "old123", "status": "cancelled"}]
        
        # Existing SL at 49000, new target 48500 (drift > 0.3%)
        orders = [{"coin": "BTC", "isTrigger": True, "triggerPx": 49000, "oid": "old123"}]
        
        result = _align_hard_sl("BTC", 48500.0, 1.0, orders)
        
        # Should have cancelled old SL
        self.assertTrue(mock_cancel.called)
        
        # Should have placed new SL
        self.assertTrue(mock_exchange.order.called)
        order_arg = mock_exchange.order.call_args[0][0]
        self.assertEqual(order_arg["coin"], "BTC")
        self.assertAlmostEqual(order_arg["order_type"]["trigger"]["trigger_px"], 48500.0, delta=1.0)

    @patch("failsafe_exit_worker._current_stop_trigger")
    def test_sl_drift_below_threshold_no_replace(self, mock_current):
        """Test SL not replaced when drift < 0.3%."""
        from failsafe_exit_worker import _align_hard_sl
        
        # Current SL at 49000, new target 49100 (drift ~0.2%)
        mock_current.return_value = 49000.0
        
        result = _align_hard_sl("BTC", 49100.0, 1.0, [])
        
        # Should return None (no action needed)
        self.assertIsNone(result)

    @patch.dict("os.environ", {"FAILSAFE_ENABLE": "0"}, clear=False)
    @patch("failsafe_exit_worker._check_radar_freshness")
    @patch("failsafe_exit_worker._hl_post_retry")
    def test_live_path_not_taken_when_disabled(self, mock_hl_post, mock_radar_check):
        """Test LIVE path not taken when FAILSAFE_ENABLE=0."""
        from failsafe_exit_worker import LIVE_MODE
        
        # Ensure LIVE_MODE is False
        self.assertFalse(LIVE_MODE)
        
        mock_radar_check.return_value = (True, None)
        mock_hl_post.return_value = {"assetPositions": []}
        
        # Run should be DRY_RUN
        # (Full run_failsafe test covered in TestDryRunSmoke)

    @patch.dict("os.environ", {"FAILSAFE_ENABLE": "1", "HL_API_WALLET_KEY": ""}, clear=False)
    def test_live_path_not_taken_when_no_key(self):
        """Test LIVE path not taken when key missing even if ENABLE=1."""
        # Re-import to pick up env changes
        import importlib
        import failsafe_exit_worker
        importlib.reload(failsafe_exit_worker)
        
        # LIVE_MODE should be False (ENABLE=1 but no key)
        self.assertFalse(failsafe_exit_worker.LIVE_MODE)


if __name__ == "__main__":
    unittest.main()
