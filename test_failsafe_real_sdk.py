#!/usr/bin/env python3
"""Real SDK tests: construct real Exchange with mocked HTTP layer to verify order payloads."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, '/home/ubuntu/.local/lib/python3.12/site-packages')


class TestRealSDKOrderPayloads(unittest.TestCase):
    """Tests with REAL Exchange object, mocked HTTP post to verify signed payloads."""

    def setUp(self):
        """Create a real Exchange with throwaway key and mocked HTTP."""
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from eth_account import Account
        
        # Create throwaway account
        self.account = Account.create()
        
        # Mock meta/spotMeta to avoid network calls in Exchange.__init__
        mock_meta = {
            "universe": [
                {"name": "BTC", "szDecimals": 4},
                {"name": "ETH", "szDecimals": 3},
            ]
        }
        mock_spot_meta = {"tokens": [], "universe": []}
        
        # Create real Exchange with mocked HTTP
        with patch.object(Info, 'post', return_value={"universe": mock_meta["universe"]}):
            self.exchange = Exchange(
                wallet=self.account,
                base_url="https://api.hyperliquid.xyz",
                meta=mock_meta,
                spot_meta=mock_spot_meta,
            )

    @patch('hyperliquid.exchange.Exchange.post')
    def test_market_close_real_sdk_payload(self, mock_post):
        """Test market_close generates correct signed payload for reduce-only close."""
        # Mock successful response
        mock_post.return_value = {"status": "ok", "response": {"data": {"statuses": [{"filled": {}}]}}}
        
        # Mock user_state (market_close fetches positions to determine side)
        mock_user_state = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "1.5",  # positive = long position
                    }
                }
            ]
        }
        with patch.object(self.exchange.info, 'user_state', return_value=mock_user_state):
            # Call market_close
            result = self.exchange.market_close("BTC", sz=1.5, slippage=0.02)
        
        # Verify Exchange.post was called
        self.assertTrue(mock_post.called)
        call_args = mock_post.call_args
        
        # First arg is URL path
        url_path = call_args[0][0]
        self.assertEqual(url_path, "/exchange")
        
        # Second arg is payload (signed action)
        payload = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get('payload')
        
        # Payload should have action with type and orders
        self.assertIn("action", payload)
        action = payload["action"]
        self.assertEqual(action["type"], "order")
        
        # Check orders list
        self.assertIn("orders", action)
        orders = action["orders"]
        self.assertIsInstance(orders, list)
        self.assertGreater(len(orders), 0)
        
        # Check first order (market close)
        order = orders[0]
        self.assertIn("a", order)  # asset index (coin ID)
        self.assertIn("b", order)  # is_buy
        self.assertIn("p", order)  # limit px (string)
        self.assertIn("s", order)  # size (string)
        self.assertIn("r", order)  # reduce_only
        self.assertIn("t", order)  # order_type
        
        # Verify reduce_only is true
        self.assertTrue(order["r"])
        
        # Verify size is string (rounded to szDecimals=4 for BTC)
        self.assertIsInstance(order["s"], str)
        self.assertEqual(order["s"], "1.5")
        
        # Verify order type has limit with Ioc tif
        self.assertIn("limit", order["t"])
        self.assertEqual(order["t"]["limit"]["tif"], "Ioc")

    @patch('hyperliquid.exchange.Exchange.post')
    def test_stop_trigger_order_real_sdk_payload(self, mock_post):
        """Test SL trigger order generates correct signed payload with triggerPx and tpsl=sl."""
        from failsafe_exit_worker import _place_stop_trigger, _get_asset_info
        
        # Mock successful response
        mock_post.return_value = {"status": "ok", "response": {"data": {"statuses": [{"filled": {}}]}}}
        
        # Mock asset info
        with patch('failsafe_exit_worker._get_asset_info', return_value={"szDecimals": 4}):
            with patch('failsafe_exit_worker._get_exchange', return_value=self.exchange):
                with patch('failsafe_exit_worker.LIVE_MODE', True):
                    result = _place_stop_trigger("BTC", trigger_px=48500.0, size=1.0, slippage_pct=2.0)
        
        # Verify Exchange.post was called
        self.assertTrue(mock_post.called)
        call_args = mock_post.call_args
        
        # Get payload
        payload = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get('payload')
        
        # Check action
        self.assertIn("action", payload)
        action = payload["action"]
        self.assertEqual(action["type"], "order")
        
        # Check order
        orders = action["orders"]
        order = orders[0]
        
        # Verify reduce_only
        self.assertTrue(order["r"])
        
        # Verify is_buy=False (sell on stop for longs)
        self.assertFalse(order["b"])
        
        # Verify order type has trigger
        self.assertIn("trigger", order["t"])
        trigger = order["t"]["trigger"]
        
        # Verify trigger has triggerPx (string), isMarket=True, tpsl="sl"
        self.assertIn("triggerPx", trigger)
        self.assertIsInstance(trigger["triggerPx"], str)
        self.assertTrue(trigger["isMarket"])
        self.assertEqual(trigger["tpsl"], "sl")
        
        # Verify triggerPx value (48500.0 rounded)
        trigger_px_float = float(trigger["triggerPx"])
        self.assertAlmostEqual(trigger_px_float, 48500.0, delta=1.0)

    @patch('hyperliquid.exchange.Exchange.post')
    def test_cancel_order_real_sdk_payload(self, mock_post):
        """Test cancel order generates correct signed payload."""
        # Mock successful response
        mock_post.return_value = {"status": "ok"}
        
        # Call cancel
        result = self.exchange.cancel("BTC", 12345)
        
        # Verify Exchange.post was called
        self.assertTrue(mock_post.called)
        call_args = mock_post.call_args
        
        # Get payload
        payload = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get('payload')
        
        # Check action
        self.assertIn("action", payload)
        action = payload["action"]
        self.assertEqual(action["type"], "cancel")
        
        # Check cancels list
        self.assertIn("cancels", action)
        cancels = action["cancels"]
        self.assertIsInstance(cancels, list)
        self.assertGreater(len(cancels), 0)
        
        # Check first cancel
        cancel = cancels[0]
        self.assertIn("a", cancel)  # asset index
        self.assertIn("o", cancel)  # oid
        self.assertEqual(cancel["o"], 12345)


if __name__ == "__main__":
    unittest.main()
