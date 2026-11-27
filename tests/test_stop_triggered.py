"""F5 tests: stop leg triggered but limit unfilled detection."""
from decimal import Decimal
from pathlib import Path

import pytest

import bot
from fake_exchange import FakeExchange, RecNotifier


def make_cfg_for_protect(tmp_path):
    import os
    env = {
        "SYMBOLS": "NEARUSDT", "TIMEFRAME": "1h", "RSI_PERIOD": "14",
        "RSI_OVERSOLD": "16", "RSI_TRIGGER": "18", "TP_PCT": "2", "SL_PCT": "2.5",
        "SL_TYPE": "STOP_LOSS_LIMIT", "SL_LIMIT_BUFFER_PCT": "0.3",
        "QUOTE_PER_TRADE": "20", "MAX_OPEN_POSITIONS": "3",
        "POLL_INTERVAL_SECONDS": "30", "CANDLE_DELAY_SECONDS": "3",
        "SIGNAL_MAX_AGE_SECONDS": "600", "KLINE_LIMIT": "500", "RECV_WINDOW_MS": "5000",
        "DRY_RUN": "false", "BINANCE_TESTNET": "false", "BINANCE_API_KEY": "k",
        "BINANCE_API_SECRET": "s", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
        "EMERGENCY_SELL_ON_OCO_FAIL": "true", "DATA_DIR": str(tmp_path),
        "STATE_FILE": "",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for k in [k for k, v in env.items() if v is None]:
        os.environ.pop(k, None)
    try:
        return bot.load_config()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestStopTriggeredUnfilled:
    def test_detect_stop_triggered_tp_canceled_sl_new(self, tmp_path):
        """TP CANCELED + SL NEW -> alert sent, pos marked."""
        cfg = make_cfg_for_protect(tmp_path)
        fake = FakeExchange()
        state = bot.State(cfg.state_path, cfg.venue)
        notifier = RecNotifier()
        trader = bot.Trader(cfg, fake, state, notifier)

        # Setup position with PROTECTED status
        pos = {
            "status": "PROTECTED",
            "qty": "10.2",
            "entry_price": "1.943",
            "tp": "1.982",
            "sl_stop": "1.894",
            "sl_limit": "1.888",
            "oco_client_id": "oco123",
            "oco_list_id": 777,
        }
        state.positions["NEARUSDT"] = pos
        state.save()

        # Mock get_order_list to return TP CANCELED, SL NEW
        fake.list_exists = True
        fake.list_state = "EXECUTING"
        fake.orders[5551] = {"orderId": 5551, "status": "CANCELED", "type": "LIMIT_MAKER",
                             "side": "SELL", "executedQty": "0", "cummulativeQuoteQty": "0",
                             "price": "1.982"}
        fake.orders[5552] = {"orderId": 5552, "status": "NEW", "type": "STOP_LOSS_LIMIT",
                             "side": "SELL", "executedQty": "0", "cummulativeQuoteQty": "0",
                             "price": "1.888", "stopPrice": "1.894"}

        # Call _check_exit
        trader._check_exit("NEARUSDT", pos, trader.get_info("NEARUSDT"))

        # Verify alert sent
        assert any("Stop leg TRIGGERED" in m for m in notifier.msgs)
        # Verify position marked
        assert state.positions["NEARUSDT"].get("stop_triggered_unfilled") is True
        assert state.positions["NEARUSDT"].get("sl_order_id") == 5552

    def test_no_alert_when_both_filled(self, tmp_path):
        """ALL_DONE with one filled -> no stop-triggered alert (handled by normal flow)."""
        cfg = make_cfg_for_protect(tmp_path)
        fake = FakeExchange()
        state = bot.State(cfg.state_path, cfg.venue)
        notifier = RecNotifier()
        trader = bot.Trader(cfg, fake, state, notifier)

        pos = {
            "status": "PROTECTED",
            "qty": "10.2",
            "entry_price": "1.943",
            "tp": "1.982",
            "sl_stop": "1.894",
            "sl_limit": "1.888",
            "oco_client_id": "oco123",
            "oco_list_id": 777,
        }
        state.positions["NEARUSDT"] = pos
        state.save()

        # ALL_DONE with TP filled
        fake.list_exists = True
        fake.list_state = "ALL_DONE"
        fake.seed_tp_fill(Decimal("10.2"), Decimal("1.982"))

        trader._check_exit("NEARUSDT", pos, trader.get_info("NEARUSDT"))

        # Should record close, not send stop-triggered alert
        assert not any("Stop leg TRIGGERED" in m for m in notifier.msgs)

    def test_no_alert_when_executing_both_active(self, tmp_path):
        """EXECUTING with both legs active -> no alert."""
        cfg = make_cfg_for_protect(tmp_path)
        fake = FakeExchange()
        state = bot.State(cfg.state_path, cfg.venue)
        notifier = RecNotifier()
        trader = bot.Trader(cfg, fake, state, notifier)

        pos = {
            "status": "PROTECTED",
            "qty": "10.2",
            "entry_price": "1.943",
            "tp": "1.982",
            "sl_stop": "1.894",
            "sl_limit": "1.888",
            "oco_client_id": "oco123",
            "oco_list_id": 777,
        }
        state.positions["NEARUSDT"] = pos
        state.save()

        # Both legs active (normal)
        fake.list_exists = True
        fake.list_state = "EXECUTING"
        fake.orders[5551] = {"orderId": 5551, "status": "NEW", "type": "LIMIT_MAKER",
                             "side": "SELL", "executedQty": "0", "cummulativeQuoteQty": "0",
                             "price": "1.982"}
        fake.orders[5552] = {"orderId": 5552, "status": "NEW", "type": "STOP_LOSS_LIMIT",
                             "side": "SELL", "executedQty": "0", "cummulativeQuoteQty": "0",
                             "price": "1.888", "stopPrice": "1.894"}

        trader._check_exit("NEARUSDT", pos, trader.get_info("NEARUSDT"))

        assert not any("Stop leg TRIGGERED" in m for m in notifier.msgs)
        assert not state.positions["NEARUSDT"].get("stop_triggered_unfilled")

    def test_no_alert_when_sl_filled_tp_canceled(self, tmp_path):
        """ALL_DONE with SL filled, TP canceled -> normal SL close, not stop-triggered alert."""
        cfg = make_cfg_for_protect(tmp_path)
        fake = FakeExchange()
        state = bot.State(cfg.state_path, cfg.venue)
        notifier = RecNotifier()
        trader = bot.Trader(cfg, fake, state, notifier)

        pos = {
            "status": "PROTECTED",
            "qty": "10.2",
            "entry_price": "1.943",
            "tp": "1.982",
            "sl_stop": "1.894",
            "sl_limit": "1.888",
            "oco_client_id": "oco123",
            "oco_list_id": 777,
        }
        state.positions["NEARUSDT"] = pos
        state.save()

        # ALL_DONE with SL filled
        fake.list_exists = True
        fake.list_state = "ALL_DONE"
        fake.seed_sl_fill(Decimal("10.2"), Decimal("1.888"))

        trader._check_exit("NEARUSDT", pos, trader.get_info("NEARUSDT"))

        # Should record close with reason SL
        assert any("SL NEARUSDT" in m for m in notifier.msgs)
        assert not any("Stop leg TRIGGERED" in m for m in notifier.msgs)