from copy import deepcopy
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import Event
from unittest import TestCase

from marvel.clock import stamp
from marvel.data.quality import utc_us
from marvel.risk.model import fingerprint
from marvel_ops.common import Stop, number
from marvel_ops.execution import Trader
from marvel_ops.exits import entry_exit_check, exit_plan
from marvel_ops.forward import bars_from_rest, tick
from marvel_ops.learning import CARDS, Learner
from marvel_ops.service import Worker
from marvel_ops.store import Store
from marvel_ops.transport import Spot
from marvel_ops.validation import readiness, release, finalize
from tests.helpers import WorkspaceCase
from tests_testnet.fixtures import Exchange, NOW
from marvel_testnet.model import DemoError
from tests_testnet.test_adapter import Opener

CARD = next(iter(CARDS))
RULES = {"step": "0.00001", "minimum_qty": "0.00001", "maximum_qty": "1000",
         "minimum_quote": "5", "maximum_quote": "1000000", "base_quantum": "0.00000001"}


class Venue(Exchange):
    environment = "testnet"
    provenance = "synthetic"
    clock = staticmethod(lambda: 0)
    authorize = None

    def request(self, method, name, params=None):
        if method != "GET":
            self.authorize(method, params)
        try:
            return super().request(method, name, params)
        except DemoError as exc:
            raise Stop(exc.code, uncertain=exc.uncertain) from None


class PureTests(TestCase):
    def test_reject_nonfinite_and_float_money(self):
        for value in (float("inf"), "NaN", "Infinity", "1e2", True, "-1"):
            with self.subTest(value=str(value)), self.assertRaises(Stop):
                number(value)

    def test_btc_entry_fee_leaves_unowned_dust_not_a_complete_exit(self):
        plan = entry_exit_check("0.0001", "80000", "78400", RULES, "0.001", "BTC")
        self.assertFalse(plan["entry_allowed"])
        self.assertEqual(plan["residual"], "0.0000099")

    def test_quote_fee_or_zero_base_fee_allows_exact_full_lots(self):
        self.assertTrue(entry_exit_check("0.0001", "80000", "78400", RULES, "0.001", "USDT")["entry_allowed"])
        self.assertTrue(entry_exit_check("0.0001", "80000", "78400", RULES, "0", "BTC")["entry_allowed"])

    def test_partial_holding_under_minimum_is_retained_in_full(self):
        plan = exit_plan("0.00002", "80000", RULES)
        self.assertEqual(plan["quantity"], "0")
        self.assertEqual(plan["residual"], "0.00002")

    def test_plan_never_sells_more_than_owned(self):
        plan = exit_plan("0.00009999", "80000", RULES)
        self.assertEqual(plan["quantity"], "0.00009")
        self.assertFalse(plan["complete"])

    def test_learner_refuses_incomplete_fee_uncertain_outcome(self):
        with self.assertRaises(Stop):
            Learner().observe("a" * 64, CARD, "trend", "1", "10", complete=False, provenance="synthetic")

    def test_learner_no_positive_samples_means_cash(self):
        self.assertIsNone(Learner().select("trend", [CARD], provenance="synthetic"))

    def test_learner_requires_qualified_arm_and_matching_provenance(self):
        learner = Learner()
        for index in range(35):
            learner.observe(fingerprint(index), CARD, "trend", "0.02", "10", complete=True, provenance="synthetic")
        self.assertEqual(learner.select("trend", [CARD], provenance="synthetic"), CARD)
        self.assertIsNone(learner.select("trend", [], provenance="synthetic"))
        self.assertIsNone(learner.select("trend", [CARD], provenance="live"))

    def test_learner_deduplicates_completed_outcomes(self):
        learner = Learner()
        args = ("a" * 64, CARD, "range", "0.01", "10")
        self.assertTrue(learner.observe(*args, complete=True, provenance="testnet"))
        self.assertFalse(learner.observe(*args, complete=True, provenance="testnet"))

    def test_learning_adapts_to_net_losses_in_rolling_window(self):
        learner = Learner()
        for index in range(35):
            learner.observe(fingerprint(index), CARD, "range", "0.02", "10", complete=True, provenance="synthetic")
        self.assertIsNotNone(learner.select("range", [CARD], provenance="synthetic"))
        for index in range(35, 240):
            learner.observe(fingerprint(index), CARD, "range", "-0.02", "10", complete=True, provenance="synthetic")
        self.assertIsNone(learner.select("range", [CARD], provenance="synthetic"))

    def test_reviewed_champion_is_retired_after_sufficient_negative_feedback(self):
        learner = Learner()
        self.assertEqual(learner.champion("trend", [CARD], provenance="testnet"), CARD)
        for index in range(35):
            learner.observe(fingerprint(index), CARD, "trend", "-0.02", "10", complete=True, provenance="testnet")
        self.assertIsNone(learner.champion("trend", [CARD], provenance="testnet"))

    def test_transport_does_not_accept_arbitrary_hosts(self):
        for mode in ("paper", "https://other.invalid", "futures"):
            with self.assertRaises(Stop):
                Spot(mode, allow_network=True)

    def test_transport_requires_network_opt_in(self):
        with self.assertRaises(Stop):
            Spot("live")

    def test_transport_private_call_requires_separate_credentials(self):
        with self.assertRaises(Stop):
            Spot("live", allow_network=True).request("GET", "account")

    def test_transport_mutation_requires_durable_authorizer(self):
        client = Spot("live", allow_network=True, acknowledge_orders=True)
        with self.assertRaises(Stop):
            client.request("DELETE", "order", {"symbol": "BTCUSDT", "origClientOrderId": "mvo_" + "a" * 20 + "_entry"})

    def test_transport_rejects_withdrawals_and_cancel_all(self):
        for name in ("withdraw", "openOrders", "margin", "/sapi/v1/convert/acceptQuote"):
            with self.assertRaises(Stop):
                Spot("live", allow_network=True).request("POST", name, {})

    def test_transport_signatures_and_receipts_do_not_leak_credentials(self):
        opener = Opener([{"serverTime": int(NOW.timestamp() * 1000)}, {"ok": True}])
        client = Spot("testnet", allow_network=True, key="K" * 64, secret="S" * 64, opener=opener, monotonic=lambda: 0)
        client.request("GET", "time")
        client.request("GET", "account")
        saved = json.dumps(client.receipts)
        self.assertNotIn("K" * 64, saved)
        self.assertNotIn("signature=", saved)
        self.assertEqual(client.provenance, "synthetic")


class OpsTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.config()
        self.store = Store(self.cfg, "testnet")
        self.store.initialize(NOW)
        self.venue = Venue()
        self.trader = Trader(self.store, self.venue, now=lambda: NOW)

    def test_initialization_is_idempotent_and_separate_from_owner_account(self):
        state = self.store.load()
        self.assertEqual(state["revision"], self.store.initialize(NOW)["revision"])
        self.assertFalse(self.cfg.database.exists())

    def test_source_or_risk_changes_stop_existing_ledger(self):
        self.document["risk"]["per_trade_cap_usd"] = "0.9"
        self.write_config()
        with self.assertRaises(Stop):
            Store(self.config(), "testnet").load()

    def test_corrupted_journal_is_detected(self):
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("UPDATE events SET digest=? WHERE seq=1", ("0" * 64,))
        with self.assertRaises(Stop):
            self.store.load()

    def test_concurrent_worker_lock_is_rejected(self):
        with self.store.lock(), self.assertRaises(Stop):
            with self.store.lock():
                pass

    def test_backup_retains_consistent_journal(self):
        path = self.store.backup()
        with closing(sqlite3.connect(self.cfg.workspace / path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_evidence_is_immutable_and_deduplicated(self):
        record = {"kind": "fixture", "value": 1}
        self.assertEqual(self.store.record(record), self.store.record(record))
        self.assertEqual(self.store.evidence(), [record])

    def test_stale_heartbeat_does_not_claim_service_health(self):
        self.store.heartbeat("running", NOW)
        self.assertTrue(self.store.health(NOW)["healthy"])
        self.assertFalse(self.store.health(NOW + timedelta(minutes=3))["healthy"])

    def test_preflight_and_full_entry_stop_exit_cycle(self):
        self.assertTrue(self.trader.preflight("78400")["risk_check_passed"])
        self.trader.enter("78400", CARD)
        active = self.store.load()["active"]
        self.assertTrue(active["protected"])
        self.assertGreater(Decimal(active["owned_btc"]), 0)
        settled = self.trader.close()
        self.assertIsNone(settled["active"])
        self.assertEqual(len(settled["closed"]), 1)
        self.assertLess(Decimal(settled["equity_usd"]), 50)

    def test_settled_outcome_keeps_its_entry_regime(self):
        self.trader.enter("78400", CARD, regime_context="volatile")
        settled = self.trader.close()
        self.assertEqual(settled["closed"][0]["regime"], "volatile")

    def test_lost_entry_acknowledgement_is_not_a_duplicate_buy(self):
        self.venue.lost_entry_ack = True
        self.trader.enter("78400", CARD)
        buys = [o for o in self.venue.orders.values() if o["side"] == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertTrue(self.store.load()["active"]["protected"])

    def test_unknown_entry_keeps_claim_and_blocks_another_entry(self):
        self.venue.entry_missing = True
        with self.assertRaises(Stop):
            self.trader.enter("78400", CARD)
        self.assertIsNotNone(self.store.load()["active"])
        with self.assertRaises(Stop):
            self.trader.enter("78400", CARD)

    def test_btc_fee_minimum_grid_failure_prevents_buy(self):
        self.venue.rate = "0.001"
        plan = self.trader.preflight("78400")
        self.assertFalse(plan["risk_check_passed"])
        self.assertEqual(self.venue.orders, {})

    def test_unexpected_fee_asset_keeps_unresolved_owned_position(self):
        self.venue.charge_unexpected_bnb = True
        with self.assertRaises(Stop):
            self.trader.enter("78400", CARD)
        self.assertIsNotNone(self.store.load()["active"])
        self.assertIn("OPS_UNPRICED_FEE_ASSET", self.store.load()["halts"])

    def test_stop_fill_during_cancel_does_not_double_sell(self):
        self.trader.enter("78400", CARD)
        self.venue.stop_fill_on_cancel = True
        state = self.trader.close()
        self.assertIsNone(state["active"])
        self.assertFalse(any(o["clientOrderId"].endswith("_exit") for o in self.venue.orders.values()))

    def test_partial_stop_residual_under_minimum_stays_owned(self):
        self.trader.enter("78400", CARD)
        self.venue.partial_stop_on_cancel = True
        with self.assertRaises(Stop):
            self.trader.close()
        self.assertIsNotNone(self.store.load()["active"])
        self.assertGreater(Decimal(self.store.load()["active"]["owned_btc"]), 0)

    def test_cancel_uncertainty_does_not_create_an_extra_sell(self):
        self.trader.enter("78400", CARD)
        self.venue.stop_unknown_on_cancel = True
        with self.assertRaises(Stop):
            self.trader.close()
        self.assertFalse(any(o["clientOrderId"].endswith("_exit") for o in self.venue.orders.values()))

    def test_reconciliation_after_restart_never_submits(self):
        self.trader.enter("78400", CARD)
        calls = len(self.venue.calls)
        Trader(self.store, self.venue, now=lambda: NOW).reconcile()
        self.assertTrue(all(method == "GET" for method, *_ in self.venue.calls[calls:]))

    def test_external_faucet_inventory_is_not_used_as_strategy_ownership(self):
        self.trader.enter("78400", CARD)
        owned = Decimal(self.store.load()["active"]["owned_btc"])
        self.assertLess(owned, Decimal("0.001"))
        self.trader.close()
        self.assertEqual(self.venue.btc, Decimal(1))

    def test_outside_balance_change_requires_review(self):
        self.trader.enter("78400", CARD)
        self.venue.usdt += 1
        with self.assertRaises(Stop):
            self.trader.reconcile()
        self.assertIn("OPS_BALANCE_MISMATCH", self.store.load()["halts"])

    def test_unknown_order_cannot_be_cancelled(self):
        with self.assertRaises(Stop):
            self.trader.authorize("DELETE", {"origClientOrderId": "mvo_" + "a" * 20 + "_entry"})

    def test_live_entry_needs_evidence_before_any_account_request(self):
        self.venue.environment = "live"
        live = Store(self.cfg, "live")
        live.initialize(NOW)
        with self.assertRaises(Stop):
            Trader(live, self.venue, now=lambda: NOW).enter("78400", CARD)
        self.assertEqual(self.venue.calls, [])

    def test_manual_release_is_not_a_qualification_override(self):
        report = release(self.store, acknowledge=True, reviewed=True, now=NOW)
        self.assertFalse(report["live_entries_allowed"])
        self.assertIn("historical_strategy_and_holdout", report["blockers"])

    def test_synthetic_settlements_never_count_as_actual_testnet_evidence(self):
        self.trader.enter("78400", CARD)
        self.trader.close()
        self.assertEqual(readiness(self.store, now=NOW)["testnet_cycles"], 0)

    def test_holdout_requires_ack_and_frozen_successful_development(self):
        with self.assertRaises(Stop):
            finalize(self.store, "a" * 64)
        with self.assertRaises(Stop):
            finalize(self.store, "a" * 64, acknowledge_holdout=True)

    def test_worker_stops_gracefully_and_reports_final_heartbeat(self):
        calls = []
        stop = Event()
        def run():
            calls.append(1)
            stop.set()
        result = Worker(self.store, run, stop=stop, now=lambda: NOW).run(max_ticks=4)
        self.assertEqual(result["ticks"], 1)
        self.assertEqual(self.store.health(NOW)["status"], "stopped")

    def test_repeated_worker_errors_latch_a_review_halt(self):
        class NoWait(Event):
            def wait(self, *_):
                return False
        def broken():
            raise Stop("INVENTED_FAILURE")
        result = Worker(self.store, broken, stop=NoWait(), now=lambda: NOW).run(max_ticks=3)
        self.assertEqual(result["consecutive_failures"], 3)
        self.assertIn("OPS_REPEATED_WORKER_FAILURE", self.store.load()["halts"])

    def test_warmup_cannot_be_relabelled_a_forward_trade(self):
        from tests.test_forward import FixtureClient
        client = FixtureClient(allow_network=True, clock=lambda: NOW)
        end = utc_us(stamp(NOW)) // 3600000000 * 3600000000
        raw, _ = client.rest(interval="1h", start_us=end - 100 * 3600000000, end_us=end, as_of_us=end)
        bars = bars_from_rest(json.loads(raw), NOW)
        paper = Store(self.cfg, "paper")
        paper.initialize(NOW)
        result = tick(paper, bars, observed_at=NOW, provenance="synthetic")
        self.assertTrue(result["warmup_only"])
        self.assertEqual(readiness(paper, now=NOW)["forward_trades"], 0)

    def fixture_bars(self, when):
        from tests.test_forward import FixtureClient
        client = FixtureClient(allow_network=True, clock=lambda: when)
        hour = 3600000000
        end = utc_us(stamp(when)) // hour * hour
        raw, _ = client.rest(interval="1h", start_us=end - 100 * hour, end_us=end, as_of_us=end)
        return bars_from_rest(json.loads(raw), when)

    def test_forward_restart_keeps_virtual_account_and_no_capital_reset(self):
        paper = Store(self.cfg)
        paper.initialize(NOW)
        tick(paper, self.fixture_bars(NOW), observed_at=NOW, provenance="synthetic")
        next_time = NOW + timedelta(hours=1, seconds=20)
        result = tick(Store(self.cfg), self.fixture_bars(next_time), observed_at=next_time, provenance="synthetic")
        self.assertTrue(result["timely"])
        self.assertEqual(paper.load()["forward"]["started_at"], stamp(NOW))
        self.assertEqual(result["equity_usd"], "50")

    def test_late_forward_capture_is_not_timely(self):
        paper = Store(self.cfg)
        paper.initialize(NOW)
        tick(paper, self.fixture_bars(NOW), observed_at=NOW, provenance="synthetic")
        when = NOW + timedelta(hours=1, minutes=4)
        result = tick(paper, self.fixture_bars(when), observed_at=when, provenance="synthetic")
        self.assertFalse(result["timely"])
        self.assertFalse(result["entered"])

    def test_missed_forward_hour_is_retained_as_gap(self):
        paper = Store(self.cfg)
        paper.initialize(NOW)
        tick(paper, self.fixture_bars(NOW), observed_at=NOW, provenance="synthetic")
        when = NOW + timedelta(hours=2, seconds=10)
        result = tick(paper, self.fixture_bars(when), observed_at=when, provenance="synthetic")
        self.assertTrue(result["gap"])
        self.assertFalse(result["timely"])

    def test_duplicate_bar_creates_no_fresh_trade_or_observation(self):
        paper = Store(self.cfg)
        paper.initialize(NOW)
        bars = self.fixture_bars(NOW)
        tick(paper, bars, observed_at=NOW, provenance="synthetic")
        revision = paper.load()["revision"]
        self.assertTrue(tick(paper, bars, observed_at=NOW, provenance="synthetic")["duplicate"])
        self.assertEqual(paper.load()["revision"], revision)

    def test_forward_source_cannot_switch_synthetic_to_actual(self):
        paper = Store(self.cfg)
        paper.initialize(NOW)
        tick(paper, self.fixture_bars(NOW), observed_at=NOW, provenance="synthetic")
        with self.assertRaises(Stop):
            tick(paper, self.fixture_bars(NOW), observed_at=NOW, provenance="public_network")

    def test_gap_in_network_candles_is_rejected(self):
        from tests.test_forward import FixtureClient
        client = FixtureClient(allow_network=True, clock=lambda: NOW)
        end = utc_us(stamp(NOW))
        raw, _ = client.rest(interval="1h", start_us=end - 100 * 3600000000, end_us=end, as_of_us=end)
        rows = json.loads(raw)
        del rows[40]
        with self.assertRaises(Stop):
            bars_from_rest(rows, NOW)

    def test_worker_paper_step_uses_no_private_account_or_order_calls(self):
        from tests.test_forward import FixtureClient
        from marvel_ops.runtime import step
        fixture = FixtureClient(allow_network=True, clock=lambda: NOW)
        class Feed:
            provenance = "synthetic"
            def request(self, method, name, params=None):
                if name == "time":
                    return {"serverTime": int(NOW.timestamp() * 1000)}
                if name == "bars":
                    end = utc_us(stamp(NOW))
                    raw, _ = fixture.rest(interval="1h", start_us=end - 100 * 3600000000, end_us=end, as_of_us=end)
                    return json.loads(raw)
                raise AssertionError("Private endpoint must never be called")
        paper = Store(self.cfg)
        paper.initialize(NOW)
        self.assertTrue(step(paper, Feed, now=lambda: NOW)["warmup_only"])

    def test_stop_acceptance_failure_keeps_claim_and_halts_entries(self):
        self.venue.fail_protection = True
        with self.assertRaises(Stop):
            self.trader.enter("78400", CARD)
        state = self.store.load()
        self.assertIsNotNone(state["active"])
        self.assertTrue(state["halts"])
        self.assertFalse(state["active"]["protected"])

    def test_empty_entry_fill_does_not_create_testnet_fill_evidence(self):
        self.venue.entry_expired = True
        self.trader.enter("78400", CARD)
        self.assertIsNone(self.store.load()["active"])
        self.assertFalse(self.store.evidence()[0]["had_fill"])

    def test_deleted_middle_evidence_record_is_detected(self):
        for index in range(3):
            self.store.record({"kind": "fixture", "value": index})
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("DELETE FROM evidence WHERE seq=2")
        with self.assertRaises(Stop):
            self.store.evidence()
