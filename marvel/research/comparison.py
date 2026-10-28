"""Bounded family comparison on previously explored development prices, never promotion."""

from dataclasses import asdict
from decimal import Decimal

from ..data.store import SnapshotStore
from ..risk.model import fingerprint, load_profile
from . import backtest, proxy
from .model import fail
from .rules import RULES, signal

PLAN_KIND = "btc_strategy_family_plan_v1"
STUDY_KIND = "btc_strategy_family_study_v1"
CRITERIA = {"minimum_closed_trades_per_scenario": 200, "maximum_drawdown_exclusive": "0.05",
    "minimum_positive_validation_windows": 4, "baseline_bootstrap_lower_exclusive": "0",
    "all_scenarios_net_positive": True, "no_open_holdings_errors_or_termination": True,
    "cash_benchmark_pnl_usd": "0", "financial_promotion_available": False}


def definitions():
    return {
        "trend_pullback": "1h close crosses back above rolling SMA20 after previous close at/below its SMA20; completed 4h close > SMA12 > prior SMA12. Exit on 1h close below SMA20.",
        "cost_breakout": "1h close exceeds prior 20 highs with the same rising 4h trend; 2*ATR14/close >= 0.04. Exit below prior 10 lows. The 4% volatility hurdle is not a return forecast or a guaranteed cost bound.",
        "range_rebound": "Completed 4h close within 1% of its SMA12; previous 1h close below its 20-bar mean minus two population standard deviations; current close recovers inside lower band but remains below mean. Exit at/above rolling mean or below rolling lower band.",
        "shared": "Long-only; initial stop close minus 2*ATR14; completed prefixes only; original one-full-hour latency, OHLC path, 23:00 UTC flat attempt and all financial halts retained. Rolling exits are recalculated; no target-fill assumption.",
        "costs": "Same five hypothetical scenarios as the prior proxy; no maker-fill, fee discount, overnight waiver, parameter search, compounding or provider claim.",
        "sources": ["https://www.nber.org/papers/w24877", "https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf",
                    "https://arxiv.org/abs/2606.00060"],
        "evidence_scope": "Sources motivate momentum/cost discipline and caution about selection; none validates these exact rule cards or a $50 strategy."}


def freeze(config, original, manifest):
    frozen = proxy.freeze(config, original, manifest)
    frozen.update(kind=PLAN_KIND, strategies=[asdict(rule) for rule in RULES],
        rule_definitions=definitions(), comparison_criteria=dict(CRITERIA),
        selection="Three additional fixed families, one parameter set each; all results retained. Cash is an explicit alternative. No best-strategy or statistical-dominance claim from reused data.")
    return frozen


def run_card(*args, **kwargs):
    return backtest.run(*args, **kwargs, signal_function=signal)


def diagnostic_checks(candidate):
    cases = candidate["validation"]
    baseline = cases.get("baseline")
    complete = set(cases) == set(proxy.SCENARIOS) and not candidate["errors"]
    lower = baseline["metrics"]["bootstrap_mean_trade_return"]["lower"] if baseline else None
    return {
        "all_cases_complete": complete,
        "positive_net_in_every_scenario": complete and all(Decimal(c["metrics"]["net_pnl_usd"]) > 0 for c in cases.values()),
        "minimum_200_trades_in_every_scenario": complete and all(c["metrics"]["closed_trades"] >= 200 for c in cases.values()),
        "drawdown_below_5_percent_in_every_scenario": complete and all(Decimal(c["metrics"]["maximum_drawdown_fraction"]) < Decimal("0.05") for c in cases.values()),
        "four_positive_windows_in_every_scenario": complete and all(sum(w["positive_expectancy_and_net_equity"] for w in c["windows"]) >= 4 for c in cases.values()),
        "baseline_bootstrap_lower_positive": lower is not None and Decimal(lower) > 0,
        "all_cases_flat_without_termination": complete and all(c["metrics"]["open_position_count"] == 0 and Decimal(c["metrics"]["holding_btc"]) == 0 and c["replay"]["termination"] is None for c in cases.values())}


def evaluate(frozen, history, config, original):
    result = proxy.evaluate_cards(frozen, history, config, original, cards=RULES,
        freeze_plan=freeze, run_card=run_card, study_kind=STUDY_KIND)
    for candidate in result["candidates"]:
        candidate["diagnostic_checks"] = diagnostic_checks(candidate)
        candidate["conditional_screen_passed"] = all(candidate["diagnostic_checks"].values())
    result["limitations"].append("Three additional rule families were considered after negative earlier results; development reuse and multiple testing prevent an unbiased winner claim. No CSCV/PBO correction or statistical dominance test is implemented.")
    return result


class ComparisonStore(proxy.ProxyStore):
    directory = "var/strategy-comparisons"

    def validate(self, document):
        proxy._validate(document, strategies=RULES, study_kind=STUDY_KIND, plan_kind=PLAN_KIND)
        try:
            if document["plan"]["rule_definitions"] != definitions() or document["plan"]["comparison_criteria"] != CRITERIA:
                fail("RESEARCH_RECORD")
            for candidate in document["candidates"]:
                checks = diagnostic_checks(candidate)
                if candidate["diagnostic_checks"] != checks or candidate["conditional_screen_passed"] is not all(checks.values()):
                    fail("RESEARCH_RECORD")
        except (KeyError, TypeError, ValueError, ArithmeticError):
            fail("RESEARCH_RECORD")

    def render(self, document):
        original = proxy.markdown(document)
        return original.replace("# Exploratory BTC cost screen", "# Exploratory BTC strategy family comparison", 1) + (
            "\nCash benchmark: $0 P&L. Previously used development data; no best-strategy or dominance claim.\n\n" +
            "\n".join(f"{c['strategy']}: conditional diagnostic screen passed = {str(c['conditional_screen_passed']).lower()}; selected for deployment = false."
                      for c in document["candidates"]) + "\n")


def execute(config, dataset):
    manifest, history = SnapshotStore(config.workspace).load(dataset)
    original = load_profile(config.workspace)
    frozen = freeze(config, original, manifest)
    store = ComparisonStore(config.workspace)
    store.freeze(frozen)
    identity = store.save(evaluate(frozen, history, config, original))
    return summary(store.load(identity))


def summary(document):
    result = proxy.summary(document)
    result["report_file"] = f"var/strategy-comparisons/{document['run_id']}/report.md"
    result["cash_benchmark_pnl_usd"] = "0"
    result["selected_strategy"] = None
    for target, candidate in zip(result["candidates"], document["candidates"]):
        target["diagnostic_checks"] = candidate["diagnostic_checks"]
        target["conditional_screen_passed"] = candidate["conditional_screen_passed"]
    return result
