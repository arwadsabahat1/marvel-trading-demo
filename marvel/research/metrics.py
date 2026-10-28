"""Net metrics and deterministic moving-block bootstrap, never win-rate-only gates."""

from decimal import Decimal, localcontext
from math import ceil, isqrt
from random import Random

from ..risk.model import PRECISION, text


def bootstrap(values, seed, replicates=1000):
    if len(values) < 20:
        return {"method": "circular_moving_block", "confidence": "0.95", "lower": None, "upper": None,
                "reason": "FEWER_THAN_20_CLOSED_TRADES", "replicates": replicates}
    with localcontext() as ctx:
        ctx.prec = PRECISION
        values = [Decimal(value) for value in values]
        size, block = len(values), max(2, isqrt(len(values)))
        random, means = Random(seed), []
        for _ in range(replicates):
            sample = []
            for _ in range(ceil(size / block)):
                index = random.randrange(size)
                sample.extend(values[(index + offset) % size] for offset in range(block))
            means.append(sum(sample[:size]) / size)
        means.sort()
        return {"method": "circular_moving_block", "confidence": "0.95", "lower": text(means[replicates * 25 // 1000]),
                "upper": text(means[min(replicates - 1, replicates * 975 // 1000)]),
                "replicates": replicates, "block_length_trades": block, "seed": seed,
                "limitation": "Trade-order blocks; sparse/regime-biased samples and unseen tail risk are not solved"}


def drawdown(points, initial):
    peak, maximum = Decimal(initial), Decimal(0)
    for point in points:
        value = Decimal(point["equity_usd"])
        peak = max(peak, value)
        maximum = max(maximum, (peak - value) / peak if peak else Decimal(0))
    return maximum


def summarize(result, initial, seed):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        initial = Decimal(initial)
        trades, marks, curve = result["trades"], result["intrabar_marks"], result["curve"]
        outcomes = [Decimal(trade["net_pnl_usd"]) for trade in trades]
        positive = sum((value for value in outcomes if value > 0), Decimal(0))
        negative = -sum((value for value in outcomes if value < 0), Decimal(0))
        streak = maximum_streak = 0
        for value in outcomes:
            streak = streak + 1 if value < 0 else (0 if value > 0 else streak)
            maximum_streak = max(streak, maximum_streak)
        turnover = sum((Decimal(c["entry_cost_usd"]) + Decimal(c["exit_proceeds_usd"]) for c in result["ledger"]["cycles"]), Decimal(0))
        exposure = sum((Decimal(p["notional_usd"]) / Decimal(p["equity_usd"]) if Decimal(p["equity_usd"]) else Decimal(0) for p in curve), Decimal(0)) / len(curve)
        return {"net_pnl_usd": text(Decimal(result["final_equity_usd"]) - initial),
                "net_return_fraction": text(Decimal(result["final_equity_usd"]) / initial - 1),
                "closed_trades": len(trades), "expectancy_usd": text(sum(outcomes) / len(outcomes)) if outcomes else None,
                "expectancy_fraction": text(sum(Decimal(t["net_return_fraction"]) for t in trades) / len(trades)) if trades else None,
                "profit_factor": text(positive / negative) if negative else None,
                "profit_factor_note": "No gross losses; ratio undefined" if not negative else None,
                "win_fraction": text(Decimal(sum(value > 0 for value in outcomes)) / len(trades)) if trades else None,
                "maximum_drawdown_fraction": text(drawdown(marks, initial)), "maximum_losing_streak": maximum_streak,
                "mean_hour_end_exposure_fraction": text(exposure), "turnover_fraction_of_initial": text(turnover / initial),
                "total_fees_usd": result["ledger"]["fees_usd"], "open_position_count": result["open_position_count"],
                "holding_btc": curve[-1]["holding_btc"], "unrealized_net_pnl_usd": result["unrealized_net_pnl_usd"],
                "largest_positive_trade_share": text(max([Decimal(0), *outcomes]) / positive) if positive else None,
                "bootstrap_mean_trade_return": bootstrap([t["net_return_fraction"] for t in trades], seed)}


def windows(result, candles, boundaries, initial):
    records, prior = [], Decimal(initial)
    for index, (start, end) in enumerate(boundaries):
        left, right = candles[start].open_us, candles[end - 1].end_us
        points = [point for point in result["curve"] if left < point["at_us"] <= right]
        trades = [trade for trade in result["trades"] if left <= trade["closed_at_us"] < right]
        final = Decimal(points[-1]["equity_usd"])
        pnl = sum((Decimal(trade["net_pnl_usd"]) for trade in trades), Decimal(0))
        change = candles[end - 1].close / candles[start].open - 1
        label = "rising" if change > Decimal("0.02") else "falling" if change < Decimal("-0.02") else "sideways"
        stress = any((c.high - c.low) / c.open > Decimal("0.05") for c in candles[start:end])
        records.append({"index": index, "training_prefix": [0, start], "test_range": [start, end],
                        "start_us": left, "end_us": right, "starting_equity_usd": text(prior), "ending_equity_usd": text(final),
                        "net_equity_change_usd": text(final - prior), "closed_trades": len(trades),
                        "net_closed_pnl_usd": text(pnl), "positive_expectancy_and_net_equity": bool(trades and pnl > 0 and final > prior),
                        "regime_diagnostic_only": label, "stress_range_observed": stress,
                        "parameter_fit": "None; rule card frozen before this study; one continuous validation portfolio"})
        prior = final
    return records
