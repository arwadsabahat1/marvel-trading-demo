from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

from marvel.errors import RiskError, StateError
from marvel.risk.engine import active_halts
from marvel.risk.model import canonical, load_profile
from marvel.risk.store import RiskStore
from tests.helpers import NOW
from tests.risk_helpers import RiskCase, proposal, token


class ReservationTests(RiskCase):
    def test_submission_bookkeeping_requires_fresh_market_recheck(self):
        claim, _ = self.reserve()
        with self.assertRaises(RiskError):
            self.risks.change("submitted", {"id": claim})
        self.now += timedelta(seconds=11)
        with self.assertRaises(RiskError):
            self.risks.change("submitted", {"id": claim, "market": proposal(NOW)["market"]})
        self.assertEqual(self.current()["claims"][0]["status"], "reserved")
    def test_reservation_persists_and_counts_toward_cash_risk_and_one_position(self):
        claim, plan = self.reserve()
        restarted = RiskStore(self.config(), clock=lambda: self.now)
        status = restarted.status()
        self.assertEqual(status["reservations"][0]["id"], claim)
        self.assertEqual(status["budgets"]["reserved_loss_usd"], plan["loss_reserved_usd"])
        self.assertGreater(Decimal(status["budgets"]["reserved_cash_usd"]), 0)
        self.assertIn("POSITION_OR_PENDING_LIMIT", restarted.check(proposal(identity="second"))["reasons"])
        self.assertEqual(restarted.foundation.read().equity_usd, Decimal("50"))

    def test_duplicate_reservation_reuses_one_claim_without_new_permission(self):
        document = proposal()
        self.risks.change("reserve", document)
        _, result = self.risks.change("reserve", document)
        self.assertTrue(result["reservation_reused"])
        self.assertFalse(result["risk_check_passed"])
        self.assertEqual(len(self.current()["claims"]), 1)

    def test_reused_identifier_with_changed_inputs_and_after_release_rejected(self):
        claim, _ = self.reserve()
        before = self.risks.foundation.read()
        changed = proposal(stop="97")
        with self.assertRaises(RiskError):
            self.risks.change("reserve", changed)
        self.assertEqual(before, self.risks.foundation.read())
        self.risks.change("release", {"id": claim})
        with self.assertRaises(RiskError):
            self.risks.change("reserve", proposal())

    def test_unsent_release_frees_budget_without_changing_equity(self):
        claim, _ = self.reserve()
        self.risks.change("release", {"id": claim})
        self.assertEqual(self.risks.status()["budgets"]["reserved_loss_usd"], "0")
        self.assertTrue(self.risks.check(proposal(identity="next"))["risk_check_passed"])
        self.assertEqual(self.risks.foundation.read().equity_usd, Decimal("50"))

    def test_unknown_or_submitted_order_is_not_released_until_confirmed_flat(self):
        claim, _ = self.submitted()
        for operation in ("release", "unknown"):
            if operation == "release":
                with self.assertRaises(RiskError):
                    self.risks.change(operation, {"id": claim})
            else:
                self.risks.change(operation, {"id": claim})
        with self.assertRaises(RiskError):
            self.risks.change("release", {"id": claim})
        report = self.report(identity="flat-confirmed")
        report["confirmed_flat_ids"] = [claim]
        report["no_fill_ids"] = [claim]
        self.risks.change("reconcile", report)
        self.assertEqual(self.current()["claims"][0]["status"], "closed")
        self.assertEqual(self.risks.status()["budgets"]["reserved_loss_usd"], "0")

    def test_concurrent_requests_cannot_both_reserve_the_portfolio(self):
        def reserve(identity):
            return RiskStore(self.config(), clock=lambda: NOW).change("reserve", proposal(identity=identity))[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ["one", "two"]))
        self.assertEqual(sum(result["risk_check_passed"] for result in results), 1)
        self.assertEqual(len(self.current()["claims"]), 1)

    def test_manual_halt_blocks_proposal_and_cannot_be_cleared_by_risk_operations(self):
        self.risks.foundation.pause("operator")
        _, result = self.risks.change("reserve", proposal())
        self.assertIn("MANUAL_HALT", result["reasons"])
        self.risks.change("sync", {})
        self.assertEqual(self.risks.foundation.read().halts, ("operator",))

    def test_failed_audit_write_rolls_back_cash_claim_and_revision(self):
        before = self.risks.foundation.read()
        self.sql("""CREATE TRIGGER reject_risk BEFORE INSERT ON journal
            WHEN NEW.event = 'risk_updated' BEGIN SELECT RAISE(ABORT, 'SECRET_WRITE_ERROR'); END""")
        with self.assertRaises(StateError):
            self.risks.change("reserve", proposal())
        self.assertEqual(self.risks.foundation.read(), before)

    def test_clock_regression_against_a_later_manual_pause_is_rejected_explicitly(self):
        self.store(lambda: NOW + timedelta(seconds=1)).pause("operator")
        with self.assertRaises(StateError) as caught:
            self.risks.change("sync", {})
        self.assertEqual(caught.exception.code, "CLOCK_REGRESSION")

    def test_risk_profile_and_risk_state_tampering_rejected_without_reset(self):
        path = self.root / "config/btc-paper-risk.json"
        original = path.read_bytes()
        changed = json.loads(original)
        changed["entry_fee_rate"] = "0"
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(RiskError) as caught:
            self.risks.foundation.read()
        self.assertEqual(caught.exception.code, "RISK_PROFILE_MISMATCH")
        path.write_bytes(original)
        state = self.current()
        state["cash_usd"] = "100"
        self.sql("UPDATE risk_state SET document = ?", (canonical(state),))
        with self.assertRaises(RiskError):
            self.risks.foundation.read()

    def test_deleting_risk_operation_from_journal_is_detected(self):
        self.reserve()
        self.sql("DELETE FROM journal WHERE event = 'risk_updated'")
        with self.assertRaises((RiskError, StateError)):
            self.risks.foundation.read()


class FinancialBreakerTests(RiskCase):
    def test_monday_cannot_reset_losses_inside_the_rolling_seven_day_window(self):
        self.close_trade("-0.6", "friday")
        self.now += timedelta(days=3)
        self.close_trade("-0.5", "monday")
        self.assertIn("WEEKLY_LOSS", active_halts(self.current()))
        self.assertEqual(self.risks.foundation.read().equity_usd, Decimal("48.9"))
    def test_tighter_weekly_loss_stops_before_daily_limit(self):
        self.close_trade("-1")
        state = self.current()
        self.assertIn("WEEKLY_LOSS", active_halts(state))
        self.assertNotIn("DAILY_LOSS", active_halts(state))
        self.assertEqual(self.risks.foundation.read().equity_usd, Decimal("49"))
        self.assertFalse(self.risks.check(proposal(identity="next"))["risk_check_passed"])

    def test_daily_and_drawdown_halts_survive_restart_and_flat_recovery(self):
        self.close_trade("-5")  # A gap can lose more than planned risk; record the real loss.
        restarted = RiskStore(self.config(), clock=lambda: self.now)
        self.assertTrue({"DAILY_LOSS", "WEEKLY_LOSS", "PEAK_DRAWDOWN"}.issubset(restarted.status()["automatic_halt_reasons"]))
        restarted.change("reconcile", self.report(identity="still-flat"))
        self.assertIn("PEAK_DRAWDOWN", restarted.status()["automatic_halt_reasons"])

    def test_unrealized_net_loss_updates_equity_and_triggers_financial_breakers(self):
        claim, _ = self.submitted()
        self.risks.change("reconcile", self.report(claim=claim, unrealized="-3", value="6", positions=1, open_risk="0.1"))
        self.assertEqual(self.risks.foundation.read().equity_usd, Decimal("47"))
        self.assertIn("PEAK_DRAWDOWN", active_halts(self.current()))
        self.assertIn("WEEKLY_LOSS", active_halts(self.current()))

    def test_normal_cost_mark_inside_reserved_loss_does_not_breach_exposure(self):
        claim, plan = self.submitted()
        self.risks.change("reconcile", self.report(claim=claim, unrealized="-0.01", value="9.9", positions=1, open_risk="0.1"))
        self.assertNotIn("EXPOSURE_LIMIT", active_halts(self.current()))

    def test_paid_entry_fee_is_not_counted_again_in_remaining_risk(self):
        claim, plan = self.submitted(price="80000", stop="78400")
        self.risks.change("reconcile", self.report(claim=claim, unrealized="-0.008", value="7.99", positions=1, open_risk="0.984"))
        self.assertNotIn("OPEN_RISK_BUDGET", active_halts(self.current()))
        limits = self.risks.status()["budgets"]
        self.assertEqual(limits["reserved_loss_usd"], plan["loss_reserved_usd"])
        self.assertEqual(Decimal(limits["future_loss_usd"]), Decimal(plan["loss_reserved_usd"]) - Decimal("0.008"))

    def test_peak_high_water_ratchets_and_cannot_be_reset_by_a_lower_mark(self):
        self.close_trade("10", "gain")
        self.assertEqual(self.risks.foundation.read().high_water_usd, Decimal("60"))
        self.close_trade("-3", "drawdown")
        self.assertEqual(self.risks.foundation.read().equity_usd, Decimal("57"))
        self.assertEqual(self.risks.foundation.read().high_water_usd, Decimal("60"))
        self.assertIn("PEAK_DRAWDOWN", active_halts(self.current()))
        self.assertNotIn("WEEKLY_LOSS", active_halts(self.current()))

    def test_three_losses_latch_review_and_zero_does_not_erase_streak(self):
        for index in range(3):
            self.close_trade("-0.1", str(index))
        self.assertEqual(self.current()["losing_streak"], 3)
        self.assertIn("LOSING_STREAK", active_halts(self.current()))
        self.now += timedelta(days=7)
        self.risks.change("sync", {})
        self.assertIn("LOSING_STREAK", active_halts(self.current()))

    def test_breakeven_keeps_streak_and_profit_resets_unlatched_streak(self):
        self.close_trade("-0.1", "loss")
        self.close_trade("0", "zero")
        self.assertEqual(self.current()["losing_streak"], 1)
        self.close_trade("0.1", "profit")
        self.assertEqual(self.current()["losing_streak"], 0)

    def test_net_fees_are_not_subtracted_twice(self):
        self.close_trade("-0.1", fee="0.01")
        self.assertEqual(self.risks.foundation.read().equity_usd, Decimal("49.9"))
        self.assertEqual(self.current()["total_fees_usd"], "0.01")

    def test_flat_utc_rollover_expires_only_period_halts_and_keeps_history(self):
        self.close_trade("-5")
        self.now += timedelta(days=1)
        self.risks.change("sync", {})
        self.assertNotIn("DAILY_LOSS", active_halts(self.current()))
        self.assertIn("WEEKLY_LOSS", active_halts(self.current()))
        self.assertIn("PEAK_DRAWDOWN", active_halts(self.current()))
        self.assertIn("DAILY_LOSS", {item["code"] for item in self.current()["halts"]})
        self.now = NOW + timedelta(days=3)  # Monday UTC
        self.risks.change("sync", {})
        self.assertIn("WEEKLY_LOSS", active_halts(self.current()))
        self.assertEqual(self.risks.status()["budgets"]["weekly_cap_usd"], "1")
        self.now = NOW + timedelta(days=7)
        self.risks.change("sync", {})
        self.assertEqual(self.risks.status()["budgets"]["weekly_cap_usd"], "0.9")
        self.assertIn("WEEKLY_LOSS", active_halts(self.current()))

    def test_unresolved_order_across_midnight_cannot_reset_period_budget(self):
        claim, _ = self.submitted()
        self.risks.change("unknown", {"id": claim})
        original_day = self.current()["day"]
        self.now += timedelta(days=1)
        self.risks.change("sync", {})
        self.assertEqual(self.current()["day"], original_day)
        self.assertIn("PERIOD_REVIEW", active_halts(self.current()))

    def test_stale_account_and_backwards_clock_fail_closed(self):
        claim, _ = self.submitted()
        self.now += timedelta(seconds=61)
        self.assertIn("ACCOUNT_STALE", self.risks.check(proposal(self.now, identity="new"))["reasons"])
        self.now = NOW - timedelta(seconds=1)
        with self.assertRaises(StateError) as caught:
            self.risks.change("sync", {})
        self.assertEqual(caught.exception.code, "CLOCK_REGRESSION")


class AccountingContractTests(RiskCase):
    def test_deposits_bad_cash_nonfinite_numbers_and_boolean_counts_rejected(self):
        for field, bad in [("cash_usd", "51"), ("cash_usd", "NaN"), ("unrealized_net_pnl_usd", "Infinity"),
                            ("position_count", True), ("pending_entry_count", -1), ("total_fees_usd", 0.0)]:
            report = self.report()
            report[field] = bad
            before = self.risks.foundation.read()
            with self.subTest(field=field), self.assertRaises(RiskError):
                self.risks.change("reconcile", report)
            self.assertEqual(self.risks.foundation.read(), before)

    def test_accounting_report_idempotency_does_not_double_count_trade(self):
        claim, _ = self.submitted()
        report = self.report(claim=claim, pnl="-0.2", identity="close", flat=True)
        self.risks.change("reconcile", report)
        _, reused = self.risks.change("reconcile", report)
        self.assertTrue(reused["accounting_report_reused"])
        self.assertEqual(len(self.current()["closed_trades"]), 1)
        self.assertEqual(self.risks.foundation.read().equity_usd, Decimal("49.8"))
        different = deepcopy(report)
        different["cash_usd"] = "49.7"
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", different)

    def test_new_report_cannot_replay_an_old_closed_trade(self):
        claim, _ = self.submitted()
        report = self.report(claim=claim, pnl="-0.1", flat=True)
        self.risks.change("reconcile", report)
        report["id"] = token("different")
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", report)

    def test_partial_position_and_remaining_pending_order_keep_full_claim(self):
        claim, plan = self.submitted()
        report = self.report(claim=claim, unrealized="-0.01", value="5", positions=1, pending=1, open_risk="0.05")
        self.risks.change("reconcile", report)
        self.assertEqual(self.risks.status()["budgets"]["reserved_loss_usd"], plan["loss_reserved_usd"])
        self.assertFalse(self.risks.check(proposal(identity="new"))["risk_check_passed"])
        report = self.report(claim=claim, unrealized="-0.01", value="5", positions=1, identity="false-flat")
        report["confirmed_flat_ids"] = [claim]
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", report)

    def test_unreserved_external_pending_exposure_latches_a_halt(self):
        report = self.report(pending=2)
        self.risks.change("reconcile", report)
        self.assertIn("UNRESERVED_EXPOSURE", active_halts(self.current()))
        self.assertIn("EXPOSURE_LIMIT", active_halts(self.current()))


class TightDailyPeriodTests(RiskCase):
    limits = {"daily_loss_cap_usd": "0.1"}

    def test_expired_daily_halt_history_does_not_block_next_day_bookkeeping(self):
        claim, _ = self.submitted(stop="99.9")
        self.risks.change("reconcile", self.report(claim=claim, pnl="-0.1", flat=True))
        self.assertEqual(active_halts(self.current()), ["DAILY_LOSS"])
        self.now += timedelta(days=1)
        self.risks.change("sync", {})
        next_claim, _ = self.reserve("next-day", stop="99.9")
        self.risks.change("submitted", {"id": next_claim, "market": proposal(self.now)["market"]})
        self.assertEqual(active_halts(self.current()), [])
