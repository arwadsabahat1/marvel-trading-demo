from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import subprocess
import sys
from unittest.mock import patch

from marvel.clock import stamp
from marvel.data.client import API, PublicClient
from marvel.data.quality import check_quote, utc_us
from marvel.errors import DataError, MarvelError
from marvel.research import forward
from marvel.risk.model import canonical, fingerprint
from tests.helpers import ROOT, WorkspaceCase

START = datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)


class FixtureClient(PublicClient):
    """Invented data with synthetic provenance; every network method is replaced."""
    calls = []

    def receipt(self, path):
        self.calls.append(path)
        self.requests.append({"url": API + path, "received_at_utc": stamp(self.clock()), "sha256": "0" * 64, "bytes": 1})

    def server_time(self):
        self.receipt("time")
        return utc_us(stamp(self.clock()))

    def rest(self, *, interval, start_us, end_us, as_of_us):
        self.receipt("klines")
        width = forward.HOUR
        rows = [[t // 1000, "80000", "80025", "79975", "80000", "10", (t + width) // 1000 - 1,
            "800000", 20, "4", "320000", "0"] for t in range(start_us, end_us, width)]
        return json.dumps(rows).encode("ascii"), {"format": "rest-json", "unit": "ms", "interval": interval,
            "start_us": start_us, "end_us": end_us, "as_of_us": as_of_us, "checksum": None}

    def provenance(self):
        return {"origin": "local_unverified", "data_kind": "synthetic", "requests": list(self.requests)}

    def observe(self, **_):
        self.receipt("exchangeInfo?symbol=BTCUSDT")
        received = utc_us(stamp(self.clock()))
        quote = check_quote({"symbol": "BTCUSDT", "bidPrice": "79999", "askPrice": "80001", "bidQty": "10", "askQty": "10"}, now_us=received)
        return {"symbol": "BTCUSDT", "metadata_observed_us": received,
            "metadata": {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
                "isSpotTradingAllowed": True, "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {"filterType": "NOTIONAL", "minNotional": "5"}]}, "quote": quote, "provenance": self.provenance()}


class BrokenClient(FixtureClient):
    def server_time(self):
        raise DataError("DATA_NETWORK", "FIXTURE_SECRET_MUST_NOT_APPEAR")


class ForwardTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.config()
        self.identity = forward.start(self.cfg, now=START)["session_id"]
        self.store = forward.ForwardStore(self.root)
        FixtureClient.calls = []

    def capture(self, when=START, client=FixtureClient):
        return forward.capture(self.cfg, self.identity, allow_network=True, clock=lambda: when, client_factory=client)

    def test_protocol_is_frozen_before_network_and_binds_source_financial_files_and_rules(self):
        plan = self.store.protocol(self.identity)
        self.assertEqual(plan["warmup_hours"], 528)
        self.assertEqual(len(plan["strategies"]), 6)
        self.assertEqual(plan["first_eligible_bar_end_us"], utc_us("2026-10-28T13:00:00Z"))
        self.assertFalse(self.cfg.database.exists())
        self.assertEqual(FixtureClient.calls, [])
        self.assertTrue(forward.bindings_match(plan, self.cfg))

    def test_start_before_safe_warmup_cannot_read_reserved_samples(self):
        with self.assertRaises(MarvelError):
            forward.start(self.cfg, now=datetime(2026, 10, 10, tzinfo=timezone.utc))

    def test_warmup_capture_cannot_be_claimed_as_a_forward_signal_or_trade(self):
        result = self.capture()
        self.assertEqual(result["captures"], 1)
        self.assertEqual(result["eligible_signal_captures"], 0)
        self.assertIn("PRE_START_WARMUP_ONLY", result["last_capture"]["eligibility_reasons"])
        self.assertEqual(len(result["last_capture"]["signals"]), 6)
        self.assertFalse(result["actual_route_qualified"])
        self.assertEqual(result["completed_paper_trades"], 0)
        self.assertFalse(self.cfg.database.exists())

    def test_only_timely_new_completed_bars_are_eligible(self):
        self.capture()
        result = self.capture(START.replace(hour=13, minute=0, second=20))
        self.assertEqual(result["eligible_signal_captures"], 1)
        self.assertEqual(result["last_capture"]["bar_end_us"], utc_us("2026-10-28T13:00:00Z"))
        self.assertEqual(result["last_capture"]["eligibility_reasons"], [])
        self.assertEqual(result["qualifying_strategy_trades"], 0)
        self.assertIsNone(result["last_capture"]["route"]["largest_full_fill_grid_aligned_gross_btc_within_cap"])

    def test_late_and_missed_hours_are_retained_without_backfilled_decisions(self):
        self.capture()
        result = self.capture(START.replace(hour=15, minute=5))
        self.assertEqual(result["captures"], 2)
        self.assertEqual(result["missed_hours"], 2)
        self.assertEqual(result["eligible_signal_captures"], 0)
        self.assertIn("LATE_CAPTURE_NOT_A_TIMELY_DECISION", result["last_capture"]["eligibility_reasons"])

    def test_slow_capture_crossing_the_deadline_is_classified_by_its_final_receipt(self):
        clock = [START.replace(hour=13, minute=0, second=20)]
        original = forward.route_report
        def slow(*args):
            clock[0] += timedelta(minutes=3)
            return original(*args)
        with patch("marvel.research.forward.route_report", side_effect=slow):
            result = forward.capture(self.cfg, self.identity, allow_network=True,
                clock=lambda: clock[0], client_factory=FixtureClient)
        self.assertFalse(result["last_capture"]["eligible_prospective_signal"])
        self.assertIn("LATE_CAPTURE_NOT_A_TIMELY_DECISION", result["last_capture"]["eligibility_reasons"])

    def test_same_hour_repeat_makes_no_more_public_calls(self):
        self.capture()
        before = list(FixtureClient.calls)
        result = self.capture(START + timedelta(minutes=5))
        self.assertEqual(result["capture_skipped"], "ALREADY_ATTEMPTED_THIS_UTC_HOUR")
        self.assertEqual(FixtureClient.calls, before)

    def test_opt_in_is_required_before_any_external_request(self):
        with self.assertRaises(MarvelError) as caught:
            forward.capture(self.cfg, self.identity, client_factory=FixtureClient)
        self.assertEqual(caught.exception.code, "NETWORK_OPT_IN_REQUIRED")
        self.assertEqual(FixtureClient.calls, [])

    def test_capture_failures_are_sanitized_and_three_failures_stop_collection(self):
        for i in range(3):
            result = self.capture(START + timedelta(hours=i), BrokenClient)
        self.assertFalse(result["collection_allowed"])
        self.assertEqual(result["consecutive_capture_failures"], 3)
        self.assertEqual(result["last_capture"]["failure_code"], "DATA_NETWORK")
        self.assertNotIn("FIXTURE_SECRET", canonical(self.store.records(self.identity)))
        with self.assertRaises(MarvelError):
            self.capture(START + timedelta(hours=4))

    def test_changed_cached_configuration_is_detected_from_the_file(self):
        self.document["risk"]["per_trade_cap_usd"] = "0.5"
        self.write_config()
        result = forward.status(self.cfg, self.identity, now=START)
        self.assertFalse(result["bindings_match"])
        with self.assertRaises(MarvelError):
            self.capture()
        self.assertEqual(FixtureClient.calls, [])

    def test_profile_or_source_change_stops_collection_before_network(self):
        with patch("marvel.research.forward.source_hashes", return_value={"changed": "0" * 64}):
            with self.assertRaises(MarvelError):
                self.capture()
        path = self.root / "config/btc-paper-risk.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(MarvelError):
            self.capture()
        self.assertEqual(FixtureClient.calls, [])

    def test_source_change_during_capture_prevents_publishing_a_misattributed_record(self):
        with patch("marvel.research.forward.bindings_match", side_effect=[True, False]):
            with self.assertRaises(MarvelError):
                self.capture()
        self.assertEqual(self.store.records(self.identity), [])

    def test_manual_stop_is_persistent_and_watch_stops_without_network(self):
        forward.stop(self.cfg, self.identity, now=START)
        result = forward.watch(self.cfg, self.identity, allow_network=True, clock=lambda: START,
            sleeper=lambda _: self.fail("A stopped worker should not sleep"))
        self.assertTrue(result["stopped"])
        with self.assertRaises(MarvelError):
            self.capture()
        self.assertEqual(FixtureClient.calls, [])

    def test_expiry_and_clock_regression_block_before_network(self):
        for when in (START - timedelta(seconds=1), START + timedelta(days=30)):
            with self.assertRaises(MarvelError):
                self.capture(when)
        self.assertEqual(FixtureClient.calls, [])

    def test_concurrent_worker_cannot_take_the_same_capture_lock(self):
        with self.store.locked(self.identity):
            with self.assertRaises(MarvelError):
                with self.store.locked(self.identity):
                    self.fail("Duplicate worker acquired capture lock")

    def test_tampered_record_and_rehashed_warmup_qualification_are_rejected(self):
        self.capture()
        path = self.store.folder(self.identity) / "captures/000001.json"
        original = json.loads(path.read_text())
        document = deepcopy(original)
        document["eligibility_reasons"] = []
        document["eligible_prospective_signal"] = True
        document["record_id"] = fingerprint({k: v for k, v in document.items() if k != "record_id"})
        path.write_text(canonical(document))
        with self.assertRaises(MarvelError):
            self.store.records(self.identity)
        path.write_bytes(b"damaged")
        with self.assertRaises(MarvelError):
            self.capture(START + timedelta(hours=1))
        self.assertEqual(path.read_bytes(), b"damaged")

    def test_missing_middle_record_and_rehashed_paper_trade_claim_are_rejected(self):
        self.capture()
        self.capture(START + timedelta(hours=1))
        path = self.store.folder(self.identity) / "captures/000001.json"
        document = json.loads(path.read_text())
        document["completed_paper_trades"] = 1
        document["record_id"] = fingerprint({k: v for k, v in document.items() if k != "record_id"})
        path.write_text(canonical(document))
        with self.assertRaises(MarvelError):
            self.store.records(self.identity)
        path.unlink()
        with self.assertRaises(MarvelError):
            self.store.records(self.identity)

    def test_invalid_identifier_or_plan_cannot_be_repaired_by_collection(self):
        with self.assertRaises(MarvelError):
            forward.status(self.cfg, "../outside", now=START)
        path = self.store.root / "plans" / (self.identity + ".json")
        path.write_bytes(b"damaged")
        with self.assertRaises(MarvelError):
            self.capture()
        self.assertEqual(path.read_bytes(), b"damaged")


class ForwardCommandTests(WorkspaceCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "marvel", "--workspace", str(self.root), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=30)

    def test_start_status_stop_preserve_existing_owner_account(self):
        self.assertEqual(self.cli("init").returncode, 0)
        before = sha256(self.config().database.read_bytes()).hexdigest()
        started = self.cli("research", "forward-start")
        self.assertEqual(started.returncode, 0, started.stdout)
        identity = json.loads(started.stdout)["forward_research"]["session_id"]
        self.assertEqual(self.cli("research", "forward-status", "--session", identity).returncode, 0)
        self.assertEqual(self.cli("research", "forward-capture", "--session", identity).returncode, 2)
        self.assertEqual(self.cli("research", "forward-stop", "--session", identity).returncode, 0)
        self.assertEqual(sha256(self.config().database.read_bytes()).hexdigest(), before)

    def test_no_live_fee_risk_or_backfill_overrides(self):
        for flag in ("--live", "--fee", "--risk", "--backfill", "--holdout"):
            self.assertEqual(self.cli("research", "forward-start", flag).returncode, 2)
        self.assertFalse(self.config().database.exists())
