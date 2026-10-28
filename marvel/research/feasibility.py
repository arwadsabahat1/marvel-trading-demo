"""Bounded full-fill liquidation checks; never changes a fee profile or account."""

from copy import deepcopy
from decimal import Decimal, localcontext

from ..risk.engine import decision, evaluate
from ..risk.model import PRECISION, ceil_step, floor_step, number, text
from .model import CONTEXT, fail

MAX_QUANTITY_STEPS = 100_000


def quantities(profile, gross):
    """Same conservative single-fill fee rounding as the paper execution model."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        fee = (ceil_step(gross * profile.n("entry_fee_rate"), profile.n("base_fee_quantum"))
               if profile.document["entry_fee_asset"] == "BTC" else Decimal(0))
        net = max(Decimal(0), gross - fee)
        sell = floor_step(net, profile.n("exit_step"))
        return {"gross_btc": text(gross), "entry_fee_btc": text(fee), "received_btc": text(net),
                "sell_btc": text(sell), "residual_btc": text(net - sell)}


def largest_complete_quantity(profile, maximum):
    """Only decrease quantity. A bounded search failure never permits an entry."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        step = profile.n("entry_step")
        units = int(floor_step(maximum, step) / step)
        for offset in range(min(units, MAX_QUANTITY_STEPS)):
            gross = (units - offset) * step
            if gross < profile.n("entry_min_qty"):
                return None, False
            item = quantities(profile, gross)
            if Decimal(item["sell_btc"]) > 0 and Decimal(item["residual_btc"]) == 0:
                return gross, False
        return None, units > MAX_QUANTITY_STEPS


def complete_entry_decision(state, config, profile, document, now, history):
    """Preflight only. Re-evaluate every changed size through the complete risk gate."""
    initial = evaluate(state, config, profile, document, now, history=history, context_kind=CONTEXT)
    if not initial["risk_check_passed"] or Decimal(initial["plan"]["dust_quantity"]) == 0:
        return initial
    quantity, limited = largest_complete_quantity(profile, Decimal(initial["plan"]["quantity"]))
    if quantity is not None and document["proposal"]["quantity"] is None:
        adjusted = deepcopy(document)
        adjusted["proposal"]["quantity"] = text(quantity)
        result = evaluate(state, config, profile, adjusted, now, history=history, context_kind=CONTEXT)
        if result["risk_check_passed"] and Decimal(result["plan"]["dust_quantity"]) == 0:
            result["liquidation_check"] = {"quantity_only_decreased": True,
                "single_full_fill_only": True, "partial_fill_liquidation_qualified": False}
            return result
        return result
    result = decision(False, ["LIQUIDATION_SEARCH_LIMIT" if limited else "FULL_LIQUIDATION_NOT_FEASIBLE"], initial["budgets"])
    result["liquidation_check"] = {"original_candidate": quantities(profile, Decimal(initial["plan"]["quantity"])),
                                  "single_full_fill_only": True, "partial_fill_liquidation_qualified": False,
                                  "account_fees_verified": False, "quantity_search_limited": limited}
    return result


def public_filter_summary(observation):
    """Public rules are dated reference evidence, never historical/account truth."""
    metadata = observation.get("metadata")
    if (observation.get("symbol") != "BTCUSDT" or not isinstance(metadata, dict)
            or metadata.get("symbol") != "BTCUSDT" or metadata.get("baseAsset") != "BTC"
            or metadata.get("quoteAsset") != "USDT" or metadata.get("status") != "TRADING"
            or metadata.get("isSpotTradingAllowed") is not True
            or not isinstance(metadata.get("filters"), list) or len(metadata["filters"]) > 100):
        fail()
    filters = {}
    for item in metadata["filters"]:
        if not isinstance(item, dict) or type(item.get("filterType")) is not str or item["filterType"] in filters:
            fail()
        filters[item["filterType"]] = item
    try:
        lot, price = filters["LOT_SIZE"], filters["PRICE_FILTER"]
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL"))
        values = {"lot_min_btc": lot["minQty"], "lot_step_btc": lot["stepSize"],
                  "price_tick_usdt": price["tickSize"], "min_notional_usdt": notional["minNotional"]}
        for value in values.values():
            number(value)
    except (KeyError, TypeError):
        fail()
    return {**values, "market_lot_filter": filters.get("MARKET_LOT_SIZE"),
            "zero_market_step_does_not_remove_lot_size_filter": True,
            "account_fees_verified": False, "historical_rules_verified": False,
            "execution_qualified": False}


def report(config, profile, observation):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        public = public_filter_summary(observation)
        quote = observation.get("quote")
        if not isinstance(quote, dict):
            fail()
        ask = number(quote.get("ask"))
        cap = config.starting_equity_usd * config.risk.max_notional_fraction
        maximum = floor_step(min(profile.n("entry_max_qty"), cap / (ask * Decimal("1.005"))), profile.n("entry_step"))
        selected, limited = largest_complete_quantity(profile, maximum)
        step = profile.n("entry_step")
        rows = [quantities(profile, index * step) for index in range(1, min(int(maximum / step), 100) + 1)]
        first = None
        for index in range(1, MAX_QUANTITY_STEPS + 1):
            gross = index * step
            if gross > profile.n("entry_max_qty"):
                break
            item = quantities(profile, gross)
            if gross >= profile.n("entry_min_qty") and Decimal(item["sell_btc"]) > 0 and Decimal(item["residual_btc"]) == 0:
                first = item
                break
        return {"symbol": "BTCUSDT", "public_filters": public, "original_profile_hash": profile.fingerprint,
                "modeled_profile": dict(profile.document), "reference_ask_usdt": text(ask),
                "observation_provenance": deepcopy(observation.get("provenance")),
                "quote_is_execution_qualified": False, "quote_has_exchange_event_time": quote.get("event_us") is not None,
                "initial_notional_cap_usd": text(cap), "illustrative_fx_upper": "1.005",
                "maximum_gross_btc_by_initial_notional_only": text(maximum),
                "largest_full_fill_grid_aligned_gross_btc_within_cap": None if selected is None else text(selected),
                "quantity_search_limited": limited, "displayed_small_sizes": rows,
                "first_grid_aligned_full_fill_within_search": first,
                "first_grid_aligned_reference_notional_usd": None if first is None else text(Decimal(first["gross_btc"]) * ask * Decimal("1.005")),
                "public_lot_step_matches_model": Decimal(public["lot_step_btc"]) == profile.n("exit_step"),
                "public_min_notional_matches_model": Decimal(public["min_notional_usdt"]) == profile.n("exit_min_notional_quote"),
                "actual_account_fee_rates_verified": False, "actual_fee_rounding_verified": False,
                "partial_fill_liquidation_qualified": False, "production_qualified": False,
                "historical_promotion_passed": False, "owner_account_used": False,
                "research_only": True, "entries_allowed": False, "execution_eligible": False,
                "limitations": ["Single-full-fill grid arithmetic does not certify partial fills, protective stops or exchange recovery",
                                "Public metadata cannot establish the account's standard, tax, special or BNB fee settings",
                                "Initial-notional screen is not the full cash, stop-loss or remaining-budget gate",
                                "No fee-asset switch, BNB purchase, dust conversion, top-up or financial-limit change is performed"]}
