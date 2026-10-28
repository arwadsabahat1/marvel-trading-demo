from copy import deepcopy
from decimal import Decimal, localcontext
import json
from unittest.mock import patch

from marvel.errors import MarvelError
from marvel.research.account_check import AccountEvidenceStore
from marvel.research.exit_costs import RECIPE, audit, conversion_floor, load_pair, pair_limits
from marvel.risk.model import fingerprint, load_profile, parse_profile
from tests import test_cli as command_fixtures
from tests.helpers import WorkspaceCase
from tests.test_account_check import fixtures, observed, Opener


def pair():
    return {"fromAsset": "BTC", "toAsset": "USDT", "fromAssetMinAmount": "0.00000013",
            "fromAssetMaxAmount": "9E+24", "toAssetMinAmount": "0.01", "toAssetMaxAmount": "9E+24", "fromIsBase": True}


class ExitCostTests(WorkspaceCase):
    def result(self, *, account=None, profile=None):
        account = account or observed()
        return audit(self.config(), profile or load_profile(self.root), account, pair(),
                     account_id=fingerprint(account), pair_source={"provenance": "invented fixture"})

    def routes(self, result):
        return [route for stop in result["scenarios"] for route in stop["routes"]]

    def test_audit_uses_existing_risk_decisions_without_trades_or_promotion(self):
        result = self.result()
        routes = self.routes(result)
        self.assertTrue(routes)
        for stop in result["scenarios"]:
            decision = stop["original_spot_sizing_decision"]
            if decision["risk_check_passed"]:
                self.assertLessEqual(Decimal(decision["plan"]["loss_reserved_usd"]), 1)
                self.assertLessEqual(Decimal(decision["plan"]["notional_usd"]), 10)
        self.assertEqual(result["out_of_sample_strategy_trades"], 0)
        self.assertEqual(result["actual_conversion_quotes_observed"], 0)
        for flag in ("phase5_historical_gate_passed", "entries_allowed", "actual_complete_exit_qualified", "strategy_route_changed", "holdout_evaluated"):
            self.assertFalse(result[flag])
        self.assertFalse(self.config().database.exists())

    def test_full_entry_cash_cost_matches_the_original_plan_exactly(self):
        result = self.result()
        for stop in result["scenarios"]:
            for route in stop["routes"]:
                if route["entry_fill_fraction"] == "1":
                    self.assertEqual(route["entry_cost_usd"], stop["original_spot_sizing_decision"]["plan"]["entry_cash_cost_usd"])

    def test_asset_and_cash_identities_preserve_remaining_btc_and_original_reservation(self):
        for route in self.routes(self.result()):
            self.assertEqual(Decimal(route["net_received_btc"]), Decimal(route["spot_sold_btc"]) + Decimal(route["remaining_btc"]))
            for case in route["scenarios"]:
                self.assertEqual(case["unresolved_btc_until_verified_settlement"], route["remaining_btc"])
                if case["invented_net_quote_usdt"] is not None and Decimal(route["remaining_btc"]):
                    expected = Decimal(route["spot_net_proceeds_usd"]) + Decimal(case["invented_net_quote_usdt"]) * Decimal(RECIPE["fx_lower"]) - Decimal(route["entry_cost_usd"])
                    self.assertEqual(Decimal(case["counterfactual_cycle_pnl_usd_if_quote_settled"]), expected)
                if case["conditional_amount_and_loss_floor_fit"]:
                    self.assertGreaterEqual(Decimal(case["counterfactual_cycle_pnl_usd_if_quote_settled"]), -Decimal(route["original_reserved_loss_usd"]))

    def test_partial_entry_can_fail_the_spot_exit_minimum_despite_acceptable_precision(self):
        routes = self.routes(self.result())
        affected = [route for route in routes if route["entry_fill_fraction"] == "0.5" and not route["modeled_spot_exit_order_meets_minimums"]]
        self.assertTrue(affected)
        for route in affected:
            self.assertEqual(route["spot_sold_btc"], "0")
            self.assertTrue(route["source_amount_precision_and_limits_fit"])
            missing = next(case for case in route["scenarios"] if case["invented_haircut_bps"] is None)
            self.assertTrue(missing["zero_recovery_stress_exceeds_original_reservation"])
            self.assertFalse(missing["conditional_amount_and_loss_floor_fit"])

    def test_unavailable_conversion_does_not_invent_cash_or_remove_ownership(self):
        for route in self.routes(self.result()):
            case = next(case for case in route["scenarios"] if case["invented_haircut_bps"] is None)
            self.assertIsNone(case["invented_net_quote_usdt"])
            self.assertIsNone(case["counterfactual_cycle_pnl_usd_if_quote_settled"])
            self.assertEqual(case["outcome"], "conversion_unavailable")
            self.assertGreater(Decimal(case["unresolved_btc_until_verified_settlement"]), 0)
            self.assertTrue(route["claim_must_remain_until_verified_flat_and_net_outcome"])

    def test_risk_floor_is_rounded_up_and_one_quantum_less_cannot_fund_the_reservation_floor(self):
        quantum, fx = Decimal("0.00000001"), Decimal("0.995")
        minimum, breakeven = conversion_floor(Decimal(8), Decimal(7), Decimal("0.5"), fx, quantum, Decimal("0.01"))
        self.assertEqual(minimum, Decimal("0.50251257"))
        self.assertGreaterEqual(minimum * fx + 7 - 8, Decimal("-0.5"))
        self.assertLess((minimum - quantum) * fx + 7 - 8, Decimal("-0.5"))
        self.assertGreaterEqual(breakeven * fx + 7 - 8, 0)
        minimum, _ = conversion_floor(Decimal(1), Decimal(1), Decimal(1), fx, quantum, Decimal("0.010000001"))
        self.assertEqual(minimum, Decimal("0.01000001"))

    def test_more_expensive_synthetic_quotes_never_improve_counterfactual_pnl(self):
        for route in self.routes(self.result()):
            values = [Decimal(case["counterfactual_cycle_pnl_usd_if_quote_settled"]) for case in route["scenarios"] if case["invented_haircut_bps"] is not None]
            self.assertEqual(values, sorted(values, reverse=True))
            self.assertTrue(all(value < 0 for value in values))

    def test_zero_remainder_needs_no_conversion_and_cannot_be_charged_one(self):
        document = dict(load_profile(self.root).document)
        document["entry_fee_asset"] = "USDT"  # Test fixture only; production profile stays unchanged.
        result = self.result(profile=parse_profile(document))
        routes = [route for route in self.routes(result) if route["remaining_btc"] == "0"]
        self.assertTrue(routes)
        for route in routes:
            self.assertEqual(route["minimum_net_convert_quote_usdt_for_reserved_loss"], "0")
            self.assertFalse(route["claim_must_remain_until_verified_flat_and_net_outcome"])
            for case in route["scenarios"]:
                self.assertEqual(case["outcome"], "spot_only_flat_model")
                self.assertEqual(Decimal(case["counterfactual_cycle_pnl_usd_if_quote_settled"]), Decimal(route["spot_net_proceeds_usd"]) - Decimal(route["entry_cost_usd"]))
        self.assertEqual(load_profile(self.root).document["entry_fee_asset"], "BTC")

    def test_lower_account_precision_rejects_the_full_remainder_instead_of_rounding_it_away(self):
        values = fixtures()
        values["precision"][0]["fraction"] = 5
        result = self.result(account=observed(Opener(values)))
        for route in self.routes(result):
            self.assertFalse(route["source_amount_precision_and_limits_fit"])
            self.assertTrue(all(case["outcome"] == "source_amount_rejected" for case in route["scenarios"]))

    def test_observed_higher_commissions_block_undercosted_scenarios_without_changing_profile(self):
        values = fixtures()
        values["commissions"]["taxCommission"]["taker"] = "0.001"
        result = self.result(account=observed(Opener(values)))
        self.assertEqual(result["input_blockers"], ["PROFILE_BELOW_OBSERVED_UNDISCOUNTED_FEES"])
        self.assertEqual(result["scenarios"], [])
        self.assertEqual(load_profile(self.root).document["entry_fee_rate"], "0.001")

    def test_incomplete_account_metadata_is_not_upgraded(self):
        account = observed()
        account["precision"] = None
        with self.assertRaises(MarvelError):
            self.result(account=account)

    def test_account_record_identity_cannot_be_attached_to_different_metadata(self):
        with self.assertRaises(MarvelError):
            audit(self.config(), load_profile(self.root), observed(), pair(),
                  account_id="a" * 64, pair_source={"provenance": "invented fixture"})

    def test_pair_limits_accept_large_provider_maximum_but_reject_malformed_or_duplicate_pair(self):
        checked = pair_limits([pair()])
        self.assertEqual(Decimal(checked["fromAssetMaxAmount"]), Decimal("9E24"))
        for values in ([pair(), pair()], [], {"pair": pair()}):
            with self.assertRaises(MarvelError):
                pair_limits(values)
        for value in (True, "NaN", "-1", "0", "1e999", [], "1e31"):
            item = pair()
            item["fromAssetMinAmount"] = value
            with self.assertRaises(MarvelError):
                pair_limits([item])

    def test_audit_preserves_inputs_and_results_across_decimal_contexts(self):
        account, profile = observed(), load_profile(self.root)
        before = deepcopy(account)
        result = self.result(account=account, profile=profile)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(self.result(account=account, profile=profile), result)
        self.assertEqual(account, before)

    def test_source_change_during_the_audit_rejects_misattributed_results(self):
        with patch("marvel.research.exit_costs.source_hashes", side_effect=({"one": "old"}, {"one": "new"})), self.assertRaises(MarvelError):
            self.result()


class ExitCostCommands(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def fixture(self):
        identity = AccountEvidenceStore(self.root).save(observed())
        path = self.root / "var/pair.json"
        path.write_text(json.dumps([pair()]), encoding="utf-8")
        return identity, "var/pair.json"

    def test_command_is_offline_deterministic_and_never_initializes_owner_execution(self):
        identity, filename = self.fixture()
        first = self.payload("research", "exit-audit", "--record", identity, "--pair-limits", filename)
        second = self.payload("research", "exit-audit", "--record", identity, "--pair-limits", filename)
        self.assertEqual(first["exit_cost_audit"], second["exit_cost_audit"])
        self.assertFalse(first["entries_allowed"])
        self.assertFalse(self.config().database.exists())
        self.assertFalse((self.root / "var/research-runs").exists())

    def test_existing_account_manual_halt_and_configs_are_byte_identical(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        paths = [self.config().database, self.root / "config/paper.json", self.root / "config/btc-paper-risk.json"]
        before = [path.read_bytes() for path in paths]
        identity, filename = self.fixture()
        self.payload("research", "exit-audit", "--record", identity, "--pair-limits", filename)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_network_risk_fee_and_live_override_flags_are_rejected(self):
        identity, filename = self.fixture()
        for flag in ("--allow-network", "--live", "--force", "--fee-asset", "--risk"):
            result = self.run_cli("research", "exit-audit", "--record", identity, "--pair-limits", filename, flag)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)

    def test_missing_tampered_and_outside_workspace_references_reject_without_repair(self):
        identity, filename = self.fixture()
        for bad in ("../OUTSIDE_SENTINEL_418", "var/missing.json"):
            result = self.run_cli("research", "exit-audit", "--record", identity, "--pair-limits", bad)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("OUTSIDE_SENTINEL_418", result.stdout + result.stderr)
        path = self.root / filename
        raw = b'{"secret":"PRIVATE_SENTINEL_418"}'
        path.write_bytes(raw)
        result = self.run_cli("research", "exit-audit", "--record", identity, "--pair-limits", filename)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("PRIVATE_SENTINEL_418", result.stdout + result.stderr)
        self.assertEqual(path.read_bytes(), raw)

    def test_pair_reader_is_bounded_and_rejects_duplicate_json_keys(self):
        path = self.root / "pairs.json"
        for raw in (b" " * 65537, b'[{"fromAsset":"BTC","fromAsset":"USDT"}]'):
            path.write_bytes(raw)
            with self.assertRaises(MarvelError):
                load_pair(self.root, path)
