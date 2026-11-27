"""F4 tests: notional adequacy with step rounding and sl_limit price."""
from decimal import Decimal
from pathlib import Path

import pytest

import bot
from fake_exchange import FakeExchange, RecNotifier


def make_cfg_for_notional(tmp_path, quote_per_trade="20"):
    import os
    env = {
        "SYMBOLS": "NEARUSDT", "TIMEFRAME": "1h", "RSI_PERIOD": "14",
        "RSI_OVERSOLD": "16", "RSI_TRIGGER": "18", "TP_PCT": "2", "SL_PCT": "2.5",
        "SL_TYPE": "STOP_LOSS_LIMIT", "SL_LIMIT_BUFFER_PCT": "0.3",
        "QUOTE_PER_TRADE": quote_per_trade, "MAX_OPEN_POSITIONS": "3",
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


class TestNotionalAdequacy:
    def test_compute_qty_and_notional_basic(self):
        """Basic computation with NEARUSDT-like filters."""
        info = bot.SymbolInfo(
            symbol="NEARUSDT", base="NEAR", quote="USDT",
            tick=Decimal("0.001"), step=Decimal("0.1"),
            min_qty=Decimal("0.1"), min_notional=Decimal("5"),
            quote_precision=8, oco_allowed=True,
        )
        cfg = bot.Config(
            api_key="k", api_secret="s", testnet=False, dry_run=False,
            symbols=["NEARUSDT"], interval="1h", rsi_period=14, rsi_oversold=16.0,
            rsi_trigger=18.0, tp_pct=Decimal("2"), sl_pct=Decimal("2.5"),
            sl_type="STOP_LOSS_LIMIT", sl_limit_buffer_pct=Decimal("0.3"),
            quote_per_trade=Decimal("20"), max_open_positions=3, poll_interval=30,
            candle_delay=3, signal_max_age=600, kline_limit=500, recv_window=5000,
            emergency_sell=True, data_dir=Path("/tmp"), tg_token="", tg_chat="",
            state_file="state.json", state_path=Path("/tmp/state.json"),
        )
        entry = Decimal("1.943")
        qty, notional_entry, notional_sl = bot.compute_qty_and_notional(entry, Decimal("20"), info, cfg)
        # raw_qty = 20 / 1.943 = 10.29 -> floor to 0.1 step = 10.2
        assert qty == Decimal("10.2")
        assert notional_entry == qty * entry
        # sl_limit computed by compute_levels with rounding: sl_price = 1.888
        assert notional_sl == qty * Decimal("1.888")
        assert notional_sl < notional_entry

    def test_check_notional_adequacy_pass(self):
        """Adequate quote_per_trade passes."""
        info = bot.SymbolInfo(
            symbol="NEARUSDT", base="NEAR", quote="USDT",
            tick=Decimal("0.001"), step=Decimal("0.1"),
            min_qty=Decimal("0.1"), min_notional=Decimal("5"),
            quote_precision=8, oco_allowed=True,
        )
        cfg = bot.Config(
            api_key="k", api_secret="s", testnet=False, dry_run=False,
            symbols=["NEARUSDT"], interval="1h", rsi_period=14, rsi_oversold=16.0,
            rsi_trigger=18.0, tp_pct=Decimal("2"), sl_pct=Decimal("2.5"),
            sl_type="STOP_LOSS_LIMIT", sl_limit_buffer_pct=Decimal("0.3"),
            quote_per_trade=Decimal("20"), max_open_positions=3, poll_interval=30,
            candle_delay=3, signal_max_age=600, kline_limit=500, recv_window=5000,
            emergency_sell=True, data_dir=Path("/tmp"), tg_token="", tg_chat="",
            state_file="state.json", state_path=Path("/tmp/state.json"),
        )
        ok, msg, qty, ne, ns = bot.check_notional_adequacy(Decimal("20"), info, cfg, Decimal("1.943"))
        assert ok
        assert msg == ""
        assert qty > 0

    def test_check_notional_adequacy_fail_min_qty(self):
        """Too small quote_per_trade -> qty < min_qty."""
        info = bot.SymbolInfo(
            symbol="NEARUSDT", base="NEAR", quote="USDT",
            tick=Decimal("0.001"), step=Decimal("0.1"),
            min_qty=Decimal("0.1"), min_notional=Decimal("5"),
            quote_precision=8, oco_allowed=True,
        )
        cfg = bot.Config(
            api_key="k", api_secret="s", testnet=False, dry_run=False,
            symbols=["NEARUSDT"], interval="1h", rsi_period=14, rsi_oversold=16.0,
            rsi_trigger=18.0, tp_pct=Decimal("2"), sl_pct=Decimal("2.5"),
            sl_type="STOP_LOSS_LIMIT", sl_limit_buffer_pct=Decimal("0.3"),
            quote_per_trade=Decimal("0.1"), max_open_positions=3, poll_interval=30,
            candle_delay=3, signal_max_age=600, kline_limit=500, recv_window=5000,
            emergency_sell=True, data_dir=Path("/tmp"), tg_token="", tg_chat="",
            state_file="state.json", state_path=Path("/tmp/state.json"),
        )
        ok, msg, qty, ne, ns = bot.check_notional_adequacy(Decimal("0.1"), info, cfg, Decimal("1.943"))
        assert not ok
        assert "minQty" in msg

    def test_check_notional_adequacy_fail_min_notional_entry(self):
        """Entry notional below min_notional."""
        info = bot.SymbolInfo(
            symbol="NEARUSDT", base="NEAR", quote="USDT",
            tick=Decimal("0.001"), step=Decimal("10"),  # large step
            min_qty=Decimal("10"), min_notional=Decimal("100"),
            quote_precision=8, oco_allowed=True,
        )
        cfg = bot.Config(
            api_key="k", api_secret="s", testnet=False, dry_run=False,
            symbols=["NEARUSDT"], interval="1h", rsi_period=14, rsi_oversold=16.0,
            rsi_trigger=18.0, tp_pct=Decimal("2"), sl_pct=Decimal("2.5"),
            sl_type="STOP_LOSS_LIMIT", sl_limit_buffer_pct=Decimal("0.3"),
            quote_per_trade=Decimal("50"), max_open_positions=3, poll_interval=30,
            candle_delay=3, signal_max_age=600, kline_limit=500, recv_window=5000,
            emergency_sell=True, data_dir=Path("/tmp"), tg_token="", tg_chat="",
            state_file="state.json", state_path=Path("/tmp/state.json"),
        )
        ok, msg, qty, ne, ns = bot.check_notional_adequacy(Decimal("50"), info, cfg, Decimal("1.0"))
        assert not ok
        assert "notional entry" in msg

    def test_check_notional_adequacy_fail_min_notional_sl(self):
        """SL limit notional below min_notional (due to sl_limit price drop)."""
        info = bot.SymbolInfo(
            symbol="NEARUSDT", base="NEAR", quote="USDT",
            tick=Decimal("0.001"), step=Decimal("0.1"),
            min_qty=Decimal("0.1"), min_notional=Decimal("20"),
            quote_precision=8, oco_allowed=True,
        )
        cfg = bot.Config(
            api_key="k", api_secret="s", testnet=False, dry_run=False,
            symbols=["NEARUSDT"], interval="1h", rsi_period=14, rsi_oversold=16.0,
            rsi_trigger=18.0, tp_pct=Decimal("2"), sl_pct=Decimal("50"),  # huge SL
            sl_type="STOP_LOSS_LIMIT", sl_limit_buffer_pct=Decimal("0.3"),
            quote_per_trade=Decimal("25"), max_open_positions=3, poll_interval=30,
            candle_delay=3, signal_max_age=600, kline_limit=500, recv_window=5000,
            emergency_sell=True, data_dir=Path("/tmp"), tg_token="", tg_chat="",
            state_file="state.json", state_path=Path("/tmp/state.json"),
        )
        ok, msg, qty, ne, ns = bot.check_notional_adequacy(Decimal("25"), info, cfg, Decimal("1.0"))
        assert not ok
        assert "notional SL limit" in msg