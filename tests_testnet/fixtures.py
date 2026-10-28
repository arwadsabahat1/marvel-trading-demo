from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from marvel.clock import stamp
from marvel.data.quality import utc_us
from marvel.risk.model import text
from marvel_testnet.model import DemoError

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)


def metadata():
    return {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
        "isSpotTradingAllowed": True, "orderTypes": ["LIMIT", "STOP_LOSS"], "baseCommissionPrecision": 8,
        "quoteCommissionPrecision": 8, "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "1000", "stepSize": "0.00001"},
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "1000000"}]}]}


def commission(rate="0"):
    return {"symbol": "BTCUSDT", **{c: {"maker": rate if c == "standardCommission" else "0",
        "taker": rate if c == "standardCommission" else "0", "buyer": "0", "seller": "0"}
        for c in ("standardCommission", "specialCommission", "taxCommission")},
        "discount": {"enabledForAccount": False, "enabledForSymbol": False, "discountAsset": "BNB", "discount": "0.25"}}


class Exchange:
    """Explicitly invented matching/account fixture. Not an exchange connection."""
    def __init__(self, *, allow_orders=True):
        self.allow_orders = allow_orders
        self.server_ms = utc_us(stamp(NOW)) // 1000
        self.monotonic = lambda: 0
        self.calls, self.receipts, self.orders, self.trades = [], [], {}, {}
        self.btc, self.usdt, self.bnb = Decimal(1), Decimal(10000), Decimal(0)
        self.rate = "0"
        self.uid = 42
        self.lost_entry_ack = self.entry_missing = self.exit_expired = self.entry_expired = False
        self.stop_fill_on_cancel = self.stop_unknown_on_cancel = False
        self.charge_unexpected_bnb = self.charge_unexpected_btc = False
        self.fail_protection = self.reject_test = False
        self.partial_stop_on_cancel = False
        self.partial_stop_one_third = False

    def account(self):
        locked = sum((Decimal(o["origQty"]) - Decimal(o["executedQty"]) for o in self.orders.values()
            if o["type"] == "STOP_LOSS" and o["status"] in {"NEW", "PARTIALLY_FILLED"}), Decimal(0))
        return {"uid": self.uid, "canTrade": True, "accountType": "SPOT", "balances": [
            {"asset": "BTC", "free": text(self.btc - locked), "locked": text(locked)},
            {"asset": "USDT", "free": text(self.usdt), "locked": "0"},
            {"asset": "BNB", "free": text(self.bnb), "locked": "0"}]}

    def fill(self, order, quantity=None):
        quantity = Decimal(order["origQty"]) - Decimal(order["executedQty"]) if quantity is None else quantity
        buy = order["side"] == "BUY"
        price = Decimal("80000" if buy else "79999")
        cost = quantity * price
        fee = quantity * Decimal("0.001") if buy and self.charge_unexpected_btc else Decimal("0.01") if buy and self.charge_unexpected_bnb else Decimal(0)
        asset = "BNB" if self.charge_unexpected_bnb else "BTC" if buy else "USDT"
        self.btc += (quantity if buy else -quantity) - (fee if asset == "BTC" else 0)
        self.usdt += -cost if buy else cost
        if asset == "BNB":
            self.bnb -= fee
        order["executedQty"] = text(Decimal(order["executedQty"]) + quantity)
        order["cummulativeQuoteQty"] = text(Decimal(order["cummulativeQuoteQty"]) + cost)
        order["status"] = "FILLED" if Decimal(order["executedQty"]) == Decimal(order["origQty"]) else "PARTIALLY_FILLED"
        fills = self.trades[order["orderId"]]
        fills.append({"symbol": "BTCUSDT", "id": len(fills) + order["orderId"] * 100, "orderId": order["orderId"],
            "qty": text(quantity), "quoteQty": text(cost), "commission": text(fee), "commissionAsset": asset,
            "isBuyer": buy, "time": self.server_ms})

    def request(self, method, name, params=None):
        values = deepcopy(params or {})
        self.calls.append((method, name, values))
        self.receipts.append({"method": method, "path": "invented-fixture", "status": 200, "response_sha256": "0" * 64, "bytes": 1})
        if name == "time":
            return {"serverTime": self.server_ms}
        if name == "rules":
            return metadata()
        if name == "quote":
            return {"symbol": "BTCUSDT", "bidPrice": "79999", "askPrice": "80000"}
        if name == "account":
            return self.account()
        if name == "fees":
            return commission(self.rate)
        if name == "open":
            return [o for o in self.orders.values() if o["status"] in {"NEW", "PARTIALLY_FILLED"}]
        if name == "test":
            if self.reject_test:
                raise DemoError("TESTNET_TRANSPORT_FAILED", uncertain=True)
            return {}
        if name == "trades":
            return deepcopy(self.trades[values["orderId"]])
        if method == "POST":
            if self.fail_protection and values["type"] == "STOP_LOSS":
                raise DemoError("TESTNET_TRANSPORT_FAILED", uncertain=True)
            identity = values["newClientOrderId"]
            order = {"symbol": "BTCUSDT", "orderId": len(self.orders) + 1, "clientOrderId": identity,
                "origQty": values["quantity"], "executedQty": "0", "cummulativeQuoteQty": "0", "status": "NEW",
                "side": values["side"], "type": values["type"], "price": values.get("price", "0"),
                "stopPrice": values.get("stopPrice", "0"), "timeInForce": values.get("timeInForce", "GTC")}
            self.orders[identity] = order
            self.trades[order["orderId"]] = []
            if values["type"] == "LIMIT":
                if self.entry_expired and values["side"] == "BUY" or self.exit_expired and values["side"] == "SELL":
                    order["status"] = "EXPIRED"
                else:
                    self.fill(order)
            if values["side"] == "BUY" and self.lost_entry_ack:
                raise DemoError("TESTNET_TRANSPORT_FAILED", uncertain=True)
            return deepcopy(order)
        identity = values["origClientOrderId"]
        if self.entry_missing and identity.endswith("_entry") or identity not in self.orders:
            raise DemoError("TESTNET_TRANSPORT_FAILED")
        order = self.orders[identity]
        if method == "DELETE":
            if self.stop_unknown_on_cancel:
                return {}
            if self.stop_fill_on_cancel:
                self.fill(order)
            elif self.partial_stop_on_cancel:
                self.fill(order, Decimal(order["origQty"]) / (3 if self.partial_stop_one_third else 2))
                order["status"] = "CANCELED"
            elif order["status"] != "FILLED":
                order["status"] = "CANCELED"
        return deepcopy(order)
