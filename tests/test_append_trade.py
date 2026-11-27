"""F3 tests: append_trade non-raising, notification before CSV write."""
import tempfile
from pathlib import Path

import pytest

import bot
from fake_exchange import RecNotifier


def test_append_trade_non_raising_on_permission_error(tmp_path, monkeypatch):
    """append_trade tidak boleh raise meski filesystem error."""
    csv_path = tmp_path / "trades.csv"
    # buat file read-only (Windows: tidak bisa write, Unix: permission denied)
    csv_path.write_text("waktu_utc,symbol,alasan,qty,harga_entry,harga_exit,pnl_quote,pnl_pct\n")
    csv_path.chmod(0o444)  # read-only

    try:
        notifier = RecNotifier()
        bot.append_trade(csv_path, ["2024-01-01", "TEST", "TP", "1", "100", "101", "1", "1%"], notifier, "test msg")
        # tidak raise
        # notifikasi tetap dikirim
        assert "test msg" in notifier.msgs
    finally:
        csv_path.chmod(0o644)  # restore for cleanup


def test_append_trade_notification_before_csv(tmp_path):
    """Notifikasi dikirim SEBELUM menulis CSV (best-effort)."""
    csv_path = tmp_path / "trades.csv"
    notifier = RecNotifier()

    bot.append_trade(csv_path, ["2024-01-01", "TEST", "TP", "1", "100", "101", "1", "1%"], notifier, "NOTIF DULU")

    # notifikasi harus terkirim
    assert "NOTIF DULU" in notifier.msgs
    # CSV harus tertulis
    assert csv_path.exists()
    lines = csv_path.read_text().strip().split("\n")
    assert len(lines) == 2  # header + data
    assert "TEST" in lines[1]


def test_append_trade_creates_header_on_first_write(tmp_path):
    """File baru: header ditulis otomatis."""
    csv_path = tmp_path / "trades.csv"
    notifier = RecNotifier()

    bot.append_trade(csv_path, ["2024-01-01", "TEST", "TP", "1", "100", "101", "1", "1%"], notifier, "")

    lines = csv_path.read_text().strip().split("\n")
    assert lines[0] == "waktu_utc,symbol,alasan,qty,harga_entry,harga_exit,pnl_quote,pnl_pct"
    assert "TEST" in lines[1]


def test_append_trade_appends_to_existing(tmp_path):
    """File existing: tidak tulis header lagi."""
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text("waktu_utc,symbol,alasan,qty,harga_entry,harga_exit,pnl_quote,pnl_pct\n")
    csv_path.write_text(csv_path.read_text() + "2024-01-01,OLD,TP,1,100,101,1,1%\n")
    notifier = RecNotifier()

    bot.append_trade(csv_path, ["2024-01-02", "NEW", "SL", "2", "200", "198", "-4", "-2%"], notifier, "")

    lines = csv_path.read_text().strip().split("\n")
    assert len(lines) == 3  # header + old + new
    assert "OLD" in lines[1]
    assert "NEW" in lines[2]