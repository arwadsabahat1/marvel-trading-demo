from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
from functools import lru_cache
from hashlib import sha256
import json
import subprocess
import sys
from unittest.mock import patch

from marvel.data.quality import Candle, History, INTERVALS, utc_us
from marvel.data.store import SnapshotStore
from marvel.errors import MarvelError, ResearchError
from marvel.research import comparison, proxy
from marvel.research.commands import synthetic_dataset
from marvel.research.rules import RULES, signal
from marvel.risk.model import canonical, fingerprint, load_profile
from tests.helpers import ROOT, WorkspaceCase


def candles(prices, *, wide=False):
    first, width = utc_us("2026-03-01T00:00:00Z"), INTERVALS["1h"]
    return [Candle(first + i * width, first + (i + 1) * width,
        Decimal(p), Decimal(p) + 25, Decimal(p) - (2500 if wide else 25), Decimal(p),
        Decimal(10), Decimal(p) * 10, 20, Decimal(4), Decimal(p) * 4) for i, p in enumerate(prices)]


@lru_cache(maxsize=1)
def fixture():
    owner = WorkspaceCase()
    owner.setUp()
    try:
        manifest, history = SnapshotStore(owner.root).load(synthetic_dataset(owner.root))
        profile = load_profile(owner.root)
        frozen = comparison.freeze(owner.config(), profile, manifest)
        body = comparison.evaluate(frozen, history, owner.config(), profile)
        return deepcopy((manifest, history, frozen, body))
    finally:
        owner.doCleanups()


class RuleTests(WorkspaceCase):
    def test_pullback_requires_a_recovery_and_completed_rising_four_hour_trend(self):
        prices = [80000 + i * 100 for i in range(60)]
        prices[-2:] = [84000, 85000]
        decision = signal(candles(prices), RULES[0])
        self.assertTrue(decision["enter"])
        self.assertTrue(decision["rising_trend"])
        self.assertLess(Decimal(decision["stop_price"]), 85000)
        prices[-1] = 83000
        self.assertFalse(signal(candles(prices), RULES[0])["enter"])

    def test_breakout_cost_filter_blocks_small_moves_without_changing_risk_limits(self):
        prices = [80000 + i * 100 for i in range(60)]
        self.assertFalse(signal(candles(prices), RULES[1])["enter"])
        self.assertTrue(signal(candles(prices, wide=True), RULES[1])["enter"])
        self.assertEqual(self.config().risk.per_trade_cap_usd, 1)

    def test_range_rebound_requires_band_recovery_in_a_flat_context(self):
        prices = [80000] * 58 + [76000, 79500]
        decision = signal(candles(prices), RULES[2])
        self.assertTrue(decision["enter"])
        self.assertFalse(decision["exit"])
        prices[-1] = 80000
        decision = signal(candles(prices), RULES[2])
        self.assertTrue(decision["exit"])
        self.assertFalse(decision["enter"])
        self.assertFalse(signal(candles([80000] * 60), RULES[2])["enter"])

    def test_short_prefix_unknown_rules_and_decimal_precision_are_handled(self):
        for rule in RULES:
            self.assertFalse(signal([], rule)["ready"])
            self.assertFalse(signal(candles([80000] * 30), rule)["ready"])
        with self.assertRaises(MarvelError):
            signal([], replace(RULES[0], lookback=19))
        history = candles([80000 + i * 100 for i in range(59)])
        expected = signal(history, RULES[1])
        self.assertLessEqual(expected["four_hour_end_us"], history[-1].end_us)
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(signal(history, RULES[1]), expected)

    def test_alternative_signals_keep_full_bar_execution_latency(self):
        cfg, profile = self.config(), proxy.profiles(load_profile(self.root))["baseline"]
        history = candles([80000 + i * 100 for i in range(120)], wide=True)
        result = comparison.run_card(history, RULES[1], cfg, profile, 60, 100)
        self.assertTrue(result["trades"])
        for trade in result["trades"]:
            self.assertGreaterEqual(trade["entry_at_us"], trade["signal_at_us"] + INTERVALS["1h"])
        self.assertFalse(cfg.database.exists())


class ComparisonTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg, self.profile = self.config(), load_profile(self.root)
        self.manifest, self.history, self.frozen, self.body = deepcopy(fixture())

    def test_frozen_rule_definitions_and_original_plan_are_separate(self):
        original = proxy.freeze(self.cfg, self.profile, self.manifest)
        self.assertNotEqual(fingerprint(original), fingerprint(self.frozen))
        self.assertEqual(original["kind"], "btc_quote_fee_proxy_plan_v1")
        self.assertEqual(self.frozen["splits"], original["splits"])
        self.assertEqual(self.frozen["protected_samples"], original["protected_samples"])
        self.assertEqual(self.frozen["scenarios"], original["scenarios"])
        self.assertEqual(self.frozen["rule_definitions"], comparison.definitions())

    def test_plan_is_saved_before_any_new_rule_or_price_evaluation(self):
        def guarded(frozen, *_):
            path = self.root / "var/strategy-comparisons/plans" / (fingerprint(frozen) + ".json")
            self.assertEqual(path.read_bytes(), (canonical(frozen) + "\n").encode("ascii"))
            return deepcopy(self.body)
        with patch("marvel.research.comparison.evaluate", side_effect=guarded):
            comparison.execute(self.cfg, synthetic_dataset(self.root))
        self.assertFalse(self.cfg.database.exists())
        self.assertFalse((self.root / "var/proxy-cost-studies").exists())

    def test_final_prices_cannot_reach_any_alternative_signal_or_metric(self):
        actual = comparison.run_card
        calls = []
        def guarded(history, *args, **kwargs):
            self.assertEqual(len(history), 576)
            calls.append(1)
            return actual(history, *args, **kwargs)
        class PrefixOnly:
            def __len__(inner):
                return 720
            def __getitem__(inner, index):
                self.assertIsInstance(index, slice)
                self.assertLessEqual(index.stop, 576)
                return self.history.candles[index]
        with patch("marvel.research.comparison.run_card", side_effect=guarded):
            body = comparison.evaluate(self.frozen, History(PrefixOnly(), 0), self.cfg, self.profile)
        self.assertEqual(len(calls), 18)
        self.assertFalse(body["holdout_evaluated"])

    def test_no_trades_match_cash_but_cannot_pass_the_evidence_screen(self):
        for candidate in self.body["candidates"]:
            self.assertEqual(len(candidate["validation"]), 5)
            self.assertEqual(candidate["errors"], [])
            self.assertFalse(candidate["conditional_screen_passed"])
            self.assertFalse(candidate["diagnostic_checks"]["minimum_200_trades_in_every_scenario"])
            self.assertTrue(candidate["diagnostic_checks"]["all_cases_flat_without_termination"])

    def test_positive_large_hypothetical_sample_still_cannot_promote_or_select(self):
        body = deepcopy(self.body)
        for candidate in body["candidates"]:
            for item in candidate["validation"].values():
                item["metrics"].update(net_pnl_usd="5", closed_trades=1000)
                item["metrics"]["bootstrap_mean_trade_return"]["lower"] = "0.01"
                for window in item["windows"]:
                    window["positive_expectancy_and_net_equity"] = True
            candidate["diagnostic_checks"] = comparison.diagnostic_checks(candidate)
            candidate["conditional_screen_passed"] = all(candidate["diagnostic_checks"].values())
        store = comparison.ComparisonStore(self.root)
        record = store.load(store.save(body))
        self.assertTrue(all(c["conditional_screen_passed"] for c in record["candidates"]))
        summary = comparison.summary(record)
        self.assertIsNone(summary["selected_strategy"])
        self.assertEqual(summary["qualified_validation_trades"], 0)
        self.assertFalse(summary["phase5_historical_gate_passed"])

    def test_negative_and_failed_cases_are_retained(self):
        with patch("marvel.research.comparison.run_card", side_effect=ResearchError("RESEARCH_INVALID", "fixture")):
            body = comparison.evaluate(self.frozen, self.history, self.cfg, self.profile)
        self.assertTrue(all(len(c["errors"]) == 6 and not c["conditional_screen_passed"] for c in body["candidates"]))
        store = comparison.ComparisonStore(self.root)
        self.assertFalse(store.load(store.save(body))["entries_allowed"])

    def test_diagnostic_forgery_parameter_changes_and_qualification_are_rejected(self):
        for change in ("rules", "criteria", "checks", "qualification"):
            body = deepcopy(self.body)
            if change == "rules":
                body["plan"]["strategies"][0]["lookback"] = 19
            elif change == "criteria":
                body["plan"]["comparison_criteria"]["minimum_closed_trades_per_scenario"] = 1
            elif change == "checks":
                body["candidates"][0]["conditional_screen_passed"] = True
            else:
                body["phase5_historical_gate_passed"] = True
            body["plan_id"] = fingerprint(body["plan"])
            with self.assertRaises(MarvelError):
                comparison.ComparisonStore(self.root).save(body)

    def test_tampered_plan_report_or_record_is_not_overwritten(self):
        for component in ("plan", "report.md", "study.json"):
            store = comparison.ComparisonStore(self.root / component.replace(".", "-"))
            identity = store.save(self.body)
            path = store.root / "plans" / (self.body["plan_id"] + ".json") if component == "plan" else store.root / identity / component
            path.write_bytes(b"damaged")
            with self.assertRaises(MarvelError):
                store.save(self.body)
            self.assertEqual(path.read_bytes(), b"damaged")

    def test_modified_frozen_costs_cannot_be_executed(self):
        frozen = deepcopy(self.frozen)
        frozen["scenarios"]["baseline"]["profile"]["entry_fee_rate"] = "0"
        with self.assertRaises(MarvelError):
            comparison.evaluate(frozen, self.history, self.cfg, self.profile)


class ComparisonCommandTests(WorkspaceCase):
    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "marvel", "--workspace", str(self.root), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=90)

    def test_new_commands_round_trip_without_owner_account_mutations(self):
        self.assertEqual(self.cli("init").returncode, 0)
        self.assertEqual(self.cli("pause", "--reason", "operator").returncode, 0)
        before = sha256(self.cfg_path().read_bytes()).hexdigest()
        result = self.cli("research", "compare", "--dataset", synthetic_dataset(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)["strategy_comparison"]
        loaded = self.cli("research", "compare-inspect", "--run", summary["run_id"])
        self.assertEqual(loaded.returncode, 0, loaded.stdout)
        self.assertIsNone(summary["selected_strategy"])
        self.assertEqual(sha256(self.cfg_path().read_bytes()).hexdigest(), before)
        self.assertFalse((self.root / "var/proxy-cost-studies").exists())

    def cfg_path(self):
        return self.config().database

    def test_no_live_fee_risk_or_final_sample_overrides(self):
        for flag in ("--live", "--allow-network", "--fee", "--risk", "--holdout"):
            self.assertEqual(self.cli("research", "compare", "--dataset", "a" * 64, flag).returncode, 2)
        self.assertEqual(self.cli("research", "compare-inspect", "--run", "a" * 64).returncode, 2)
        self.assertFalse(self.config().database.exists())
