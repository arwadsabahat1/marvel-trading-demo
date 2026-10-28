"""Closed paper assumptions and strict, separately supplied proposal/market schemas."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType

from ..clock import parse_stamp
from ..config import amount
from ..errors import RiskError

PRECISION = 256
ACTIVE = {"reserved", "submitted", "unknown"}
TERMINAL = {"released", "closed"}
MAX_EVENTS = 1_000
MAX_HISTORICAL_EVENTS = 200_000  # Offline RAM-only research budget; never a paper-account policy change.
MAX_ACCOUNT_AGE_SECONDS = 60
AUTO_HALTS = {"DAILY_LOSS", "WEEKLY_LOSS", "PEAK_DRAWDOWN", "LOSING_STREAK",
              "EXPOSURE_LIMIT", "OPEN_RISK_BUDGET", "UNRESERVED_EXPOSURE", "PERIOD_REVIEW", "PAPER_LIQUIDATION_BLOCKED"}
NUMERIC_PROFILE = {
    "tick_size", "min_price", "max_price", "entry_step", "entry_min_qty", "entry_max_qty",
    "exit_step", "exit_min_qty", "exit_max_qty", "entry_min_notional_quote", "entry_max_notional_quote",
    "exit_min_notional_quote", "exit_max_notional_quote", "entry_fee_rate", "exit_fee_rate",
    "base_fee_quantum", "quote_fee_quantum", "entry_slippage_bps", "exit_slippage_bps", "cost_buffer_usd",
}
PROFILE_KEYS = NUMERIC_PROFILE | {"schema_version", "mode", "symbol", "qualification", "entry_fee_asset", "exit_fee_asset"}


def history_limit(context_kind):
    return MAX_HISTORICAL_EVENTS if context_kind == "historical_ohlcv_model" else MAX_EVENTS


def reject(code="RISK_INVALID"):
    messages = {
        "RISK_INVALID": "Paper risk input, accounting or saved risk history failed validation.",
        "RISK_PROFILE_MISMATCH": "Paper cost/instrument assumptions differ from this account's saved profile; no reset was performed.",
        "RISK_ID_CONFLICT": "A risk identifier was reused with different input or a completed reservation.",
        "RISK_TRANSITION": "The requested reservation transition is not permitted; exposure remains reserved.",
        "RISK_HISTORY_FULL": "The bounded paper risk history is full; preserve it for reviewed archival before continuing.",
    }
    raise RiskError(code, messages[code])


def text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(value) -> str:
    return sha256(canonical(value).encode("ascii")).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            reject()
        result[key] = value
    return result


def decode(raw: bytes | str):
    try:
        if len(raw) > 8 * 1024 * 1024:
            reject()
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: reject())
    except (UnicodeError, ValueError, RecursionError):
        reject()


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != expected:
        reject()
    return value


def number(value, *, zero=False) -> Decimal:
    try:
        return amount(value, maximum=Decimal("1000000000000"), zero=zero)
    except (ValueError, TypeError):
        reject()


def signed(value) -> Decimal:
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        reject()
    result = Decimal(value)
    if not result.is_finite() or result < Decimal("-1000000000000") or result > Decimal("1000000000000"):
        reject()
    return result


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        reject()
    return value


def timestamp(value):
    try:
        return parse_stamp(value)
    except (TypeError, ValueError, OverflowError):
        reject()


def integer(value, maximum=100):
    if type(value) is not int or not 0 <= value <= maximum:
        reject()
    return value


def floor_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(value, step):
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


@dataclass(frozen=True)
class Profile:
    document: dict
    fingerprint: str

    def n(self, name):
        return Decimal(self.document[name])


def load_profile(workspace: Path) -> Profile:
    try:
        with (workspace / "config/btc-paper-risk.json").open("rb") as handle:
            raw = handle.read(16_385)
        if len(raw) > 16_384:
            reject()
        return parse_profile(decode(raw))
    except OSError:
        reject()


def parse_profile(document) -> Profile:
    root = keys(document, PROFILE_KEYS)
    if (type(root["schema_version"]) is not int or root["schema_version"] != 1 or root["mode"] != "paper"
            or root["symbol"] != "BTCUSDT" or root["qualification"] != "illustrative_not_venue_qualified"
            or type(root["entry_fee_asset"]) is not str or root["entry_fee_asset"] not in {"BTC", "USDT"}
            or root["exit_fee_asset"] != "USDT"):
        reject()
    result = dict(root)
    for name in NUMERIC_PROFILE:
        value = number(root[name], zero=name in {"entry_fee_rate", "exit_fee_rate", "entry_slippage_bps", "exit_slippage_bps"})
        if (name.endswith("fee_rate") and value >= Decimal("0.1")) or (name.endswith("slippage_bps") and value > 1000):
            reject()
        result[name] = text(value)
    for low, high in (("min_price", "max_price"), ("entry_min_qty", "entry_max_qty"), ("exit_min_qty", "exit_max_qty"),
                      ("entry_min_notional_quote", "entry_max_notional_quote"), ("exit_min_notional_quote", "exit_max_notional_quote")):
        if Decimal(result[low]) > Decimal(result[high]):
            reject()
    return Profile(MappingProxyType(result), fingerprint(result))


def request(document, *, context_kind="synthetic"):
    root = keys(document, {"proposal", "market"})
    proposal = keys(root["proposal"], {"id", "symbol", "side", "stop_price", "quantity"})
    identifier(proposal["id"])
    if proposal["symbol"] != "BTCUSDT" or proposal["side"] != "BUY":
        reject()
    number(proposal["stop_price"])
    if proposal["quantity"] is not None:
        number(proposal["quantity"])
    market = keys(root["market"], {"kind", "bid", "ask", "quote_at", "fx_lower", "fx_upper", "fx_at"})
    if context_kind not in {"synthetic", "historical_ohlcv_model"} or market["kind"] != context_kind:
        reject()  # Offline modelling requires a separately bound adapter context.
    bid, ask, lower, upper = [number(market[name]) for name in ("bid", "ask", "fx_lower", "fx_upper")]
    if bid > ask or lower > upper:
        reject()
    timestamp(market["quote_at"])
    timestamp(market["fx_at"])
    return root
