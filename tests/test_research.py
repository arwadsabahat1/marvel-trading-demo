from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from unittest.mock import patch

from marvel.data.quality import History, INTERVALS
from marvel.data.store import SnapshotStore
from marvel.errors import MarvelError, ResearchError, RiskError
from marvel.paper.store import PaperStore
from marvel.research.backtest import Broker, at, market, run
from marvel.research.commands import synthetic_dataset
from marvel.research.features import signal
from marvel.research.metrics import bootstrap, drawdown, windows
from marvel.research.model import CONTEXT, STRATEGIES, plan, scenario_profile, split
from marvel.research.store import ResearchStore, decode_record
from marvel.research.study import evaluate, gates
from marvel.risk.commands import example_request
from marvel.risk.model import canonical, fingerprint, load_profile, parse_profile
from marvel.risk.engine import evaluate as risk_evaluate
from tests.helpers import WorkspaceCase


class ResearchCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.dataset = synthetic_dataset(self.root)
        self.manifest, self.history = SnapshotStore(self.root).load(self.dataset)
        self.profile = load_profile(self.root)

    def quote_profile(self):
        document = dict(self.profile.document)
        document["entry_fee_asset"] = "USDT"
        return parse_profile(document)


def legacy_entry_decision(state, config, profile, document, now, history):
    # Explicit forensic fixture for v1 dust already acquired before the new guard.
    # No production command exposes this bypass; residual accounting must still work.
    return risk_evaluate(state, config, profile, document, now, history=history, context_kind=CONTEXT)


class FeatureTests(ResearchCase):
    def test_breakout_uses_prior_highs_and_completed_four_hour_context(self):
        decision = signal(self.history.candles[:100], STRATEGIES[0])
        self.assertTrue(decision["ready"])
        self.assertTrue(decision["enter"])
        self.assertLess(Decimal(decision["stop_price"]), self.history.candles[99].close)
        self.assertLessEqual(decision["four_hour_end_us"], decision["signal_at_us"])

    def test_incomplete_four_hour_and_daily_signals_are_unavailable(self):
        self.assertFalse(signal(self.history.candles[:101], STRATEGIES[1])["ready"])
        self.assertFalse(signal(self.history.candles[:480], STRATEGIES[2])["ready"])
        self.assertTrue(signal(self.history.candles[:504], STRATEGIES[2])["ready"])

    def test_training_validation_holdout_splits_are_chronological_and_nonoverlapping(self):
        ranges = split(720)
        self.assertEqual(ranges["train"], [0, 360])
        self.assertEqual(ranges["validation"], [360, 576])
        self.assertEqual(ranges["holdout"], [576, 720])
        self.assertEqual(ranges["windows"], [[360, 396], [396, 432], [432, 468], [468, 504], [504, 540], [540, 576]])
        for size in (0, 119, True, 30_001):
            with self.assertRaises(ResearchError):
                split(size)

    def test_frozen_cards_and_profiles_have_no_tunable_command_limits(self):
        before = deepcopy(dict(self.profile.document))
        doubled = scenario_profile(self.profile, "double_spread_slippage")
        higher = scenario_profile(self.profile, "assumed_higher_fees")
        self.assertEqual(doubled.n("exit_slippage_bps"), 40)
        self.assertEqual(higher.n("entry_fee_rate"), Decimal("0.002"))
        self.assertEqual(dict(self.profile.document), before)
        self.assertFalse(plan()["worst_account_fee_tier_qualified"])


class HistoricalExecutionTests(ResearchCase):
    def test_public_paper_adapter_rejects_historical_context_without_balance_change(self):
        paper = PaperStore(self.config())
        paper.risks.foundation.initialize()
        paper.change({"action": "init"})
        request = example_request()
        request["market"]["kind"] = CONTEXT
        before = paper.risks.foundation.path.read_bytes()
        with self.assertRaises(RiskError):
            paper.change({"action": "submit", "request": request})
        self.assertEqual(paper.risks.foundation.path.read_bytes(), before)

    def test_signal_execution_waits_a_full_bar_and_never_uses_future_prices(self):
        candles = self.history.candles
        result = run(candles, STRATEGIES[0], self.config(), self.quote_profile(), 360, 410)
        self.assertTrue(result["ledger"]["orders"])
        for order in result["ledger"]["orders"]:
            self.assertEqual(order["side"] in {"BUY", "SELL"}, True)
        # Even a nonterminal dust cycle retains its original signal and fill timestamps.
        first = result["ledger"]["orders"][0]["fills"][0]
        self.assertEqual(first["at"], at(candles[361].open_us).isoformat(timespec="microseconds"))
        for trade in result["trades"]:
            self.assertGreaterEqual(trade["entry_at_us"] - trade["signal_at_us"], INTERVALS["1h"])
        modified = list(candles)
        modified[410:] = [replace(c, high=Decimal("999999"), close=Decimal("500000")) for c in modified[410:]]
        repeated = run(tuple(modified), STRATEGIES[0], self.config(), self.quote_profile(), 360, 410)
        self.assertEqual(result, repeated)

    def test_historical_adapter_keeps_its_context_and_exact_dust_exposure(self):
        with patch("marvel.research.backtest.complete_entry_decision", side_effect=legacy_entry_decision):
            result = run(self.history.candles, STRATEGIES[0], self.config(), self.profile, 360, 450)
        self.assertEqual(result["ledger"]["mode"], CONTEXT)
        self.assertEqual(result["ledger"]["market"]["kind"], CONTEXT)
        self.assertGreater(Decimal(result["curve"][-1]["holding_btc"]), 0)
        self.assertIn("PAPER_LIQUIDATION_BLOCKED", result["halt_reasons"])
        self.assertEqual(result["open_position_count"], 1)
        self.assertEqual(result["trades"], [])
        self.assertTrue(result["reconciliation_checked"])

    def test_walk_forward_never_resets_a_loss_or_residual_between_folds(self):
        ranges = split(720)
        with patch("marvel.research.backtest.complete_entry_decision", side_effect=legacy_entry_decision):
            result = run(self.history.candles, STRATEGIES[0], self.config(), self.profile, 360, 576)
        folds = windows(result, self.history.candles, ranges["windows"], 50)
        self.assertLess(Decimal(folds[0]["ending_equity_usd"]), 50)
        for first, second in zip(folds, folds[1:]):
            self.assertEqual(first["ending_equity_usd"], second["starting_equity_usd"])
        self.assertEqual(sum(order["side"] == "BUY" for order in result["ledger"]["orders"]), 1)

    def test_same_replay_produces_same_event_chain_and_final_state_hash(self):
        first = run(self.history.candles, STRATEGIES[0], self.config(), self.profile, 360, 420)
        second = run(self.history.candles, STRATEGIES[0], self.config(), self.profile, 360, 420)
        self.assertEqual(first["event_chain_sha256"], second["event_chain_sha256"])
        self.assertEqual(first["final_state_sha256"], second["final_state_sha256"])

    def test_gap_losses_and_costs_are_recorded_beyond_planned_reserve(self):
        broker = Broker(self.config(), self.quote_profile(), self.history.candles[0].open_us, "baseline")
        when = self.history.candles[0].open_us
        broker.enter(when, Decimal("100"), "98", when - INTERVALS["1h"])
        broker.mark(when + 1, Decimal("60"))
        self.assertLess(Decimal(broker.book["cash_usd"]), 49)
        self.assertEqual(len(broker.state["closed_trades"]), 1)
        self.assertIn("WEEKLY_LOSS", [h["code"] for h in broker.state["halts"]])
        broker.check()

    def test_repeating_atr_decimal_is_rounded_down_to_tick_before_risk_validation(self):
        from decimal import localcontext
        with localcontext() as context:
            context.prec = 256
            stop = format(Decimal(100) - Decimal(2) / 14, "f")
        broker = Broker(self.config(), self.quote_profile(), self.history.candles[0].open_us, "baseline")
        result = broker.enter(self.history.candles[0].open_us, Decimal("100"), stop,
                              self.history.candles[0].open_us - INTERVALS["1h"])
        self.assertTrue(result["paper_submitted"])
        self.assertEqual(broker.book["cycles"][0]["stop_trigger"], "99.85")

    def test_positive_stop_rounding_to_zero_skips_the_setup_without_reset_or_exception(self):
        when = self.history.candles[0].open_us
        broker = Broker(self.config(), self.quote_profile(), when, "baseline")
        before = deepcopy(broker.state)
        result = broker.enter(when, Decimal("0.01"), "0.001", when - INTERVALS["1h"])
        self.assertFalse(result["paper_submitted"])
        self.assertEqual(result["risk_decision"]["reasons"], ["PRICE_OR_STOP_INVALID"])
        self.assertEqual(broker.state, before)

    def test_no_network_or_owner_account_is_needed(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result = run(self.history.candles, STRATEGIES[0], self.config(), self.profile, 360, 420)
        self.assertTrue(result["reconciliation_checked"])
        self.assertFalse(self.config().database.exists())


class StatisticsTests(WorkspaceCase):
    def test_block_bootstrap_is_deterministic_and_insufficient_samples_fail_closed(self):
        small = bootstrap(["0.01"] * 19, "frozen")
        self.assertIsNone(small["lower"])
        first = bootstrap(["0.01"] * 200, "frozen")
        self.assertEqual(first, bootstrap(["0.01"] * 200, "frozen"))
        self.assertEqual(first["lower"], "0.01")
        self.assertGreater(first["block_length_trades"], 1)

    def test_drawdown_includes_intrabar_highs_and_lows_not_only_hour_closes(self):
        marks = [{"equity_usd": "50"}, {"equity_usd": "55"}, {"equity_usd": "49"}, {"equity_usd": "54"}]
        self.assertGreater(drawdown(marks, 50), Decimal("0.10"))

    def test_even_positive_statistics_cannot_promote_unqualified_inputs_or_holdout(self):
        metrics = {"expectancy_usd": "0.1", "net_pnl_usd": "20", "closed_trades": 200,
                   "bootstrap_mean_trade_return": {"lower": "0.001"}, "maximum_drawdown_fraction": "0.01",
                   "open_position_count": 0, "holding_btc": "0"}
        folds = [{"positive_expectancy_and_net_equity": True, "regime_diagnostic_only": label, "stress_range_observed": True}
                 for label in ("rising", "falling", "sideways") * 2]
        result = gates(metrics, folds, {"double_spread_slippage": metrics, "assumed_higher_fees": metrics}, False)
        self.assertTrue(result["development_statistical_screen_passed"])
        self.assertFalse(result["historical_promotion_passed"])
        self.assertNotIn("forward_paper_record_passed", result["checks"])
        self.assertFalse(result["later_release_prerequisites"]["forward_paper_record_passed"])
        self.assertIn("untouched_final_holdout_passed", result["failed_or_unverified"])
        metrics["closed_trades"] = 199
        self.assertFalse(gates(metrics, folds, {}, False)["checks"]["at_least_200_oos_closed_trades"])
        self.assertFalse(gates(metrics, folds, {}, True)["checks"]["real_market_sample"])
        self.assertFalse(gates(metrics, folds, {}, False, model_complete=False)["checks"]["no_resource_or_model_termination"])

    def test_duplicate_keys_and_nonfinite_research_json_are_fixed_record_errors(self):
        for raw in (b'{"id":1,"id":2}', b'{"profit":NaN}', b'\xff'):
            with self.assertRaises(ResearchError):
                decode_record(raw)


class StudyTests(ResearchCase):
    def test_all_variants_scenarios_and_negative_results_are_retained(self):
        result = evaluate(self.manifest, self.history, self.config(), self.profile)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertFalse(result["phase5_historical_gate_passed"])
        for candidate in result["candidates"]:
            self.assertEqual(set(candidate["validation"]), {"baseline", "double_spread_slippage", "assumed_higher_fees"})
            self.assertFalse(candidate["selected_for_deployment"])
            self.assertEqual(len(candidate["validation"]["baseline"]["windows"]), 6)
            self.assertIn("bootstrap_mean_trade_return", candidate["validation"]["baseline"]["metrics"])
            self.assertIn("exposure_matched_cash_passive", candidate["validation"]["baseline"]["benchmarks"])
        self.assertFalse(result["holdout"]["evaluated"])

    def test_reserved_tail_is_never_given_to_features_execution_or_benchmarks(self):
        original = self.history.candles

        class PrefixOnly:
            def __len__(self):
                return len(original)

            def __getitem__(self, key):
                if not isinstance(key, slice) or key.stop > 576:
                    raise AssertionError("Reserved tail was accessed")
                return original[key]

        guarded = History(PrefixOnly(), 0)
        result = evaluate(self.manifest, guarded, self.config(), self.profile)
        self.assertEqual(result["holdout"]["range"], [576, 720])

    def test_source_change_during_calculation_rejects_misattributed_evidence(self):
        with patch("marvel.research.study.source_hashes", side_effect=[{"code": "before"}, {"code": "after"}]):
            with self.assertRaises(ResearchError):
                evaluate(self.manifest, self.history, self.config(), self.profile)

    def test_study_records_are_deterministic_immutable_and_tamper_checked(self):
        document = evaluate(self.manifest, self.history, self.config(), self.profile)
        store = ResearchStore(self.root)
        store.freeze_plan()
        identity = store.save(document)
        self.assertEqual(store.save(document), identity)
        self.assertEqual(len(store.inventory()), 1)
        self.assertEqual(store.load(identity)["run_id"], identity)
        path = store.folder(identity) / "study.json"
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["holdout"]["evaluated"] = True
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(MarvelError):
            store.load(identity)
        with self.assertRaises(MarvelError):
            store.save(document)

    def test_plan_corruption_cannot_silently_change_or_repair_rules(self):
        store = ResearchStore(self.root)
        identity = store.freeze_plan()
        (store.root / "plans" / (identity + ".json")).write_text("{}", encoding="utf-8")
        with self.assertRaises(ResearchError):
            store.freeze_plan()

    def test_malformed_consistently_hashed_study_schema_is_rejected_before_publication(self):
        document = evaluate(self.manifest, self.history, self.config(), self.profile)
        for field, value in (("holdout", []), ("schema_version", True), ("candidates", {}), ("phase5_historical_gate_passed", True)):
            changed = deepcopy(document)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ResearchError):
                ResearchStore(self.root).save(changed)

    def test_store_rejects_path_escape_and_missing_records(self):
        store = ResearchStore(self.root)
        for identity in ("../escape", [], "a" * 64):
            with self.assertRaises(MarvelError):
                store.load(identity)

