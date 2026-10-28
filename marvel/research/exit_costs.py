"""Offline exit-cost scenarios, not executable quotes or strategy returns.

Uses an untouched hypothetical risk state and the existing fee/Spot reducers.
The historical strategy's complete-liquidation guard is never bypassed.
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from pathlib import Path
import re

from .. import __version__
from ..clock import parse_stamp
from ..paper.ledger import proceeds
from ..risk.engine import evaluate, seed, valuation_point
from ..risk.model import PRECISION, canonical, ceil_step, fingerprint, floor_step, text
from .account_check import _validate as validate_account, undiscounted_taker_rates
from .feasibility import quantities
from .model import fail
from .store import decode_record
from .study import source_hashes

ZERO = Decimal(0)
MAX_PAIR_BYTES = 65536
RECIPE = {"name": "synthetic_exit_cost_audit_v1", "reference_kind": "invented_engineering_scenarios",
          "reference_ask_usdt": "80000", "reference_bid_usdt": "79999.99",
          "fixed_model_time": "2026-03-01T12:00:00+00:00", "fx_lower": "0.995", "fx_upper": "1.005",
          "stop_distance_fractions": ["0.01", "0.02", "0.05"],
          "entry_fill_fractions": ["1", "0.5"], "spot_exit_fill_fractions": ["1", "0.5", "0"],
          "conversion_haircut_bps": ["0", "25", "100", "500", "1000", None],
          "conversion_haircut_note": "Invented all-in deductions from the invented stop bid, not observed Convert prices",
          "unavailable_recovery": "Zero cash recovery while BTC ownership and the original claim remain unresolved",
          "partial_fill_note": "One modeled entry fill and one modeled Spot exit fill; actual per-fill rounding is unverified",
          "selection": "Keep every scenario; no ranking, optimization, winner or historical strategy change"}


def pair_limits(document):
    if type(document) is not list or not 1 <= len(document) <= 5000:
        fail()
    matches = [item for item in document if type(item) is dict and item.get("fromAsset") == "BTC" and item.get("toAsset") == "USDT"]
    if len(matches) != 1:
        fail()
    item = matches[0]
    required = {"fromAsset", "toAsset", "fromAssetMinAmount", "fromAssetMaxAmount", "toAssetMinAmount", "toAssetMaxAmount"}
    if set(item) not in (required, required | {"fromIsBase"}) or ("fromIsBase" in item and type(item["fromIsBase"]) is not bool):
        fail()
    try:
        values = {}
        for name in ("fromAssetMinAmount", "fromAssetMaxAmount", "toAssetMinAmount", "toAssetMaxAmount"):
            value = item[name]
            if type(value) is not str or len(value) > 40 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,2})?", value) is None:
                fail()
            number = Decimal(value)
            if not ZERO < number <= Decimal("1e30"):
                fail()
            values[name] = text(number)
        if (Decimal(values["fromAssetMinAmount"]) > Decimal(values["fromAssetMaxAmount"])
                or Decimal(values["toAssetMinAmount"]) > Decimal(values["toAssetMaxAmount"])):
            fail()
    except InvalidOperation:
        fail()
    return {"fromAsset": "BTC", "toAsset": "USDT", **values}


def load_pair(workspace, filename):
    workspace = Path(workspace).resolve()
    path = Path(filename)
    path = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(workspace):
        fail("RESEARCH_RECORD")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_PAIR_BYTES + 1)
    except OSError:
        fail("RESEARCH_RECORD")
    if len(raw) > MAX_PAIR_BYTES:
        fail("RESEARCH_RECORD")
    limits = pair_limits(decode_record(raw))
    return limits, {"workspace_file": path.relative_to(workspace).as_posix(), "sha256": sha256(raw).hexdigest(),
                    "provenance": "Local dated reference; not authenticated account access or historical execution rules"}


def conversion_floor(entry_cost, already_sold, reserved_loss, fx_lower, quantum, destination_min):
    """Round the minimum required net quote UP; cannot spend more than the claim."""
    risk_floor = ceil_step(max(ZERO, entry_cost - already_sold - reserved_loss) / fx_lower, quantum)
    breakeven = ceil_step(max(ZERO, entry_cost - already_sold) / fx_lower, quantum)
    return ceil_step(max(destination_min, risk_floor), quantum), breakeven


def _entry(profile, plan, fraction):
    gross = floor_step(Decimal(plan["quantity"]) * fraction, profile.n("entry_step"))
    amounts = quantities(profile, gross)
    price = Decimal(plan["entry_price_bound"])
    quote_fee = (ceil_step(gross * price * profile.n("entry_fee_rate"), profile.n("quote_fee_quantum"))
                 if profile.document["entry_fee_asset"] == "USDT" else ZERO)
    return gross, Decimal(amounts["received_btc"]), (gross * price + quote_fee) * Decimal(RECIPE["fx_upper"])


def _routes(profile, plan, stop_bid, btc_quantum, usdt_quantum, limits):
    routes = []
    reserved = Decimal(plan["loss_reserved_usd"])
    fx = Decimal(RECIPE["fx_lower"])
    price = Decimal(plan["exit_price_estimate"])
    for entry_fraction in RECIPE["entry_fill_fractions"]:
        gross, received, cost = _entry(profile, plan, Decimal(entry_fraction))
        if not gross:
            continue
        spot_quantity = floor_step(received, profile.n("exit_step"))
        spot_available = (profile.n("exit_min_qty") <= spot_quantity <= profile.n("exit_max_qty")
                          and profile.n("exit_min_notional_quote") <= spot_quantity * price <= profile.n("exit_max_notional_quote"))
        for exit_fraction in RECIPE["spot_exit_fill_fractions"]:
            # Fractions describe fills of an initially compliant exit order, not new small orders.
            sold = floor_step(spot_quantity * Decimal(exit_fraction), profile.n("exit_step")) if spot_available else ZERO
            spot_usd, spot_fee = proceeds(sold, price, {"fx_lower": RECIPE["fx_lower"]}, profile)
            remaining = received - sold
            minimum, breakeven = conversion_floor(cost, spot_usd, reserved, fx, usdt_quantum, Decimal(limits["toAssetMinAmount"]))
            source_ok = (remaining % btc_quantum == 0
                         and Decimal(limits["fromAssetMinAmount"]) <= remaining <= Decimal(limits["fromAssetMaxAmount"]))
            scenarios = []
            for haircut in RECIPE["conversion_haircut_bps"]:
                quoted = None if haircut is None else floor_step(remaining * stop_bid * (1 - Decimal(haircut) / 10000), usdt_quantum)
                amount_ok = bool(source_ok and quoted is not None
                    and Decimal(limits["toAssetMinAmount"]) <= quoted <= Decimal(limits["toAssetMaxAmount"]))
                floor_ok = bool(amount_ok and quoted >= minimum)
                pnl = None if quoted is None else spot_usd + quoted * fx - cost
                if not remaining:
                    outcome, pnl, floor_ok = "spot_only_flat_model", spot_usd - cost, spot_usd - cost >= -reserved
                elif not source_ok:
                    outcome = "source_amount_rejected"
                elif quoted is None:
                    outcome = "conversion_unavailable"
                elif not amount_ok:
                    outcome = "destination_amount_rejected"
                elif not floor_ok:
                    outcome = "quote_below_reserved_loss_floor"
                else:
                    outcome = "conditional_amount_and_loss_floor_fit"
                scenarios.append({"invented_haircut_bps": haircut, "invented_net_quote_usdt": None if quoted is None else text(quoted),
                    "outcome": outcome, "conditional_amount_and_loss_floor_fit": floor_ok,
                    "counterfactual_cycle_pnl_usd_if_quote_settled": None if pnl is None else text(pnl),
                    "zero_recovery_marked_pnl_usd_if_cleanup_unsettled": text(spot_usd - cost),
                    "zero_recovery_stress_exceeds_original_reservation": cost - spot_usd > reserved,
                    "unresolved_btc_until_verified_settlement": text(remaining),
                    "actual_quote_observed": False, "actual_exit_qualified": False})
            routes.append({"entry_fill_fraction": entry_fraction, "spot_exit_fill_fraction": exit_fraction,
                "gross_filled_btc": text(gross), "net_received_btc": text(received), "entry_cost_usd": text(cost),
                "modeled_spot_exit_order_meets_minimums": spot_available,
                "spot_sold_btc": text(sold), "spot_fee_usdt": text(spot_fee), "spot_net_proceeds_usd": text(spot_usd),
                "remaining_btc": text(remaining), "source_amount_precision_and_limits_fit": source_ok,
                "minimum_net_convert_quote_usdt_for_reserved_loss": text(minimum) if remaining else "0",
                "minimum_net_convert_quote_usdt_for_cycle_break_even": text(breakeven) if remaining else "0",
                "original_reserved_loss_usd": text(reserved),
                "terminal_entry_and_exit_orders_required_before_cleanup": bool(remaining),
                "claim_must_remain_until_verified_flat_and_net_outcome": bool(remaining), "scenarios": scenarios})
    return routes


def audit(config, profile, account, limits, *, account_id, pair_source):
    validate_account(account)
    if account["observation_status"] != "complete" or account_id != fingerprint(account):
        fail()
    limits = pair_limits([limits])
    with localcontext() as context:
        context.prec = PRECISION
        before = source_hashes()
        rates = undiscounted_taker_rates(account["commissions"])
        fractions = {item["asset"]: item["fraction"] for item in account["precision"]}
        quantums = {name: Decimal(1).scaleb(-value) for name, value in fractions.items()}
        blocked = []
        if Decimal(rates["buy"]) > profile.n("entry_fee_rate") or Decimal(rates["sell"]) > profile.n("exit_fee_rate"):
            blocked.append("PROFILE_BELOW_OBSERVED_UNDISCOUNTED_FEES")
        scenarios = []
        when = RECIPE["fixed_model_time"]
        now = parse_stamp(when)
        for distance in (() if blocked else RECIPE["stop_distance_fractions"]):
            state = seed(config, profile, config.starting_equity_usd, config.starting_equity_usd, when)
            original = deepcopy(state)
            stop = floor_step(Decimal(RECIPE["reference_bid_usdt"]) * (1 - Decimal(distance)), profile.n("tick_size"))
            market = {"kind": "synthetic", "bid": RECIPE["reference_bid_usdt"], "ask": RECIPE["reference_ask_usdt"],
                      "quote_at": when, "fx_at": when, "fx_lower": RECIPE["fx_lower"], "fx_upper": RECIPE["fx_upper"]}
            request = {"proposal": {"id": fingerprint({"exit_audit_stop": distance}), "symbol": "BTCUSDT", "side": "BUY",
                                    "stop_price": text(stop), "quantity": None}, "market": market}
            decision = evaluate(state, config, profile, request, now, history=[valuation_point(state)])
            if canonical(state) != canonical(original):
                fail("RESEARCH_RECORD")
            routes = _routes(profile, decision["plan"], stop, quantums["BTC"], quantums["USDT"], limits) if decision["risk_check_passed"] else []
            scenarios.append({"invented_stop_distance": distance, "invented_stop_bid_usdt": text(stop),
                              "original_spot_sizing_decision": decision, "routes": routes})
        if source_hashes() != before:
            fail("RESEARCH_RECORD")
        document = {"schema_version": 1, "application_version": __version__, "kind": "synthetic_exit_cost_audit",
            "status": "unqualified_cost_and_exit_scenarios", "recipe": deepcopy(RECIPE), "recipe_id": fingerprint(RECIPE),
            "source_sha256": before, "config_fingerprint": config.fingerprint, "profile_hash": profile.fingerprint,
            "account_record_id": account_id, "account_observed_at_utc": account["observed_at_utc"],
            "observed_undiscounted_taker_rate_fractions": rates, "observed_asset_fractions": fractions,
            "pair_limits": limits, "pair_limits_id": fingerprint(limits), "pair_reference": deepcopy(pair_source),
            "input_blockers": blocked, "scenarios": scenarios,
            "actual_conversion_quotes_observed": 0, "orders_or_conversions": 0, "out_of_sample_strategy_trades": 0,
            "owner_account_used": False, "strategy_route_changed": False, "holdout_evaluated": False,
            "actual_fill_costs_qualified": False, "actual_complete_exit_qualified": False,
            "phase5_historical_gate_passed": False, "entries_allowed": False, "live_execution_available": False,
            "limitations": ["Prices, stop distances, FX bounds, haircut amounts and fill fractions are invented scenarios, not market history",
                "Sizing uses the unchanged Spot model including zero recovery for dust; no strategy entry guard is bypassed",
                "Floors constrain a hypothetical quote at the assumed prices and FX; gaps, delays and depegs can exceed planned risk",
                "No BNB discount, actual fee asset, per-fill rounding or additional provider fee is qualified",
                "A fitted floor or amount precision does not prove quote access, costs, protective exits or settlement",
                "All Spot orders must be terminal and settlement/net balances verified before a real claim could close",
                "Counterfactual P&L is a cash-flow calculation, not a completed trade, realized loss or profitability evidence"]}
        return {**document, "audit_id": fingerprint(document)}
