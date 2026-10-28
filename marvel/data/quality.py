"""Strict UTC spot-candle parsing and causal features, using only the standard library."""

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import io
import json
import re
import zipfile
import zlib

from ..clock import parse_stamp
from ..config import amount
from ..errors import DataError

INTERVALS = {"1h": 3_600_000_000, "4h": 14_400_000_000, "1d": 86_400_000_000}
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 100_000
MIN_TIME = 946_684_800_000_000  # 2000-01-01 UTC
MAX_TIME = 4_102_444_800_000_000  # 2100-01-01 UTC
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def fail(code="DATA_INVALID"):
    messages = {
        "DATA_INVALID": "Research data failed schema, price, size or coverage validation.",
        "DATA_GAP": "Research candles contain a gap, duplicate or ordering error; no prices were filled in.",
        "DATA_TIMESTAMP": "Research timestamps failed explicit-unit, UTC, boundary or as-of validation.",
        "DATA_CHECKSUM": "Archive checksum is missing, invalid or does not match the input.",
        "DATA_SNAPSHOT": "Research snapshot is missing, damaged or incompatible; it was not replaced.",
        "DATA_LOOKBACK": "Not enough completed candles exist at the requested replay time.",
        "DATA_STALE": "A completed candle is missing at the requested replay time.",
    }
    raise DataError(code, messages[code])


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail()
            result[key] = value
        return result

    try:
        if len(raw) > MAX_BYTES:
            fail()
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: fail())
    except (UnicodeError, ValueError, RecursionError):
        fail()


def utc_us(value: str) -> int:
    try:
        parsed = parse_stamp(value)
        delta = parsed - EPOCH
        result = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        if not MIN_TIME <= result < MAX_TIME:
            fail("DATA_TIMESTAMP")
        return result
    except (ValueError, TypeError, OverflowError):
        fail("DATA_TIMESTAMP")


def wire_time(value, unit: str, *, csv_input=False) -> int:
    if csv_input and isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value):
        value = int(value)
    if type(value) is not int or unit not in {"ms", "us"}:
        fail("DATA_TIMESTAMP")
    result = value * (1000 if unit == "ms" else 1)
    if not MIN_TIME <= result < MAX_TIME:
        fail("DATA_TIMESTAMP")
    return result


def number(value, *, zero=False) -> Decimal:
    try:
        return amount(value, maximum=Decimal("1e18"), zero=zero)
    except (ValueError, TypeError):
        fail()


@dataclass(frozen=True)
class Candle:
    open_us: int
    end_us: int  # EXCLUSIVE close; available only at or after this instant
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int
    taker_base: Decimal
    taker_quote: Decimal

    def document(self):
        return {key: decimal_text(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}


@dataclass(frozen=True)
class History:
    candles: tuple[Candle, ...]
    excluded_unfinished: int

    def encoded(self):
        return b"".join(canonical(c.document()) for c in self.candles)


def read_rows(raw: bytes, format: str, checksum: str | None = None) -> list:
    if not raw or len(raw) > MAX_BYTES:
        fail()
    if format == "rest-json":
        rows = strict_json(raw)
    else:
        if format == "archive-zip":
            if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum) or digest(raw) != checksum:
                fail("DATA_CHECKSUM")
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    files = archive.infolist()
                    if len(files) != 1:
                        fail()
                    entry = files[0]
                    # Never extract filesystem paths. Limit decompressed bytes as well as input bytes.
                    if (not re.fullmatch(r"BTCUSDT-(1h|4h|1d)-[0-9]{4}-[0-9]{2}\.csv", entry.filename)
                            or entry.file_size > MAX_BYTES or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                        fail()
                    raw = archive.read(entry)
            except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, OSError):
                fail()
        elif format != "csv":
            fail()
        try:
            rows = []
            for row in csv.reader(io.StringIO(raw.decode("utf-8")), strict=True):
                if len(rows) >= MAX_ROWS:
                    fail()
                rows.append(row)
        except (UnicodeError, csv.Error):
            fail()
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        fail()
    return rows


def parse_history(raw: bytes, *, format: str, unit: str, interval: str,
                  start_us: int, end_us: int, as_of_us: int, checksum: str | None = None) -> History:
    width = INTERVALS.get(interval)
    if (width is None or any(type(t) is not int or not MIN_TIME <= t < MAX_TIME for t in (start_us, end_us, as_of_us))
            or start_us >= end_us or end_us > as_of_us or start_us % width or end_us % width
            or (end_us - start_us) // width > MAX_ROWS):
        fail("DATA_TIMESTAMP")
    rows = read_rows(raw, format, checksum)
    candles = []
    previous = None
    excluded = 0
    for row in rows:
        if not isinstance(row, list) or len(row) != 12:
            fail()
        opening = wire_time(row[0], unit, csv_input=format != "rest-json")
        close_wire = wire_time(row[6], unit, csv_input=format != "rest-json")
        ending = close_wire + (1000 if unit == "ms" else 1)
        if opening % width or ending - opening != width or opening > as_of_us:
            fail("DATA_TIMESTAMP")
        if previous is not None and opening != previous + width:
            fail("DATA_GAP")
        previous = opening
        prices = [number(row[n]) for n in (1, 2, 3, 4)]
        volumes = [number(row[n], zero=True) for n in (5, 7, 9, 10)]
        trades = row[8]
        if format != "rest-json" and isinstance(trades, str) and re.fullmatch(r"[0-9]{1,12}", trades):
            trades = int(trades)
        if type(trades) is not int or not 0 <= trades <= 10**12:
            fail()
        op, high, low, close = prices
        volume, quote_volume, taker_base, taker_quote = volumes
        if not low <= min(op, close) <= max(op, close) <= high or taker_base > volume or taker_quote > quote_volume:
            fail()
        if (volume == 0) != (quote_volume == 0) or (volume == 0) != (trades == 0):
            fail()
        candle = Candle(opening, ending, *prices, volume, quote_volume, trades, taker_base, taker_quote)
        if ending > as_of_us:
            if opening != end_us:
                fail("DATA_TIMESTAMP")
            excluded += 1
        elif opening < start_us or ending > end_us:
            fail()
        else:
            candles.append(candle)
    if not candles or candles[0].open_us != start_us or candles[-1].end_us != end_us:
        fail("DATA_GAP")
    return History(tuple(candles), excluded)


def aggregate_4h(candles: tuple[Candle, ...]) -> tuple[Candle, ...]:
    groups = {}
    width = INTERVALS["4h"]
    for candle in candles:
        groups.setdefault(candle.open_us // width, []).append(candle)
    result = []
    with localcontext() as ctx:
        ctx.prec = 60
        for key, group in groups.items():
            if len(group) != 4 or any(c.open_us != key * width + i * INTERVALS["1h"] or c.end_us - c.open_us != INTERVALS["1h"] for i, c in enumerate(group)):
                continue
            result.append(Candle(group[0].open_us, group[-1].end_us, group[0].open,
                                 max(c.high for c in group), min(c.low for c in group), group[-1].close,
                                 sum(c.volume for c in group), sum(c.quote_volume for c in group),
                                 sum(c.trades for c in group), sum(c.taker_base for c in group), sum(c.taker_quote for c in group)))
    return tuple(result)


def replay(history: History, *, interval: str, as_of_us: int, captured_as_of_us: int, lookback: int = 20) -> dict:
    if type(lookback) is not int or not 2 <= lookback <= 1000:
        fail()
    if type(as_of_us) is not int or not MIN_TIME <= as_of_us <= captured_as_of_us:
        fail("DATA_TIMESTAMP")
    visible = tuple(c for c in history.candles if c.end_us <= as_of_us)
    if len(visible) < lookback:
        fail("DATA_LOOKBACK")
    if as_of_us - visible[-1].end_us >= INTERVALS[interval]:
        fail("DATA_STALE")
    window = visible[-lookback:]
    with localcontext() as ctx:
        ctx.prec = 60
        features = {"last_close": decimal_text(window[-1].close),
                    "sma": decimal_text(sum(c.close for c in window) / lookback),
                    "window_return": decimal_text(window[-1].close / window[0].close - 1)}
    four = aggregate_4h(visible) if interval == "1h" else ()
    result = {"symbol": "BTCUSDT", "calendar": "BTC_UTC_24X7_NO_GAP_FILL", "interval": interval,
              "as_of_us": as_of_us, "available_candles": len(visible), "lookback": lookback,
              "window_start_us": window[0].open_us, "last_completed_end_us": window[-1].end_us,
              "features": features, "last_completed_4h": four[-1].document() if four else None,
              "availability_model": "Offline candle-end assumption; later provider revisions remain possible",
              "research_only": True, "execution_eligible": False, "entries_allowed": False}
    result["feature_sha256"] = digest(canonical(result))
    return result


def check_quote(document: dict, *, now_us: int, event_us: int | None = None,
                max_age_seconds=10, max_spread_bps=Decimal("10")) -> dict:
    if not isinstance(document, dict) or set(document) != {"symbol", "bidPrice", "bidQty", "askPrice", "askQty"} or document["symbol"] != "BTCUSDT":
        fail()
    bid, ask = number(document["bidPrice"]), number(document["askPrice"])
    number(document["bidQty"])
    number(document["askQty"])
    if bid > ask:
        fail()
    with localcontext() as ctx:
        ctx.prec = 60
        spread = (ask - bid) / ((ask + bid) / 2) * 10_000
    reasons = []
    if event_us is None:
        reasons.append("EXCHANGE_EVENT_TIMESTAMP_MISSING")
    elif type(event_us) is not int or event_us > now_us or event_us < MIN_TIME:
        reasons.append("QUOTE_TIMESTAMP_INVALID")
    elif now_us - event_us > max_age_seconds * 1_000_000:
        reasons.append("QUOTE_STALE")
    if spread > max_spread_bps:
        reasons.append("SPREAD_TOO_WIDE")
    return {"symbol": "BTCUSDT", "bid": decimal_text(bid), "ask": decimal_text(ask),
            "spread_bps": decimal_text(spread), "received_us": now_us, "event_us": event_us,
            "timestamp_basis": "receipt_only" if event_us is None else "exchange_event",
            "freshness_verified": not any(r != "SPREAD_TOO_WIDE" for r in reasons),
            "quality_passed": not reasons, "reasons": reasons, "research_only": True,
            "execution_eligible": False, "entries_allowed": False}
