"""Opt-in, GET-only Binance research observations; never an execution adapter.

Only two purpose-named process variables are read, after explicit network opt-in.
No credential files, browser sessions, balances, orders or trade history are read.
"""

from decimal import Decimal, localcontext
from hashlib import sha256
import hmac
import http.client
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..clock import stamp, utc_now
from ..errors import ResearchError
from ..risk.model import PRECISION, canonical, text
from .model import fail
from .store import decode_record

MAX_BYTES = 262144
ORIGIN = "https://api.binance.com"
ENDPOINTS = {"time": "/api/v3/time", "permissions": "/sapi/v1/account/apiRestrictions",
             "commissions": "/api/v3/account/commission", "precision": "/sapi/v1/convert/assetInfo"}
VARIABLES = ("MARVEL_BINANCE_READ_API_KEY", "MARVEL_BINANCE_READ_API_SECRET")
MUTATIONS = {"enableWithdrawals", "enableInternalTransfer", "enableMargin", "enableFutures",
             "permitsUniversalTransfer", "enableVanillaOptions", "enableFixApiTrade",
             "enableSpotAndMarginTrading", "enablePortfolioMarginTrading"}
PERMISSION_FLAGS = MUTATIONS | {"enableReading", "enableFixReadOnly", "ipRestrict"}
BLOCKERS = {"NETWORK_OPT_IN_REQUIRED", "READ_ONLY_CREDENTIALS_MISSING", "READ_ONLY_CREDENTIALS_INVALID",
            "API_PERMISSION_SCOPE_REJECTED", "ACCOUNT_READ_FAILED", "ACCOUNT_RESPONSE_INVALID"}
FAILURE_REASONS = {"SCHEMA_INVALID", "INVALID_ASSET_LIST", "INVALID_ASSET_ROW", "INVALID_REQUIRED_ASSET",
                   "INVALID_REQUIRED_FRACTION", "MISSING_REQUIRED_ASSETS", "DUPLICATE_REQUIRED_ASSET",
                   "INVALID_JSON", "RESPONSE_TOO_LARGE", "UNEXPECTED_HTTP_STATUS", "UNEXPECTED_RESPONSE_URL",
                   "UNEXPECTED_CONTENT_TYPE", "PROVIDER_ERROR_RESPONSE", "HTTP_ERROR", "TRANSPORT_ERROR",
                   "UNEXPECTED_RESPONSE_FAILURE", "SIGNING_CLOCK_INVALID", "UNSAFE_KEY_PERMISSIONS",
                   "REQUEST_SEQUENCE_INVALID"}
VALUE_KINDS = {str: "string", int: "integer", bool: "boolean", float: "number", list: "array", dict: "object", type(None): "null"}
SHAPE_HINTS = {prefix + kind for prefix in ("root_", "fraction_") for kind in VALUE_KINDS.values()}
UNRESOLVED = ["FILL_FEE_ASSET_AND_ROUNDING_UNVERIFIED", "CONVERT_ACCESS_AND_EXECUTABLE_COSTS_UNVERIFIED",
              "PROTECTIVE_AND_PARTIAL_FILL_EXITS_UNVERIFIED", "USD_FX_AND_BOUNDARY_ACCOUNTING_UNQUALIFIED",
              "STRATEGY_HISTORICAL_GATES_NOT_PASSED"]


class _ReadFailure(Exception):
    def __init__(self, code, reason="SCHEMA_INVALID", *, http_status=None, provider_error_code=None, response_sha256=None, shape_hint=None):
        if code not in BLOCKERS or reason not in FAILURE_REASONS:
            fail()
        super().__init__(code)
        self.code = code
        self.reason, self.http_status = reason, http_status
        self.provider_error_code, self.response_sha256 = provider_error_code, response_sha256
        self.shape_hint = shape_hint


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")


def _rate(value):
    if type(value) is not str or len(value) > 40 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    result = Decimal(value)
    if not 0 <= result <= 1:
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    return result


def permissions(document):
    _keys(document, PERMISSION_FLAGS | {"createTime"})
    if (any(type(document[name]) is not bool for name in PERMISSION_FLAGS)
            or type(document["createTime"]) is not int or not 0 <= document["createTime"] < 10**16):
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    return {name: document[name] for name in sorted(document)}


def commission(document):
    _keys(document, {"symbol", "standardCommission", "taxCommission", "specialCommission", "discount"})
    if document["symbol"] != "BTCUSDT":
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    result = {"symbol": "BTCUSDT"}
    for component in ("standardCommission", "taxCommission", "specialCommission"):
        rates = document[component]
        _keys(rates, {"maker", "taker", "buyer", "seller"})
        result[component] = {name: text(_rate(value)) for name, value in rates.items()}
    discount = document["discount"]
    _keys(discount, {"enabledForAccount", "enabledForSymbol", "discountAsset", "discount"})
    if (type(discount["enabledForAccount"]) is not bool or type(discount["enabledForSymbol"]) is not bool
            or discount["discountAsset"] != "BNB"):
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    result["discount"] = {**discount, "discount": text(_rate(discount["discount"]))}
    return result


def precision(document):
    if type(document) is not list or not 1 <= len(document) <= 5000:
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "INVALID_ASSET_LIST")
    result = {}
    for item in document:
        if (type(item) is not dict or type(item.get("asset")) is not str
                or not 1 <= len(item["asset"]) <= 256):
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "INVALID_ASSET_ROW")
        # AssetInfo describes many coins. Unrelated names and precision rules
        # must not qualify or disqualify the two exact assets we actually use.
        if item["asset"] not in {"BTC", "USDT"}:
            continue
        if set(item) != {"asset", "fraction"}:
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "INVALID_REQUIRED_ASSET")
        if type(item["fraction"]) is not int or not 0 <= item["fraction"] <= 18:
            hint = "fraction_" + VALUE_KINDS[type(item["fraction"])]
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "INVALID_REQUIRED_FRACTION", shape_hint=hint)
        if item["asset"] in result:
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "DUPLICATE_REQUIRED_ASSET")
        result[item["asset"]] = item["fraction"]
    if set(result) != {"BTC", "USDT"}:
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "MISSING_REQUIRED_ASSETS")
    return [{"asset": asset, "fraction": result[asset]} for asset in sorted(result)]


def undiscounted_taker_rates(document):
    checked = commission(document)
    with localcontext() as context:
        context.prec = PRECISION
        return {side: text(sum((Decimal(checked[name]["taker"]) + Decimal(checked[name][role])
                               for name in ("standardCommission", "taxCommission", "specialCommission")), Decimal(0)))
                for side, role in (("buy", "buyer"), ("sell", "seller"))}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Signed URLs and the API-key header must never follow redirects.
        return None


class _Client:
    def __init__(self, key, secret, *, opener=None, monotonic=time.monotonic):
        self.key, self.secret = key, secret
        self.opener = opener if opener is not None else build_opener(
            ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
        self.monotonic = monotonic
        self.anchor = self.server_ms = None
        self.attempts = 0
        self.receipts = []

    def get(self, endpoint):
        if (type(endpoint) is not str or endpoint not in ENDPOINTS or self.attempts >= 4
                or endpoint != list(ENDPOINTS)[self.attempts]):
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "REQUEST_SEQUENCE_INVALID")
        headers = {"Accept": "application/json", "User-Agent": "MarvelResearch/readonly-account-check"}
        query = ""
        if endpoint != "time":
            if self.anchor is None:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "SIGNING_CLOCK_INVALID")
            elapsed = self.monotonic() - self.anchor
            if not 0 <= elapsed <= 60:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "SIGNING_CLOCK_INVALID")
            params = {"symbol": "BTCUSDT"} if endpoint == "commissions" else {}
            params.update(timestamp=self.server_ms + int(elapsed * 1000), recvWindow=5000)
            unsigned = urlencode(params)
            signature = hmac.new(self.secret.encode("ascii"), unsigned.encode("ascii"), "sha256").hexdigest()
            query = "?" + unsigned + "&signature=" + signature
            headers["X-MBX-APIKEY"] = self.key
        url = ORIGIN + ENDPOINTS[endpoint] + query
        request = Request(url, headers=headers, method="GET")
        self.attempts += 1
        raw, status, decoded = None, None, False
        try:
            with self.opener.open(request, timeout=10) as response:
                status = response.status if type(response.status) is int and 100 <= response.status <= 599 else None
                if status != 200:
                    raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "UNEXPECTED_HTTP_STATUS")
                if response.geturl() != url:
                    raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "UNEXPECTED_RESPONSE_URL")
                if response.headers.get_content_type() != "application/json":
                    raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "UNEXPECTED_CONTENT_TYPE")
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "RESPONSE_TOO_LARGE")
            try:
                document = decode_record(raw)
                decoded = True
            except ResearchError:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "INVALID_JSON") from None
            if type(document) is dict and type(document.get("code")) is int and -99999 <= document["code"] < 0:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "PROVIDER_ERROR_RESPONSE", provider_error_code=document["code"])
            if endpoint == "time":
                _keys(document, {"serverTime"})
                if type(document["serverTime"]) is not int or not 0 < document["serverTime"] < 10**16:
                    raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
                self.server_ms, self.anchor = document["serverTime"], self.monotonic()
            elif endpoint == "permissions":
                document = permissions(document)
            elif endpoint == "commissions":
                document = commission(document)
            else:
                document = precision(document)
        except _ReadFailure as error:
            error.http_status = status
            error.response_sha256 = sha256(raw).hexdigest() if raw is not None and len(raw) <= MAX_BYTES else None
            if error.shape_hint is None and decoded:
                error.shape_hint = "root_" + VALUE_KINDS[type(document)]
            raise
        except HTTPError as error:
            code = error.code if type(error.code) is int and 100 <= error.code <= 599 else None
            raise _ReadFailure("ACCOUNT_READ_FAILED", "HTTP_ERROR", http_status=code) from None
        except (URLError, http.client.HTTPException, OSError):
            raise _ReadFailure("ACCOUNT_READ_FAILED", "TRANSPORT_ERROR") from None
        except Exception:
            # No provider body, key, signature, URL or exception text crosses this boundary.
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "UNEXPECTED_RESPONSE_FAILURE", http_status=status) from None
        self.receipts.append({"endpoint": endpoint, "response_sha256": sha256(raw).hexdigest()})
        return document


def collect(*, allow_network=False, environ=None, opener=None, monotonic=time.monotonic):
    result = {"schema_version": 2, "kind": "btc_read_only_account_observation", "observed_at_utc": stamp(utc_now()),
              "network_opt_in": allow_network is True, "network_requests_attempted": 0, "receipts": [],
              "permissions": None, "commissions": None, "precision": None, "observation_blockers": [],
              "observation_status": "not_attempted", "phase5_status": "blocked", "failure_detail": None,
              "owner_paper_account_used": False, "profile_changed": False,
              "financial_qualification_passed": False, "execution_eligible": False,
              "entries_allowed": False, "live_execution_available": False}
    if allow_network is not True:
        result["observation_blockers"] = ["NETWORK_OPT_IN_REQUIRED"]
        return result
    environment = os.environ if environ is None else environ
    key, secret = (environment.get(name) for name in VARIABLES)
    if not key or not secret:
        result["observation_blockers"] = ["READ_ONLY_CREDENTIALS_MISSING"]
        return result
    if any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9]{32,256}", value) is None for value in (key, secret)):
        result["observation_blockers"] = ["READ_ONLY_CREDENTIALS_INVALID"]
        return result
    client = _Client(key, secret, opener=opener, monotonic=monotonic)
    result["observation_status"] = "partial"
    stage = "time"
    try:
        client.get("time")
        stage = "permissions"
        result["permissions"] = client.get("permissions")
        rights = result["permissions"]
        if rights["enableReading"] is not True or any(rights[name] for name in MUTATIONS):
            raise _ReadFailure("API_PERMISSION_SCOPE_REJECTED", "UNSAFE_KEY_PERMISSIONS")
        stage = "commissions"
        result["commissions"] = client.get("commissions")
        stage = "precision"
        result["precision"] = client.get("precision")
        result["observation_status"] = "complete"
    except _ReadFailure as error:
        result["observation_blockers"] = [error.code]
        result["failure_detail"] = {"endpoint": stage, "reason": error.reason, "http_status": error.http_status,
                                    "provider_error_code": error.provider_error_code, "response_sha256": error.response_sha256,
                                    "shape_hint": error.shape_hint}
    result.update(network_requests_attempted=client.attempts, receipts=client.receipts)
    return result


def summary(document):
    return {"observation_status": document["observation_status"], "phase5_status": "blocked",
            "observation_blockers": list(document["observation_blockers"]),
            "failure_detail": document.get("failure_detail"),
            "remaining_phase5_blockers": list(UNRESOLVED),
            "network_requests_attempted": document["network_requests_attempted"],
            "actual_account_commission_rates_observed": document["commissions"] is not None,
            "convert_precision_observed": document["precision"] is not None,
            "undiscounted_taker_rate_fractions": None if document["commissions"] is None else undiscounted_taker_rates(document["commissions"]),
            "bnb_discount_applied": False, "actual_fill_fee_assets_and_rounding_verified": False,
            "bnb_discount_enabled_for_account": None if document["commissions"] is None else document["commissions"]["discount"]["enabledForAccount"],
            "bnb_discount_enabled_for_symbol": None if document["commissions"] is None else document["commissions"]["discount"]["enabledForSymbol"],
            "convert_quote_access_or_costs_verified": False, "financial_qualification_passed": False,
            "owner_paper_account_used": False, "profile_changed": False, "real_orders_or_conversions": 0,
            "entries_allowed": False, "live_execution_available": False,
            "note": "A current metadata observation cannot qualify past costs, actual fills, conversion access or profitability."}


def _validate(document):
    template = collect()  # No environment read or transport is possible in this validation.
    if type(document) is not dict or type(document.get("schema_version")) is not int or document["schema_version"] not in {1, 2}:
        fail("RESEARCH_RECORD")
    expected_keys = set(template) - ({"failure_detail"} if document["schema_version"] == 1 else set())
    if set(document) != expected_keys:
        fail("RESEARCH_RECORD")
    if (document["kind"] != template["kind"] or document["phase5_status"] != "blocked"
            or type(document["observation_status"]) is not str
            or document["observation_status"] not in {"not_attempted", "partial", "complete"}
            or type(document["network_opt_in"]) is not bool
            or type(document["network_requests_attempted"]) is not int
            or not 0 <= document["network_requests_attempted"] <= 4
            or any(document[name] is not False for name in ("owner_paper_account_used", "profile_changed",
                   "financial_qualification_passed", "execution_eligible", "entries_allowed", "live_execution_available"))
            or type(document["observation_blockers"]) is not list or len(document["observation_blockers"]) > 1
            or any(type(code) is not str or code not in BLOCKERS for code in document["observation_blockers"])):
        fail("RESEARCH_RECORD")
    from ..clock import parse_stamp
    try:
        parse_stamp(document["observed_at_utc"])
        if document["permissions"] is not None:
            permissions(document["permissions"])
        if document["commissions"] is not None:
            if canonical(commission(document["commissions"])) != canonical(document["commissions"]):
                fail("RESEARCH_RECORD")
        if document["precision"] is not None:
            if canonical(precision(document["precision"])) != canonical(document["precision"]):
                fail("RESEARCH_RECORD")
        receipts = document["receipts"]
        if type(receipts) is not list or len(receipts) > document["network_requests_attempted"]:
            fail("RESEARCH_RECORD")
        expected = list(ENDPOINTS)[:len(receipts)]
        for item, endpoint in zip(receipts, expected):
            _keys(item, {"endpoint", "response_sha256"})
            if (item["endpoint"] != endpoint or type(item["response_sha256"]) is not str
                    or re.fullmatch(r"[a-f0-9]{64}", item["response_sha256"]) is None):
                fail("RESEARCH_RECORD")
        if ((document["permissions"] is not None) != (len(receipts) >= 2)
                or (document["commissions"] is not None) != (len(receipts) >= 3)
                or (document["precision"] is not None) != (len(receipts) == 4)):
            fail("RESEARCH_RECORD")
        rights = document["permissions"]
        if document["commissions"] is not None and (rights["enableReading"] is not True or any(rights[name] for name in MUTATIONS)):
            fail("RESEARCH_RECORD")
        complete = document["observation_status"] == "complete"
        if complete != (len(receipts) == 4 and not document["observation_blockers"]):
            fail("RESEARCH_RECORD")
        if document["network_requests_attempted"] and not document["network_opt_in"]:
            fail("RESEARCH_RECORD")
        if document["network_requests_attempted"] and document["observation_status"] == "not_attempted":
            fail("RESEARCH_RECORD")
        if not document["network_requests_attempted"] and (document["observation_status"] != "not_attempted" or not document["observation_blockers"]):
            fail("RESEARCH_RECORD")
        if document["schema_version"] == 2:
            detail = document["failure_detail"]
            failed_read = document["observation_status"] == "partial"
            if failed_read != (detail is not None):
                fail("RESEARCH_RECORD")
            if detail is not None:
                _keys(detail, {"endpoint", "reason", "http_status", "provider_error_code", "response_sha256", "shape_hint"})
                if (not document["observation_blockers"] or document["network_requests_attempted"] == 0
                        or type(detail["reason"]) is not str or detail["reason"] not in FAILURE_REASONS):
                    fail("RESEARCH_RECORD")
                if detail["reason"] == "UNSAFE_KEY_PERMISSIONS":
                    if detail["endpoint"] != "permissions" or len(receipts) != 2 or document["network_requests_attempted"] != 2:
                        fail("RESEARCH_RECORD")
                else:
                    expected_attempts = len(receipts) + (0 if detail["reason"] == "SIGNING_CLOCK_INVALID" else 1)
                    if (len(receipts) >= 4 or detail["endpoint"] != list(ENDPOINTS)[len(receipts)]
                            or document["network_requests_attempted"] != expected_attempts):
                        fail("RESEARCH_RECORD")
                for name, low, high in (("http_status", 100, 599), ("provider_error_code", -99999, -1)):
                    if detail[name] is not None and (type(detail[name]) is not int or not low <= detail[name] <= high):
                        fail("RESEARCH_RECORD")
                value = detail["response_sha256"]
                if value is not None and (type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None):
                    fail("RESEARCH_RECORD")
                hint = detail["shape_hint"]
                if hint is not None and (type(hint) is not str or hint not in SHAPE_HINTS):
                    fail("RESEARCH_RECORD")
    except (TypeError, ValueError, _ReadFailure):
        fail("RESEARCH_RECORD")


class AccountEvidenceStore:
    """Local ignored evidence; no credentials, signatures or raw private bodies."""
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "var/research-account-checks"
        self._path(self.root)

    def _path(self, path):
        if not path.resolve().is_relative_to(self.workspace) or not path.resolve().is_relative_to(self.root.resolve()):
            fail("RESEARCH_RECORD")
        return path

    def _folder(self, identity):
        if type(identity) is not str or re.fullmatch(r"[a-f0-9]{64}", identity) is None:
            fail("RESEARCH_RECORD")
        return self._path(self.root / identity)

    def save(self, document):
        _validate(document)
        raw = canonical(document).encode("ascii")
        if len(raw) > MAX_BYTES:
            fail("RESEARCH_RECORD")
        identity, target = sha256(raw).hexdigest(), self._folder(sha256(raw).hexdigest())
        if target.exists():
            self.load(identity)
            return identity
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=self.root) as name:
            staging = self._path(Path(name))
            (staging / "record.json").write_bytes(raw)
            try:
                staging.rename(target)
            except OSError:
                if not target.exists():
                    fail("RESEARCH_RECORD")
                self.load(identity)
        return identity

    def load(self, identity):
        try:
            with self._path(self._folder(identity) / "record.json").open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except OSError:
            fail("RESEARCH_RECORD")
        if len(raw) > MAX_BYTES or sha256(raw).hexdigest() != identity:
            fail("RESEARCH_RECORD")
        document = decode_record(raw)
        _validate(document)
        return document
