from decimal import Decimal
import json
import unittest

from marvel.data.quality import (aggregate_4h, check_quote, digest, parse_history, replay,
                                 strict_json, utc_us)
from marvel.errors import DataError
from tests.data_helpers import HOUR, START, archive_bytes, csv_bytes, encoded, params, rows


class CandleQualityTests(unittest.TestCase):
    def parse(self, values=None, **changes):
        settings = {**params(), **changes}
        return parse_history(encoded(rows() if values is None else values), **settings)

    def test_ms_us_and_csv_normalize_identically(self):
        expected = self.parse()
        micros = parse_history(encoded(rows(unit="us")), **params(unit="us"))
        csv = parse_history(csv_bytes(rows()), **params(format="csv"))
        self.assertEqual(expected.encoded(), micros.encoded())
        self.assertEqual(expected.encoded(), csv.encoded())
        self.assertEqual(expected.candles[-1].end_us, START + 72 * HOUR)

    def test_wrong_mixed_and_boolean_timestamp_units_rejected(self):
        for values, unit in [(rows(), "us"), (rows(unit="us"), "ms"), (rows(), "seconds")]:
            with self.subTest(unit=unit), self.assertRaises(DataError):
                self.parse(values, unit=unit)
        for bad in [True, "1772841600000", 1772841600000.0]:
            values = rows()
            values[0][0] = bad
            with self.assertRaises(DataError):
                self.parse(values)
        values = rows()
        values[20][0] *= 1000
        with self.assertRaises(DataError):
            self.parse(values)

    def test_gap_duplicate_and_out_of_order_rejected(self):
        for mode in ("gap", "duplicate", "order"):
            values = rows()
            if mode == "gap":
                del values[10]
            elif mode == "duplicate":
                values.insert(10, values[10])
            else:
                values[10], values[11] = values[11], values[10]
            with self.subTest(mode=mode), self.assertRaisesRegex(DataError, "gap, duplicate"):
                self.parse(values)

    def test_missing_range_edges_rejected(self):
        for values in (rows()[1:], rows()[:-1]):
            with self.assertRaises(DataError):
                self.parse(values)

    def test_unfinished_last_candle_excluded_at_boundary(self):
        history = self.parse(rows(73))
        self.assertEqual(len(history.candles), 72)
        self.assertEqual(history.excluded_unfinished, 1)
        self.assertEqual(history.encoded(), self.parse().encoded())

    def test_future_and_invalid_close_time_and_alignment_rejected(self):
        for column, change in [(0, HOUR // 1000), (6, -1), (0, 1)]:
            values = rows()
            values[-1][column] += change
            with self.assertRaises(DataError):
                self.parse(values)
        with self.assertRaises(DataError):
            self.parse(rows(74))
        with self.assertRaises(DataError):
            self.parse(as_of_us=START + 71 * HOUR)

    def test_bad_ohlc_nonfinite_float_and_volume_rejected(self):
        for column, bad in [(1, "0"), (1, "NaN"), (1, "Infinity"), (1, "1e2"), (1, 100.0),
                            (2, "99"), (3, "150"), (5, "-1"), (9, "11"), (10, "1501"), (8, True), (8, -1)]:
            values = rows()
            values[10][column] = bad
            with self.subTest(column=column, bad=bad), self.assertRaises(DataError):
                self.parse(values)

    def test_shape_and_duplicate_json_and_unbounded_inputs_rejected(self):
        for raw in (b'[]', b'{}', b'[[1,2]]', b'{"a":1,"a":2}', b'[NaN]', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(DataError):
                parse_history(raw, **params())
        with self.assertRaises(DataError):
            strict_json(b" " * (16 * 1024 * 1024 + 1))

    def test_explicit_utc_dates_required(self):
        self.assertEqual(utc_us("2026-03-07T00:00:00Z"), START)
        for invalid in ("2026-03-07", "2026-03-07T00:00:00", "2026-03-07T05:00:00+05:00", "secret"):
            with self.assertRaises(DataError):
                utc_us(invalid)

    def test_archive_checksum_and_zip_path_validation(self):
        raw = archive_bytes(rows(unit="us"))
        settings = params(unit="us", format="archive-zip")
        actual = parse_history(raw, **{**settings, "checksum": digest(raw)})
        self.assertEqual(actual.encoded(), self.parse().encoded())
        for checksum in (None, "0" * 64):
            with self.assertRaises(DataError):
                parse_history(raw, **{**settings, "checksum": checksum})
        for name in ("../BTCUSDT-1h-2026-03.csv", "BTCUSDT.csv", "GOLD-1h-2026-03.csv"):
            malicious = archive_bytes(rows(), name)
            with self.assertRaises(DataError):
                parse_history(malicious, **{**settings, "checksum": digest(malicious)})

    def test_zero_volume_is_valid_but_zero_price_is_not(self):
        values = rows()
        for column in (5, 7, 9, 10):
            values[1][column] = "0"
        values[1][8] = 0
        self.assertEqual(len(self.parse(values).candles), 72)

    def test_contradictory_volume_and_trade_totals_rejected(self):
        for column in (5, 7, 8):
            values = rows()
            values[10][column] = 0 if column == 8 else "0"
            if column == 5:
                values[10][9] = "0"
            if column == 7:
                values[10][10] = "0"
            with self.subTest(column=column), self.assertRaises(DataError):
                self.parse(values)

    def test_invalid_deflate_stream_is_a_fixed_data_error(self):
        raw = bytearray(archive_bytes(rows()))
        # Corrupt the first DEFLATE byte, preserving the ZIP directory/header.
        offset = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")
        raw[offset] = 255
        raw = bytes(raw)
        with self.assertRaises(DataError):
            parse_history(raw, **{**params(format="archive-zip"), "checksum": digest(raw)})


class CausalReplayTests(unittest.TestCase):
    def history(self, values=None, **settings):
        return parse_history(encoded(rows() if values is None else values), **{**params(), **settings})

    def replay(self, history=None, **settings):
        return replay(history or self.history(), **{"interval": "1h", "as_of_us": START + 48 * HOUR,
                      "captured_as_of_us": START + 72 * HOUR, **settings})

    def test_repeat_replay_is_deterministic(self):
        first, second = self.replay(), self.replay()
        self.assertEqual(first, second)
        self.assertEqual(first["features"]["sma"], "138.5")
        self.assertEqual(first["available_candles"], 48)

    def test_future_changes_cannot_change_past_features_or_4h_context(self):
        original = self.replay()
        changed = rows()
        for row in changed[48:]:
            row[4] = "199"
        self.assertEqual(original, self.replay(self.history(changed)))

    def test_close_is_visible_only_at_exclusive_end(self):
        before = self.replay(as_of_us=START + 48 * HOUR - 1)
        after = self.replay()
        self.assertEqual(before["available_candles"], 47)
        self.assertEqual(after["available_candles"], 48)
        self.assertEqual(before["last_completed_4h"]["end_us"], START + 44 * HOUR)
        self.assertEqual(after["last_completed_4h"]["end_us"], START + 48 * HOUR)

    def test_short_stale_and_future_replay_rejected(self):
        for settings, code in [({"as_of_us": START + 19 * HOUR}, "DATA_LOOKBACK"),
                               ({"as_of_us": START + 73 * HOUR, "captured_as_of_us": START + 75 * HOUR}, "DATA_STALE"),
                               ({"as_of_us": START + 73 * HOUR}, "DATA_TIMESTAMP"), ({"lookback": True}, "DATA_INVALID")]:
            with self.subTest(code=code), self.assertRaises(DataError) as caught:
                self.replay(**settings)
            self.assertEqual(caught.exception.code, code)

    def test_partial_4h_groups_are_not_used(self):
        candles = self.history().candles[1:11]
        four = aggregate_4h(candles)
        self.assertEqual(len(four), 1)
        self.assertEqual(four[0].open_us, START + 4 * HOUR)
        self.assertEqual(four[0].end_us, START + 8 * HOUR)
        self.assertEqual(four[0].volume, Decimal("40"))

    def test_crypto_calendar_does_not_skip_weekends_dst_or_holidays(self):
        for start in ("2026-03-07T00:00:00Z", "2026-10-31T00:00:00Z", "2026-12-24T00:00:00Z"):
            beginning = utc_us(start)
            history = self.history(rows(start=beginning), **params(start=beginning))
            self.assertEqual(len(aggregate_4h(history.candles)), 18)
            self.assertEqual(history.candles[-1].end_us - history.candles[0].open_us, 72 * HOUR)

    def test_4h_and_daily_inputs_have_no_invented_hourly_context(self):
        for interval in ("4h", "1d"):
            settings = params(24, interval=interval)
            history = self.history(rows(24, interval=interval), **settings)
            result = replay(history, interval=interval, as_of_us=settings["end_us"], captured_as_of_us=settings["as_of_us"])
            self.assertIsNone(result["last_completed_4h"])
            self.assertFalse(result["execution_eligible"])


class QuoteQualityTests(unittest.TestCase):
    def quote(self, **changes):
        return {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.01", "bidQty": "10", "askQty": "10", **changes}

    def test_receipt_time_does_not_prove_quote_freshness(self):
        result = check_quote(self.quote(), now_us=START)
        self.assertEqual(result["reasons"], ["EXCHANGE_EVENT_TIMESTAMP_MISSING"])
        self.assertFalse(result["freshness_verified"])
        self.assertFalse(result["execution_eligible"])

    def test_timestamped_quote_boundaries_and_future_stale(self):
        self.assertTrue(check_quote(self.quote(), now_us=START, event_us=START - 10_000_000)["quality_passed"])
        for event, reason in [(START - 10_000_001, "QUOTE_STALE"), (START + 1, "QUOTE_TIMESTAMP_INVALID"), (True, "QUOTE_TIMESTAMP_INVALID")]:
            result = check_quote(self.quote(), now_us=START, event_us=event)
            self.assertIn(reason, result["reasons"])
            self.assertFalse(result["quality_passed"])

    def test_wide_spread_crossed_or_bad_book(self):
        self.assertIn("SPREAD_TOO_WIDE", check_quote(self.quote(askPrice="101"), now_us=START, event_us=START)["reasons"])
        for changes in ({"bidPrice": "102"}, {"askQty": "0"}, {"bidPrice": "NaN"}, {"symbol": "GOLD"}):
            with self.assertRaises(DataError):
                check_quote(self.quote(**changes), now_us=START)
