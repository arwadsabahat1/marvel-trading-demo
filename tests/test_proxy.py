from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
from functools import lru_cache
from hashlib import sha256
import json
import subprocess
import sys
from unittest.mock import patch

from marvel.data.quality import History, utc_us
from marvel.data.store import SnapshotStore
from marvel.errors import MarvelError, ResearchError
from marvel.research.backtest import Broker
from marvel.research.commands import synthetic_dataset
from marvel.research.model import plan as original_plan
from marvel.research import proxy
from marvel.risk.model import canonical, fingerprint, load_profile
from tests.helpers import ROOT, WorkspaceCase


@lru_cache(maxsize=1)
def fixture():
    owner = WorkspaceCase()
    owner.setUp()
    try:
        manifest, history = SnapshotStore(owner.root).load(synthetic_dataset(owner.root))
        profile = load_profile(owner.root)
        frozen = proxy.freeze(owner.config(), profile, manifest)
        result = proxy.evaluate(frozen, history, owner.config(), profile)
        return deepcopy((manifest, history, frozen, result))
    finally:
        owner.doCleanups()


class ProxyTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg, self.profile = self.config(), load_profile(self.root)
        self.manifest, self.history, self.frozen, self.body = deepcopy(fixture())

    def test_original_profile_plan_limits_and_owner_account_remain_unchanged(self):
        original = dict(self.profile.document)
        plan_id = fingerprint(original_plan())
        profiles = proxy.profiles(self.profile)
        self.assertEqual(dict(self.profile.document), original)
        self.assertEqual(profiles["baseline"].document["entry_fee_asset"], "USDT")
        self.assertEqual(profiles["baseline"].n("entry_fee_rate"), Decimal("0.008"))
        self.assertEqual(profiles["double_fees"].n("entry_fee_rate"), Decimal("0.016"))
        self.assertEqual(profiles["double_spread_slippage"].n("exit_slippage_bps"), 40)
        self.assertEqual(profiles["btc_entry_fee_fallback"].document["entry_fee_asset"], "BTC")
        self.assertEqual(fingerprint(original_plan()), plan_id)
        self.assertEqual(self.cfg.risk.per_trade_cap_usd, 1)
        self.assertFalse(self.cfg.database.exists())

    def test_plan_preserves_splits_source_and_both_protected_samples(self):
        self.assertEqual(self.frozen["splits"]["holdout"], [576, 720])
        self.assertEqual(len(self.frozen["protected_samples"]), 2)
        self.assertTrue(all(p["analytics_allowed"] is False for p in self.frozen["protected_samples"]))
        self.assertIn("not a Kraken adapter", self.frozen["hypothetical_instrument"])
        self.assertEqual(self.frozen["original_profile_hash"], self.profile.fingerprint)

    def test_different_real_import_cannot_shift_or_unlock_a_reserved_range(self):
        document = deepcopy(self.manifest)
        document["provenance"]["data_kind"] = "binance_spot"
        with self.assertRaises(MarvelError):
            proxy.freeze(self.cfg, self.profile, document)
        identity, (count, first, last) = next(iter(proxy.REAL_DATASETS.items()))
        document.update(snapshot_id=identity, completed_candles=count)
        document["parameters"].update(start_us=utc_us(first), end_us=utc_us(last))
        frozen = proxy.freeze(self.cfg, self.profile, document)
        self.assertFalse(frozen["synthetic_engineering_only"])
        document["parameters"]["start_us"] += 3600000000
        with self.assertRaises(MarvelError):
            proxy.freeze(self.cfg, self.profile, document)

    def test_saved_plan_exists_before_any_strategy_evaluation(self):
        dataset = synthetic_dataset(self.root)
        def evaluate(frozen, *_):
            path = self.root / "var/proxy-cost-studies/plans" / (fingerprint(frozen) + ".json")
            self.assertEqual(path.read_bytes(), (canonical(frozen) + "\n").encode("ascii"))
            return deepcopy(self.body)
        with patch("marvel.research.proxy.evaluate", side_effect=evaluate):
            result = proxy.execute(self.cfg, dataset)
        self.assertEqual(result["qualified_validation_trades"], 0)
        self.assertFalse(self.cfg.database.exists())
        self.assertFalse((self.root / "var/research-runs").exists())

    def test_no_feature_execution_or_metric_receives_the_reserved_tail(self):
        actual = proxy.run
        received = []
        def guarded(candles, *args, **kwargs):
            self.assertEqual(len(candles), 576)
            received.append(len(candles))
            return actual(candles, *args, **kwargs)
        class PrefixOnly:
            def __len__(inner):
                return 720
            def __getitem__(inner, index):
                self.assertIsInstance(index, slice)
                self.assertLessEqual(index.stop, 576)
                return self.history.candles[index]
        with patch("marvel.research.proxy.run", side_effect=guarded):
            result = proxy.evaluate(self.frozen, History(PrefixOnly(), 0), self.cfg, self.profile)
        self.assertEqual(len(received), 18)
        self.assertFalse(result["holdout_evaluated"])

    def test_unqualified_positive_statistics_cannot_promote_any_candidate(self):
        body = deepcopy(self.body)
        for candidate in body["candidates"]:
            for item in candidate["validation"].values():
                item["metrics"].update(net_pnl_usd="100", closed_trades=1000)
            candidate["diagnostic_positive_baseline_net"] = True
            candidate["diagnostic_positive_in_every_scenario"] = True
        store = proxy.ProxyStore(self.root)
        identity = store.save(body)
        saved = store.load(identity)
        self.assertEqual(saved["qualified_validation_trades"], 0)
        self.assertFalse(saved["phase5_historical_gate_passed"])
        self.assertTrue(all(c["selected_for_deployment"] is False for c in saved["candidates"]))

    def test_record_cannot_advertise_qualification_even_if_rehashed(self):
        for target, field in (("root", "entries_allowed"), ("plan", "phase5_historical_gate_passed"), ("candidate", "live_eligible")):
            with self.subTest(target=target, field=field):
                body = deepcopy(self.body)
                selected = body if target == "root" else body["plan"] if target == "plan" else body["candidates"][0]
                selected[field] = True
                body["plan_id"] = fingerprint(body["plan"])
                with self.assertRaises(MarvelError):
                    proxy.ProxyStore(self.root).save(body)

    def test_unknown_plan_or_fee_scenario_cannot_be_executed(self):
        frozen = deepcopy(self.frozen)
        frozen["scenarios"]["baseline"]["profile"]["entry_fee_rate"] = "0"
        with self.assertRaises(MarvelError):
            proxy.evaluate(frozen, self.history, self.cfg, self.profile)

    def test_failed_cases_are_retained_without_an_approved_result(self):
        with patch("marvel.research.proxy.run", side_effect=ResearchError("RESEARCH_INVALID", "fixture")):
            result = proxy.evaluate(self.frozen, self.history, self.cfg, self.profile)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertTrue(all(len(c["errors"]) == 6 for c in result["candidates"]))
        self.assertTrue(all(c["validation"] == {} for c in result["candidates"]))
        self.assertFalse(result["phase5_historical_gate_passed"])

    def test_each_scenario_keeps_its_continuous_portfolio_and_halts(self):
        for candidate in self.body["candidates"]:
            self.assertEqual(set(candidate["validation"]), set(proxy.SCENARIOS))
            for item in candidate["validation"].values():
                windows = item["windows"]
                self.assertEqual(len(windows), 6)
                for previous, current in zip(windows, windows[1:]):
                    self.assertEqual(previous["ending_equity_usd"], current["starting_equity_usd"])
                self.assertEqual(item["replay"]["open_position_count"], 0)
                self.assertEqual(item["metrics"]["holding_btc"], "0")

    def test_missed_entry_stress_never_reserves_or_fills_a_skipped_entry(self):
        when = utc_us("2026-01-01T12:00:00Z")
        profile = proxy.profiles(self.profile)["baseline"]
        broker = Broker(self.cfg, profile, when, "baseline", miss_every=1)
        before = deepcopy(broker.state)
        result = broker.enter(when, Decimal(80000), "79000", when - 3600000000)
        self.assertFalse(result["paper_submitted"])
        self.assertIn("MODELED_ENTRY_MISS", result["risk_decision"]["reasons"])
        self.assertEqual(broker.state, before)
        self.assertEqual(broker.book["orders"], [])

    def test_default_backtest_does_not_skip_entries_and_bad_miss_counts_fail(self):
        when = utc_us("2026-01-01T12:00:00Z")
        profile = proxy.profiles(self.profile)["baseline"]
        broker = Broker(self.cfg, profile, when, "baseline")
        self.assertTrue(broker.enter(when, Decimal(80000), "79000", when - 3600000000)["paper_submitted"])
        for count in (True, -1, 101, "3"):
            with self.assertRaises(MarvelError):
                Broker(self.cfg, profile, when, "baseline", miss_every=count)

    def test_identical_save_and_restart_preserve_one_study(self):
        store = proxy.ProxyStore(self.root)
        identity = store.save(self.body)
        self.assertEqual(store.save(self.body), identity)
        self.assertEqual(proxy.ProxyStore(self.root).load(identity)["plan_id"], fingerprint(self.frozen))
        self.assertEqual(len([p for p in store.root.iterdir() if p.name != "plans"]), 1)

    def test_corrupt_study_report_or_frozen_plan_is_not_replaced(self):
        for component in ("study.json", "report.md", "plan"):
            with self.subTest(component=component):
                store = proxy.ProxyStore(self.root / component.replace(".", "-"))
                identity = store.save(self.body)
                path = store.root / identity / component if component != "plan" else store.root / "plans" / (self.body["plan_id"] + ".json")
                path.write_bytes(b"damaged")
                with self.assertRaises(MarvelError):
                    store.load(identity)
                with self.assertRaises(MarvelError):
                    store.save(self.body)
                self.assertEqual(path.read_bytes(), b"damaged")

    def test_strict_schema_size_and_identifier_validation(self):
        for change in ({"schema_version": True}, {"unknown": "value"}, {"qualified_validation_trades": True}):
            body = {**self.body, **change}
            with self.assertRaises(MarvelError):
                proxy.ProxyStore(self.root).save(body)
        for value in ("../escape", "A" * 64, None):
            with self.assertRaises(MarvelError):
                proxy.ProxyStore(self.root).load(value)
        store = proxy.ProxyStore(self.root)
        identity = store.save(self.body)
        with patch("marvel.research.proxy.MAX_RECORD_BYTES", 100), self.assertRaises(MarvelError):
            store.load(identity)

    def test_source_change_during_calculation_rejects_the_result(self):
        with patch("marvel.research.proxy.source_hashes", side_effect=[{"version": "old"}, {"version": "new"}]), patch(
                "marvel.research.proxy.run", side_effect=ResearchError("RESEARCH_INVALID", "fixture")):
            with self.assertRaises(MarvelError):
                proxy.evaluate(self.frozen, self.history, self.cfg, self.profile)

    def test_low_decimal_context_cannot_change_the_frozen_costs(self):
        expected = fingerprint(self.frozen)
        with localcontext() as context:
            context.prec = 6
            actual = proxy.freeze(self.cfg, self.profile, self.manifest)
        self.assertEqual(fingerprint(actual), expected)


class ProxyCommandTests(WorkspaceCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "marvel", "--workspace", str(self.root), *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=90)

    def test_command_has_no_live_fee_risk_or_holdout_overrides(self):
        for flag in ("--live", "--allow-network", "--fee", "--risk", "--holdout"):
            result = self.run_cli("research", "proxy-screen", "--dataset", "a" * 64, flag)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.config().database.exists())

    def test_proxy_study_round_trips_across_processes_without_touching_owner_state(self):
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("pause", "--reason", "operator").returncode, 0)
        before = sha256(self.config().database.read_bytes()).hexdigest()
        result = self.run_cli("research", "proxy-screen", "--dataset", synthetic_dataset(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)["proxy_cost_screen"]
        inspected = self.run_cli("research", "proxy-inspect", "--run", summary["run_id"])
        self.assertEqual(inspected.returncode, 0, inspected.stdout)
        record = json.loads(inspected.stdout)["proxy_cost_study"]
        self.assertEqual(len(record["candidates"]), 3)
        self.assertFalse(record["phase5_historical_gate_passed"])
        self.assertEqual(sha256(self.config().database.read_bytes()).hexdigest(), before)
        self.assertFalse((self.root / "var/research-runs").exists())
