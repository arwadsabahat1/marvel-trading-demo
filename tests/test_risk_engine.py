from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext

from marvel.clock import stamp
from marvel.errors import RiskError
from marvel.risk.engine import budgets, evaluate, seed
from marvel.risk.model import load_profile, parse_profile, signed
from tests.helpers import NOW
from tests.risk_helpers import RiskCase, proposal


class SizingTests(RiskCase):
    def check(self, document=None, state=None, profile=None):
        return evaluate(state or self.current(), self.config(), profile or self.profile,
                        document or proposal(), self.now)

    def test_safe_size_respects_every_budget_and_exit_minimum(self):
        result = self.check()
        self.assertTrue(result["risk_check_passed"], result)
        plan, limits = result["plan"], result["budgets"]
        self.assertLessEqual(Decimal(plan["loss_reserved_usd"]), Decimal("1"))
        self.assertLessEqual(Decimal(plan["notional_usd"]), Decimal("10"))
        self.assertLessEqual(Decimal(plan["cash_reserved_usd"]), Decimal(limits["available_cash_usd"]))
        self.assertLessEqual(Decimal(plan["model_loss_usd"]), Decimal(plan["loss_reserved_usd"]))
        self.assertGreaterEqual(Decimal(plan["exit_quantity"]) * Decimal(plan["exit_price_estimate"]), Decimal("5"))
        self.assertFalse(result["entries_allowed"])
        self.assertFalse(plan["execution_eligible"])

    def test_base_fee_and_dust_are_charged_with_zero_recovery(self):
        result = self.check(proposal(price="80000", stop="78400"))
        self.assertTrue(result["risk_check_passed"], result)
        plan = result["plan"]
        self.assertGreater(Decimal(plan["entry_fee_base"]), 0)
        self.assertGreater(Decimal(plan["dust_quantity"]), 0)
        self.assertLess(Decimal(plan["exit_quantity"]), Decimal(plan["quantity"]))
        self.assertEqual(Decimal(plan["model_loss_usd"]), Decimal(plan["entry_cash_cost_usd"]) - Decimal(plan["exit_proceeds_usd"]) + Decimal("0.01"))

    def test_entry_and_exit_rounding_are_adverse_and_quantity_rounds_down(self):
        document = proposal(stop="98.009", quantity="0.06000")
        result = self.check(document)
        self.assertTrue(result["risk_check_passed"], result)
        self.assertEqual(result["plan"]["stop_trigger"], "98")
        self.assertEqual(result["plan"]["entry_price_bound"], "100.1")
        self.assertEqual(result["plan"]["exit_price_estimate"], "97.8")
        self.assertEqual(result["plan"]["quantity"], "0.06")
        bad = deepcopy(document)
        bad["proposal"]["quantity"] = "0.050001"
        self.assertIn("REQUESTED_QUANTITY_EXCEEDS_SAFE_SIZE", self.check(bad)["reasons"])

    def test_requested_size_cannot_force_upward_rounding_or_exceed_caps(self):
        for quantity in ("1", "0.000001"):
            result = self.check(proposal(quantity=quantity))
            self.assertFalse(result["risk_check_passed"])

    def test_small_remaining_budget_or_cash_returns_no_trade(self):
        state = self.current()
        state["day_start_equity"] = "54.99"
        result = self.check(state=state)
        self.assertFalse(result["risk_check_passed"])
        state = self.current()
        state["cash_usd"] = "0.1"
        result = self.check(state=state)
        self.assertFalse(result["risk_check_passed"])

    def test_safe_entry_that_cannot_form_protective_exit_is_rejected(self):
        profile = dict(self.profile.document)
        profile["exit_min_notional_quote"] = "10"
        result = self.check(profile=parse_profile(profile))
        self.assertFalse(result["risk_check_passed"])
        self.assertIn("ENTRY_OR_PROTECTIVE_EXIT_MINIMUM", result["reasons"])

    def test_fx_spread_is_reserved_in_usd_and_can_reduce_size(self):
        normal = self.check()["plan"]
        document = proposal()
        document["market"].update({"fx_lower": "0.99", "fx_upper": "1.01"})
        changed = self.check(document)
        self.assertTrue(changed["risk_check_passed"], changed)
        self.assertLessEqual(Decimal(changed["plan"]["quantity"]), Decimal(normal["quantity"]))
        self.assertGreater(Decimal(changed["plan"]["model_loss_usd"]), Decimal(normal["model_loss_usd"]))
        document["market"]["fx_lower"] = "0.1"
        self.assertFalse(self.check(document)["risk_check_passed"])

    def test_quote_and_fx_future_stale_and_wide_spread_fail_closed(self):
        for field, when, reason in [("quote_at", NOW - timedelta(seconds=10, microseconds=1), "QUOTE_STALE"),
                                    ("quote_at", NOW + timedelta(microseconds=1), "QUOTE_FUTURE"),
                                    ("fx_at", NOW - timedelta(seconds=11), "FX_STALE"),
                                    ("fx_at", NOW + timedelta(seconds=1), "FX_FUTURE")]:
            document = proposal()
            document["market"][field] = stamp(when)
            self.assertIn(reason, self.check(document)["reasons"])
        document = proposal()
        document["market"]["bid"] = "99"
        self.assertIn("SPREAD_TOO_WIDE", self.check(document)["reasons"])

    def test_missing_nonfinite_wrong_types_and_policy_overrides_rejected(self):
        for section, key, bad in [("market", "ask", "NaN"), ("market", "fx_lower", "Infinity"),
                                  ("proposal", "stop_price", 98.0), ("proposal", "quantity", True),
                                  ("proposal", "symbol", "GOLD"), ("proposal", "side", "SELL"),
                                  ("market", "kind", "live"), ("proposal", "risk_limit", "100")]:
            document = proposal()
            document[section][key] = bad
            with self.subTest(section=section, key=key), self.assertRaises(RiskError):
                self.check(document)
        document = proposal()
        del document["market"]["fx_at"]
        with self.assertRaises(RiskError):
            self.check(document)

    def test_invalid_stop_and_disabled_market_rejected(self):
        self.assertIn("PRICE_OR_STOP_INVALID", self.check(proposal(stop="101"))["reasons"])
        self.document["markets"] = ["GOLD"]
        self.write_config()
        state = seed(self.config(), self.profile, Decimal("50"), Decimal("50"), stamp(NOW))
        self.assertIn("MARKET_DISABLED", evaluate(state, self.config(), self.profile, proposal(), NOW)["reasons"])

    def test_quote_fee_model_and_unsupported_fee_currency(self):
        settings = dict(self.profile.document)
        settings["entry_fee_asset"] = "USDT"
        result = self.check(profile=parse_profile(settings))
        self.assertTrue(result["risk_check_passed"], result)
        self.assertEqual(result["plan"]["entry_fee_base"], "0")
        self.assertGreater(Decimal(result["plan"]["entry_fee_quote"]), 0)
        settings["exit_fee_asset"] = "BNB"
        with self.assertRaises(RiskError):
            parse_profile(settings)

    def test_precision_context_cannot_change_decision_or_hash(self):
        document = proposal(stop="98.000000000000000000000000000000000001")
        with localcontext() as ctx:
            ctx.prec = 6
            reduced = self.check(document)
        self.assertEqual(reduced, self.check(document))

    def test_profile_is_immutable_and_fingerprint_keeps_accepted_digits(self):
        with self.assertRaises(TypeError):
            self.profile.document["entry_fee_rate"] = "0"
        first = dict(self.profile.document)
        first["entry_fee_rate"] = "0.0010000000000000000000000000000000001"
        second = {**first, "entry_fee_rate": "0.0010000000000000000000000000000000002"}
        self.assertNotEqual(parse_profile(first).fingerprint, parse_profile(second).fingerprint)

    def test_gains_do_not_automatically_increase_notional_or_weekly_ceiling(self):
        state = self.current()
        state.update({"origin_equity": "100", "cash_usd": "100", "high_water_usd": "100", "week_start_equity": "100"})
        result = budgets(state, self.config())
        self.assertEqual(result["notional_cap_usd"], "10")
        self.assertEqual(result["weekly_cap_usd"], "1")

    def test_different_exit_lots_never_underreserve_model_loss(self):
        # Larger exit lots can make actual rounded losses non-monotone; sizing uses an upper bound.
        for step in ("0.00001", "0.00003", "0.001", "0.005"):
            for fee_asset in ("BTC", "USDT"):
                profile = dict(self.profile.document)
                profile.update({"exit_step": step, "entry_fee_asset": fee_asset})
                result = self.check(profile=parse_profile(profile))
                if result["risk_check_passed"]:
                    self.assertLessEqual(Decimal(result["plan"]["model_loss_usd"]), Decimal(result["plan"]["loss_reserved_usd"]))
                    self.assertLessEqual(Decimal(result["plan"]["loss_reserved_usd"]), Decimal("1"))

    def test_signed_amount_above_maximum_is_not_rounded_into_range(self):
        with self.assertRaises(RiskError):
            signed("1000000000000.0000000000000001")
