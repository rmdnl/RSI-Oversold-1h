"""Shared test setup: make bot.py importable and give non-POSIX hosts an fcntl stand-in."""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # bot.py imports fcntl at module level (POSIX-only)
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - exercised on Windows
    import msvcrt

    stub = types.ModuleType("fcntl")
    setattr(stub, "LOCK_SH", 1)
    setattr(stub, "LOCK_EX", 2)
    setattr(stub, "LOCK_NB", 4)
    setattr(stub, "LOCK_UN", 8)

    def _flock(target, op):
        fd = target if isinstance(target, int) else target.fileno()
        if op & 8:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return
        msvcrt.locking(fd, msvcrt.LK_NBLCK if op & 4 else msvcrt.LK_LOCK, 1)

    setattr(stub, "flock", _flock)
    sys.modules["fcntl"] = stub

import bot  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    """A live-mode Config without touching the environment or the real .env file."""
    return bot.Config(
        api_key="key", api_secret="secret", testnet=False, dry_run=False,
        symbols=["NEARUSDT"], interval="1h", rsi_period=14, rsi_oversold=16.0,
        rsi_trigger=18.0, tp_pct=bot.Decimal("2"), sl_pct=bot.Decimal("2.5"),
        sl_type="STOP_LOSS_LIMIT", sl_limit_buffer_pct=bot.Decimal("0.3"),
        quote_per_trade=bot.Decimal("20"), max_open_positions=3, poll_interval=30,
        candle_delay=3, signal_max_age=600, kline_limit=500, recv_window=5000,
        emergency_sell=True, data_dir=tmp_path, tg_token="", tg_chat="",
    )