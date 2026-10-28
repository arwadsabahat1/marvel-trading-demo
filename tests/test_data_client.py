from datetime import datetime, timezone
from email.message import Message
import io
import json
import unittest
from urllib.error import HTTPError, URLError

from marvel.data.client import API, ARCHIVE, PublicClient
from marvel.data.quality import digest, parse_history
from marvel.errors import DataError
from tests.data_helpers import HOUR, START, archive_bytes, encoded, rows

NOW = datetime(2026, 3, 10, tzinfo=timezone.utc)
NOW_US = START + 72 * HOUR


class Response(io.BytesIO):
    def __init__(self, raw, url):
        super().__init__(raw)
        self.url, self.status = url, 200

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Response(reply, request.full_url)


def server(us=NOW_US):
    return encoded({"serverTime": us // 1000})


class PublicClientTests(unittest.TestCase):
    def client(self, *replies):
        opener, sleeps = Opener(*replies), []
        client = PublicClient(allow_network=True, opener=opener, sleeper=sleeps.append, clock=lambda: NOW)
        return client, opener, sleeps

    def test_network_requires_explicit_boolean_opt_in(self):
        for value in (False, None, 1):
            with self.assertRaises(DataError) as caught:
                PublicClient(allow_network=value)
            self.assertEqual(caught.exception.code, "NETWORK_OPT_IN_REQUIRED")

    def test_fixed_endpoints_get_only_no_credentials(self):
        client, opener, _ = self.client(server())
        self.assertEqual(client.server_time(), NOW_US)
        request, timeout = opener.calls[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(timeout, 10)
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("X-mbx-apikey", request.headers)
        for url in ("https://example.com/", API + "order", API + "time?apiKey=SECRET", API + "exchangeInfo?symbol=GOLD"):
            with self.assertRaises(DataError):
                client._get(url)
        self.assertEqual(len(opener.calls), 1)

    def test_pagination_exceeds_one_page_without_overlap(self):
        values = rows(1001)
        client, opener, _ = self.client(encoded(values[:1000]), encoded(values[1000:]))
        end = START + 1001 * HOUR
        raw, parameters = client.rest(interval="1h", start_us=START, end_us=end, as_of_us=end)
        self.assertEqual(len(parse_history(raw, **parameters).candles), 1001)
        self.assertEqual(len(opener.calls), 2)
        self.assertIn("startTime=" + str((START + 1000 * HOUR) // 1000), opener.calls[1][0].full_url)
        self.assertEqual(len(client.provenance()["requests"]), 2)

    def test_stuck_empty_and_overlapping_pages_fail(self):
        for pages in ([encoded([])], [encoded(rows(2)), encoded(rows(1))]):
            client, opener, _ = self.client(*pages)
            with self.assertRaises(DataError):
                client.rest(interval="1h", start_us=START, end_us=START + 3 * HOUR, as_of_us=NOW_US)
            self.assertLessEqual(len(opener.calls), 2)

    def test_archive_checks_checksum_filename_hash_and_timestamp_unit(self):
        raw = archive_bytes(rows(unit="us"))
        checksum = (digest(raw) + "  BTCUSDT-1h-2026-03.zip\n").encode("ascii")
        future = datetime(2026, 4, 2, tzinfo=timezone.utc)
        future_us = int(future.timestamp()) * 1_000_000
        client, opener, _ = self.client(server(future_us), checksum, raw)
        client.clock = lambda: future
        payload, settings = client.archive(month="2026-03", interval="1h")
        self.assertEqual(settings["unit"], "us")
        self.assertEqual(settings["checksum"], digest(payload))
        self.assertEqual(opener.calls[-1][0].full_url, ARCHIVE + "1h/BTCUSDT-1h-2026-03.zip")
        bad, _, _ = self.client(server(future_us), b"0" * 64 + b"  WRONG.zip", raw)
        bad.clock = lambda: future
        with self.assertRaises(DataError):
            bad.archive(month="2026-03", interval="1h")

    def test_current_or_invalid_archive_month_does_not_download(self):
        for month in ("2026-03", "2026-13", "../secret"):
            client, opener, _ = self.client(server())
            with self.assertRaises(DataError):
                client.archive(month=month, interval="1h")
            self.assertLessEqual(len(opener.calls), 1)

    def test_rate_limit_backoff_is_bounded_and_honors_long_wait_by_stopping(self):
        headers = Message()
        headers["Retry-After"] = "2"
        error = HTTPError(API + "time", 429, "SECRET_RATE_LIMIT", headers, io.BytesIO())
        client, opener, sleeps = self.client(error, server())
        client.server_time()
        self.assertEqual(sleeps, [2])
        headers = Message()
        headers["Retry-After"] = "60"
        error = HTTPError(API + "time", 429, "SECRET_RATE_LIMIT", headers, io.BytesIO())
        client, opener, sleeps = self.client(error)
        with self.assertRaises(DataError) as caught:
            client.server_time()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(sleeps, [])
        self.assertEqual(len(opener.calls), 1)

    def test_network_failure_redacted_and_attempts_bounded(self):
        client, opener, sleeps = self.client(*(URLError("SECRET_CONNECT_FAILURE") for _ in range(3)))
        with self.assertRaises(DataError) as caught:
            client.server_time()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(sleeps, [1, 2])

    def test_large_response_and_clock_skew_rejected(self):
        for reply in (b" " * 4097, server(NOW_US - 31_000_000), b'{"serverTime":true}'):
            client, _, _ = self.client(reply)
            with self.assertRaises(DataError):
                client.server_time()

    def test_redirect_is_not_followed(self):
        from marvel.data.client import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com/"))

    def test_observed_metadata_is_current_only_and_quote_stays_unqualified(self):
        metadata = {"timezone": "UTC", "symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
                    "status": "TRADING", "isSpotTradingAllowed": True, "filters": [{"filterType": "LOT_SIZE", "minQty": "0.00001"}]}]}
        quote = {"symbol": "BTCUSDT", "bidPrice": "100", "bidQty": "10", "askPrice": "100.01", "askQty": "10"}
        client, _, _ = self.client(server(), encoded(metadata), encoded(quote), server())
        result = client.observe(max_age_seconds=10, max_spread_bps=10)
        self.assertFalse(result["metadata_point_in_time_for_history"])
        self.assertFalse(result["quote"]["quality_passed"])
        self.assertFalse(result["quote"]["execution_eligible"])
        self.assertEqual(len(client.requests), 4)
