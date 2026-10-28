"""Causal completed UTC bars; the caller supplies a prefix, never a future slice."""

from decimal import Decimal, localcontext

from ..data.quality import Candle, INTERVALS
from ..risk.model import PRECISION, text


def aggregate(candles, hours):
    groups = {}
    width = hours * INTERVALS["1h"]
    for candle in candles:
        groups.setdefault(candle.open_us // width, []).append(candle)
    result = []
    for key, group in groups.items():
        if len(group) != hours or any(c.open_us != key * width + i * INTERVALS["1h"] for i, c in enumerate(group)):
            continue
        result.append(Candle(group[0].open_us, group[-1].end_us, group[0].open, max(c.high for c in group),
                             min(c.low for c in group), group[-1].close, sum(c.volume for c in group),
                             sum(c.quote_volume for c in group), sum(c.trades for c in group),
                             sum(c.taker_base for c in group), sum(c.taker_quote for c in group)))
    return result


def signal(completed, strategy):
    """Return a fixed proposal/exit decision using at most 22 model bars and 4h context."""
    if not completed:
        return {"ready": False, "enter": False, "exit": False}
    with localcontext() as ctx:
        ctx.prec = PRECISION
        tail = completed[-max((strategy.breakout + 2) * strategy.hours, 52):]
        bars = aggregate(tail, strategy.hours)
        four = aggregate(tail, 4)
        if (len(bars) < max(strategy.breakout + 1, strategy.atr_period + 1)
                or len(four) < 12 or bars[-1].end_us != completed[-1].end_us):
            return {"ready": False, "enter": False, "exit": False}
        current, prior = bars[-1], bars[:-1]
        true_ranges = [max(bars[i].high - bars[i].low, abs(bars[i].high - bars[i - 1].close),
                           abs(bars[i].low - bars[i - 1].close)) for i in range(len(bars) - strategy.atr_period, len(bars))]
        atr = sum(true_ranges) / strategy.atr_period
        trend = four[-1].close > sum(bar.close for bar in four[-12:]) / 12
        stop = current.close - strategy.atr_multiple * atr
        return {"ready": True, "enter": bool(current.close > max(bar.high for bar in prior[-strategy.breakout:]) and trend and stop > 0),
                "exit": bool(current.close < min(bar.low for bar in prior[-strategy.exit_lookback:])),
                "stop_price": text(stop) if stop > 0 else None, "signal_at_us": current.end_us,
                "atr": text(atr), "four_hour_end_us": four[-1].end_us}
