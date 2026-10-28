"""Cash, costed 20%-budget passive BTC, and explicitly retrospective exposure matching."""

from decimal import Decimal, localcontext

from ..risk.model import PRECISION, ceil_step, floor_step, text
from .backtest import market
from ..paper.ledger import exit_price, proceeds


def passive(candles, start, end, config, profile, scenario):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        initial = config.starting_equity_usd
        quote = market(candles[start].open, candles[start].open_us, scenario)
        price = ceil_step(Decimal(quote["ask"]) * (1 + profile.n("entry_slippage_bps") / 10000), profile.n("tick_size"))
        rate = profile.n("entry_fee_rate") if profile.document["entry_fee_asset"] == "USDT" else Decimal(0)
        quantity = floor_step(initial * config.risk.max_notional_fraction / (price * Decimal(quote["fx_upper"]) * (1 + rate)), profile.n("entry_step"))
        if not (profile.n("entry_min_qty") <= quantity <= profile.n("entry_max_qty") and profile.n("entry_min_notional_quote") <= quantity * price <= profile.n("entry_max_notional_quote")):
            return {"available": False, "reason": "PASSIVE_ENTRY_MINIMUM", "curve": []}
        fee_base = ceil_step(quantity * profile.n("entry_fee_rate"), profile.n("base_fee_quantum")) if not rate else Decimal(0)
        fee_quote = ceil_step(quantity * price * rate, profile.n("quote_fee_quantum")) if rate else Decimal(0)
        cash = initial - (quantity * price + fee_quote) * Decimal(quote["fx_upper"])
        holding = quantity - fee_base
        if holding <= 0 or cash < 0:
            return {"available": False, "reason": "PASSIVE_COST_OR_FEE_PRECISION", "curve": []}
        sold_quantity = floor_step(holding, profile.n("exit_step"))
        curve = []
        for candle in candles[start:end]:
            mark = market(candle.close, candle.end_us, scenario)
            sell_price = exit_price(mark, profile)
            feasible = profile.n("exit_min_qty") <= sold_quantity <= profile.n("exit_max_qty") and profile.n("exit_min_notional_quote") <= sold_quantity * sell_price <= profile.n("exit_max_notional_quote")
            net = proceeds(sold_quantity, sell_price, mark, profile)[0] if feasible else Decimal(0)
            curve.append({"at_us": candle.end_us, "equity_usd": text(cash + net),
                          "notional_usd": text(holding * Decimal(mark["bid"]) * Decimal(mark["fx_lower"]))})
        mean_exposure = sum(Decimal(p["notional_usd"]) / Decimal(p["equity_usd"]) for p in curve) / len(curve)
        return {"available": True, "initial_btc": text(holding), "end_residual_btc": text(holding - sold_quantity if feasible else holding),
                "final_equity_usd": curve[-1]["equity_usd"], "net_return_fraction": text(Decimal(curve[-1]["equity_usd"]) / initial - 1),
                "mean_hour_end_exposure_fraction": text(mean_exposure), "curve": curve,
                "qualification": "Costed passive comparison; no strategy stops/rebalancing; not a risk-gated deployment"}


def matched(passive_result, strategy_exposure, initial):
    if not passive_result["available"]:
        return {"available": False, "reason": passive_result["reason"]}
    base_exposure = Decimal(passive_result["mean_hour_end_exposure_fraction"])
    weight = min(Decimal(1), Decimal(strategy_exposure) / base_exposure) if base_exposure else Decimal(0)
    initial = Decimal(initial)
    final = initial + weight * (Decimal(passive_result["final_equity_usd"]) - initial)
    return {"available": True, "cash_plus_passive_weight": text(weight), "final_equity_usd": text(final),
            "net_return_fraction": text(final / initial - 1), "retrospective_diagnostic": True,
            "target_mean_exposure_fraction": strategy_exposure,
            "achieved_mean_exposure_fraction": text(base_exposure * weight),
            "limitation": "Matches average exposure after the fact by scaling the costed passive curve; fractional scaled orders/fees may violate venue minimums; not investable or promotion evidence"}
