"""Opt-in BTC-to-USDT quote observation. No quote acceptance or order transport.

Binance groups getQuote under TRADE permission. This separate client does not
weaken the GET-only account checker or read its credential variables.
"""

from decimal import Decimal, localcontext
from hashlib import sha256
import hmac
import http.client
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from ..clock import stamp, utc_now
from ..errors import ResearchError
from ..risk.model import PRECISION, fingerprint, text
from .account_check import MAX_BYTES, MUTATIONS, ORIGIN, _Client, _ReadFailure, undiscounted_taker_rates
from .model import fail
from .store import decode_record

VARIABLES = ("MARVEL_BINANCE_QUOTE_API_KEY", "MARVEL_BINANCE_QUOTE_API_SECRET")
QUOTE_PATH = "/sapi/v1/convert/getQuote"
DEFAULT_AMOUNT = "0.0000099"
MAX_PROBE_BTC = Decimal("0.0001")


def amount(value):
    if type(value) is not str or len(value) > 24 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        fail()
    with localcontext() as context:
        context.prec = PRECISION
        number = Decimal(value)
        if not Decimal(0) < number <= MAX_PROBE_BTC or number % Decimal("0.00000001"):
            fail()
        return text(number)


def _positive(value):
    if type(value) is not str or len(value) > 40 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    result = Decimal(value)
    if not 0 < result <= Decimal("1000000000000"):
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    return result


def quote_fields(document, requested, fractions, sent_ms, received_ms):
    required = {"ratio", "inverseRatio", "fromAmount", "toAmount"}
    if type(document) is not dict or not required <= set(document) <= required | {"quoteId", "validTimestamp"}:
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    numbers = {name: _positive(document[name]) for name in required}
    if (numbers["fromAmount"] != Decimal(requested)
            or any(numbers[name] % Decimal(1).scaleb(-fractions[asset])
                   for name, asset in (("fromAmount", "BTC"), ("toAmount", "USDT")))):
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    qid, expires = document.get("quoteId"), document.get("validTimestamp")
    if "quoteId" in document and (type(qid) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", qid) is None):
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    if ("validTimestamp" in document and (type(expires) is not int or not 0 < expires < 10**16
                                          or expires > sent_ms + 20000)) or (qid is not None and expires is None):
        raise _ReadFailure("ACCOUNT_RESPONSE_INVALID")
    return {**{name: text(value) for name, value in numbers.items()},
            "provider_offer_id_present": qid is not None,
            "provider_offer_id_sha256": None if qid is None else sha256(qid.encode("ascii")).hexdigest(),
            "valid_timestamp_ms": expires, "received_server_estimate_ms": received_ms,
            "offer_unexpired_at_receipt_estimate": bool(qid is not None and expires > received_ms),
            "note": "An indicative price without an offer ID does not establish executable access; no offer is accepted"}


class _QuoteClient(_Client):
    """Four existing fixed GETs, then at most one fixed-path POST with fixed assets."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scope_checked = False
        self.fractions = None

    def get(self, endpoint):
        if endpoint in {"commissions", "precision"} and not self.scope_checked:
            raise _ReadFailure("API_PERMISSION_SCOPE_REJECTED", "UNSAFE_KEY_PERMISSIONS")
        document = super().get(endpoint)
        if endpoint == "permissions":
            if (not document["enableReading"] or not document["enableSpotAndMarginTrading"] or not document["ipRestrict"]
                    or any(document[name] for name in MUTATIONS - {"enableSpotAndMarginTrading"})):
                raise _ReadFailure("API_PERMISSION_SCOPE_REJECTED", "UNSAFE_KEY_PERMISSIONS")
            self.scope_checked = True
        if endpoint == "precision":
            self.fractions = {item["asset"]: item["fraction"] for item in document}
        return document

    def request_quote(self, requested):
        requested = amount(requested)
        if not self.scope_checked or self.fractions is None or self.attempts != 4:
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "REQUEST_SEQUENCE_INVALID")
        if Decimal(requested) % Decimal(1).scaleb(-self.fractions["BTC"]):
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "INVALID_REQUIRED_FRACTION")
        elapsed = self.monotonic() - self.anchor
        if not 0 <= elapsed <= 60:
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "SIGNING_CLOCK_INVALID")
        sent_ms = self.server_ms + int(elapsed * 1000)
        params = {"fromAsset": "BTC", "toAsset": "USDT", "fromAmount": requested,
                  "walletType": "SPOT", "validTime": "10s", "timestamp": sent_ms, "recvWindow": 5000}
        unsigned = urlencode(params)
        signature = hmac.new(self.secret.encode("ascii"), unsigned.encode("ascii"), "sha256").hexdigest()
        url = ORIGIN + QUOTE_PATH
        request = Request(url, data=(unsigned + "&signature=" + signature).encode("ascii"), method="POST",
                          headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                                   "User-Agent": "MarvelResearch/quote-observation", "X-MBX-APIKEY": self.key})
        self.attempts += 1
        raw, status = None, None
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
            except ResearchError:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "INVALID_JSON") from None
            if type(document) is dict and type(document.get("code")) is int and -99999 <= document["code"] < 0:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "PROVIDER_ERROR_RESPONSE", provider_error_code=document["code"])
            elapsed_after = self.monotonic() - self.anchor
            if not elapsed <= elapsed_after <= 70:
                raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "SIGNING_CLOCK_INVALID")
            result = quote_fields(document, requested, self.fractions, sent_ms, self.server_ms + int(elapsed_after * 1000))
        except _ReadFailure as error:
            error.http_status = status
            error.response_sha256 = sha256(raw).hexdigest() if raw is not None and len(raw) <= MAX_BYTES else None
            raise
        except HTTPError as error:
            code = error.code if type(error.code) is int and 100 <= error.code <= 599 else None
            raise _ReadFailure("ACCOUNT_READ_FAILED", "HTTP_ERROR", http_status=code) from None
        except (URLError, http.client.HTTPException, OSError):
            raise _ReadFailure("ACCOUNT_READ_FAILED", "TRANSPORT_ERROR") from None
        except Exception:
            raise _ReadFailure("ACCOUNT_RESPONSE_INVALID", "UNEXPECTED_RESPONSE_FAILURE", http_status=status) from None
        self.receipts.append({"endpoint": "quote", "response_sha256": sha256(raw).hexdigest()})
        return result


def collect(*, from_btc=DEFAULT_AMOUNT, allow_network=False, acknowledge_trade_permission=False,
            environ=None, opener=None, monotonic=time.monotonic):
    with localcontext() as context:
        context.prec = PRECISION
        return _collect(from_btc=from_btc, allow_network=allow_network,
                        acknowledge_trade_permission=acknowledge_trade_permission,
                        environ=environ, opener=opener, monotonic=monotonic)


def _collect(*, from_btc, allow_network, acknowledge_trade_permission, environ, opener, monotonic):
    requested = amount(from_btc)
    result = {"schema_version": 1, "kind": "btc_convert_quote_observation", "observed_at_utc": stamp(utc_now()),
              "requested_from_btc": requested, "wallet_type": "SPOT", "network_opt_in": allow_network is True,
              "trade_permission_acknowledged": acknowledge_trade_permission is True,
              "observation_status": "not_attempted", "blockers": [], "network_requests_attempted": 0,
              "permissions": None, "undiscounted_taker_rate_fractions": None, "asset_fractions": None,
              "quote": None, "failure_detail": None, "receipts": [],
              "quote_acceptance_available": False, "real_orders_or_conversions": 0,
              "owner_paper_account_used": False, "profile_changed": False, "entries_allowed": False,
              "live_execution_available": False, "actual_fill_fees_qualified": False,
              "actual_complete_exit_qualified": False, "historical_costs_qualified": False,
              "phase5_historical_gate_passed": False,
              "limitations": ["A quote is not a fill, completed exit, settled balance or historical cost bound",
                  "The request amount is a research probe; it does not establish an owner holding or risk reservation",
                  "The provider permission is broader than this client's single non-accepting POST",
                  "Current-key metadata cannot be assumed to belong to any earlier captured account",
                  "No raw quote ID, credentials, signatures or raw private bodies are retained"]}
    if allow_network is not True:
        result["blockers"] = ["NETWORK_OPT_IN_REQUIRED"]
    elif acknowledge_trade_permission is not True:
        result["blockers"] = ["TRADE_PERMISSION_ACKNOWLEDGEMENT_REQUIRED"]
    else:
        environment = os.environ if environ is None else environ
        key, secret = (environment.get(name) for name in VARIABLES)
        if not key or not secret:
            result["blockers"] = ["QUOTE_CREDENTIALS_MISSING"]
        elif any(type(value) is not str or re.fullmatch(r"[A-Za-z0-9]{32,256}", value) is None for value in (key, secret)):
            result["blockers"] = ["QUOTE_CREDENTIALS_INVALID"]
        else:
            client = _QuoteClient(key, secret, opener=opener, monotonic=monotonic)
            result["observation_status"] = "partial"
            stage = "time"
            try:
                client.get("time")
                stage = "permissions"
                rights = client.get("permissions")
                result["permissions"] = {name: rights[name] for name in ("enableReading", "enableSpotAndMarginTrading", "ipRestrict")}
                stage = "commissions"
                result["undiscounted_taker_rate_fractions"] = undiscounted_taker_rates(client.get("commissions"))
                stage = "precision"
                result["asset_fractions"] = {item["asset"]: item["fraction"] for item in client.get("precision")}
                stage = "quote"
                result["quote"] = client.request_quote(requested)
                result["observation_status"] = "complete"
                if not result["quote"]["provider_offer_id_present"]:
                    result["blockers"] = ["INDICATIVE_PRICE_ONLY_NO_OFFER_ID"]
                elif not result["quote"]["offer_unexpired_at_receipt_estimate"]:
                    result["blockers"] = ["OFFER_EXPIRED_AT_RECEIPT_ESTIMATE"]
            except _ReadFailure as error:
                result["blockers"] = [error.code]
                result["failure_detail"] = {"endpoint": stage, "reason": error.reason, "http_status": error.http_status,
                                            "provider_error_code": error.provider_error_code,
                                            "response_sha256": error.response_sha256}
            result.update(network_requests_attempted=client.attempts, receipts=client.receipts)
    return {**result, "observation_id": fingerprint(result)}
