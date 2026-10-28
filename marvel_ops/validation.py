"""Costed historical evaluation and evidence-derived release gates."""

from decimal import Decimal
from datetime import timedelta

from marvel.data.store import SnapshotStore
from marvel.research.study import evaluate
from marvel.research.backtest import run
from marvel.research.metrics import summarize
from marvel.research.model import STRATEGIES, SCENARIOS, scenario_profile, split
from marvel.risk.model import fingerprint, load_profile
from .common import bindings, parse_stamp, require, stamp, utc_now


def validate(config, dataset, store):
    manifest, history = SnapshotStore(config.workspace).load(dataset)
    study = evaluate(manifest, history, config, load_profile(config.workspace))
    candidates = []
    for candidate in study["candidates"]:
        checks = candidate["gates"].get("checks", {"complete_model_result": False})
        candidates.append({"strategy": candidate["strategy"], "checks": checks,
                           "statistical_passed": candidate["gates"].get("development_statistical_screen_passed", False),
                           "qualified": all(checks.values())})
    document = {"kind": "historical_validation", "bindings": bindings(config), "at": stamp(utc_now()),
                "dataset": dataset, "synthetic": manifest["provenance"]["data_kind"] == "synthetic",
                "candidates": candidates, "holdout_evaluated": study["holdout"]["evaluated"],
                "complete_study": study}
    return {"record": store.record(document), "candidates": candidates, "strategy_qualified": False,
            "reason": "Additional real-cost, forward, Testnet and operator evidence is required"}


def readiness(store, state=None, now=None):
    state, now = state or store.load(), now or utc_now()
    bound = bindings(store.config)
    from .store import Store
    records = []
    for mode in ("paper", "testnet", "live"):
        source = Store(store.config, mode)
        if source.path.exists():
            source.load()
            records.extend(r for r in source.evidence() if r.get("bindings") == bound)
    qualified = set()
    for row in records:
        if row.get("kind") == "historical_validation_final" and row.get("synthetic") is False:
            for candidate in row["candidates"]:
                if candidate["qualified"] is True and row["holdout_evaluated"] is True:
                    qualified.add(candidate["strategy"])
    forward = [r for r in records if r.get("kind") == "forward_paper_trade" and r.get("timely") is True
               and r.get("closed") is True and r.get("strategy") in qualified and r.get("provenance") == "public_network"]
    testnet = [r for r in records if r.get("kind") == "exchange_settlement" and r.get("environment") == "testnet"
               and r.get("provenance") == "authenticated_network" and r.get("owned_btc") == "0"
               and r.get("balanced") is True and r.get("had_fill") is True]
    days = (parse_stamp(forward[-1]["at"]) - parse_stamp(forward[0]["at"])).total_seconds() / 86400 if len(forward) > 1 else 0
    attestation = state.get("release")
    reviewed = bool(attestation and attestation.get("bindings") == bound
                    and 0 <= (now - parse_stamp(attestation["at"])).total_seconds() <= 86400
                    and all(attestation.get(k) is True for k in (
                        "operator_acknowledged", "provider_access_reviewed", "data_rights_reviewed",
                        "actual_costs_and_fx_reviewed")))
    checks = {"historical_strategy_and_holdout": bool(qualified),
              "forward_200_trades_30_days": len(forward) >= 200 and days >= 30,
              "forward_positive_net": bool(forward) and sum(Decimal(r["net_pnl_usd"]) for r in forward) > 0,
              "ten_reconciled_testnet_fills": len(testnet) >= 10,
              "current_operator_release": reviewed, "no_review_halts": not state["halts"],
              "source_and_policy_unchanged": state["bindings"] == bound}
    return {"checks": checks, "qualified_strategies": sorted(qualified), "forward_trades": len(forward),
            "forward_days": days, "testnet_cycles": len(testnet), "live_entries_allowed": all(checks.values()),
            "blockers": sorted(k for k, passed in checks.items() if not passed)}


def finalize(store, development_record, *, acknowledge_holdout=False):
    with store.lock():
        return _finalize(store, development_record, acknowledge_holdout=acknowledge_holdout)


def _finalize(store, development_record, *, acknowledge_holdout=False):
    """One immutable holdout attempt after a successful fixed development screen."""
    require(acknowledge_holdout is True, "OPS_HOLDOUT_ACK_REQUIRED")
    records = store.evidence()
    matches = [r for r in records if fingerprint(r) == development_record and r.get("kind") == "historical_validation"]
    require(len(matches) == 1 and matches[0]["bindings"] == bindings(store.config), "OPS_DEVELOPMENT_RECORD_INVALID")
    development = matches[0]
    candidates = [c for c in development["candidates"] if c["statistical_passed"]]
    require(candidates and not development["synthetic"], "OPS_NO_QUALIFIED_DEVELOPMENT_CANDIDATE")
    # Selection is frozen before the holdout is loaded or evaluated; no retuning.
    selected = sorted(c["strategy"] for c in candidates)[0]
    require(not any(r.get("kind") == "holdout_attempt" and r["dataset"] == development["dataset"] for r in records),
            "OPS_HOLDOUT_ALREADY_ATTEMPTED")
    plan = {"kind": "holdout_attempt", "bindings": bindings(store.config), "dataset": development["dataset"],
            "development_record": development_record, "strategy": selected, "at": stamp(utc_now()),
            "minimum_closed_trades": 50, "neighbour_breakout_offsets": [-1, 1]}
    store.record(plan)
    manifest, history = SnapshotStore(store.config.workspace).load(plan["dataset"])
    profile = load_profile(store.config.workspace)
    card = next(c for c in STRATEGIES if c.identity == selected)
    left, right = split(len(history.candles))["holdout"]
    outcomes = {}
    for scenario in SCENARIOS:
        result = run(history.candles, card, store.config, scenario_profile(profile, scenario), left, right, scenario)
        outcomes[scenario] = summarize(result, store.config.starting_equity_usd, fingerprint({"plan": plan, "scenario": scenario}))
    from dataclasses import replace
    stability = []
    for offset in (-1, 1):
        result = run(history.candles, replace(card, breakout=card.breakout + offset), store.config, profile, left, right)
        stability.append(Decimal(result["final_equity_usd"]) > store.config.starting_equity_usd and result["open_position_count"] == 0)
    baseline = outcomes["baseline"]
    lower = baseline["bootstrap_mean_trade_return"]["lower"]
    checks = {"positive_all_cost_stresses": all(Decimal(m["net_pnl_usd"]) > 0 for m in outcomes.values()),
              "fifty_holdout_trades": baseline["closed_trades"] >= 50,
              "positive_bootstrap_lower_bound": lower is not None and Decimal(lower) > 0,
              "drawdown_below_five_percent": all(Decimal(m["maximum_drawdown_fraction"]) < Decimal("0.05") for m in outcomes.values()),
              "fully_flat": all(m["holding_btc"] == "0" and m["open_position_count"] == 0 for m in outcomes.values()),
              "neighbour_stability": all(stability)}
    document = {"kind": "historical_validation_final", "bindings": bindings(store.config), "synthetic": False,
                "holdout_evaluated": True, "at": stamp(utc_now()), "development_record": development_record,
                "plan": plan, "outcomes": outcomes,
                "candidates": [{"strategy": selected, "qualified": all(checks.values()), "checks": checks}],
                "limitation": "Conditional model qualification; actual costs, forward evidence and operator release remain separate"}
    return {"record": store.record(document), "checks": checks, "conditional_model_passed": all(checks.values())}


def release(store, *, acknowledge=False, reviewed=False, now=None):
    require(acknowledge is True and reviewed is True, "OPS_OPERATOR_REVIEW_REQUIRED")
    with store.lock():
        state = store.load()
        state["release"] = {"at": stamp(now or utc_now()), "bindings": bindings(store.config),
            "operator_acknowledged": True, "provider_access_reviewed": True,
            "data_rights_reviewed": True, "actual_costs_and_fx_reviewed": True}
        store.save(state, "operator_review_recorded")
        return readiness(store, state, now)
