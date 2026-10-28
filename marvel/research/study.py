"""All fixed candidates and failures retained; reserved final bars never evaluated."""

from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path

from .. import __version__
from ..errors import MarvelError
from ..risk.model import PRECISION, fingerprint, text
from .backtest import run
from .benchmarks import matched, passive
from .metrics import summarize, windows
from .model import SCENARIOS, STRATEGIES, card_id, fail, plan, scenario_profile, split


def source_hashes():
    package = Path(__file__).resolve().parents[1]
    from hashlib import sha256
    return {path.relative_to(package).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(package.rglob("*.py"))}


def gates(metrics, folds, stress_metrics, synthetic, *, model_complete=True):
    ci = metrics["bootstrap_mean_trade_return"]["lower"]
    statistical = {
        "positive_aggregate_net_expectancy": metrics["expectancy_usd"] is not None and Decimal(metrics["expectancy_usd"]) > 0 and Decimal(metrics["net_pnl_usd"]) > 0,
        "six_windows_two_thirds_positive": len(folds) >= 6 and sum(f["positive_expectancy_and_net_equity"] for f in folds) * 3 >= len(folds) * 2,
        "at_least_200_oos_closed_trades": metrics["closed_trades"] >= 200,
        "positive_lower_95_block_bootstrap_bound": ci is not None and Decimal(ci) > 0,
        "drawdown_below_five_percent": Decimal(metrics["maximum_drawdown_fraction"]) < Decimal("0.05"),
        "net_positive_doubled_spread_slippage": "double_spread_slippage" in stress_metrics and Decimal(stress_metrics["double_spread_slippage"]["net_pnl_usd"]) > 0,
        "net_positive_assumed_higher_fees": "assumed_higher_fees" in stress_metrics and Decimal(stress_metrics["assumed_higher_fees"]["net_pnl_usd"]) > 0,
        "flat_no_residual_exposure": metrics["open_position_count"] == 0 and Decimal(metrics["holding_btc"]) == 0,
        "rising_falling_sideways_and_stress_coverage": {"rising", "falling", "sideways"} <= {f["regime_diagnostic_only"] for f in folds} and any(f["stress_range_observed"] for f in folds),
        "real_market_sample": not synthetic,
        "no_resource_or_model_termination": model_complete,
    }
    # No simulated statistic can certify missing actual costs, availability or a holdout.
    missing = {"applicable_worst_fee_tier_verified": False, "historical_fx_and_execution_inputs_qualified": False,
               "untouched_final_holdout_passed": False,
               "neighbour_parameter_stability_qualified": False,
               "partial_and_missed_fill_performance_qualified": False}
    return {"checks": {**statistical, **missing}, "development_statistical_screen_passed": all(statistical.values()),
            "historical_promotion_passed": False, "live_eligible": False,
            "later_release_prerequisites": {"forward_paper_record_passed": False},
            "failed_or_unverified": sorted(key for key, value in {**statistical, **missing}.items() if not value)}


def compact(result):
    result = deepcopy(result)
    result.pop("intrabar_marks")  # Metrics include every mark; the immutable replay recipe recreates the path.
    result["ledger"].pop("events")  # The full event-chain checksum and deterministic recipe cover these IDs.
    return result


def evaluate(manifest, history, config, profile):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        sources_before = source_hashes()
        ranges = split(len(history.candles))
        train_start, train_end = ranges["train"]
        validation_start, validation_end = ranges["validation"]
        # No execution, feature or benchmark code receives the reserved tail.
        development = history.candles[:validation_end]
        synthetic = manifest["provenance"]["data_kind"] == "synthetic"
        candidates = []
        for strategy in STRATEGIES:
            identifier = card_id(strategy)
            candidate = {"candidate_id": identifier, "strategy": strategy.identity, "selected_for_deployment": False,
                         "training": None, "validation": {}, "status": "insufficient_or_negative_evidence"}
            try:
                training = run(development, strategy, config, profile, train_start, train_end)
                candidate["training"] = {"metrics": summarize(training, config.starting_equity_usd, identifier + "training"),
                                         "selection_use": "Diagnostic only; no fitted parameters or winner chosen"}
                stress_metrics = {}
                for scenario in SCENARIOS:
                    assumptions = scenario_profile(profile, scenario)
                    result = run(development, strategy, config, assumptions, validation_start, validation_end, scenario)
                    metrics = summarize(result, config.starting_equity_usd, identifier + scenario)
                    folds = windows(result, development, ranges["windows"], config.starting_equity_usd)
                    benchmark = passive(development, validation_start, validation_end, config, assumptions, scenario)
                    candidate["validation"][scenario] = {"profile_hash": assumptions.fingerprint,
                        "profile": dict(assumptions.document), "metrics": metrics, "windows": folds,
                        "benchmarks": {"cash": {"final_equity_usd": text(config.starting_equity_usd), "net_return_fraction": "0"},
                                       "passive_initial_twenty_percent_budget": benchmark,
                                       "exposure_matched_cash_passive": matched(benchmark, metrics["mean_hour_end_exposure_fraction"], config.starting_equity_usd)},
                        "replay": compact(result)}
                    stress_metrics[scenario] = metrics
                baseline = candidate["validation"]["baseline"]
                complete = all(result["replay"]["termination"] is None for result in candidate["validation"].values())
                candidate["gates"] = gates(baseline["metrics"], baseline["windows"], stress_metrics, synthetic, model_complete=complete)
                if baseline["metrics"]["open_position_count"] or baseline["replay"]["halt_reasons"]:
                    candidate["status"] = "blocked_by_risk_or_tradeability"
                if not complete:
                    candidate["status"] = "blocked_by_resource_limit"
            except MarvelError as exc:
                candidate.update(status="model_or_history_error", error_code=exc.code,
                                 gates={"historical_promotion_passed": False, "live_eligible": False})
            candidates.append(candidate)
        if source_hashes() != sources_before:
            fail("RESEARCH_RECORD")  # Do not attach changed source to calculations from an earlier version.
        return {"schema_version": 1, "application_version": __version__, "plan": plan(), "plan_id": fingerprint(plan()),
                "source_sha256": sources_before, "config_fingerprint": config.fingerprint, "original_profile_hash": profile.fingerprint,
                "dataset": {key: manifest[key] for key in ("snapshot_id", "raw_sha256", "candles_sha256", "completed_candles", "provenance", "attribution")},
                "data_kind": "synthetic_engineering_only" if synthetic else "historical_research_observations",
                "splits": ranges, "validation_portfolio": "Each fixed card/scenario has one continuous $50 hypothetical portfolio; folds never reset balances/halts",
                "holdout": {"range": ranges["holdout"], "evaluated": False, "reason": "Reserved; no release while development/qualification gates remain unmet",
                            "historical_untouched_status": "Designated by this frozen plan; external prior inspection cannot be certified"},
                "candidates": candidates, "phase5_historical_gate_passed": False,
                "status": "blocked_historical_qualification", "research_only": True, "entries_allowed": False,
                "execution_eligible": False, "live_execution_available": False,
                "limitations": ["Hourly OHLCV does not contain executable bid/ask, order-book liquidity, actual latency or intrabar event order",
                                "Open-high-low-close is one hypothetical path, not verified intrabar chronology or a tail-loss bound",
                                "USD/USDT bands and higher-fee scenario are assumptions, not historical FX or an applicable worst fee tier",
                                "23:00 flat attempt is a fixed operational constraint; residuals and existing halts are never erased",
                                "All fixed variants/scenarios are retained; no holdout result or automatic strategy deployment is supplied"]}
