"""A small, closed configuration schema. Credentials and live mode are unsupported."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .errors import ConfigError

MARKETS = ("BTC", "GOLD", "NASDAQ100", "US30")
MAX_CONFIG_BYTES = 16_384
DECIMAL_TEXT = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def _invalid() -> ConfigError:
    return ConfigError("CONFIG_INVALID", "Configuration rejected; check the documented schema and risk ceilings.")


def _keys(value: object, names: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != names:
        raise _invalid()
    return value


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def amount(value: object, *, maximum: Decimal = Decimal("1000000000000"), zero: bool = False) -> Decimal:
    """Require finite, plain decimal strings; floats, booleans and exponents fail."""
    if not isinstance(value, str) or len(value) > 40 or not DECIMAL_TEXT.fullmatch(value):
        raise ValueError("A decimal string is required")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite() or result > maximum or result < 0 or (not zero and result == 0):
        raise ValueError("Decimal is outside its permitted range")
    return result


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _decimal_text(value: Decimal) -> str:
    # Decimal.normalize() uses the active precision context and can round.
    # A policy fingerprint must preserve every accepted digit exactly.
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _runtime_path(value: object, workspace: Path, suffix: str) -> Path:
    if not isinstance(value, str) or len(value) > 240 or "\\" in value or ":" in value:
        raise _invalid()
    lexical = PurePosixPath(value)
    if lexical.is_absolute() or PureWindowsPath(value).drive or ".." in lexical.parts:
        raise _invalid()
    if len(lexical.parts) < 2 or lexical.parts[0] != "var" or lexical.suffix != suffix:
        raise _invalid()
    resolved = (workspace / value).resolve()
    if not resolved.is_relative_to(workspace) or resolved == workspace:
        raise _invalid()
    return resolved


@dataclass(frozen=True)
class RiskConfig:
    per_trade_cap_usd: Decimal
    daily_loss_cap_usd: Decimal
    weekly_loss_fraction: Decimal
    peak_drawdown_fraction: Decimal
    max_notional_fraction: Decimal
    max_positions: int
    losing_streak_limit: int


@dataclass(frozen=True)
class Config:
    workspace: Path
    starting_equity_usd: Decimal
    markets: tuple[str, ...]
    risk: RiskConfig
    max_quote_age_seconds: int
    max_spread_bps: Decimal
    database: Path
    log: Path
    fingerprint: str


def load_config(path: Path, workspace: Path) -> Config:
    """Validate the whole document before any logs or database are created."""
    try:
        workspace = workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise _invalid()
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise _invalid()
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_pairs)
        return _parse(document, workspace)
    except ConfigError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ConfigError("CONFIG_INVALID", "Configuration is missing, unreadable or invalid; no state was initialized.") from exc


def _parse(document: object, workspace: Path) -> Config:
    root = _keys(document, {"schema_version", "mode", "network_enabled", "starting_equity_usd", "markets", "risk", "data", "storage"})
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise _invalid()
    if root["mode"] != "paper" or root["network_enabled"] is not False:
        raise ConfigError("PAPER_ONLY", "Only offline paper mode is implemented; live trading and networking are unavailable.")
    equity = amount(root["starting_equity_usd"], maximum=Decimal("50"))
    markets = root["markets"]
    if not isinstance(markets, list) or not markets or len(markets) > len(MARKETS):
        raise _invalid()
    if any(type(m) is not str or m not in MARKETS for m in markets) or len(set(markets)) != len(markets):
        raise _invalid()
    r = _keys(root["risk"], {"per_trade_cap_usd", "daily_loss_cap_usd", "weekly_loss_fraction", "peak_drawdown_fraction", "max_notional_fraction", "max_positions", "losing_streak_limit"})
    risk = RiskConfig(
        amount(r["per_trade_cap_usd"], maximum=Decimal("1")),
        amount(r["daily_loss_cap_usd"], maximum=Decimal("5")),
        amount(r["weekly_loss_fraction"], maximum=Decimal("0.02")),
        amount(r["peak_drawdown_fraction"], maximum=Decimal("0.05")),
        amount(r["max_notional_fraction"], maximum=Decimal("0.20")),
        _integer(r["max_positions"], 1, 1),
        _integer(r["losing_streak_limit"], 1, 3),
    )
    data = _keys(root["data"], {"max_quote_age_seconds", "max_spread_bps"})
    quote_age = _integer(data["max_quote_age_seconds"], 1, 60)
    spread = amount(data["max_spread_bps"], maximum=Decimal("50"))
    storage = _keys(root["storage"], {"database", "log"})
    database = _runtime_path(storage["database"], workspace, ".sqlite3")
    log = _runtime_path(storage["log"], workspace, ".jsonl")
    if database == log:
        raise _invalid()
    # Bind only operational semantics, not local file locations, to the account.
    canonical = {
        "schema_version": 1, "mode": "paper", "network_enabled": False,
        "starting_equity_usd": _decimal_text(equity), "markets": sorted(markets),
        "risk": {key: _decimal_text(getattr(risk, key)) if isinstance(getattr(risk, key), Decimal) else getattr(risk, key) for key in r},
        "data": {"max_quote_age_seconds": quote_age, "max_spread_bps": _decimal_text(spread)},
    }
    fingerprint = sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return Config(workspace, equity, tuple(markets), risk, quote_age, spread, database, log, fingerprint)
