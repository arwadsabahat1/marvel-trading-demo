"""Three fixed long-only hypotheses; parameters are never fitted to price history."""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from ..risk.model import PRECISION, text
from .model import fail


@dataclass(frozen=True)
class Rule:
    identity: str
    family: str
    hours: int = 1
    lookback: int = 20
    trend_period: int = 12
    atr_period: int = 14
    atr_multiple: int = 2
    minimum_move_fraction: str = "0.04"
    flat_trend_fraction: str = "0.01"


RULES = (
    Rule("btc-trend-pullback-1h-v1", "trend_pullback"),
    Rule("btc-cost-breakout-1h-v1", "cost_breakout"),
    Rule("btc-range-rebound-1h-v1", "range_rebound"))


def band(bars):
    mean = sum(b.close for b in bars) / len(bars)
    deviation = (sum((b.close - mean) ** 2 for b in bars) / len(bars)).sqrt()
    return mean, mean - 2 * deviation


def signal(completed, rule):
    from .features import aggregate

    if rule not in RULES:
        fail("RESEARCH_RECORD")
    unavailable = {"ready": False, "enter": False, "exit": False}
    if not completed:
        return unavailable
    with localcontext() as context:
        context.prec = PRECISION
        bars = completed[-max(rule.lookback + 2, (rule.trend_period + 2) * 4):]
        four = aggregate(bars, 4)
        if len(bars) < rule.lookback + 1 or len(four) < rule.trend_period + 1:
            return unavailable
        current, previous = bars[-1], bars[-2]
        mean, lower = band(bars[-rule.lookback:])
        previous_mean, previous_lower = band(bars[-rule.lookback - 1:-1])
        trend_mean = sum(b.close for b in four[-rule.trend_period:]) / rule.trend_period
        previous_trend_mean = sum(b.close for b in four[-rule.trend_period - 1:-1]) / rule.trend_period
        rising = four[-1].close > trend_mean > previous_trend_mean
        atr = sum(max(bars[i].high - bars[i].low, abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close)) for i in range(len(bars) - rule.atr_period, len(bars))) / rule.atr_period
        stop = current.close - rule.atr_multiple * atr
        if rule.family == "trend_pullback":
            enter = rising and previous.close <= previous_mean and current.close > mean and current.close > previous.close
            leave = current.close < mean
        elif rule.family == "cost_breakout":
            # A volatility hurdle, NOT an expected-return forecast or a proved cost bound.
            enter = rising and current.close > max(b.high for b in bars[-rule.lookback - 1:-1]) and (
                rule.atr_multiple * atr / current.close >= Decimal(rule.minimum_move_fraction))
            leave = current.close < min(b.low for b in bars[-10 - 1:-1])
        else:
            ranging = abs(four[-1].close / trend_mean - 1) <= Decimal(rule.flat_trend_fraction)
            enter = ranging and previous.close < previous_lower and lower <= current.close < mean
            leave = current.close >= mean or current.close < lower
        return {"ready": True, "enter": bool(enter and stop > 0), "exit": bool(leave),
            "stop_price": text(stop) if stop > 0 else None, "signal_at_us": current.end_us,
            "atr": text(atr), "four_hour_end_us": four[-1].end_us,
            "mean": text(mean), "lower_band": text(lower), "rising_trend": bool(rising)}
