#!/usr/bin/env python3
"""Bot trading SPOT Binance berbasis RSI (multi symbol).

Strategi (hanya LONG spot, hanya memakai candle yang SUDAH CLOSE):
  * RSI Wilder (identik dengan ta.rsi TradingView) periode RSI_PERIOD pada TIMEFRAME.
  * Sinyal BUY pada candle close terakhir bila:
      - RSI naik menembus RSI_TRIGGER (RSI sebelumnya < trigger, sekarang >= trigger), dan
      - selama episode di bawah trigger itu RSI pernah <= RSI_OVERSOLD.
  * Entry: MARKET BUY senilai QUOTE_PER_TRADE (quoteOrderQty).
  * Exit : OCO SELL di sisi Binance (TP = LIMIT_MAKER, SL = STOP_LOSS_LIMIT / STOP_LOSS),
           jadi TP/SL tetap aktif walau bot/VPS mati.

Jalankan:  python bot.py          (mode normal)
           python bot.py --check  (validasi konfigurasi + koneksi + tampilkan RSI, tanpa trading)
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

log = logging.getLogger("rsibot")
STOP = threading.Event()

BASE_DIR = Path(__file__).resolve().parent
TP_RANGE = (Decimal("1"), Decimal("3"))  # persen, batas yang diizinkan
SL_RANGE = (Decimal("2"), Decimal("3"))
INTERVAL_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")
EXIT_CONFIG_ERROR = 2
EXIT_LOCKED = 3


# --------------------------------------------------------------------------- #
# Konfigurasi
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    pass


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def _get_dec(name: str, default) -> Decimal:
    raw = _get(name)
    if raw is None:
        return Decimal(str(default))
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ConfigError(f"{name}='{raw}' bukan angka yang valid") from None
    if not value.is_finite():
        raise ConfigError(f"{name}='{raw}' bukan angka yang valid")
    return value


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}='{raw}' harus berupa bilangan bulat") from None


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in ("1", "true", "yes", "y", "on"):
        return True
    if low in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"{name}='{raw}' harus true/false")


@dataclass
class Config:
    api_key: str
    api_secret: str
    testnet: bool
    dry_run: bool
    symbols: list
    interval: str
    rsi_period: int
    rsi_oversold: float
    rsi_trigger: float
    tp_pct: Decimal
    sl_pct: Decimal
    sl_type: str
    sl_limit_buffer_pct: Decimal
    quote_per_trade: Decimal
    max_open_positions: int
    poll_interval: int
    candle_delay: int
    signal_max_age: int
    kline_limit: int
    recv_window: int
    emergency_sell: bool
    data_dir: Path
    tg_token: str
    tg_chat: str
    state_file: str  # raw from env (may be empty)
    state_path: Path  # resolved absolute path

    @property
    def venue(self) -> str:
        """'mainnet' atau 'testnet'."""
        return "testnet" if self.testnet else "mainnet"

    @property
    def interval_ms(self) -> int:
        return INTERVAL_SECONDS[self.interval] * 1000


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")

    dry_run = _get_bool("DRY_RUN", True)
    api_key = _get("BINANCE_API_KEY", "") or ""
    api_secret = _get("BINANCE_API_SECRET", "") or ""
    if not dry_run and (not api_key or not api_secret):
        raise ConfigError("BINANCE_API_KEY dan BINANCE_API_SECRET wajib diisi bila DRY_RUN=false")

    raw_symbols = _get("SYMBOLS")
    if not raw_symbols:
        raise ConfigError("SYMBOLS wajib diisi, contoh: SYMBOLS=BNBUSDT,ETHUSDT")
    symbols: list = []
    for s in raw_symbols.split(","):
        s = s.strip().upper()
        if not s:
            continue
        if not SYMBOL_RE.match(s):
            raise ConfigError(f"Simbol '{s}' tidak valid")
        if s not in symbols:
            symbols.append(s)
    if not symbols:
        raise ConfigError("SYMBOLS kosong")

    interval = (_get("TIMEFRAME", "1h") or "1h").lower()
    if interval not in INTERVAL_SECONDS:
        raise ConfigError(f"TIMEFRAME harus salah satu dari {', '.join(INTERVAL_SECONDS)}")

    rsi_period = _get_int("RSI_PERIOD", 14)
    if rsi_period < 2:
        raise ConfigError("RSI_PERIOD minimal 2")
    oversold = float(_get_dec("RSI_OVERSOLD", 16))
    trigger = float(_get_dec("RSI_TRIGGER", 18))
    if not (0 < oversold < trigger < 100):
        raise ConfigError("Syarat: 0 < RSI_OVERSOLD < RSI_TRIGGER < 100")

    tp_pct = _get_dec("TP_PCT", 2)
    sl_pct = _get_dec("SL_PCT", 2.5)
    if not (TP_RANGE[0] <= tp_pct <= TP_RANGE[1]):
        raise ConfigError(f"TP_PCT harus di antara {TP_RANGE[0]} dan {TP_RANGE[1]} (persen)")
    if not (SL_RANGE[0] <= sl_pct <= SL_RANGE[1]):
        raise ConfigError(f"SL_PCT harus di antara {SL_RANGE[0]} dan {SL_RANGE[1]} (persen)")

    sl_type = (_get("SL_TYPE", "STOP_LOSS_LIMIT") or "").upper()
    if sl_type not in ("STOP_LOSS_LIMIT", "STOP_LOSS"):
        raise ConfigError("SL_TYPE harus STOP_LOSS_LIMIT atau STOP_LOSS")
    buffer_pct = _get_dec("SL_LIMIT_BUFFER_PCT", "0.3")
    if not (Decimal("0") <= buffer_pct <= Decimal("2")):
        raise ConfigError("SL_LIMIT_BUFFER_PCT harus 0 - 2")

    quote = _get_dec("QUOTE_PER_TRADE", 0)
    if quote <= 0:
        raise ConfigError("QUOTE_PER_TRADE wajib diisi (> 0), nilai per entry dalam mata uang quote, mis. USDT")

    max_pos = _get_int("MAX_OPEN_POSITIONS", 3)
    if max_pos < 1:
        raise ConfigError("MAX_OPEN_POSITIONS minimal 1")
    poll = _get_int("POLL_INTERVAL_SECONDS", 30)
    if not (10 <= poll <= 300):
        raise ConfigError("POLL_INTERVAL_SECONDS harus 10 - 300")
    delay = _get_int("CANDLE_DELAY_SECONDS", 3)
    if not (0 <= delay <= 60):
        raise ConfigError("CANDLE_DELAY_SECONDS harus 0 - 60")
    max_age = _get_int("SIGNAL_MAX_AGE_SECONDS", 600)
    if max_age < 60:
        raise ConfigError("SIGNAL_MAX_AGE_SECONDS minimal 60")
    klimit = _get_int("KLINE_LIMIT", 500)
    if not (150 <= klimit <= 1000):
        raise ConfigError("KLINE_LIMIT harus 150 - 1000")
    recv = _get_int("RECV_WINDOW_MS", 5000)
    if not (1000 <= recv <= 60000):
        raise ConfigError("RECV_WINDOW_MS harus 1000 - 60000")

    tg_token = _get("TELEGRAM_BOT_TOKEN", "") or ""
    tg_chat = _get("TELEGRAM_CHAT_ID", "") or ""
    if bool(tg_token) != bool(tg_chat):
        raise ConfigError("TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID harus diisi keduanya (atau kosong keduanya)")

    data_dir = Path(_get("DATA_DIR", str(BASE_DIR / "data")) or "").expanduser()

    # STATE_FILE: explicit path (relative to DATA_DIR) or empty -> default per venue
    raw_state_file = (_get("STATE_FILE", "") or "").strip()
    if raw_state_file:
        # validate no path traversal
        if os.path.isabs(raw_state_file) or ".." in Path(raw_state_file).parts:
            raise ConfigError("STATE_FILE tidak boleh path absolut atau mengandung '..'")
        state_file = raw_state_file
        state_path = (data_dir / raw_state_file).resolve()
    else:
        # default per venue
        state_file = "state.testnet.json" if _get_bool("BINANCE_TESTNET", False) else "state.json"
        state_path = (data_dir / state_file).resolve()

    return Config(
        api_key=api_key, api_secret=api_secret,
        testnet=_get_bool("BINANCE_TESTNET", False), dry_run=dry_run,
        symbols=symbols, interval=interval, rsi_period=rsi_period,
        rsi_oversold=oversold, rsi_trigger=trigger, tp_pct=tp_pct, sl_pct=sl_pct,
        sl_type=sl_type, sl_limit_buffer_pct=buffer_pct, quote_per_trade=quote,
        max_open_positions=max_pos, poll_interval=poll, candle_delay=delay,
        signal_max_age=max_age, kline_limit=klimit, recv_window=recv,
        emergency_sell=_get_bool("EMERGENCY_SELL_ON_OCO_FAIL", True),
        data_dir=data_dir, tg_token=tg_token, tg_chat=tg_chat,
        state_file=state_file, state_path=state_path,
    )


# --------------------------------------------------------------------------- #
# Util angka: RSI, pembulatan sesuai filter Binance
# --------------------------------------------------------------------------- #
def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_wilder(closes: list, period: int) -> list:
    """RSI Wilder (RMA). Hasil sejajar dengan closes; None bila belum cukup data."""
    n = len(closes)
    out: list = [None] * n
    if n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_value(avg_g, avg_l)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def detect_signal(rsi: list, oversold: float, trigger: float) -> tuple:
    """(sinyal, RSI terendah pada episode). rsi[-1] = candle close terbaru."""
    if len(rsi) < 2 or rsi[-1] is None or rsi[-2] is None:
        return False, None
    cur, prev = rsi[-1], rsi[-2]
    if not (prev < trigger <= cur):
        return False, None
    lowest = prev
    i = len(rsi) - 2
    while i >= 0 and rsi[i] is not None and rsi[i] < trigger:
        lowest = min(lowest, rsi[i])
        i -= 1
    return lowest <= oversold, lowest


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_to_tick(value: Decimal, tick: Decimal, rounding) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=rounding) * tick


def redact(text: str) -> str:
    """Sembunyikan signature request dan token bot Telegram dari pesan error sebelum masuk log."""
    text = re.sub(r"signature=[0-9a-fA-F]+", "signature=***", text)
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", text)


def fmt(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


@dataclass
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    quote_precision: int
    oco_allowed: bool


def parse_symbol_info(s: dict) -> SymbolInfo:
    filters = {f["filterType"]: f for f in s.get("filters", [])}
    pf, lot = filters.get("PRICE_FILTER"), filters.get("LOT_SIZE")
    if not pf or not lot:
        raise ConfigError(f"{s['symbol']}: filter PRICE_FILTER/LOT_SIZE tidak ditemukan")
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return SymbolInfo(
        symbol=s["symbol"], base=s["baseAsset"], quote=s["quoteAsset"],
        tick=Decimal(pf["tickSize"]), step=Decimal(lot["stepSize"]),
        min_qty=Decimal(lot["minQty"]), min_notional=Decimal(notional.get("minNotional", "0")),
        quote_precision=int(s.get("quoteAssetPrecision", 8)),
        oco_allowed=bool(s.get("ocoAllowed", True)),
    )


def compute_levels(entry: Decimal, info: SymbolInfo, cfg: Config) -> tuple:
    """(tp, sl_stop, sl_limit) sudah dibulatkan ke tickSize."""
    hundred = Decimal(100)
    tp = round_to_tick(entry * (1 + cfg.tp_pct / hundred), info.tick, ROUND_UP)
    sl_stop = round_to_tick(entry * (1 - cfg.sl_pct / hundred), info.tick, ROUND_DOWN)
    sl_limit = round_to_tick(sl_stop * (1 - cfg.sl_limit_buffer_pct / hundred), info.tick, ROUND_DOWN)
    return tp, sl_stop, sl_limit


def compute_qty_and_notional(entry: Decimal, quote_per_trade: Decimal, info: SymbolInfo, cfg: Config) -> tuple:
    """
    Hitung qty entry (setelah pembulatan stepSize) dan notional di entry & SL limit.
    Return: (qty_entry, notional_entry, notional_sl_limit)
    """
    # qty yang bisa dibeli dengan quote_per_trade (fee sudah dipotong di _place_buy)
    # estimasi: quote_per_trade / entry_price, lalu floor ke stepSize
    raw_qty = quote_per_trade / entry
    qty = floor_to_step(raw_qty, info.step)
    if qty < info.min_qty:
        qty = Decimal(0)
    notional_entry = qty * entry
    # hitung sl_limit untuk notional di SL
    tp, sl_stop, sl_limit = compute_levels(entry, info, cfg)
    notional_sl = qty * sl_limit
    return qty, notional_entry, notional_sl


def check_notional_adequacy(quote_per_trade: Decimal, info: SymbolInfo, cfg: Config, entry: Decimal) -> tuple:
    """
    Validasi kelayakan notional dengan mempertimbangkan stepSize & sl_limit.
    Return: (ok: bool, msg: str, qty: Decimal, notional_entry: Decimal, notional_sl: Decimal)
    """
    qty, notional_entry, notional_sl = compute_qty_and_notional(entry, quote_per_trade, info, cfg)
    min_notional = info.min_notional
    if qty == 0:
        return False, f"qty {qty} < minQty {info.min_qty}", qty, notional_entry, notional_sl
    if notional_entry < min_notional:
        return False, f"notional entry {fmt(notional_entry)} < minNotional {fmt(min_notional)}", qty, notional_entry, notional_sl
    if notional_sl < min_notional:
        return False, f"notional SL limit {fmt(notional_sl)} < minNotional {fmt(min_notional)}", qty, notional_entry, notional_sl
    return True, "", qty, notional_entry, notional_sl


# --------------------------------------------------------------------------- #
# Klien REST Binance (patuh rate limit, sinkronisasi waktu, penanganan error)
# --------------------------------------------------------------------------- #
class NetworkError(Exception):
    pass


class AmbiguousResult(NetworkError):
    """Request tulis (order) gagal di jaringan/5xx: status order TIDAK diketahui."""


class BinanceError(Exception):
    def __init__(self, status: int, code, msg: str):
        super().__init__(f"HTTP {status} code={code} msg={msg}")
        self.status, self.code, self.msg = status, code, msg

    @property
    def transient(self) -> bool:
        return self.status == 429 or self.code in (-1003, -1015)

    @property
    def not_found(self) -> bool:
        return self.code == -2013 or (self.status == 400 and "does not exist" in (self.msg or "").lower())

    @property
    def invalid_symbol(self) -> bool:
        """Binance error -1121: Invalid symbol."""
        return self.code == -1121


class BinanceBanned(BinanceError):
    def __init__(self, status: int, msg: str, retry_after: float):
        super().__init__(status, None, msg)
        self.retry_after = retry_after


class BinanceClient:
    def __init__(self, cfg: Config):
        self.base = "https://testnet.binance.vision" if cfg.testnet else "https://api.binance.com"
        self.secret = cfg.api_secret
        self.recv_window = cfg.recv_window
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": cfg.api_key, "User-Agent": "rsi-spot-bot/1.0"})
        self.time_offset_ms = 0
        self.last_sync = 0.0
        self.weight_limit = 6000
        self.used_weight = 0
        self.weight_minute = 0
        self.banned_until = 0.0
        self.cooldown_until = 0.0

    # ---- waktu ---------------------------------------------------------- #
    def server_now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def sync_time(self) -> None:
        t0 = time.time()
        data = self.request("GET", "/api/v3/time")
        t1 = time.time()
        self.time_offset_ms = int(data["serverTime"]) - int((t0 + t1) / 2 * 1000)
        self.last_sync = time.time()
        log.info("Sinkronisasi waktu server: offset %+d ms", self.time_offset_ms)
        if abs(self.time_offset_ms) > 1000:
            log.warning("Jam VPS melenceng >1 detik dari Binance; aktifkan NTP (chrony/timesyncd).")

    # ---- inti request --------------------------------------------------- #
    def _respect_limits(self) -> None:
        now = time.time()
        if now < self.banned_until:
            raise BinanceBanned(418, "IP sedang diblokir sementara oleh Binance", self.banned_until - now)
        if now < self.cooldown_until:
            log.warning("Menahan request %.0f dtk karena 429 sebelumnya", self.cooldown_until - now)
            STOP.wait(self.cooldown_until - now)
        minute = int(time.time() // 60)
        if minute == self.weight_minute and self.used_weight >= self.weight_limit * 0.8:
            wait = (minute + 1) * 60 - time.time() + 1
            log.warning("Weight terpakai %d/%d per menit, menunggu %.0f dtk", self.used_weight, self.weight_limit, wait)
            STOP.wait(max(wait, 1.0))

    def _track(self, resp: requests.Response) -> None:
        w = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        if w and w.isdigit():
            self.used_weight = int(w)
            self.weight_minute = int(time.time() // 60)

    @staticmethod
    def _retry_after(resp: requests.Response, default: int = 60) -> int:
        v = resp.headers.get("Retry-After", "")
        return int(v) if v.isdigit() else default

    def _build_query(self, params: Optional[dict], signed: bool) -> str:
        q = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            q["timestamp"] = self.server_now_ms()
            q["recvWindow"] = self.recv_window
        query = urlencode(q)
        if signed:
            sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={sig}"
        return query

    def request(self, method: str, path: str, params: Optional[dict] = None, signed: bool = False):
        is_read = method == "GET"
        max_attempts = 3 if is_read else 1
        err_cls = NetworkError if is_read else AmbiguousResult
        attempt = 0
        resynced = False
        while True:
            attempt += 1
            self._respect_limits()
            url = f"{self.base}{path}?{self._build_query(params, signed)}"
            try:
                resp = self.session.request(method, url, timeout=(5, 15))
            except (requests.Timeout, requests.ConnectionError) as exc:
                if is_read and attempt < max_attempts:
                    STOP.wait(2 ** attempt)
                    continue
                raise err_cls(f"{method} {path}: {redact(str(exc))}") from exc

            self._track(resp)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise err_cls(f"{method} {path}: respons bukan JSON") from exc

            try:
                body = resp.json()
            except ValueError:
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            msg = body.get("msg") if isinstance(body, dict) else None
            msg = msg or resp.text[:200]

            if resp.status_code == 418:
                retry = self._retry_after(resp, 300)
                self.banned_until = time.time() + retry
                raise BinanceBanned(418, f"IP di-ban Binance (Retry-After {retry}s)", retry)
            if resp.status_code == 403:
                self.banned_until = time.time() + 300
                raise BinanceBanned(403, "Diblokir WAF Binance (403)", 300)
            if resp.status_code == 429:
                retry = self._retry_after(resp, 60)
                self.cooldown_until = time.time() + retry
                if is_read and attempt < max_attempts:
                    continue
                raise BinanceError(429, code, msg)
            if code == -1021 and not resynced:  # timestamp di luar recvWindow: ditolak sebelum diproses, aman diulang
                resynced = True
                attempt -= 1
                self.sync_time()
                continue
            if resp.status_code >= 500:
                if is_read and attempt < max_attempts:
                    STOP.wait(2 ** attempt)
                    continue
                if is_read:
                    raise BinanceError(resp.status_code, code, msg)
                raise AmbiguousResult(f"{method} {path}: HTTP {resp.status_code} {msg}")
            raise BinanceError(resp.status_code, code, msg)

    # ---- endpoint publik ------------------------------------------------ #
    def exchange_info(self, symbols: list) -> dict:
        data = self.request("GET", "/api/v3/exchangeInfo", {"symbols": json.dumps(symbols, separators=(",", ":"))})
        for rl in data.get("rateLimits", []):
            if rl.get("rateLimitType") == "REQUEST_WEIGHT" and rl.get("interval") == "MINUTE" and rl.get("intervalNum") == 1:
                self.weight_limit = int(rl["limit"])
        return data

    def klines(self, symbol: str, interval: str, limit: int) -> list:
        return self.request("GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def ticker_price(self, symbols: Optional[list] = None) -> list:
        params = {"symbols": symbols} if symbols else {}
        return self.request("GET", "/api/v3/ticker/price", params)

    # ---- endpoint signed ------------------------------------------------ #
    def account(self) -> dict:
        return self.request("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)

    def new_order(self, params: dict) -> dict:
        return self.request("POST", "/api/v3/order", params, signed=True)

    def get_order(self, symbol: str, client_id: Optional[str] = None, order_id=None) -> dict:
        return self.request("GET", "/api/v3/order",
                            {"symbol": symbol, "origClientOrderId": client_id, "orderId": order_id}, signed=True)

    def get_order_list(self, client_id: str) -> dict:
        return self.request("GET", "/api/v3/orderList", {"origClientOrderId": client_id}, signed=True)

    def new_oco(self, params: dict) -> dict:
        return self.request("POST", "/api/v3/orderList/oco", params, signed=True)

    def my_trades(self, symbol: str, order_id) -> list:
        return self.request("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id}, signed=True)


# --------------------------------------------------------------------------- #
# State (JSON atomik), notifikasi, trades.csv
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, path: Path, venue: str):
        self.path = path
        self.venue = venue  # "mainnet" or "testnet" (current bot venue)
        self.data = {"positions": {}, "last_candle": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.data["positions"] = dict(loaded.get("positions", {}))
                self.data["last_candle"] = dict(loaded.get("last_candle", {}))
                # venue tag in file (if missing, treat as legacy mainnet)
                file_venue = loaded.get("venue", "mainnet")
                if file_venue != venue:
                    # if positions exist, this is a hard mismatch -> raise
                    if self.data["positions"]:
                        raise ConfigError(
                            f"State file {path} belongs to venue '{file_venue}' but bot runs on '{venue}' "
                            f"with open positions: {', '.join(self.data['positions'].keys())}. "
                            f"Gunakan STATE_FILE yang benar atau kosongkan state dulu."
                        )
                    # no positions -> adopt current venue (will be saved on next save)
                self.data["venue"] = venue  # always store current venue
            except (ValueError, OSError, AttributeError) as exc:
                raise ConfigError(f"{path} rusak ({exc}). Periksa/rename file itu secara manual; "
                                  "jangan dihapus bila masih ada posisi terbuka.") from exc
        else:
            # new file - store current venue
            self.data["venue"] = venue

    @property
    def positions(self) -> dict:
        return self.data["positions"]

    @property
    def last_candle(self) -> dict:
        return self.data["last_candle"]

    def save(self) -> None:
        self.data["venue"] = self.venue
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)


class Notifier:
    def __init__(self, cfg: Config):
        self.token, self.chat = cfg.tg_token, cfg.tg_chat

    def send(self, text: str) -> None:
        if not self.token:
            return
        try:
            resp = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                 data={"chat_id": self.chat, "text": text[:4000]}, timeout=10)
            if resp.status_code != 200:
                # Log warning with status code and response body (redacted)
                body = redact(resp.text[:200])
                log.warning("Telegram HTTP %d: %s", resp.status_code, body)
                # Could also send to a fallback chat or operator here
        except requests.RequestException as exc:
            log.warning("Telegram gagal: %s", redact(str(exc)))


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def append_trade(path: Path, row: list, notifier: Notifier | None = None, msg: str = "") -> None:
    """Tulis baris ke trades.csv (non-raising). Kirim notifikasi SEBELUM menulis CSV."""
    if notifier and msg:
        try:
            notifier.send(msg)
        except Exception:
            pass  # notifikasi best-effort
    try:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["waktu_utc", "symbol", "alasan", "qty", "harga_entry", "harga_exit", "pnl_quote", "pnl_pct"])
            w.writerow(row)
    except Exception as exc:
        log.warning("Gagal menulis trades.csv: %s", exc)


# --------------------------------------------------------------------------- #
# Trader
# --------------------------------------------------------------------------- #
class Trader:
    def __init__(self, cfg: Config, client: BinanceClient, state: State, notifier: Notifier):
        self.cfg, self.client, self.state, self.notify = cfg, client, state, notifier
        self.infos: dict = {}
        self.trades_csv = cfg.data_dir / "trades.csv"

    # ---- startup -------------------------------------------------------- #
    def get_info(self, symbol: str) -> SymbolInfo:
        if symbol not in self.infos:
            data = self.client.exchange_info([symbol])
            found = [s for s in data.get("symbols", []) if s["symbol"] == symbol]
            if not found:
                raise ConfigError(f"Simbol {symbol} tidak ditemukan di Binance")
            self.infos[symbol] = parse_symbol_info(found[0])
        return self.infos[symbol]

    def startup(self) -> None:
        cfg = self.cfg
        self.client.sync_time()
        try:
            data = self.client.exchange_info(cfg.symbols)
        except BinanceError as exc:
            if exc.invalid_symbol:
                raise ConfigError(f"Simbol tidak valid (Binance -1121): {', '.join(cfg.symbols)}. Periksa ejaan symbol di .env")
            raise
        by_symbol = {s["symbol"]: s for s in data.get("symbols", [])}
        missing = [s for s in cfg.symbols if s not in by_symbol]
        if missing:
            raise ConfigError(f"Simbol tidak ditemukan di Binance: {', '.join(missing)}")

        quotes = set()
        for sym in cfg.symbols:
            s = by_symbol[sym]
            if s.get("status") != "TRADING":
                raise ConfigError(f"{sym}: status {s.get('status')} (bukan TRADING)")
            if not s.get("isSpotTradingAllowed", True):
                raise ConfigError(f"{sym}: spot trading tidak diizinkan")
            if "MARKET" not in s.get("orderTypes", []):
                raise ConfigError(f"{sym}: order MARKET tidak didukung")
            if not s.get("quoteOrderQtyMarketAllowed", True):
                raise ConfigError(f"{sym}: quoteOrderQty pada MARKET tidak didukung")
            info = parse_symbol_info(s)
            if not info.oco_allowed:
                raise ConfigError(f"{sym}: OCO tidak didukung untuk pair ini")

            # Notional check dengan stepSize & sl_limit: estimasi pakai ticker price
            ticker = self.client.ticker_price([sym])
            est_price = Decimal(ticker[0]["price"])
            ok, msg, qty, notional_entry, notional_sl = check_notional_adequacy(
                cfg.quote_per_trade, info, cfg, est_price)
            if not ok:
                raise ConfigError(f"{sym}: QUOTE_PER_TRADE={cfg.quote_per_trade} terlalu kecil; "
                                  f"qty_est={fmt(qty)} notional_entry={fmt(notional_entry)} "
                                  f"notional_SL={fmt(notional_sl)} minNotional={fmt(info.min_notional)} ({msg})")

            self.infos[sym] = info
            quotes.add(info.quote)
        if len(quotes) > 1:
            raise ConfigError(f"Semua simbol harus memakai quote asset yang sama (ditemukan: {', '.join(sorted(quotes))})")

        if not cfg.dry_run or self.state.positions:
            if not cfg.api_key or not cfg.api_secret:
                raise ConfigError("API key diperlukan (DRY_RUN=false atau ada posisi terbuka di state.json)")
            acct = self.client.account()
            if not acct.get("canTrade", False):
                raise ConfigError("Akun/API key tidak punya izin trading (canTrade=false)")
            if "SPOT" not in acct.get("permissions", ["SPOT"]):
                raise ConfigError("Akun tidak punya permission SPOT")
            quote = next(iter(quotes))
            free = self._balance_from(acct, quote)
            log.info("Saldo %s bebas: %s", quote, fmt(free))
            if free < cfg.quote_per_trade:
                log.warning("Saldo %s (%s) < QUOTE_PER_TRADE (%s): entry akan dilewati sampai saldo cukup",
                            quote, fmt(free), fmt(cfg.quote_per_trade))

        log.info("Konfigurasi: mode=%s%s symbols=%s tf=%s RSI(%d) oversold=%s trigger=%s TP=%s%% SL=%s%% (%s) quote/trade=%s max_pos=%d",
                 "DRY-RUN" if cfg.dry_run else "LIVE", " [TESTNET]" if cfg.testnet else "",
                 ",".join(cfg.symbols), cfg.interval, cfg.rsi_period, cfg.rsi_oversold, cfg.rsi_trigger,
                 cfg.tp_pct, cfg.sl_pct, cfg.sl_type, cfg.quote_per_trade, cfg.max_open_positions)

        # Check for legacy state file with positions (e.g., state.json when running testnet with state.testnet.json)
        default_mainnet = cfg.data_dir / "state.json"
        default_testnet = cfg.data_dir / "state.testnet.json"
        legacy_path = default_testnet if cfg.venue == "mainnet" else default_mainnet
        current_path = cfg.state_path
        if legacy_path != current_path and legacy_path.exists():
            try:
                legacy_data = json.loads(legacy_path.read_text(encoding="utf-8"))
                legacy_positions = legacy_data.get("positions", {})
                if legacy_positions:
                    msg = (f"State file lama {legacy_path.name} memiliki posisi terbuka: "
                           f"{', '.join(legacy_positions.keys())}. "
                           f"Bot memakai {current_path.name} ({cfg.venue}). "
                           f"Jika posisi ini masih relevan, pindahkan ke {current_path.name} atau gunakan STATE_FILE yang benar.")
                    log.warning(msg)
                    self.notify.send(msg)
            except Exception:
                pass  # ignore legacy check failures

        if self.state.positions:
            log.info("Posisi dari state.json: %s", ", ".join(f"{k}:{v.get('status')}" for k, v in self.state.positions.items()))

    # ---- saldo ---------------------------------------------------------- #
    @staticmethod
    def _balance_from(acct: dict, asset: str) -> Decimal:
        for b in acct.get("balances", []):
            if b.get("asset") == asset:
                return Decimal(b["free"])
        return Decimal(0)

    def _free_balance(self, asset: str) -> Decimal:
        return self._balance_from(self.client.account(), asset)

    # ---- sinyal --------------------------------------------------------- #
    def latest_closed_open_ms(self, now_ms: int) -> int:
        ivl = self.cfg.interval_ms
        return ((now_ms - self.cfg.candle_delay * 1000) // ivl) * ivl - ivl

    def analyze(self, symbol: str, now_ms: Optional[int] = None):
        """Ambil kline, kembalikan (open_ms_candle_terakhir, close, rsi_list) atau None bila belum siap."""
        now_ms = now_ms or self.client.server_now_ms()
        expected = self.latest_closed_open_ms(now_ms)
        klines = self.client.klines(symbol, self.cfg.interval, self.cfg.kline_limit)
        closed = [k for k in klines if int(k[6]) < now_ms]
        if not closed or int(closed[-1][0]) != expected:
            return None
        closes = [float(k[4]) for k in closed]
        return int(closed[-1][0]), closes[-1], rsi_wilder(closes, self.cfg.rsi_period)

    def process_symbol(self, symbol: str) -> None:
        cfg = self.cfg
        now_ms = self.client.server_now_ms()
        expected = self.latest_closed_open_ms(now_ms)
        if self.state.last_candle.get(symbol, 0) >= expected:
            return
        result = self.analyze(symbol, now_ms)
        if result is None:
            log.debug("%s: candle close terbaru belum tersedia, coba lagi", symbol)
            return
        open_ms, close, rsi = result
        cur, prev = rsi[-1], rsi[-2]
        signal_ok, lowest = detect_signal(rsi, cfg.rsi_oversold, cfg.rsi_trigger)
        close_ts = datetime.fromtimestamp(open_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        log.info("%s candle %s UTC close=%s RSI prev=%s cur=%s%s", symbol, close_ts, close,
                 "n/a" if prev is None else f"{prev:.2f}", "n/a" if cur is None else f"{cur:.2f}",
                 f" -> SINYAL (RSI min episode {lowest:.2f})" if signal_ok else "")

        def mark() -> None:
            self.state.last_candle[symbol] = open_ms
            self.state.save()

        if not signal_ok:
            mark()
            return

        age_s = (now_ms - (open_ms + cfg.interval_ms)) / 1000
        if age_s > cfg.signal_max_age:
            log.warning("%s: sinyal sudah basi (%.0f dtk setelah close), dilewati", symbol, age_s)
            mark()
            return
        if symbol in self.state.positions:
            log.info("%s: sudah ada posisi, sinyal dilewati", symbol)
            mark()
            return
        if len(self.state.positions) >= cfg.max_open_positions:
            log.info("%s: batas MAX_OPEN_POSITIONS (%d) tercapai, sinyal dilewati", symbol, cfg.max_open_positions)
            mark()
            return
        info = self.infos[symbol]
        if cfg.dry_run:
            tp, sl_stop, sl_limit = compute_levels(Decimal(str(close)), info, cfg)
            msg = (f"[DRY-RUN] SINYAL BUY {symbol} @~{close} | RSI {cur:.2f} | beli {fmt(cfg.quote_per_trade)} {info.quote} "
                   f"| TP {fmt(tp)} | SL stop {fmt(sl_stop)}")
            log.info(msg)
            self.notify.send(msg)
            mark()
            return
        # Bila melempar error SEBELUM order dikirim (mis. cek saldo gagal), candle tidak ditandai
        # sehingga sinyal dicoba lagi di siklus berikut selama belum basi.
        self.open_position(symbol, info, cur)
        mark()
        self.advance_position(symbol)  # pasang OCO segera, tanpa menunggu siklus berikutnya

    # ---- entry ---------------------------------------------------------- #
    def open_position(self, symbol: str, info: SymbolInfo, rsi_now: float) -> None:
        cfg = self.cfg
        free = self._free_balance(info.quote)
        if free < cfg.quote_per_trade:
            log.warning("%s: saldo %s %s < %s, entry dilewati", symbol, fmt(free), info.quote, fmt(cfg.quote_per_trade))
            return
        client_id = f"rsi{int(time.time())}{uuid.uuid4().hex[:8]}"
        pos = {"status": "PENDING_BUY", "buy_client_id": client_id, "created_at": time.time(), "rsi": round(rsi_now, 2)}
        self.state.positions[symbol] = pos
        self.state.save()  # tulis SEBELUM kirim order agar aman bila proses mati di tengah jalan

        quote = cfg.quote_per_trade.quantize(Decimal(1).scaleb(-info.quote_precision), rounding=ROUND_DOWN)
        params = {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": fmt(quote),
                  "newClientOrderId": client_id, "newOrderRespType": "FULL"}
        try:
            order = self.client.new_order(params)
        except AmbiguousResult as exc:
            log.warning("%s: status order beli belum pasti (%s); akan diverifikasi", symbol, exc)
            return
        except BinanceBanned:
            self._drop_position(symbol)
            raise
        except BinanceError as exc:
            log.error("%s: order beli ditolak: %s", symbol, exc)
            self.notify.send(f"BUY {symbol} DITOLAK: {exc.msg}")
            self._drop_position(symbol)
            return
        self._apply_buy(symbol, pos, info, order, order.get("fills"))

    def _drop_position(self, symbol: str) -> None:
        self.state.positions.pop(symbol, None)
        self.state.save()

    def _fee_from_trades(self, symbol: str, order_id, asset: str) -> Decimal:
        trades = self.client.my_trades(symbol, order_id)
        return sum((Decimal(t["commission"]) for t in trades if t.get("commissionAsset") == asset), Decimal(0))

    def _apply_buy(self, symbol: str, pos: dict, info: SymbolInfo, order: dict, fills) -> None:
        cfg = self.cfg
        exec_qty = Decimal(order.get("executedQty", "0"))
        quote_spent = Decimal(order.get("cummulativeQuoteQty", "0"))
        if exec_qty <= 0:
            log.warning("%s: order beli tidak terisi (status %s)", symbol, order.get("status"))
            self._drop_position(symbol)
            return
        if fills is not None:
            fee_base = sum((Decimal(f["commission"]) for f in fills if f.get("commissionAsset") == info.base), Decimal(0))
        else:
            fee_base = self._fee_from_trades(symbol, order["orderId"], info.base)
        entry = quote_spent / exec_qty
        net = exec_qty - fee_base
        try:
            net = min(net, self._free_balance(info.base))
        except (BinanceError, NetworkError) as exc:
            if isinstance(exc, BinanceBanned):
                raise
            log.warning("%s: cek saldo base gagal (%s), memakai qty hasil hitung", symbol, exc)
        qty = floor_to_step(net, info.step)
        tp, sl_stop, sl_limit = compute_levels(entry, info, cfg)

        pos.update({
            "status": "UNPROTECTED", "qty": str(qty), "bought_qty": str(exec_qty), "entry_price": str(entry),
            "entry_quote": str(quote_spent), "tp": str(tp), "sl_stop": str(sl_stop), "sl_limit": str(sl_limit),
            "oco_client_id": f"oco{int(time.time())}{uuid.uuid4().hex[:8]}", "oco_attempted": False,
            "oco_failures": 0, "opened_at": utc_now_str(),
        })
        if qty < info.min_qty or qty * sl_limit < info.min_notional:
            pos["stuck"] = True
            self.state.save()
            msg = (f"BUY {symbol} terisi tapi qty {fmt(qty)} terlalu kecil untuk order TP/SL (minQty/minNotional). "
                   "TIDAK terlindungi, tangani manual.")
            log.critical(msg)
            self.notify.send(msg)
            return
        self.state.save()
        msg = (f"BUY {symbol} terisi: qty {fmt(exec_qty)} @ {fmt(entry.quantize(info.tick))} "
               f"({fmt(quote_spent)} {info.quote}) | TP {fmt(tp)} | SL {fmt(sl_stop)}")
        log.info(msg)
        self.notify.send(msg)

    # ---- manajemen posisi ----------------------------------------------- #
    def manage_positions(self) -> None:
        for symbol in list(self.state.positions):
            try:
                self.advance_position(symbol)
            except BinanceBanned:
                raise
            except (BinanceError, NetworkError, ConfigError) as exc:
                log.warning("%s: manajemen posisi gagal sementara: %s", symbol, exc)
            except Exception:
                log.exception("%s: error tak terduga saat mengelola posisi", symbol)

    def advance_position(self, symbol: str) -> None:
        pos = self.state.positions.get(symbol)
        if not pos:
            return
        info = self.get_info(symbol)
        if pos["status"] == "PENDING_BUY":
            self._resolve_pending_buy(symbol, pos, info)
            pos = self.state.positions.get(symbol)
            if not pos:
                return
        if pos.get("stuck"):
            return
        if pos["status"] == "UNPROTECTED":
            self._protect(symbol, pos, info)
        elif pos["status"] == "PROTECTED":
            self._check_exit(symbol, pos, info)

    def _resolve_pending_buy(self, symbol: str, pos: dict, info: SymbolInfo) -> None:
        if time.time() - pos.get("created_at", 0) < 10:
            return
        try:
            order = self.client.get_order(symbol, client_id=pos["buy_client_id"])
        except BinanceError as exc:
            if exc.not_found:
                log.info("%s: order beli %s tidak pernah masuk, posisi dibatalkan", symbol, pos["buy_client_id"])
                self._drop_position(symbol)
                return
            raise
        log.info("%s: order beli terverifikasi (status %s)", symbol, order.get("status"))
        self._apply_buy(symbol, pos, info, order, None)

    def _protect(self, symbol: str, pos: dict, info: SymbolInfo) -> None:
        cfg = self.cfg
        if pos.get("oco_attempted"):  # percobaan sebelumnya mungkin sudah masuk (ambigu) -> cek dulu
            try:
                existing = self.client.get_order_list(pos["oco_client_id"])
                pos.update({"status": "PROTECTED", "oco_list_id": existing.get("orderListId")})
                self.state.save()
                log.info("%s: OCO sudah terpasang di Binance (listId %s)", symbol, existing.get("orderListId"))
                return
            except BinanceError as exc:
                if not exc.not_found:
                    raise

        params = {
            "symbol": symbol, "side": "SELL", "quantity": fmt(Decimal(pos["qty"])),
            "listClientOrderId": pos["oco_client_id"],
            "aboveType": "LIMIT_MAKER", "abovePrice": fmt(Decimal(pos["tp"])),
            "belowType": cfg.sl_type, "belowStopPrice": fmt(Decimal(pos["sl_stop"])),
        }
        if cfg.sl_type == "STOP_LOSS_LIMIT":
            params["belowPrice"] = fmt(Decimal(pos["sl_limit"]))
            params["belowTimeInForce"] = "GTC"
        pos["oco_attempted"] = True
        self.state.save()
        try:
            resp = self.client.new_oco(params)
        except AmbiguousResult as exc:
            log.warning("%s: status OCO belum pasti (%s); diverifikasi di siklus berikut", symbol, exc)
            return
        except BinanceBanned:
            raise
        except BinanceError as exc:
            if exc.transient:
                log.warning("%s: OCO tertunda (rate limit): %s", symbol, exc)
                return
            pos["oco_failures"] = pos.get("oco_failures", 0) + 1
            self.state.save()
            log.error("%s: OCO ditolak (%d): %s", symbol, pos["oco_failures"], exc)
            self.notify.send(f"OCO {symbol} DITOLAK ({pos['oco_failures']}x): {exc.msg}")
            if pos["oco_failures"] >= 2:
                self._emergency_exit(symbol, pos, info)
            return
        pos.update({"status": "PROTECTED", "oco_list_id": resp.get("orderListId")})
        self.state.save()
        msg = f"OCO {symbol} terpasang: TP {pos['tp']} | SL stop {pos['sl_stop']}"
        log.info(msg)
        self.notify.send(msg)

    def _emergency_exit(self, symbol: str, pos: dict, info: SymbolInfo) -> None:
        if not self.cfg.emergency_sell:
            pos["stuck"] = True
            self.state.save()
            self.notify.send(f"{symbol}: posisi TIDAK terlindungi (OCO gagal). Tangani manual.")
            log.critical("%s: OCO gagal berulang, EMERGENCY_SELL nonaktif -> manual", symbol)
            return
        log.critical("%s: OCO gagal berulang, menjual MARKET untuk menutup posisi", symbol)
        qty = Decimal(pos["qty"])
        try:
            order = self.client.new_order({
                "symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": fmt(qty),
                "newClientOrderId": f"emg{int(time.time())}{uuid.uuid4().hex[:8]}",
            })
        except BinanceBanned:
            raise
        except (BinanceError, NetworkError) as exc:
            pos["stuck"] = True
            self.state.save()
            msg = f"{symbol}: market sell darurat GAGAL/tidak pasti ({exc}). CEK MANUAL di Binance."
            log.critical(msg)
            self.notify.send(msg)
            return
        self._record_close(symbol, pos, "EMERGENCY_SELL", order)

    def _check_exit(self, symbol: str, pos: dict, info: SymbolInfo) -> None:
        lst = self.client.get_order_list(pos["oco_client_id"])
        status = lst.get("listOrderStatus")
        if status == "EXECUTING":
            # Check for stop-leg-triggered-but-limit-unfilled scenario
            self._check_stop_triggered_unfilled(symbol, pos, lst)
            return
        if status == "REJECT":
            log.error("%s: OCO berstatus REJECT, mencoba pasang ulang", symbol)
            pos.update({"status": "UNPROTECTED", "oco_attempted": False, "oco_failures": pos.get("oco_failures", 0) + 1})
            pos["oco_client_id"] = f"oco{int(time.time())}{uuid.uuid4().hex[:8]}"
            self.state.save()
            if pos["oco_failures"] >= 2:
                self._emergency_exit(symbol, pos, info)
            return
        if status != "ALL_DONE":
            log.warning("%s: status OCO tak dikenal: %s", symbol, status)
            return
        filled = None
        for leg in lst.get("orders", []):
            o = self.client.get_order(symbol, order_id=leg["orderId"])
            if o.get("status") == "FILLED":
                filled = o
                break
        if filled is None:
            msg = f"{symbol}: OCO selesai tanpa order terisi (dibatalkan manual?). Posisi dihapus dari pelacakan; cek saldo."
            log.warning(msg)
            self.notify.send(msg)
            self._drop_position(symbol)
            return
        reason = "TP" if filled.get("type") in ("LIMIT_MAKER", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT") else "SL"
        self._record_close(symbol, pos, reason, filled)

    def _check_stop_triggered_unfilled(self, symbol: str, pos: dict, lst: dict) -> None:
        """
        Deteksi skenario: stop leg triggered (TP canceled) tapi limit order belum terisi.
        Binance: saat satu leg kena, leg lain dibatalkan. Jika stop kena, TP dibatalkan,
        stop-limit leg jadi order LIMIT biasa menunggu fill. listOrderStatus bisa masih EXECUTING.
        """
        orders = lst.get("orders", [])
        if len(orders) != 2:
            return
        # Fetch detail both legs
        leg_details = []
        for leg in orders:
            try:
                o = self.client.get_order(symbol, order_id=leg["orderId"])
                leg_details.append(o)
            except BinanceError:
                leg_details.append({"status": "UNKNOWN", "orderId": leg["orderId"]})
        # Check if one leg CANCELED (TP) and other NEW/PARTIALLY_FILLED (stop-limit waiting)
        statuses = [o.get("status") for o in leg_details]
        types = [o.get("type") for o in leg_details]
        # Pattern: LIMIT_MAKER (TP) CANCELED + STOP_LOSS_LIMIT (SL) NEW/PARTIALLY_FILLED
        if "CANCELED" in statuses and any(s in ("NEW", "PARTIALLY_FILLED") for s in statuses):
            tp_leg = None
            sl_leg = None
            for o in leg_details:
                if o.get("type") == "LIMIT_MAKER" and o.get("status") == "CANCELED":
                    tp_leg = o
                elif o.get("type") == "STOP_LOSS_LIMIT" and o.get("status") in ("NEW", "PARTIALLY_FILLED"):
                    sl_leg = o
            if tp_leg and sl_leg:
                msg = (f"⚠️ {symbol}: Stop leg TRIGGERED (TP canceled), tapi SL LIMIT belum terisi! "
                       f"SL orderId={sl_leg.get('orderId')} status={sl_leg.get('status')} "
                       f"limitPrice={sl_leg.get('price')} stopPrice={sl_leg.get('stopPrice')}. "
                       f"Posisi unprotected. Operator: cek Binance Open Orders, putar manual atau tunggu fill.")
                log.warning(msg)
                self.notify.send(msg)
                # Mark position for operator attention
                pos["stop_triggered_unfilled"] = True
                pos["sl_order_id"] = sl_leg.get("orderId")
                self.state.save()

    def _record_close(self, symbol: str, pos: dict, reason: str, order: dict) -> None:
        exec_qty = Decimal(order.get("executedQty", "0"))
        proceeds = Decimal(order.get("cummulativeQuoteQty", "0"))
        entry = Decimal(pos["entry_price"])
        exit_price = proceeds / exec_qty if exec_qty > 0 else Decimal(0)
        pnl = proceeds - entry * exec_qty
        pnl_pct = (exit_price / entry - 1) * 100 if entry > 0 else Decimal(0)
        msg = f"{reason} {symbol}: exit {fmt(exit_price)} (entry {fmt(entry)}) PnL ~{pnl:.4f} ({pnl_pct:.2f}%) sebelum fee"
        append_trade(self.trades_csv, [utc_now_str(), symbol, reason, fmt(exec_qty), fmt(entry), fmt(exit_price),
                                      f"{pnl:.4f}", f"{pnl_pct:.2f}"], self.notify, msg)
        log.info(msg)
        self._drop_position(symbol)

    # ---- loop utama ----------------------------------------------------- #
    def run(self) -> None:
        self.notify.send(f"Bot RSI dimulai ({'DRY-RUN' if self.cfg.dry_run else 'LIVE'}): {', '.join(self.cfg.symbols)}")
        while not STOP.is_set():
            try:
                if time.time() - self.client.last_sync > 1800:
                    self.client.sync_time()
                self.manage_positions()
                for symbol in self.cfg.symbols:
                    if STOP.is_set():
                        break
                    try:
                        self.process_symbol(symbol)
                    except BinanceBanned:
                        raise
                    except (BinanceError, NetworkError) as exc:
                        log.warning("%s: gagal memproses sinyal: %s", symbol, exc)
                    except Exception:
                        log.exception("%s: error tak terduga saat memproses sinyal", symbol)
            except BinanceBanned as exc:
                wait = min(max(exc.retry_after, 1) + 5, 3600)
                log.error("%s -> bot menunggu %.0f dtk", exc, wait)
                self.notify.send(f"Binance membatasi akses: {exc.msg}. Bot menunggu {wait:.0f} dtk.")
                STOP.wait(wait)
                continue
            except Exception:
                log.exception("Error tak terduga di loop utama")
            STOP.wait(self.cfg.poll_interval)
        log.info("Bot berhenti. Order OCO yang aktif tetap berjalan di Binance.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def setup_logging(data_dir: Path, to_file: bool = True) -> None:
    fmt_ = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt_)
    root.addHandler(sh)
    if to_file:
        fh = RotatingFileHandler(data_dir / "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt_)
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def acquire_lock(data_dir: Path):
    fh = open(data_dir / "bot.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot RSI spot Binance")
    parser.add_argument("--check", action="store_true", help="validasi konfigurasi/koneksi dan tampilkan RSI, tanpa trading")
    args = parser.parse_args()

    try:
        cfg = load_config()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    except (ConfigError, OSError) as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        # Send Telegram alert for load_config errors too (read token/chat from env)
        from os import getenv
        tg_token = getenv("TELEGRAM_BOT_TOKEN", "") or ""
        tg_chat = getenv("TELEGRAM_CHAT_ID", "") or ""
        if tg_token and tg_chat:
            try:
                requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                              data={"chat_id": tg_chat, "text": f"🚫 [CONFIG ERROR] {exc}"[:4000]}, timeout=10)
            except Exception:
                pass
        return EXIT_CONFIG_ERROR

    # --check hanya membaca (tidak trading, tidak menulis state), jadi aman dijalankan saat service berjalan
    setup_logging(cfg.data_dir, to_file=not args.check)
    lock = None if args.check else acquire_lock(cfg.data_dir)
    if lock is None and not args.check:
        log.error("Instance lain sudah berjalan (bot.lock terkunci). Keluar.")
        return EXIT_LOCKED

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: STOP.set())

    try:
        client = BinanceClient(cfg)
        notifier = Notifier(cfg)
        trader = Trader(cfg, client, State(cfg.state_path, cfg.venue), notifier)
        trader.startup()
        if args.check:
            now_ms = client.server_now_ms()
            for sym in cfg.symbols:
                res = trader.analyze(sym, now_ms)
                if res is None:
                    log.info("%s: candle close terbaru belum tersedia", sym)
                    continue
                _, close, rsi = res
                sig_ok, _ = detect_signal(rsi, cfg.rsi_oversold, cfg.rsi_trigger)
                log.info("%s close=%s RSI=%.2f sinyal=%s", sym, close, rsi[-1], sig_ok)
            log.info("Cek selesai: konfigurasi & koneksi OK.")
            notifier.send("✅ Bot check OK: konfigurasi & koneksi valid.")
            return 0
        trader.run()
        return 0
    except ConfigError as exc:
        log.error("[CONFIG ERROR] %s", exc)
        notifier.send(f"🚫 [CONFIG ERROR] {exc}")
        return EXIT_CONFIG_ERROR
    except BinanceBanned as exc:
        # Jangan langsung exit: systemd akan me-restart dan menembak API lagi selagi di-ban (memperpanjang ban).
        log.error("Diblokir Binance saat startup: %s", exc)
        notifier.send(f"🚫 Binance block saat startup: {exc.msg}. Retry after {exc.retry_after:.0f}s")
        if not args.check:
            STOP.wait(min(exc.retry_after + 5, 3600))
        return 1
    except BinanceError as exc:
        log.error("Gagal saat startup: %s", exc)
        notifier.send(f"🚫 Startup Binance error: {exc}")
        return 1
    except NetworkError as exc:
        log.error("Gagal terhubung saat startup: %s", exc)
        notifier.send(f"🚫 Startup network error: {exc}")
        return 1
    except Exception as exc:
        log.exception("Error tak terduga saat startup")
        notifier.send(f"🚫 Startup unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
