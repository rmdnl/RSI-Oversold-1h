"""F7 tests: -1121 invalid symbol -> ConfigError (exit 2)."""
from decimal import Decimal
from pathlib import Path

import pytest

import bot
from fake_exchange import FakeExchange, RecNotifier


def make_cfg_for_startup(tmp_path, symbols="NEARUSDT"):
    import os
    env = {
        "SYMBOLS": symbols, "TIMEFRAME": "1h", "RSI_PERIOD": "14",
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


class TestInvalidSymbolError:
    def test_binance_error_invalid_symbol_property(self):
        """BinanceError.invalid_symbol true untuk code -1121."""
        exc = bot.BinanceError(400, -1121, "Invalid symbol")
        assert exc.invalid_symbol is True
        assert exc.transient is False
        assert exc.not_found is False

    def test_binance_error_not_invalid_symbol(self):
        """BinanceError.invalid_symbol false untuk code lain."""
        for code in [-2013, -1021, -1003, 0, 123]:
            exc = bot.BinanceError(400, code, "msg")
            assert exc.invalid_symbol is False, f"code {code} should not be invalid_symbol"

    def test_startup_invalid_symbol_raises_config_error(self, tmp_path):
        """Startup dengan symbol invalid -> ConfigError (exit 2), bukan BinanceError."""
        cfg = make_cfg_for_startup(tmp_path, symbols="INVALIDUSDT")
        fake = FakeExchange()
        # Simulasikan exchange_info melempar BinanceError code -1121
        def bad_exchange_info(symbols):
            raise bot.BinanceError(400, -1121, "Invalid symbol")
        fake.exchange_info = bad_exchange_info

        state = bot.State(cfg.state_path, cfg.venue)
        notifier = RecNotifier()
        trader = bot.Trader(cfg, fake, state, notifier)

        with pytest.raises(bot.ConfigError, match="Simbol tidak valid.*-1121"):
            trader.startup()

    def test_startup_other_binance_error_not_caught(self, tmp_path):
        """BinanceError lain (bukan -1121) tetap dilempari."""
        cfg = make_cfg_for_startup(tmp_path, symbols="NEARUSDT")
        fake = FakeExchange()
        def bad_exchange_info(symbols):
            raise bot.BinanceError(500, -1000, "Internal error")
        fake.exchange_info = bad_exchange_info

        state = bot.State(cfg.state_path, cfg.venue)
        notifier = RecNotifier()
        trader = bot.Trader(cfg, fake, state, notifier)

        with pytest.raises(bot.BinanceError):
            trader.startup()