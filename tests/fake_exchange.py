"""Test double: a scriptable fake Binance Spot exchange for end-to-end tests.

Usage:
    fake = FakeExchange(symbol="NEARUSDT", base="NEAR", quote="USDT", price="1.943")
    tr = Trader(cfg, fake, state, notifier)

Every method returns a dict/list and records the call in fake.calls for assertions.
"""
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import bot  # importable via conftest sys.path insert

# --- helpers ---------------------------------------------------------------- #


def make_signal_closes(entry: float = 1.943, period: int = 14, oversold: float = 16.0,
                       trigger: float = 18.0):
    """Generate a list of closes that produces exactly one BUY signal at the end."""
    closes = [entry] * 30
    for _ in range(26):
        closes.append(closes[-1] * 0.993)
    for _ in range(60):
        closes.append(closes[-1] * 1.0035)
        rsi = bot.rsi_wilder(closes, period)
        if len(rsi) > 2 and rsi[-2] is not None and rsi[-1] is not None:
            ok, _ = bot.detect_signal(bot.rsi_wilder(closes, period), oversold, trigger)
            if ok:
                return closes, rsi
    raise AssertionError("could not engineer a signal")


CLOSES, SIGNAL_RSI = make_signal_closes()
LAST_OPEN = 1_795_784_400_000  # a real 1h boundary (UTC)
IVL = 3_600_000


# --- the fake --------------------------------------------------------------- #

class FakeExchange:
    def __init__(self, symbol: str = "NEARUSDT", base: str = "NEAR", quote: str = "USDT",
                 price: str = "1.943", filters: Optional[list] = None):
        self.symbol = symbol
        self.base, self.quote = base, quote
        self.price = Decimal(price)
        self.filters = filters or [
            {"filterType": "PRICE_FILTER", "minPrice": "0.001", "maxPrice": "1000",
             "tickSize": "0.001"},
            {"filterType": "LOT_SIZE", "minQty": "0.1", "maxQty": "90000", "stepSize": "0.1"},
            {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
        ]
        self.closes = [float(c) for c in CLOSES]
        self.last_open = LAST_OPEN
        self.calls: list = []
        self.quote_free = Decimal("1000")
        self.base_free = Decimal("0")
        self.oco_place = "ok"
        self.oco_error = None
        self.list_state = "EXECUTING"
        self.list_error = None
        self.list_exists = False
        self.orders: dict = {}
        self.next_oid = 9000
        self.fee_rate = Decimal("0.001")  # 0.1% paid in base
        self._ticker_price = self.price

    # ---------- clock ----------
    def sync_time(self) -> None:
        self.calls.append(("sync_time",))

    def server_now_ms(self) -> int:
        return self.last_open + IVL + 5000

    # ---------- market data ----------
    def exchange_info(self, symbols: list) -> dict:
        self.calls.append(("exchange_info", tuple(symbols)))
        return {"symbols": [{
            "symbol": f"{self.base}{self.quote}", "baseAsset": self.base,
            "quoteAsset": self.quote, "quoteAssetPrecision": 8, "status": "TRADING",
            "isSpotTradingAllowed": True, "ocoAllowed": True,
            "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT",
                           "LIMIT_MAKER", "TAKE_PROFIT_LIMIT"],
            "quoteOrderQtyMarketAllowed": True, "filters": self.filters,
        } for _ in symbols]}

    def ticker_price(self, symbols: Optional[list] = None) -> list:
        self.calls.append(("ticker_price", symbols))
        if symbols:
            return [{"symbol": s, "price": str(self._ticker_price)} for s in symbols]
        return [{"symbol": self.symbol, "price": str(self._ticker_price)}]

    def klines(self, symbol: str, interval: str, limit: int) -> list:
        self.calls.append(("klines", symbol, interval, limit))
        n = len(self.closes)
        rows = []
        for i, c in enumerate(self.closes):
            o = self.last_open - (n - 1 - i) * IVL
            rows.append([o, str(c), str(c), str(c), str(c), "1.0", o + IVL - 1, "0"])
        # forming candle with a wild price so any look-ahead fails visibly
        rows.append([self.last_open + IVL, "999", "999", "999", "999", "1",
                     self.last_open + 2 * IVL - 1, "0"])
        return rows

    # ---------- account ----------
    def account(self) -> dict:
        bal = [{"asset": self.quote, "free": str(self.quote_free), "locked": "0"}]
        if self.base_free > 0:
            bal.append({"asset": self.base, "free": str(self.base_free), "locked": "0"})
        return {"canTrade": True, "permissions": ["SPOT"], "balances": bal}

    # ---------- orders ----------
    def new_order(self, params: dict) -> dict:
        self.calls.append(("new_order", dict(params)))
        side = params["side"]
        if side == "BUY":
            qty = (Decimal(params["quoteOrderQty"]) / self.price).quantize(
                Decimal("0.00000001"))
            fee = qty * self.fee_rate
            self.base_free += qty - fee
            self.quote_free -= Decimal(params["quoteOrderQty"])
            oid = self.next_oid
            self.next_oid += 1
            self.orders[oid] = {
                "orderId": oid, "clientOrderId": params["newClientOrderId"],
                "status": "FILLED", "type": "MARKET", "side": "BUY",
                "executedQty": str(qty), "cummulativeQuoteQty": params["quoteOrderQty"],
                "fills": [{"commission": str(fee), "commissionAsset": self.base}],
            }
            return self.orders[oid]
        # market SELL
        qty = Decimal(params["quantity"])
        self.base_free -= qty
        oid = self.next_oid
        self.next_oid += 1
        self.orders[oid] = {
            "orderId": oid, "clientOrderId": params["newClientOrderId"],
            "status": "FILLED", "type": "MARKET", "side": "SELL",
            "executedQty": str(qty),
            "cummulativeQuoteQty": str(qty * self.price),
        }
        return self.orders[oid]

    def new_oco(self, params: dict) -> dict:
        self.calls.append(("new_oco", dict(params)))
        if self.oco_error is not None:
            raise self.oco_error
        self.list_exists = True
        self.list_state = "EXECUTING"
        return {"orderListId": 777, "listClientOrderId": params["listClientOrderId"],
                "orders": [{"orderId": 5551}, {"orderId": 5552}]}

    def cancel_order_list(self, client_id: str) -> dict:
        self.calls.append(("cancel_order_list", client_id))
        if not self.list_exists:
            raise bot.BinanceError(400, -2013, "Order list does not exist.")
        self.list_exists = False
        return {"orderListId": 777, "listClientOrderId": client_id}

    def get_order_list(self, client_id: str) -> dict:
        self.calls.append(("get_order_list", client_id))
        if self.list_error is not None:
            raise self.list_error
        if not self.list_exists:
            raise bot.BinanceError(400, -2013, "Order list does not exist.")
        return {"orderListId": 777, "listClientOrderId": client_id,
                "listOrderStatus": self.list_state,
                "orders": [{"orderId": 5551}, {"orderId": 5552}]}

    def get_order(self, symbol: str, client_id: Optional[str] = None,
                  order_id: Optional[int] = None) -> dict:
        self.calls.append(("get_order", client_id, order_id))
        if client_id is not None:
            for o in self.orders.values():
                if o["clientOrderId"] == client_id:
                    return o
            raise bot.BinanceError(400, -2013, "Order does not exist.")
        if order_id is not None and order_id in self.orders:
            return self.orders[order_id]
        raise bot.BinanceError(400, -2013, "Order does not exist.")

    def my_trades(self, symbol: str, order_id) -> list:
        return []

    # ---------- seed helpers ----------
    def seed_tp_fill(self, qty: Decimal, price: Decimal) -> None:
        self.list_exists = True
        self.list_state = "ALL_DONE"
        self.orders[5551] = {"orderId": 5551, "status": "FILLED", "type": "LIMIT_MAKER",
                             "side": "SELL", "executedQty": str(qty),
                             "cummulativeQuoteQty": str(qty * price)}
        self.orders[5552] = {"orderId": 5552, "status": "CANCELED",
                             "type": "STOP_LOSS_LIMIT", "side": "SELL",
                             "executedQty": "0", "cummulativeQuoteQty": "0"}
        self.base_free -= qty
        self.quote_free += qty * price

    def seed_sl_fill(self, qty: Decimal, price: Decimal) -> None:
        self.list_exists = True
        self.list_state = "ALL_DONE"
        self.orders[5552] = {"orderId": 5552, "status": "FILLED",
                             "type": "STOP_LOSS_LIMIT", "side": "SELL",
                             "executedQty": str(qty), "cummulativeQuoteQty": str(qty * price)}
        self.orders[5551] = {"orderId": 5551, "status": "CANCELED",
                             "type": "LIMIT_MAKER", "side": "SELL",
                             "executedQty": "0", "cummulativeQuoteQty": "0"}
        self.base_free -= qty
        self.quote_free += qty * price

    def seed_stop_triggered(self, qty: Decimal) -> None:
        """Stop limit triggered: TP canceled, stop-limit leg still NEW (resting unfilled)."""
        self.list_exists = True
        self.list_state = "EXECUTING"
        self.orders[5551] = {"orderId": 5551, "status": "CANCELED",
                             "type": "LIMIT_MAKER", "side": "SELL",
                             "executedQty": "0", "cummulativeQuoteQty": "0"}
        self.orders[5552] = {"orderId": 5552, "status": "NEW",
                             "type": "STOP_LOSS_LIMIT", "side": "SELL",
                             "executedQty": "0", "cummulativeQuoteQty": "0"}

    # ---------- assertions ----------
    def calls_of(self, name: str) -> list:
        return [c for c in self.calls if c[0] == name]

    def reset_calls(self) -> None:
        self.calls.clear()


# --- notifier & log capture (re-exported for tests) ------------------------ #

class RecNotifier:
    def __init__(self):
        self.msgs: list = []

    def send(self, text: str) -> None:
        self.msgs.append(text)


import logging


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def clear(self):
        self.records.clear()