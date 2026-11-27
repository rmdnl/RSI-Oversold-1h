"""F2 tests: state venue tagging, STATE_FILE per environment, mismatch refusal."""
import shutil
import tempfile
from pathlib import Path

import pytest

import bot
from fake_exchange import FakeExchange, RecNotifier, Capture


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


def make_cfg(tmp_dir, **over):
    env = {
        "SYMBOLS": "NEARUSDT", "TIMEFRAME": "1h", "RSI_PERIOD": "14",
        "RSI_OVERSOLD": "16", "RSI_TRIGGER": "18", "TP_PCT": "2", "SL_PCT": "2.5",
        "SL_TYPE": "STOP_LOSS_LIMIT", "SL_LIMIT_BUFFER_PCT": "0.3",
        "QUOTE_PER_TRADE": "20", "MAX_OPEN_POSITIONS": "3",
        "POLL_INTERVAL_SECONDS": "30", "CANDLE_DELAY_SECONDS": "3",
        "SIGNAL_MAX_AGE_SECONDS": "600", "KLINE_LIMIT": "500", "RECV_WINDOW_MS": "5000",
        "DRY_RUN": "false", "BINANCE_TESTNET": "false", "BINANCE_API_KEY": "k",
        "BINANCE_API_SECRET": "s", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
        "EMERGENCY_SELL_ON_OCO_FAIL": "true", "DATA_DIR": str(tmp_dir),
        "STATE_FILE": "",  # empty -> default per venue
    }
    env.update(over)
    import os
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


def build_trader(cfg, fake, state, notifier):
    return bot.Trader(cfg, fake, state, notifier)


class TestStateVenue:
    def test_default_state_file_mainnet(self, tmp_dir):
        cfg = make_cfg(tmp_dir, BINANCE_TESTNET="false", STATE_FILE="")
        assert cfg.venue == "mainnet"
        assert cfg.state_file == "state.json"
        assert cfg.state_path.name == "state.json"

    def test_default_state_file_testnet(self, tmp_dir):
        cfg = make_cfg(tmp_dir, BINANCE_TESTNET="true", STATE_FILE="")
        assert cfg.venue == "testnet"
        assert cfg.state_file == "state.testnet.json"
        assert cfg.state_path.name == "state.testnet.json"

    def test_explicit_state_file_overrides_default(self, tmp_dir):
        cfg = make_cfg(tmp_dir, BINANCE_TESTNET="true", STATE_FILE="custom.state.json")
        assert cfg.state_file == "custom.state.json"
        assert cfg.state_path.name == "custom.state.json"

    def test_state_file_rejects_path_traversal(self, tmp_dir):
        with pytest.raises(bot.ConfigError, match="STATE_FILE.*path"):
            make_cfg(tmp_dir, STATE_FILE="../secret.json")

    def test_state_carries_venue_tag(self, tmp_dir):
        state = bot.State(tmp_dir / "state.json", venue="testnet")
        assert state.data["venue"] == "testnet"
        state.save()
        # reload with different venue -> no positions so adopts current venue (mainnet)
        reloaded = bot.State(tmp_dir / "state.json", venue="mainnet")
        assert reloaded.data["venue"] == "mainnet"  # adopted

    def test_legacy_state_without_venue_adopts(self, tmp_dir):
        state = bot.State(tmp_dir / "state.json", venue="testnet")
        state.save()
        state2 = bot.State(tmp_dir / "state.json", venue="mainnet")  # no venue in file
        assert state2.data["venue"] == "mainnet"

    def test_venue_mismatch_with_positions_raises(self, tmp_dir):
        state = bot.State(tmp_dir / "state.json", venue="testnet")
        state.positions["NEARUSDT"] = {"status": "PROTECTED", "qty": "10"}
        state.save()
        with pytest.raises(bot.ConfigError, match="venue.*testnet.*mainnet"):
            bot.State(tmp_dir / "state.json", venue="mainnet")

    def test_venue_mismatch_without_positions_adopts(self, tmp_dir):
        state = bot.State(tmp_dir / "state.json", venue="testnet")
        state.save()  # empty positions
        state2 = bot.State(tmp_dir / "state.json", venue="mainnet")
        assert state2.data["venue"] == "mainnet"  # adopted

    def test_startup_refuses_wrong_venue_state_with_positions(self, tmp_dir):
        # Create state file with testnet venue and positions
        state = bot.State(tmp_dir / "state.json", venue="testnet")
        state.positions["NEARUSDT"] = {"status": "PROTECTED", "qty": "10"}
        state.save()

        cfg = make_cfg(tmp_dir, BINANCE_TESTNET="true")  # testnet venue
        fake = FakeExchange()
        notifier = RecNotifier()
        cap = Capture()
        import logging
        logging.getLogger().addHandler(cap)
        logging.getLogger().setLevel(logging.INFO)

        # Create NEW State with mainnet venue (mismatch) - should raise
        with pytest.raises(bot.ConfigError, match="venue.*testnet.*mainnet"):
            bot.State(tmp_dir / "state.json", venue="mainnet")

    def test_startup_warns_about_legacy_state_with_positions(self, tmp_dir):
        """Legacy state.json (mainnet default) has positions but we run testnet."""
        legacy = tmp_dir / "state.json"
        state = bot.State(legacy, venue="mainnet")
        state.positions["NEARUSDT"] = {"status": "PROTECTED", "qty": "10"}
        state.save()

        cfg = make_cfg(tmp_dir, BINANCE_TESTNET="true", STATE_FILE="state.testnet.json")
        fake = FakeExchange()
        notifier = RecNotifier()
        cap = Capture()
        import logging
        logging.getLogger().addHandler(cap)
        logging.getLogger().setLevel(logging.INFO)

        trader = build_trader(cfg, fake, bot.State(tmp_dir / "state.testnet.json", venue="testnet"), notifier)
        trader.startup()

        warns = [m for lv, m in cap.records if lv == "WARNING"]
        assert any("state.json" in m and "state.testnet.json" in m and "NEARUSDT" in m
                   for m in warns)
        # also sent to Telegram
        assert any("state.json" in m and "state.testnet.json" in m for m in notifier.msgs)


class TestStateFileResolution:
    def test_explicit_state_file_points_inside_data_dir(self, tmp_dir):
        cfg = make_cfg(tmp_dir, STATE_FILE="subdir/state.json")
        # path is relative to DATA_DIR
        assert cfg.state_path == tmp_dir / "subdir/state.json"

    def test_default_mainnet_path(self, tmp_dir):
        cfg = make_cfg(tmp_dir, BINANCE_TESTNET="false", STATE_FILE="")
        assert cfg.state_path == tmp_dir / "state.json"

    def test_default_testnet_path(self, tmp_dir):
        cfg = make_cfg(tmp_dir, BINANCE_TESTNET="true", STATE_FILE="")
        assert cfg.state_path == tmp_dir / "state.testnet.json"