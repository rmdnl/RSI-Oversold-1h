"""F10/F11 tests: Telegram HTTP status check + alerts for startup exit paths."""
from unittest.mock import patch, MagicMock
from decimal import Decimal
from pathlib import Path

import pytest

import bot
from fake_exchange import FakeExchange, RecNotifier


def make_cfg_for_notifier(tmp_path):
    import os
    env = {
        "SYMBOLS": "NEARUSDT", "TIMEFRAME": "1h", "RSI_PERIOD": "14",
        "RSI_OVERSOLD": "16", "RSI_TRIGGER": "18", "TP_PCT": "2", "SL_PCT": "2.5",
        "SL_TYPE": "STOP_LOSS_LIMIT", "SL_LIMIT_BUFFER_PCT": "0.3",
        "QUOTE_PER_TRADE": "20", "MAX_OPEN_POSITIONS": "3",
        "POLL_INTERVAL_SECONDS": "30", "CANDLE_DELAY_SECONDS": "3",
        "SIGNAL_MAX_AGE_SECONDS": "600", "KLINE_LIMIT": "500", "RECV_WINDOW_MS": "5000",
        "DRY_RUN": "false", "BINANCE_TESTNET": "false", "BINANCE_API_KEY": "k",
        "BINANCE_API_SECRET": "s", "TELEGRAM_BOT_TOKEN": "bot123:abc", "TELEGRAM_CHAT_ID": "456",
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


class TestTelegramNotifier:
    def test_send_checks_http_status_200(self, tmp_path):
        """HTTP 200 -> no warning logged."""
        cfg = make_cfg_for_notifier(tmp_path)
        notifier = bot.Notifier(cfg)

        with patch("bot.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"ok":true}'
            mock_post.return_value = mock_resp

            with patch("bot.log") as mock_log:
                notifier.send("test message")
                mock_post.assert_called_once()
                # No warning for 200
                warning_calls = [c for c in mock_log.warning.call_args_list if "Telegram HTTP" in str(c)]
                assert len(warning_calls) == 0

    def test_send_warns_on_non_200(self, tmp_path):
        """HTTP 400/401/500 -> warning logged with status and body."""
        cfg = make_cfg_for_notifier(tmp_path)
        notifier = bot.Notifier(cfg)

        with patch("bot.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 400
            mock_resp.text = '{"error_code":400,"description":"Bad Request: chat not found"}'
            mock_post.return_value = mock_resp

            with patch("bot.log") as mock_log:
                notifier.send("test message")
                warning_calls = [c for c in mock_log.warning.call_args_list if "Telegram HTTP" in str(c)]
                assert len(warning_calls) == 1
                args, _ = warning_calls[0]
                # args[0] is format string, args[1:] are the values
                assert args[1] == 400  # status code
                assert "Bad Request" in args[2]  # body

    def test_send_warns_on_request_exception(self, tmp_path):
        """Network error -> warning logged."""
        cfg = make_cfg_for_notifier(tmp_path)
        notifier = bot.Notifier(cfg)

        with patch("bot.requests.post", side_effect=bot.requests.RequestException("connection failed")):
            with patch("bot.log") as mock_log:
                notifier.send("test message")
                warning_calls = [c for c in mock_log.warning.call_args_list if "Telegram gagal" in str(c)]
                assert len(warning_calls) == 1

    def test_send_no_token_no_call(self, tmp_path):
        """Empty token -> no HTTP call."""
        cfg = make_cfg_for_notifier(tmp_path)
        cfg.tg_token = ""
        cfg.tg_chat = ""
        notifier = bot.Notifier(cfg)

        with patch("bot.requests.post") as mock_post:
            notifier.send("test message")
            mock_post.assert_not_called()


class TestStartupAlerts:
    def test_main_sends_alert_on_config_error(self, tmp_path):
        """ConfigError -> Telegram alert sent."""
        import os
        import sys
        # Invalid SYMBOLS to trigger ConfigError
        env = {
            "SYMBOLS": "", "TIMEFRAME": "1h", "RSI_PERIOD": "14",
            "RSI_OVERSOLD": "16", "RSI_TRIGGER": "18", "TP_PCT": "2", "SL_PCT": "2.5",
            "SL_TYPE": "STOP_LOSS_LIMIT", "SL_LIMIT_BUFFER_PCT": "0.3",
            "QUOTE_PER_TRADE": "20", "MAX_OPEN_POSITIONS": "3",
            "POLL_INTERVAL_SECONDS": "30", "CANDLE_DELAY_SECONDS": "3",
            "SIGNAL_MAX_AGE_SECONDS": "600", "KLINE_LIMIT": "500", "RECV_WINDOW_MS": "5000",
            "DRY_RUN": "false", "BINANCE_TESTNET": "false", "BINANCE_API_KEY": "k",
            "BINANCE_API_SECRET": "s", "TELEGRAM_BOT_TOKEN": "bot123:abc", "TELEGRAM_CHAT_ID": "456",
            "EMERGENCY_SELL_ON_OCO_FAIL": "true", "DATA_DIR": str(tmp_path),
            "STATE_FILE": "",
        }
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update({k: v for k, v in env.items() if v is not None})
        for k in [k for k, v in env.items() if v is None]:
            os.environ.pop(k, None)
        try:
            with patch("bot.requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_post.return_value = mock_resp
                with patch.object(sys, "argv", ["bot.py", "--check"]):
                    rc = bot.main()
                    assert rc == bot.EXIT_CONFIG_ERROR
                    # Should send alert - debug print
                    for c in mock_post.call_args_list:
                        print("TELEGRAM CALL:", c.kwargs.get("data", {}).get("text", ""))
                    # Check for config error in message
                    calls = [c for c in mock_post.call_args_list 
                             if "[config error]" in c.kwargs.get("data", {}).get("text", "").lower()]
                    assert len(calls) >= 1
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_main_sends_alert_on_check_ok(self, tmp_path):
        """--check OK -> Telegram alert sent."""
        cfg = make_cfg_for_notifier(tmp_path)
        fake = FakeExchange()

        with patch("bot.BinanceClient", return_value=fake):
            with patch("bot.Trader") as mock_trader_class:
                mock_trader = MagicMock()
                mock_trader_class.return_value = mock_trader
                mock_trader.analyze.return_value = (1234567890, Decimal("1.943"), [None, 17.0, 18.5])

                with patch("bot.requests.post") as mock_post:
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_post.return_value = mock_resp

                    with patch("bot.load_config", return_value=cfg):
                        with patch("bot.acquire_lock", return_value=MagicMock()):
                            with patch("bot.setup_logging"):
                                import sys
                                with patch.object(sys, "argv", ["bot.py", "--check"]):
                                    rc = bot.main()
                                    assert rc == 0
                                    # Check call arguments properly
                                    calls = [c for c in mock_post.call_args_list 
                                             if "check ok" in c.kwargs.get("data", {}).get("text", "").lower()]
                                    assert len(calls) >= 1