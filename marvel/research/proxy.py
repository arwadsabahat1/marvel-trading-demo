"""Frozen hypothetical cost screens on Binance development prices, never Kraken returns.

All profiles are local RAM-only hypotheses. Existing accounts, original studies,
the original plan and reserved final prices cannot be promoted or overridden.
"""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, localcontext
from pathlib import Path
import re
import tempfile

from .. import __version__
from ..data.quality import INTERVALS, utc_us
from ..data.store import SnapshotStore
from ..errors import MarvelError
from ..risk.model import PRECISION, canonical, fingerprint, load_profile, parse_profile
from .backtest import run
from .metrics import summarize, windows
from .model import STRATEGIES, fail, plan as original_plan, split
from .store import MAX_RECORD_BYTES, decode_record
from .study import compact, source_hashes

# Illustrative reserved ranges, with deliberately invented catalogue identities.
# No personal research snapshot or market-data file is distributed. Real imports
# cannot qualify for a proxy study using these placeholder identities.
REAL_DATASETS = {
    "1" * 64:
        (720, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"),
    "2" * 64:
        (23376, "2024-01-01T00:00:00Z", "2026-09-01T00:00:00Z")}
SCENARIOS = ("baseline", "double_spread_slippage", "double_fees", "miss_every_third_entry", "btc_entry_fee_fallback")


def profiles(original):
    base = dict(original.document)
    base.update(tick_size="0.1", entry_step="0.00000001", exit_step="0.00000001",
        entry_min_qty="0.00006", exit_min_qty="0.00006", entry_min_notional_quote="0.5",
        exit_min_notional_quote="0.5", entry_fee_rate="0.008", exit_fee_rate="0.008",
        entry_fee_asset="USDT", exit_fee_asset="USDT", quote_fee_quantum="0.01",
        base_fee_quantum="0.00000001", entry_slippage_bps="10", exit_slippage_bps="20")
    result = {}
    for scenario in SCENARIOS:
        document = dict(base)
        if scenario == "double_spread_slippage":
            document.update(entry_slippage_bps="20", exit_slippage_bps="40")
        elif scenario == "double_fees":
            document.update(entry_fee_rate="0.016", exit_fee_rate="0.016")
        elif scenario == "btc_entry_fee_fallback":
            document["entry_fee_asset"] = "BTC"
        result[scenario] = parse_profile(document)
    return result


def freeze(config, original, manifest):
    count = manifest["completed_candles"]
    ranges = split(count)
    synthetic = manifest["provenance"]["data_kind"] == "synthetic"
    if manifest["parameters"]["interval"] != "1h":
        fail("RESEARCH_DATA")
    if not synthetic:
        expected = REAL_DATASETS.get(manifest["snapshot_id"])
        if expected is None or (count, manifest["parameters"]["start_us"], manifest["parameters"]["end_us"]) != (
                expected[0], utc_us(expected[1]), utc_us(expected[2])):
            fail("RESEARCH_DATA")  # No re-splitting a different import to unlock a reserved period.
    bound = {name: {"profile_hash": profile.fingerprint, "profile": dict(profile.document),
                   "miss_every": 3 if name == "miss_every_third_entry" else 0}
             for name, profile in profiles(original).items()}
    protected = []
    for identity, (size, start, end) in REAL_DATASETS.items():
        protected.append({"snapshot_id": identity, "start_us": utc_us(start) + split(size)["holdout"][0] * INTERVALS["1h"],
                          "end_us": utc_us(end), "analytics_allowed": False})
    result = {"schema_version": 1, "kind": "btc_quote_fee_proxy_plan_v1",
        "original_plan_id": fingerprint(original_plan()), "config_fingerprint": config.fingerprint,
        "original_profile_hash": original.fingerprint,
        "dataset": {key: deepcopy(manifest[key]) for key in ("snapshot_id", "raw_sha256", "candles_sha256",
            "completed_candles", "provenance", "attribution")},
        "synthetic_engineering_only": synthetic, "splits": ranges, "protected_samples": protected,
        "strategies": [asdict(strategy) for strategy in STRATEGIES], "scenarios": bound,
        "price_source": "Local invented fixture" if synthetic else "Binance BTCUSDT; cross-venue price proxy only",
        "hypothetical_instrument": "BTCUSDT quote-fee hypothesis inspired by dated public Kraken references; not a Kraken adapter",
        "cost_reference_date": "2026-10-28", "reference_fee_schedule": "https://www.kraken.com/features/fee-schedule",
        "full_fill_assumption": "Orders fill fully at modeled prices; one fill fee rounded upward using assumed quanta",
        "failure_policy": "Original gap/stop/minimum/unknown/financial-halt accounting retained; missed-entry and BTC-entry-fee stresses are fixed",
        "partial_or_exit_fee_fallback_qualified": False,
        "quote_rounding": "Upward 0.01 USDT is invented; not observed commission precision or a proved upper bound",
        "spread_fx_latency_and_session": deepcopy({key: original_plan()[key] for key in ("spread_bps", "fx", "latency", "session", "intrabar")}),
        "selection": "Retain all three cards and every scenario; no fitting, ranking, winner, risk reset or automatic promotion",
        "training_use": "Baseline diagnostic only; no parameter estimation",
        "validation_use": "Previously used development ranges; not a new untouched or venue-specific out-of-sample sample",
        "portfolio": "Separate continuous $50 hypothetical portfolio per card/scenario; window boundaries never reset halts",
        "funding_and_actual_execution_costs_qualified": False,
        "historical_promotion_passed": False, "phase5_historical_gate_passed": False,
        "entries_allowed": False, "live_execution_available": False}
    return result


def evaluate(frozen, history, config, original):
    return evaluate_cards(frozen, history, config, original, cards=STRATEGIES,
        freeze_plan=freeze, run_card=run, study_kind="btc_quote_fee_proxy_study_v1")


def evaluate_cards(frozen, history, config, original, *, cards, freeze_plan, run_card, study_kind):
    """Only the development prefix reaches signals, execution, windows and metrics."""
    if fingerprint(frozen) != fingerprint(freeze_plan(config, original, {
            **frozen["dataset"], "parameters": {"interval": "1h", **_parameters(frozen)}})):
        fail("RESEARCH_RECORD")
    if len(history.candles) != frozen["dataset"]["completed_candles"]:
        fail("RESEARCH_DATA")
    before = source_hashes()
    ranges = frozen["splits"]
    development = history.candles[:ranges["validation"][1]]
    if not frozen["synthetic_engineering_only"]:
        for interval in frozen["protected_samples"]:
            if development[0].open_us < interval["end_us"] and development[-1].end_us > interval["start_us"]:
                fail("RESEARCH_DATA")
    candidates = []
    with localcontext() as context:
        context.prec = PRECISION
        for strategy in cards:
            candidate = {"strategy": strategy.identity, "selected_for_deployment": False,
                         "training": None, "validation": {}, "errors": []}
            baseline_profile = parse_profile(frozen["scenarios"]["baseline"]["profile"])
            try:
                training = run_card(development, strategy, config, baseline_profile, *ranges["train"])
                candidate["training"] = {"metrics": summarize(training, config.starting_equity_usd, fingerprint({"plan": frozen, "strategy": strategy.identity, "stage": "training"})),
                                         "halt_reasons": training["halt_reasons"], "termination": training["termination"]}
            except MarvelError as exc:
                candidate["errors"].append({"stage": "training", "code": exc.code})
            for scenario in SCENARIOS:
                try:
                    assumption = frozen["scenarios"][scenario]
                    result = run_card(development, strategy, config, parse_profile(assumption["profile"]),
                        *ranges["validation"], scenario=scenario, miss_every=assumption["miss_every"])
                    metrics = summarize(result, config.starting_equity_usd, fingerprint({"plan": frozen, "strategy": strategy.identity, "scenario": scenario}))
                    candidate["validation"][scenario] = {"metrics": metrics,
                        "windows": windows(result, development, ranges["windows"], config.starting_equity_usd),
                        "cash_comparison_final_equity_usd": str(config.starting_equity_usd), "replay": compact(result)}
                except MarvelError as exc:
                    candidate["errors"].append({"stage": scenario, "code": exc.code})
            baseline = candidate["validation"].get("baseline")
            candidate["diagnostic_positive_baseline_net"] = bool(baseline and Decimal(baseline["metrics"]["net_pnl_usd"]) > 0)
            candidate["diagnostic_positive_in_every_scenario"] = bool(len(candidate["validation"]) == len(SCENARIOS)
                and all(Decimal(item["metrics"]["net_pnl_usd"]) > 0 for item in candidate["validation"].values()))
            candidate["status"] = "conditional_diagnostics_only" if candidate["diagnostic_positive_in_every_scenario"] else "negative_or_insufficient_conditional_evidence"
            candidate["historical_promotion_passed"] = candidate["live_eligible"] = False
            candidates.append(candidate)
    if source_hashes() != before:
        fail("RESEARCH_RECORD")
    return {"schema_version": 1, "kind": study_kind, "application_version": __version__,
        "plan_id": fingerprint(frozen), "plan": deepcopy(frozen), "source_sha256": before,
        "data_kind": "synthetic_engineering_only" if frozen["synthetic_engineering_only"] else "conditional_binance_price_proxy",
        "candidates": candidates, "holdout_evaluated": False, "qualified_validation_trades": 0,
        "phase5_historical_gate_passed": False, "entries_allowed": False, "live_execution_available": False,
        "owner_account_used": False, "network_used": False,
        "status": "unqualified_exploratory_proxy_results",
        "limitations": ["These are hypothetical Binance-price/cost-model results, not Kraken returns or account-specific fees.",
            "Development ranges were already explored; confidence intervals do not correct selection bias or proxy mismatch.",
            "Partial/protective exits, fee-currency fallback, order-book liquidity, funding and regional eligibility remain unqualified.",
            "Missed entries use a fixed deterministic rule; complete fills and the assumed OHLC path are not venue evidence.",
            "Historical guards, fee/FX assumptions, halts and residual ownership remain enforced; failed cases are retained.",
            "Positive numbers or 200 hypothetical trades cannot qualify this proxy for promotion or unlock a final holdout."]}


def _parameters(frozen):
    if frozen["synthetic_engineering_only"]:
        return {}  # A synthetic plan does not bind its invented dates through a real snapshot catalogue.
    _, start, end = REAL_DATASETS[frozen["dataset"]["snapshot_id"]]
    return {"start_us": utc_us(start), "end_us": utc_us(end)}


def markdown(document):
    rows = ["# Exploratory BTC cost screen", "", "Unqualified Binance price proxy. No Kraken return, untouched-sample claim or deployment approval.", "",
            f"Plan: `{document['plan_id']}`. Final holdout evaluated: false.", "",
            "| Strategy | Scenario | Net P&L USD | Hypothetical closed trades | Max drawdown | Halts |",
            "| --- | --- | ---: | ---: | ---: | --- |"]
    for candidate in document["candidates"]:
        for scenario, item in sorted(candidate["validation"].items()):
            metrics = item["metrics"]
            rows.append(f"| {candidate['strategy']} | {scenario} | {metrics['net_pnl_usd']} | {metrics['closed_trades']} | {metrics['maximum_drawdown_fraction']} | {', '.join(item['replay']['halt_reasons'])} |")
        for error in candidate["errors"]:
            rows.append(f"| {candidate['strategy']} | {error['stage']} | unavailable | 0 | unavailable | {error['code']} |")
    rows.extend(["", "All financial qualification flags are false; hypothetical trades are not qualifying strategy trades.", ""])
    return "\n".join(rows)


class ProxyStore:
    directory = "var/proxy-cost-studies"

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / self.directory
        self.path(self.root)

    def validate(self, document):
        _validate(document)

    def render(self, document):
        return markdown(document)

    def path(self, path):
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace) or not resolved.is_relative_to(self.root.resolve()):
            fail("RESEARCH_RECORD")
        return path

    def identity(self, value):
        if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
            fail("RESEARCH_RECORD")
        return value

    def read(self, path):
        try:
            with self.path(path).open("rb") as handle:
                raw = handle.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                fail("RESEARCH_RECORD")
            return raw
        except OSError:
            fail("RESEARCH_RECORD")

    def freeze(self, frozen):
        identity = fingerprint(frozen)
        folder = self.path(self.root / "plans")
        folder.mkdir(parents=True, exist_ok=True)
        path = self.path(folder / (identity + ".json"))
        raw = (canonical(frozen) + "\n").encode("ascii")
        try:
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            if self.read(path) != raw:
                fail("RESEARCH_RECORD")
        return identity

    def load(self, identity):
        folder = self.path(self.root / self.identity(identity))
        try:
            document = decode_record(self.read(folder / "study.json"))
            body = {key: value for key, value in document.items() if key != "run_id"}
            if document.get("run_id") != identity or fingerprint(body) != identity:
                fail("RESEARCH_RECORD")
            self.validate(body)
            if self.read(self.root / "plans" / (body["plan_id"] + ".json")) != (canonical(body["plan"]) + "\n").encode("ascii"):
                fail("RESEARCH_RECORD")
            if self.read(folder / "report.md") != self.render(body).encode("utf-8"):
                fail("RESEARCH_RECORD")
            return document
        except (KeyError, TypeError, ValueError):
            fail("RESEARCH_RECORD")

    def save(self, body):
        self.validate(body)
        identity = fingerprint(body)
        raw = (canonical({**body, "run_id": identity}) + "\n").encode("ascii")
        if len(raw) > MAX_RECORD_BYTES:
            fail("RESEARCH_RECORD")
        self.freeze(body["plan"])
        folder = self.path(self.root / identity)
        if folder.exists():
            self.load(identity)
            return identity
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=self.root) as name:
            staging = self.path(Path(name))
            (staging / "study.json").write_bytes(raw)
            (staging / "report.md").write_bytes(self.render(body).encode("utf-8"))
            try:
                staging.rename(folder)
            except OSError:
                if not folder.exists():
                    fail("RESEARCH_RECORD")
                self.load(identity)
        return identity


def _validate(document, *, strategies=STRATEGIES, study_kind="btc_quote_fee_proxy_study_v1", plan_kind="btc_quote_fee_proxy_plan_v1"):
    try:
        expected = {"schema_version", "kind", "application_version", "plan_id", "plan", "source_sha256",
            "data_kind", "candidates", "holdout_evaluated", "qualified_validation_trades", "phase5_historical_gate_passed",
            "entries_allowed", "live_execution_available", "owner_account_used", "network_used", "status", "limitations"}
        if (type(document) is not dict or set(document) != expected
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or document["kind"] != study_kind or document["status"] != "unqualified_exploratory_proxy_results"
                or fingerprint(document["plan"]) != document["plan_id"] or len(document["candidates"]) != len(strategies)
                or type(document["qualified_validation_trades"]) is not int or document["qualified_validation_trades"] != 0
                or any(document[field] is not False for field in ("holdout_evaluated", "phase5_historical_gate_passed",
                    "entries_allowed", "live_execution_available", "owner_account_used", "network_used"))):
            fail("RESEARCH_RECORD")
        frozen = document["plan"]
        if (frozen["kind"] != plan_kind or type(frozen["schema_version"]) is not int
                or frozen["schema_version"] != 1 or frozen["strategies"] != [asdict(s) for s in strategies]
                or set(frozen["scenarios"]) != set(SCENARIOS)
                or frozen["splits"] != split(frozen["dataset"]["completed_candles"])
                or any(frozen[field] is not False for field in ("partial_or_exit_fee_fallback_qualified",
                    "funding_and_actual_execution_costs_qualified", "historical_promotion_passed",
                    "phase5_historical_gate_passed", "entries_allowed", "live_execution_available"))):
            fail("RESEARCH_RECORD")
        for name, item in frozen["scenarios"].items():
            parse_profile(item["profile"])
            if fingerprint(item["profile"]) != item["profile_hash"] or type(item["miss_every"]) is not int or item["miss_every"] != (3 if name == "miss_every_third_entry" else 0):
                fail("RESEARCH_RECORD")
        for candidate, strategy in zip(document["candidates"], strategies):
            if (candidate["strategy"] != strategy.identity or candidate["selected_for_deployment"] is not False
                    or candidate["historical_promotion_passed"] is not False or candidate["live_eligible"] is not False
                    or not set(candidate["validation"]) <= set(SCENARIOS)):
                fail("RESEARCH_RECORD")
    except (KeyError, TypeError, ValueError):
        fail("RESEARCH_RECORD")


def execute(config, dataset):
    manifest, history = SnapshotStore(config.workspace).load(dataset)
    original = load_profile(config.workspace)
    frozen = freeze(config, original, manifest)
    store = ProxyStore(config.workspace)
    store.freeze(frozen)  # Must happen before the first feature, execution or metric call.
    body = evaluate(frozen, history, config, original)
    identity = store.save(body)
    return summary(store.load(identity))


def summary(document):
    return {key: document[key] for key in ("run_id", "plan_id", "data_kind", "status", "holdout_evaluated",
            "qualified_validation_trades", "phase5_historical_gate_passed", "entries_allowed", "live_execution_available")} | {
        "dataset_id": document["plan"]["dataset"]["snapshot_id"], "owner_account_used": False,
        "report_file": f"var/proxy-cost-studies/{document['run_id']}/report.md",
        "candidates": [{"strategy": candidate["strategy"], "status": candidate["status"], "errors": candidate["errors"],
            "validation": {name: {"metrics": item["metrics"], "halt_reasons": item["replay"]["halt_reasons"],
                "termination": item["replay"]["termination"], "rejections": item["replay"]["rejections"]}
                for name, item in candidate["validation"].items()}} for candidate in document["candidates"]]}
