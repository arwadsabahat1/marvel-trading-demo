from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch

from marvel.errors import RiskError, StateError
from marvel.paper.store import PaperStore
from marvel.risk.engine import equity
from marvel.risk.model import canonical, load_profile
from tests.helpers import NOW, WorkspaceCase
from tests.risk_helpers import proposal, token


class PaperCase(WorkspaceCase):
    quote_fee = True

    def setUp(self):
        super().setUp()
        if self.quote_fee:
            path = self.root / "config/btc-paper-risk.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["entry_fee_asset"] = "USDT"
            path.write_text(json.dumps(document), encoding="utf-8")
        self.now = NOW
        self.paper = PaperStore(self.config(), clock=lambda: self.now)
        self.paper.risks.foundation.initialize()
        self.paper.change({"action": "init"})
        self.sequence = 0

    def state(self):
        return self.paper.risks.foundation.read().risk_state

    def book(self):
        return self.state()["paper_state"]

    def submit(self, identity="entry", quantity="0.09"):
        document = proposal(self.now, identity=identity, quantity=quantity)
        snapshot, result = self.paper.change({"action": "submit", "request": document})
        self.assertTrue(result["paper_submitted"], result)
        self.reconciles(snapshot.risk_state)
        return document["proposal"]["id"], result

    def event(self, kind, order=None, quantity=None, price="100", identity=None, quote=True):
        self.sequence += 1
        market = proposal(self.now, price=price)["market"] if quote else None
        event = {"id": token(identity or f"event-{self.sequence}"), "kind": kind, "order_id": order,
                 "quantity": quantity, "market": market}
        snapshot, result = self.paper.change({"action": "event", "event": event})
        self.reconciles(snapshot.risk_state)
        return event, result

    def reconciles(self, state=None):
        state = state or self.state()
        book = state["paper_state"]
        cost = sum((Decimal(c["entry_cost_usd"]) for c in book["cycles"]), Decimal(0))
        exits = sum((Decimal(c["exit_proceeds_usd"]) for c in book["cycles"]), Decimal(0))
        self.assertEqual(Decimal(book["cash_usd"]), Decimal(book["origin_cash_usd"]) - cost + exits)
        self.assertEqual(book["cash_usd"], state["cash_usd"])
        self.assertEqual(Decimal(state["cash_usd"]) + Decimal(state["position_value_usd"]), equity(state))
        fee = sum((Decimal(fill["fee_usd"]) for order in book["orders"] for fill in order["fills"]), Decimal(0))
        self.assertEqual(fee + Decimal(book["origin_fees_usd"]), Decimal(book["fees_usd"]))
        self.assertEqual(book["fees_usd"], state["total_fees_usd"])
        for cycle in book["cycles"]:
            bought = sum((Decimal(fill["quantity"]) - Decimal(fill["fee_btc"])
                          for order in book["orders"] if order["cycle_id"] == cycle["id"] and order["side"] == "BUY" for fill in order["fills"]), Decimal(0))
            sold = sum((Decimal(order["filled_quantity"]) for order in book["orders"] if order["cycle_id"] == cycle["id"] and order["side"] == "SELL"), Decimal(0))
            self.assertEqual(Decimal(cycle["holding_btc"]), bought - sold)
        for order in book["orders"]:
            self.assertEqual(Decimal(order["filled_quantity"]), sum((Decimal(f["quantity"]) for f in order["fills"]), Decimal(0)))

    def full_entry(self):
        identity, _ = self.submit()
        self.event("buy_fill", identity, "0.09")
        return identity


class ExecutionTests(PaperCase):
    def test_initialization_is_explicit_idempotent_and_does_not_reset_account(self):
        before = deepcopy(self.book())
        _, result = self.paper.change({"action": "init"})
        self.assertTrue(result["paper_initialization_reused"])
        self.assertEqual(self.book(), before)
        self.assertEqual(self.state()["cash_usd"], "50")

    def test_duplicate_submit_and_events_do_not_dispatch_or_charge_twice(self):
        document = proposal(self.now, quantity="0.09")
        operation = {"action": "submit", "request": document}
        self.paper.change(operation)
        _, reused = self.paper.change(operation)
        self.assertTrue(reused["paper_event_reused"])
        self.assertEqual(len(self.book()["orders"]), 1)
        event, _ = self.event("buy_fill", document["proposal"]["id"], "0.09")
        before = deepcopy(self.book())
        self.paper.change({"action": "event", "event": event})
        self.assertEqual(self.book(), before)
        self.reconciles()

    def test_conflicting_event_id_rolls_back(self):
        identity, _ = self.submit()
        event, _ = self.event("ack", identity)
        before = self.paper.risks.foundation.path.read_bytes()
        event["kind"] = "reject"
        with self.assertRaises(RiskError):
            self.paper.change({"action": "event", "event": event})
        self.assertEqual(self.paper.risks.foundation.path.read_bytes(), before)

    def test_timeout_recovery_restarts_without_resubmission_or_risk_release(self):
        identity, _ = self.submit()
        self.event("timeout", identity, quote=False)
        restarted = PaperStore(self.config(), clock=lambda: self.now)
        self.assertTrue(restarted.status()["recovery_required"])
        self.assertEqual(restarted.status()["risk"]["reservations"][0]["status"], "unknown")
        with self.assertRaises(RiskError):
            restarted.risks.change("release", {"id": identity})
        self.event("recover", identity)
        self.assertFalse(restarted.status()["recovery_required"])
        self.assertEqual(len(self.book()["orders"]), 1)
        self.assertEqual(self.book()["orders"][0]["status"], "open")
        self.assertEqual(self.state()["pending_entry_count"], 1)

    def test_partial_entry_then_full_fill_retains_original_risk(self):
        identity, _ = self.submit()
        reserve = self.state()["claims"][0]["plan"]["loss_reserved_usd"]
        self.event("buy_fill", identity, "0.06")
        self.assertEqual(self.book()["orders"][0]["status"], "partially_filled")
        self.assertEqual(self.state()["pending_entry_count"], 1)
        self.assertEqual(self.state()["position_count"], 1)
        self.assertEqual(self.state()["claims"][0]["plan"]["loss_reserved_usd"], reserve)
        self.event("buy_fill", identity, "0.03")
        self.assertEqual(self.book()["orders"][0]["status"], "filled")
        self.assertEqual(self.state()["pending_entry_count"], 0)
        self.assertEqual(self.book()["cycles"][0]["holding_btc"], "0.09")

    def test_reject_is_proven_no_fill_and_releases_only_once(self):
        identity, _ = self.submit()
        self.event("reject", identity)
        self.assertEqual(self.state()["claims"][0]["status"], "closed")
        self.assertEqual(self.state()["closed_trades"], [])
        self.assertEqual(self.book()["cash_usd"], "50")
        self.assertTrue(self.book()["cycles"][0]["settled"])

    def test_cancel_pending_can_fill_before_cancel_confirmation(self):
        identity, _ = self.submit()
        self.event("cancel", identity)
        self.event("buy_fill", identity, "0.09")
        _, result = self.event("cancelled", identity)
        self.assertTrue(result["cancel_lost_fill_race"])
        self.assertEqual(self.book()["orders"][0]["status"], "filled")
        self.assertEqual(self.state()["position_count"], 1)

    def test_cancelled_partial_entry_still_owns_position(self):
        identity, _ = self.submit()
        self.event("buy_fill", identity, "0.06")
        self.event("cancel", identity)
        self.event("cancelled", identity)
        self.assertEqual(self.state()["position_count"], 1)
        self.assertEqual(self.state()["pending_entry_count"], 0)
        self.assertFalse(self.book()["cycles"][0]["settled"])

    def test_partial_fill_during_cancellation_keeps_cancellation_pending(self):
        identity, _ = self.submit()
        self.event("cancel", identity)
        self.event("buy_fill", identity, "0.06")
        self.assertEqual(self.book()["orders"][0]["status"], "cancel_pending")
        self.event("cancelled", identity)
        self.assertEqual(self.book()["orders"][0]["status"], "cancelled")
        self.assertEqual(self.book()["cycles"][0]["holding_btc"], "0.06")

    def test_zero_fill_cancellation_does_not_count_as_trade(self):
        identity, _ = self.submit()
        self.event("cancel", identity)
        self.event("cancelled", identity)
        self.assertEqual(self.state()["position_count"], 0)
        self.assertEqual(self.state()["claims"][0]["status"], "closed")
        self.assertEqual(len(self.state()["closed_trades"]), 0)

    def test_partial_exit_then_completion_records_one_net_trade(self):
        identity = self.full_entry()
        _, closed = self.event("close", identity)
        exit_id = closed["exit_order_id"]
        self.event("sell_fill", exit_id, "0.04")
        self.assertEqual(self.book()["cycles"][0]["holding_btc"], "0.05")
        self.assertEqual(self.state()["closed_trades"], [])
        self.event("sell_fill", exit_id, "0.05")
        self.assertEqual(len(self.state()["closed_trades"]), 1)
        self.assertEqual(self.state()["unrealized_net_pnl_usd"], "0")
        self.assertEqual(self.state()["position_count"], 0)
        self.assertEqual(self.state()["losing_streak"], 1)
        self.assertEqual(self.state()["claims"][0]["status"], "closed")
        self.assertEqual(Decimal(self.state()["realized_net_pnl_usd"]), Decimal(self.book()["cash_usd"]) - 50)

    def test_stop_gap_fills_at_adverse_new_price_and_latches_loss_halts(self):
        self.full_entry()
        self.event("quote", price="60")
        exit_order = self.book()["orders"][-1]
        self.assertEqual(exit_order["fills"][0]["price"], "59.87")
        self.assertLess(Decimal(self.state()["cash_usd"]), Decimal("49"))
        self.assertIn("WEEKLY_LOSS", [item["code"] for item in self.state()["halts"]])
        self.assertIn("PEAK_DRAWDOWN", [item["code"] for item in self.state()["halts"]])
        self.assertEqual(len(self.state()["closed_trades"]), 1)
        self.assertEqual(self.state()["position_count"], 0)
        self.now += timedelta(days=1)
        restarted = PaperStore(self.config(), clock=lambda: self.now)
        self.assertIn("WEEKLY_LOSS", restarted.status()["risk"]["automatic_halt_reasons"])

    def test_stop_cancels_unfilled_entry_before_exiting_partial_fill(self):
        identity, _ = self.submit()
        self.event("buy_fill", identity, "0.06")
        self.event("quote", price="97")
        self.assertEqual(self.book()["orders"][0]["status"], "cancelled")
        self.assertEqual(self.state()["position_count"], 0)
        with self.assertRaises(RiskError):
            self.event("buy_fill", identity, "0.03")

    def test_manual_pause_cancels_pending_and_liquidates_on_next_fresh_event(self):
        identity, _ = self.submit()
        self.event("buy_fill", identity, "0.06")
        self.paper.risks.foundation.pause("operator")
        self.event("quote")
        self.assertEqual(self.book()["orders"][0]["status"], "cancelled")
        self.assertEqual(self.state()["position_count"], 0)
        self.assertTrue(self.paper.risks.foundation.read().halts)

    def test_minimums_never_force_partial_position_exit_upward(self):
        identity, _ = self.submit()
        self.event("buy_fill", identity, "0.01")
        self.event("quote", price="97")
        self.assertEqual(self.book()["cycles"][0]["holding_btc"], "0.01")
        self.assertIn("PAPER_LIQUIDATION_BLOCKED", [item["code"] for item in self.state()["halts"]])
        self.assertFalse(any(order["side"] == "SELL" for order in self.book()["orders"]))

    def test_oversell_overfill_off_step_and_fill_after_cancel_are_rejected_atomically(self):
        identity, _ = self.submit()
        for quantity in ("0.1", "0.000001", "-1", "NaN"):
            with self.subTest(quantity=quantity):
                before = deepcopy(self.book())
                with self.assertRaises(RiskError):
                    self.event("buy_fill", identity, quantity)
                self.assertEqual(self.book(), before)
        self.event("cancel", identity)
        self.event("cancelled", identity)
        with self.assertRaises(RiskError):
            self.event("buy_fill", identity, "0.09")

    def test_new_entry_cannot_bypass_single_position_or_manual_halt(self):
        self.full_entry()
        snapshot, result = self.paper.change({"action": "submit", "request": proposal(self.now, identity="other")})
        self.assertFalse(result["paper_submitted"])
        self.assertIn("POSITION_OR_PENDING_LIMIT", result["risk_decision"]["reasons"])
        self.assertEqual(len(snapshot.risk_state["paper_state"]["orders"]), 1)

    def test_stale_future_and_real_markets_do_not_change_ledger(self):
        identity = self.full_entry()
        for mutate in (lambda m: m.update(kind="rest"), lambda m: m.update(quote_at=(self.now + timedelta(seconds=1)).isoformat()),
                       lambda m: m.update(fx_at=(self.now - timedelta(seconds=11)).isoformat())):
            market = proposal(self.now)["market"]
            mutate(market)
            operation = {"action": "event", "event": {"id": token(str(market)), "kind": "quote", "order_id": None, "quantity": None, "market": market}}
            before = deepcopy(self.book())
            with self.assertRaises(RiskError):
                self.paper.change(operation)
            self.assertEqual(self.book(), before)

    def test_timeout_without_quote_does_not_refresh_account_mark(self):
        identity = self.full_entry()
        _, result = self.event("close", identity)
        marked = self.state()["last_accounting_at"]
        self.now += timedelta(seconds=70)
        self.event("timeout", result["exit_order_id"], quote=False)
        self.assertEqual(self.state()["last_accounting_at"], marked)
        self.assertTrue(self.paper.status()["recovery_required"])

    def test_rejected_new_submission_never_refreshes_existing_position_mark(self):
        self.full_entry()
        marked = self.state()["last_accounting_at"]
        self.now += timedelta(seconds=70)
        _, result = self.paper.change({"action": "submit", "request": proposal(self.now, identity="blocked")})
        self.assertFalse(result["paper_submitted"])
        self.assertEqual(self.state()["last_accounting_at"], marked)

    def test_direct_accounting_or_reservations_cannot_bypass_execution(self):
        for kind, data in (("reserve", proposal(self.now)), ("reconcile", {}), ("release", {"id": token("x")})):
            with self.assertRaises(RiskError):
                self.paper.risks.change(kind, data)

    def test_database_write_failure_rolls_back_order_cash_and_journal(self):
        identity, _ = self.submit()
        self.sql("CREATE TRIGGER reject_write BEFORE INSERT ON journal BEGIN SELECT RAISE(ABORT, 'FAIL'); END")
        before = deepcopy(self.book())
        revision = self.paper.risks.foundation.read().revision
        with self.assertRaises(StateError):
            self.event("buy_fill", identity, "0.09")
        self.assertEqual(self.book(), before)
        self.assertEqual(self.paper.risks.foundation.read().revision, revision)

    def test_tampered_fill_or_balance_fails_replay_instead_of_resetting(self):
        self.full_entry()
        state = self.state()
        state["paper_state"]["orders"][0]["fills"][0]["price"] = "1"
        self.sql("UPDATE risk_state SET document = ?", (canonical(state),))
        with self.assertRaises(RiskError):
            self.paper.status()

    def test_identical_scenarios_replay_to_identical_execution_and_risk(self):
        identity = self.full_entry()
        self.event("quote", price="97")
        before = deepcopy(self.state())
        for _ in range(3):
            restarted = PaperStore(self.config(), clock=lambda: self.now)
            self.assertEqual(restarted.risks.foundation.read().risk_state, before)
        self.reconciles(before)

    def test_no_socket_is_needed_for_execution(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.full_entry()
            self.event("quote", price="97")

    def test_exact_net_fees_and_cash_are_not_double_counted(self):
        identity = self.full_entry()
        _, result = self.event("close", identity)
        self.event("sell_fill", result["exit_order_id"], "0.09")
        self.assertEqual(self.state()["realized_net_pnl_usd"], "-0.0458901")
        self.assertEqual(self.state()["cash_usd"], "49.9541099")
        self.assertEqual(self.state()["total_fees_usd"], "0.0179901")

    def test_unobserved_midnight_latches_review_and_controls_exposure(self):
        self.full_entry()
        self.now += timedelta(days=1)
        self.event("quote")
        self.assertIn("PERIOD_REVIEW", self.paper.status()["risk"]["automatic_halt_reasons"])
        self.assertEqual(self.state()["position_count"], 0)

    def test_unknown_partial_fill_requires_fresh_quote_for_recovery(self):
        identity, _ = self.submit()
        self.event("buy_fill", identity, "0.06")
        self.event("timeout", identity, quote=False)
        with self.assertRaises(RiskError):
            self.event("recover", identity, quote=False)
        self.event("recover", identity)
        self.assertEqual(self.book()["orders"][0]["status"], "partially_filled")

    def test_rejection_after_a_fill_cannot_hide_the_holding(self):
        identity, _ = self.submit()
        self.event("buy_fill", identity, "0.06")
        before = deepcopy(self.book())
        with self.assertRaises(RiskError):
            self.event("reject", identity)
        self.assertEqual(self.book(), before)

    def test_profitable_close_ratchets_high_water_and_preserves_policy(self):
        identity = self.full_entry()
        policy_hash = self.config().fingerprint
        _, result = self.event("close", identity, price="103")
        self.event("sell_fill", result["exit_order_id"], "0.09", price="103")
        self.assertGreater(Decimal(self.state()["cash_usd"]), 50)
        self.assertEqual(self.state()["high_water_usd"], self.state()["cash_usd"])
        self.assertEqual(self.config().fingerprint, policy_hash)
        self.assertEqual(self.config().risk.per_trade_cap_usd, 1)

    def test_fill_uses_explicit_fx_bounds_instead_of_assuming_parity(self):
        document = proposal(self.now, quantity="0.09")
        document["market"].update(fx_lower="0.99", fx_upper="1.01")
        _, submitted = self.paper.change({"action": "submit", "request": document})
        self.assertTrue(submitted["paper_submitted"])
        identity = document["proposal"]["id"]
        for index, (kind, order_id, quantity) in enumerate((("buy_fill", identity, "0.09"), ("close", identity, None))):
            event = {"id": token(f"fx-{index}"), "kind": kind, "order_id": order_id, "quantity": quantity, "market": document["market"]}
            _, result = self.paper.change({"action": "event", "event": event})
        event.update(id=token("fx-exit"), kind="sell_fill", order_id=result["exit_order_id"], quantity="0.09")
        self.paper.change({"action": "event", "event": event})
        self.assertEqual(self.state()["cash_usd"], "49.774208621")
        self.reconciles()


class BaseFeeTests(PaperCase):
    quote_fee = False

    def test_base_fee_dust_is_retained_and_never_reported_flat(self):
        identity, _ = self.submit(quantity="0.09001")
        self.event("buy_fill", identity, "0.09001")
        self.event("quote", price="97")
        cycle = self.book()["cycles"][0]
        self.assertGreater(Decimal(cycle["holding_btc"]), 0)
        self.assertLess(Decimal(cycle["holding_btc"]), load_profile(self.root).n("exit_step"))
        self.assertFalse(cycle["settled"])
        self.assertEqual(self.state()["position_count"], 1)
        self.assertEqual(len(self.state()["closed_trades"]), 0)
        self.assertNotEqual(self.state()["claims"][0]["status"], "closed")
        self.assertIn("PAPER_LIQUIDATION_BLOCKED", self.paper.status()["risk"]["automatic_halt_reasons"])
        self.assertGreater(Decimal(self.book()["fees_usd"]), 0)

