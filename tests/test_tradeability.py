from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

from marvel.data.quality import utc_us, digest
from marvel.data.store import SnapshotStore
from marvel.errors import MarvelError
from marvel.research.backtest import Broker
from marvel.research.feasibility import largest_complete_quantity, quantities, public_filter_summary, report
from marvel.research.model import plan
from marvel.risk.model import load_profile, parse_profile
from tests import test_cli as command_fixtures
from tests.helpers import WorkspaceCase


class TradeabilityTests(WorkspaceCase):
    def test_known_dust_is_refused_before_any_claim_or_fill(self):
        when = utc_us("2026-01-01T00:00:00Z")
        broker = Broker(self.config(), load_profile(self.root), when, "baseline")
        before = deepcopy(broker.state)
        result = broker.enter(when, Decimal("80000"), "79000", when - 3_600_000_000)
        self.assertFalse(result["paper_submitted"])
        self.assertIn("FULL_LIQUIDATION_NOT_FEASIBLE", result["risk_decision"]["reasons"])
        self.assertEqual(broker.state, before)
        self.assertEqual(broker.book["orders"], [])

    def test_fee_grid_boundary_requires_one_thousand_original_entry_steps(self):
        profile = load_profile(self.root)
        self.assertNotEqual(quantities(profile, Decimal("0.00999"))["residual_btc"], "0")
        self.assertEqual(quantities(profile, Decimal("0.01"))["residual_btc"], "0")
        self.assertEqual(largest_complete_quantity(profile, Decimal("0.00999")), (None, False))
        self.assertEqual(largest_complete_quantity(profile, Decimal("0.01")), (Decimal("0.01"), False))

    def test_search_only_decreases_and_preserves_exact_rounding(self):
        profile = load_profile(self.root)
        self.assertEqual(largest_complete_quantity(profile, Decimal("0.04999")), (Decimal("0.04"), False))
        self.assertEqual(quantities(profile, Decimal("0.00011")), {
            "gross_btc": "0.00011", "entry_fee_btc": "0.00000011", "received_btc": "0.00010989",
            "sell_btc": "0.0001", "residual_btc": "0.00000989"})

    def test_valid_smaller_size_is_rechecked_and_closes_without_altering_profile(self):
        when = utc_us("2026-01-01T00:00:00Z")
        profile = load_profile(self.root)
        original = dict(profile.document)
        broker = Broker(self.config(), profile, when, "baseline")
        result = broker.enter(when, Decimal("100"), "98", when - 3_600_000_000)
        self.assertTrue(result["paper_submitted"])
        self.assertEqual(result["risk_decision"]["plan"]["quantity"], "0.09")
        self.assertEqual(result["risk_decision"]["plan"]["dust_quantity"], "0")
        self.assertEqual(result["risk_decision"]["plan"]["stop_trigger"], "98")
        self.assertLessEqual(Decimal(result["risk_decision"]["plan"]["loss_reserved_usd"]), 1)
        broker.close(when + 1, Decimal("100"))
        self.assertEqual(broker.current(), None)
        self.assertEqual(len(broker.state["closed_trades"]), 1)
        self.assertEqual(dict(profile.document), original)
        self.assertFalse(self.config().database.exists())

    def test_quote_fee_fixture_has_no_base_deduction_but_does_not_change_real_profile(self):
        profile = load_profile(self.root)
        changed = dict(profile.document)
        changed["entry_fee_asset"] = "USDT"
        self.assertEqual(quantities(parse_profile(changed), Decimal("0.00011"))["residual_btc"], "0")
        self.assertEqual(profile.document["entry_fee_asset"], "BTC")

    def test_excessive_search_fails_closed(self):
        with patch("marvel.research.feasibility.MAX_QUANTITY_STEPS", 2):
            self.assertEqual(largest_complete_quantity(load_profile(self.root), Decimal("0.00999")), (None, True))

    def test_new_plan_binds_liquidation_check_and_retains_evidence_gates(self):
        frozen = plan()
        self.assertEqual(frozen["name"], "btc-baseline-screen-v2")
        self.assertIn("Reject", frozen["entry_liquidation"])
        self.assertFalse(frozen["partial_fill_liquidation_qualified"])
        self.assertEqual(frozen["gates"]["minimum_oos_closed_trades"], 200)


def observation():
    return {"symbol": "BTCUSDT", "metadata_observed_us": utc_us("2026-01-01T00:00:00Z"),
            "metadata": {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING",
                         "isSpotTradingAllowed": True, "filters": [
                             {"filterType": "LOT_SIZE", "minQty": "0.00001", "stepSize": "0.00001"},
                             {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                             {"filterType": "NOTIONAL", "minNotional": "5"},
                             {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "stepSize": "0"}]},
            "quote": {"ask": "80000", "bid": "79999.99", "event_us": None},
            "provenance": {"origin": "local_unverified", "data_kind": "synthetic_engineering_fixture"}}


class FeasibilityCommands(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def test_public_filters_keep_lot_step_despite_zero_market_step(self):
        public = public_filter_summary(observation())
        self.assertEqual(public["lot_step_btc"], "0.00001")
        self.assertFalse(public["account_fees_verified"])
        self.assertTrue(public["zero_market_step_does_not_remove_lot_size_filter"])

    def test_missing_duplicate_zero_or_malformed_filters_reject(self):
        for kind in ("missing", "duplicate", "zero", "malformed"):
            item = observation()
            filters = item["metadata"]["filters"]
            if kind == "missing":
                filters.pop(0)
            elif kind == "duplicate":
                filters.append(deepcopy(filters[0]))
            elif kind == "zero":
                filters[0]["stepSize"] = "0"
            else:
                filters[0]["minQty"] = []
            with self.subTest(kind=kind), self.assertRaises(MarvelError):
                public_filter_summary(item)

    def test_report_demonstrates_incompatibility_without_qualifying_fees_or_increasing_budget(self):
        result = report(self.config(), load_profile(self.root), observation())
        self.assertEqual(result["initial_notional_cap_usd"], "10")
        self.assertIsNone(result["largest_full_fill_grid_aligned_gross_btc_within_cap"])
        self.assertEqual(result["first_grid_aligned_full_fill_within_search"]["gross_btc"], "0.01")
        self.assertEqual(result["first_grid_aligned_reference_notional_usd"], "804")
        self.assertFalse(result["actual_account_fee_rates_verified"])
        self.assertFalse(result["actual_fee_rounding_verified"])
        self.assertFalse(result["partial_fill_liquidation_qualified"])
        self.assertFalse(result["production_qualified"])

    def test_cli_is_read_only_offline_and_never_opens_owner_account(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        before = self.config().database.read_bytes()
        identifier = SnapshotStore(self.root).save_observation(observation())
        result = self.payload("research", "feasibility", "--observation", identifier)["btc_feasibility"]
        self.assertEqual(result["observation_id"], identifier)
        self.assertFalse(result["owner_account_used"])
        self.assertEqual(self.config().database.read_bytes(), before)
        self.assertFalse((self.root / "var/research-runs").exists())

    def test_tampered_observation_is_rejected_and_not_repaired(self):
        store = SnapshotStore(self.root)
        identifier = store.save_observation(observation())
        path = store.root / "observations" / (identifier + ".json")
        changed = path.read_bytes() + b" "
        path.write_bytes(changed)
        self.assertNotEqual(digest(changed), identifier)
        with self.assertRaises(MarvelError):
            store.load_observation(identifier)
        self.assertEqual(path.read_bytes(), changed)

    def test_malformed_observation_ids_are_sanitized(self):
        for identifier in ("../SECRET_SENTINEL_631", "a" * 64):
            process = self.run_cli("research", "feasibility", "--observation", identifier)
            self.assertEqual(process.returncode, 2)
            self.assertNotIn("SECRET_SENTINEL_631", process.stdout + process.stderr)
            self.assertFalse(self.config().database.exists())
