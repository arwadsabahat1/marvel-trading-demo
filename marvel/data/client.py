"""Bounded public GETs to two fixed data-only hosts. No credentials or order API."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..clock import stamp, utc_now
from ..errors import DataError
from .quality import INTERVALS, MAX_BYTES, MAX_ROWS, canonical, check_quote, digest, fail, strict_json, utc_us, wire_time

API = "https://data-api.binance.vision/api/v3/"
ARCHIVE = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    def __init__(self, *, allow_network: bool, opener=None, sleeper=time.sleep, clock=utc_now):
        if allow_network is not True:
            raise DataError("NETWORK_OPT_IN_REQUIRED", "Public research downloads require an explicit --allow-network for this command.")
        self.opener = opener or build_opener(_NoRedirect())
        self.sleeper, self.clock = sleeper, clock
        self.requests = []

    def _get(self, url: str, *, limit=MAX_BYTES) -> bytes:
        if not (re.fullmatch(re.escape(API) + r"(?:time|exchangeInfo\?symbol=BTCUSDT|ticker/bookTicker\?symbol=BTCUSDT|klines\?symbol=BTCUSDT&interval=(?:1h|4h|1d)&startTime=[0-9]+&endTime=[0-9]+&limit=1000)", url)
                or re.fullmatch(re.escape(ARCHIVE) + r"(1h|4h|1d)/BTCUSDT-\1-[0-9]{4}-[0-9]{2}\.zip(?:\.CHECKSUM)?", url)):
            raise DataError("DATA_NETWORK", "Unsupported research endpoint was rejected before connecting.")
        for attempt in range(3):
            try:
                request = Request(url, headers={"User-Agent": "Marvel-personal-research/0.2", "Accept-Encoding": "identity"}, method="GET")
                with self.opener.open(request, timeout=10) as response:
                    if response.geturl() != url or response.status != 200:
                        raise URLError("Unexpected response")
                    raw = response.read(limit + 1)
                if not raw or len(raw) > limit:
                    fail()
                self.requests.append({"url": url, "received_at_utc": stamp(self.clock()), "sha256": digest(raw), "bytes": len(raw)})
                return raw
            except HTTPError as exc:
                code, retry = exc.code, exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if code not in {429, 500, 502, 503, 504} or attempt == 2:
                    break
                # If the server asks for a longer wait, stop rather than violate it or block indefinitely.
                delay = 1 + attempt
                if retry:
                    try:
                        requested = float(retry) if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", retry) else (parsedate_to_datetime(retry) - self.clock()).total_seconds()
                        if requested > 5:
                            break
                        delay = max(delay, requested)
                    except (ValueError, TypeError, OverflowError):
                        break
                self.sleeper(delay)
            except (OSError, URLError, HTTPException):
                if attempt == 2:
                    break
                self.sleeper(1 + attempt)
        raise DataError("DATA_NETWORK", "Public research request failed after bounded attempts; no snapshot was published.")

    def server_time(self) -> int:
        document = strict_json(self._get(API + "time", limit=4096))
        if not isinstance(document, dict) or set(document) != {"serverTime"}:
            fail("DATA_TIMESTAMP")
        result = wire_time(document["serverTime"], "ms")
        local = utc_us(stamp(self.clock()))
        if abs(local - result) > 30_000_000:
            fail("DATA_TIMESTAMP")
        return result

    def rest(self, *, interval: str, start_us: int, end_us: int, as_of_us: int):
        width = INTERVALS.get(interval)
        if width is None or start_us >= end_us or start_us % width or end_us % width or end_us > as_of_us or (end_us - start_us) // width > MAX_ROWS:
            fail("DATA_TIMESTAMP")
        rows, cursor = [], start_us
        while cursor < end_us:
            query = urlencode({"symbol": "BTCUSDT", "interval": interval, "startTime": cursor // 1000,
                               "endTime": end_us // 1000 - 1, "limit": 1000})
            page = strict_json(self._get(API + "klines?" + query))
            if not isinstance(page, list) or not 1 <= len(page) <= 1000:
                fail()
            for row in page:
                if not isinstance(row, list) or len(row) != 12 or wire_time(row[0], "ms") != cursor:
                    fail("DATA_GAP")
                rows.append(row)
                cursor += width
            if len(rows) > MAX_ROWS or cursor > end_us:
                fail()
        raw = canonical(rows)  # Reassembled JSON; per-response hashes preserve wire provenance.
        return raw, {"format": "rest-json", "unit": "ms", "interval": interval,
                     "start_us": start_us, "end_us": end_us, "as_of_us": as_of_us, "checksum": None}

    def archive(self, *, month: str, interval: str):
        if not isinstance(month, str) or not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", month) or interval not in INTERVALS:
            fail()
        year, number = map(int, month.split("-"))
        start = datetime(year, number, 1, tzinfo=timezone.utc)
        end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=timezone.utc)
        start_us, end_us, as_of_us = utc_us(stamp(start)), utc_us(stamp(end)), self.server_time()
        if end_us > as_of_us:
            fail("DATA_TIMESTAMP")
        basename = f"BTCUSDT-{interval}-{month}.zip"
        url = ARCHIVE + interval + "/" + basename
        check = self._get(url + ".CHECKSUM", limit=4096)
        try:
            matched = re.fullmatch(r"([a-f0-9]{64})[ \t]+\*?" + re.escape(basename) + r"\s*", check.decode("ascii"))
        except UnicodeError:
            fail("DATA_CHECKSUM")
        if not matched:
            fail("DATA_CHECKSUM")
        raw = self._get(url)
        if digest(raw) != matched[1]:
            fail("DATA_CHECKSUM")
        return raw, {"format": "archive-zip", "unit": "us" if month >= "2025-01" else "ms",
                     "interval": interval, "start_us": start_us, "end_us": end_us,
                     "as_of_us": as_of_us, "checksum": matched[1]}

    def observe(self, *, max_age_seconds, max_spread_bps):
        before = self.server_time()
        metadata = strict_json(self._get(API + "exchangeInfo?symbol=BTCUSDT"))
        if (not isinstance(metadata, dict) or metadata.get("timezone") != "UTC"
                or not isinstance(metadata.get("symbols"), list) or len(metadata["symbols"]) != 1):
            fail()
        symbol = metadata["symbols"][0]
        if (not isinstance(symbol, dict) or symbol.get("symbol") != "BTCUSDT" or symbol.get("status") != "TRADING"
                or symbol.get("isSpotTradingAllowed") is not True or symbol.get("baseAsset") != "BTC"
                or symbol.get("quoteAsset") != "USDT" or not isinstance(symbol.get("filters"), list)):
            fail()
        quote = strict_json(self._get(API + "ticker/bookTicker?symbol=BTCUSDT", limit=4096))
        received = utc_us(stamp(self.clock()))
        after = self.server_time()
        if after < before:
            fail("DATA_TIMESTAMP")
        quality = check_quote(quote, now_us=received, max_age_seconds=max_age_seconds, max_spread_bps=max_spread_bps)
        return {"symbol": "BTCUSDT", "server_before_us": before, "server_after_us": after,
                "metadata_observed_us": received, "metadata_point_in_time_for_history": False,
                "metadata": {key: symbol[key] for key in ("symbol", "baseAsset", "quoteAsset", "status", "isSpotTradingAllowed", "filters")},
                "quote": quality, "provenance": self.provenance()}

    def provenance(self):
        return {"origin": "public_https_get", "data_kind": "binance_spot", "requests": list(self.requests),
                "identity_limit": "TLS endpoint and SHA256 integrity; not a signed provider attestation"}
