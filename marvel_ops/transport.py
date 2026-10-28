"""Explicit fixed-host Spot connector; no live mutation without ledger authorization."""

from hashlib import sha256
import hmac
import http.client
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler, HTTPRedirectHandler

from marvel.data.quality import strict_json
from marvel.errors import MarvelError
from .common import Stop, number, require

HOSTS = {"testnet": "https://testnet.binance.vision", "live": "https://api.binance.com"}
PATHS = {"time": "/api/v3/time", "rules": "/api/v3/exchangeInfo", "quote": "/api/v3/ticker/bookTicker",
         "bars": "/api/v3/klines", "account": "/api/v3/account", "fees": "/api/v3/account/commission",
         "open": "/api/v3/openOrders", "order": "/api/v3/order", "trades": "/api/v3/myTrades"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Spot:
    def __init__(self, environment, *, allow_network=False, acknowledge_orders=False,
                 key=None, secret=None, authorize=None, opener=None, monotonic=time.monotonic):
        require(environment in HOSTS and allow_network is True, "OPS_NETWORK_OPT_IN_REQUIRED")
        require(type(acknowledge_orders) is bool)
        self.environment, self.host = environment, HOSTS[environment]
        self.key, self.secret, self.authorize = key, secret, authorize
        self.orders, self.clock = acknowledge_orders, monotonic
        self.provenance = "synthetic" if opener is not None else "authenticated_network" if key and secret else "public_network"
        self.opener = opener or build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        self.server_ms = self.anchor = None
        self.receipts, self.attempts = [], 0

    def request(self, method, name, params=None):
        require(name in PATHS and self.attempts < 100, "OPS_ENDPOINT_REJECTED")
        values = dict(params or {})
        require((method == "GET") or (name == "order" and method in {"POST", "DELETE"}), "OPS_ENDPOINT_REJECTED")
        public = name in {"time", "rules", "quote", "bars"}
        if name == "time":
            require(not values)
        elif name in {"rules", "quote", "fees"}:
            require(values == {"symbol": "BTCUSDT"})
        elif name == "bars":
            require(values == {"symbol": "BTCUSDT", "interval": "1h", "limit": 600})
        elif name in {"account", "open"}:
            require(not values)
        elif name == "trades":
            require(set(values) == {"symbol", "orderId", "limit"} and values["symbol"] == "BTCUSDT"
                    and type(values["orderId"]) is int and values["orderId"] >= 0 and values["limit"] == 1000)
        elif method in {"GET", "DELETE"}:
            require(set(values) == {"symbol", "origClientOrderId"} and values["symbol"] == "BTCUSDT")
            self.identifier(values["origClientOrderId"])
        else:
            expected = {"symbol", "side", "type", "quantity", "newClientOrderId", "newOrderRespType"}
            expected |= {"price", "timeInForce"} if values.get("type") == "LIMIT" else {"stopPrice"}
            require(set(values) == expected and values["symbol"] == "BTCUSDT" and values["newOrderRespType"] == "FULL")
            self.identifier(values["newClientOrderId"])
            quantity = number(values["quantity"], positive=True)
            price = number(values.get("price", values.get("stopPrice")), positive=True)
            require(quantity * price * number("1.005") <= 10, "OPS_NOTIONAL_CEILING")
            require((values["type"] == "LIMIT" and values["side"] in {"BUY", "SELL"} and values["timeInForce"] == "FOK")
                    or (values["type"] == "STOP_LOSS" and values["side"] == "SELL"), "OPS_ENDPOINT_REJECTED")
        if method != "GET":
            require(self.orders and callable(self.authorize), "OPS_ORDER_OPT_IN_REQUIRED")
            self.authorize(method, values)
        headers = {"Accept": "application/json", "User-Agent": "Marvel-Operations/0.1"}
        if not public:
            require(all(type(v) is str and re.fullmatch(r"[A-Za-z0-9]{32,128}", v) for v in (self.key, self.secret)), "OPS_CREDENTIALS_MISSING")
            elapsed = self.clock() - self.anchor if self.anchor is not None else -1
            require(0 <= elapsed < 300, "OPS_SIGNING_CLOCK_INVALID")
            values.update(timestamp=self.server_ms + int(elapsed * 1000), recvWindow=5000)
            unsigned = urlencode(values)
            values["signature"] = hmac.new(self.secret.encode(), unsigned.encode(), "sha256").hexdigest()
            headers["X-MBX-APIKEY"] = self.key
        url, payload = self.host + PATHS[name], None
        encoded = urlencode(values)
        if method == "GET" and encoded:
            url += "?" + encoded
        elif encoded:
            payload = encoded.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.attempts += 1
        try:
            with self.opener.open(Request(url, data=payload, headers=headers, method=method), timeout=10) as response:
                require(response.status == 200 and response.geturl() == url, "OPS_RESPONSE_INVALID")
                raw = response.read(1_000_001)
            require(0 < len(raw) <= 1_000_000, "OPS_RESPONSE_INVALID")
            document = strict_json(raw)
            self.receipts.append({"method": method, "path": PATHS[name], "sha256": sha256(raw).hexdigest(), "bytes": len(raw)})
            if name == "time":
                require(type(document["serverTime"]) is int)
                self.server_ms, self.anchor = document["serverTime"], self.clock()
            return document
        except (Stop, MarvelError, HTTPError, URLError, OSError, http.client.HTTPException, ValueError, KeyError):
            raise Stop("OPS_TRANSPORT_FAILED", uncertain=method != "GET") from None

    @staticmethod
    def identifier(value):
        require(type(value) is str and re.fullmatch(r"mvo_[a-f0-9]{20}_(?:entry|stop|exit|rearm)", value) is not None,
                "OPS_ORDER_ID_INVALID")
