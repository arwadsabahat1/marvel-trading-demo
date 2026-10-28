"""Fixed Testnet host only; no production host, redirects, retries or raw private receipts."""

from hashlib import sha256
from decimal import Decimal, localcontext
import hmac
import http.client
import os
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from marvel.data.quality import strict_json
from marvel.errors import MarvelError
from .model import HOST, DemoError, fail, integer, number

VARIABLES = ("MARVEL_TESTNET_API_KEY", "MARVEL_TESTNET_API_SECRET")
MAX_BYTES = 262144


def credentials(environ=None):
    environment = os.environ if environ is None else environ
    values = tuple(environment.get(name) for name in VARIABLES)
    if any(value is None or value == "" for value in values):
        fail("TESTNET_CREDENTIALS_MISSING")
    if any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9]{32,128}", value) is None for value in values):
        fail("TESTNET_CREDENTIALS_INVALID")
    return values


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, *, allow_network=False, allow_orders=False, key=None, secret=None, opener=None, monotonic=time.monotonic):
        if allow_network is not True:
            fail("TESTNET_NETWORK_OPT_IN_REQUIRED")
        if type(allow_orders) is not bool:
            fail("TESTNET_ARGUMENTS_INVALID")
        self.allow_orders = allow_orders
        self.key, self.secret, self.monotonic = key, secret, monotonic
        self.opener = opener or build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        self.server_ms = self.anchor = None
        self.receipts, self.attempts = [], 0

    def request(self, method, name, params=None):
        paths = {"time": "/api/v3/time", "rules": "/api/v3/exchangeInfo", "quote": "/api/v3/ticker/bookTicker",
            "account": "/api/v3/account", "fees": "/api/v3/account/commission", "open": "/api/v3/openOrders",
            "order": "/api/v3/order", "test": "/api/v3/order/test", "trades": "/api/v3/myTrades"}
        allowed = {("GET", n) for n in paths if n != "test"} | {("POST", "order"), ("POST", "test"), ("DELETE", "order")}
        if (method, name) not in allowed or self.attempts >= 64:
            fail("TESTNET_ENDPOINT_REJECTED")
        values = dict(params or {})
        public = name in {"time", "rules", "quote"}
        if public:
            if values != ({} if name == "time" else {"symbol": "BTCUSDT"}):
                fail("TESTNET_ENDPOINT_REJECTED")
        elif name in {"account", "open"}:
            if values:
                fail("TESTNET_ENDPOINT_REJECTED")
        elif name == "fees":
            if values != {"symbol": "BTCUSDT"}:
                fail("TESTNET_ENDPOINT_REJECTED")
        elif name == "trades":
            if set(values) != {"symbol", "orderId", "limit"} or values["symbol"] != "BTCUSDT" or values["limit"] != 1000:
                fail("TESTNET_ENDPOINT_REJECTED")
            integer(values["orderId"])
        elif method in {"GET", "DELETE"}:
            if set(values) != {"symbol", "origClientOrderId"} or values["symbol"] != "BTCUSDT":
                fail("TESTNET_ENDPOINT_REJECTED")
            self.identifier(values["origClientOrderId"])
        else:
            if not self.allow_orders:
                fail("TESTNET_VIRTUAL_ORDER_OPT_IN_REQUIRED")
            expected = {"symbol", "side", "type", "quantity", "newClientOrderId", "newOrderRespType"}
            expected |= {"price", "timeInForce"} if values.get("type") == "LIMIT" else {"stopPrice"}
            if set(values) != expected or values["symbol"] != "BTCUSDT" or values["newOrderRespType"] != "FULL":
                fail("TESTNET_ENDPOINT_REJECTED")
            self.identifier(values["newClientOrderId"])
            number(values["quantity"], positive=True)
            if values["type"] == "LIMIT":
                if values["side"] not in {"BUY", "SELL"} or values["timeInForce"] != "FOK":
                    fail("TESTNET_ENDPOINT_REJECTED")
                number(values["price"], positive=True)
            elif values["type"] != "STOP_LOSS" or values["side"] != "SELL":
                fail("TESTNET_ENDPOINT_REJECTED")
            else:
                number(values["stopPrice"], positive=True)
            with localcontext() as context:
                context.prec = 256
                if number(values["quantity"]) * number(values.get("price", values.get("stopPrice"))) * Decimal("1.005") > 10:
                    fail("TESTNET_ORDER_NOTIONAL_CEILING")
        if method == "DELETE" and not self.allow_orders:
            fail("TESTNET_VIRTUAL_ORDER_OPT_IN_REQUIRED")
        headers = {"Accept": "application/json", "User-Agent": "Marvel-isolated-Spot-Testnet/0.1"}
        if not public:
            if not self.key or not self.secret:
                fail("TESTNET_CREDENTIALS_MISSING")
            elapsed = self.monotonic() - self.anchor if self.anchor is not None else -1
            if not 0 <= elapsed <= 300:
                fail("TESTNET_SIGNING_CLOCK_INVALID")
            values.update(timestamp=self.server_ms + int(elapsed * 1000), recvWindow=5000)
            unsigned = urlencode(values)
            values["signature"] = hmac.new(self.secret.encode("ascii"), unsigned.encode("ascii"), "sha256").hexdigest()
            headers["X-MBX-APIKEY"] = self.key
        encoded = urlencode(values)
        url = HOST + paths[name]
        payload = None
        if method == "GET" and encoded:
            url += "?" + encoded
        elif encoded:
            payload = encoded.encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=payload, headers=headers, method=method)
        self.attempts += 1
        try:
            with self.opener.open(request, timeout=10) as response:
                if response.status != 200 or response.geturl() != url:
                    fail("TESTNET_TRANSPORT_FAILED", uncertain=method != "GET")
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES or not raw:
                fail("TESTNET_RESPONSE_INVALID", uncertain=method != "GET")
            document = strict_json(raw)
            self.receipts.append({"method": method, "path": paths[name], "status": 200,
                "response_sha256": sha256(raw).hexdigest(), "bytes": len(raw)})
            if name == "time":
                self.server_ms, self.anchor = integer(document["serverTime"]), self.monotonic()
            return document
        except DemoError:
            raise
        except (HTTPError, URLError, OSError, http.client.HTTPException, MarvelError, KeyError, TypeError, ValueError):
            # Even a received rejection is conservatively reconciled after an order attempt. Never echo its body/URL.
            fail("TESTNET_TRANSPORT_FAILED", uncertain=method != "GET")

    @staticmethod
    def identifier(value):
        if type(value) is not str or re.fullmatch(r"mtd_[a-f0-9]{20}_(?:entry|stop|exit|rearm|recovery)", value) is None:
            fail("TESTNET_ENDPOINT_REJECTED")
