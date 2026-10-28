"""Frozen research choices, declared before screening; no parameter optimizer."""

from dataclasses import asdict, dataclass
from decimal import Decimal

from ..errors import ResearchError
from ..risk.model import fingerprint, parse_profile, text

CONTEXT = "historical_ohlcv_model"
MAX_BARS = 30_000
MIN_BARS = 120


def fail(code="RESEARCH_INVALID"):
    messages = {
        "RESEARCH_INVALID": "Research input, plan or saved evidence failed validation; nothing was promoted.",
        "RESEARCH_DATA": "Research needs a verified contiguous hourly snapshot of 120 to 30000 bars; no data was invented or downloaded.",
        "RESEARCH_RECORD": "Saved research evidence is missing, damaged or incompatible; it was not replaced.",
    }
    raise ResearchError(code, messages[code])


@dataclass(frozen=True)
class Strategy:
    identity: str
    hours: int
    breakout: int = 20
    exit_lookback: int = 10
    atr_period: int = 14
    atr_multiple: int = 2


STRATEGIES = (Strategy("btc-breakout-1h-v1", 1), Strategy("btc-breakout-4h-v1", 4), Strategy("btc-breakout-1d-v1", 24))
SCENARIOS = ("baseline", "double_spread_slippage", "assumed_higher_fees")


def plan():
    return {"schema_version": 1, "name": "btc-baseline-screen-v2", "strategies": [asdict(s) for s in STRATEGIES],
            "entry_liquidation": "Reject modeled full-fill dust before entry; only decrease size and repeat the complete risk gate",
            "partial_fill_liquidation_qualified": False,
            "selection": "No optimization or automatic winner; all fixed variants retained",
            "split": {"training_percent": 50, "validation_percent": 30, "reserved_holdout_percent": 20, "oos_windows": 6},
            "signal": "Completed-bar breakout above prior highs; completed 4h SMA12 trend; ATR2 stop; prior-low exit",
            "latency": "Signal at a completed bar end waits one entire hourly bar before next-open execution",
            "intrabar": "Modelled open-high-low-close path; gap/stop exits at observed adverse model bid",
            "session": "Mandatory flat attempt at 23:00 UTC; no entries from 22:00 UTC until next day",
            "boundary_reason": "Avoid unqualified overnight boundary accounting; dust/period review never reset",
            "fx": "Unverified constant USD/USDT bounds 0.995..1.005; not historical FX observations",
            "spread_bps": "2", "scenarios": list(SCENARIOS), "assumed_higher_fee_rate": "0.002",
            "worst_account_fee_tier_qualified": False,
            "resource_limits": {"maximum_bars": MAX_BARS, "maximum_historical_event_history": 200000,
                                "saved_paper_account_history_limit_unchanged": 1000},
            "gates": {"minimum_oos_windows": 6, "minimum_positive_window_fraction": "0.6666666666666666666666666667",
                      "minimum_oos_closed_trades": 200, "maximum_drawdown_fraction_exclusive": "0.05",
                      "bootstrap_replicates": 1000, "bootstrap_minimum_trades": 20,
                      "bootstrap_confidence": "0.95", "final_holdout_required": True},
            "production_qualified": False, "research_only": True, "execution_eligible": False}


def split(count):
    if type(count) is not int or not MIN_BARS <= count <= MAX_BARS:
        fail("RESEARCH_DATA")
    train_end, validation_end = count // 2, count * 4 // 5
    boundaries = [train_end + (validation_end - train_end) * index // 6 for index in range(7)]
    return {"train": [0, train_end], "validation": [train_end, validation_end], "holdout": [validation_end, count],
            "windows": [[boundaries[i], boundaries[i + 1]] for i in range(6)]}


def scenario_profile(profile, scenario):
    if scenario not in SCENARIOS:
        fail()
    document = dict(profile.document)
    if scenario == "double_spread_slippage":
        for field in ("entry_slippage_bps", "exit_slippage_bps"):
            document[field] = text(Decimal(document[field]) * 2)
    if scenario == "assumed_higher_fees":
        for field in ("entry_fee_rate", "exit_fee_rate"):
            document[field] = text(max(Decimal(document[field]), Decimal("0.002")))
    return parse_profile(document)


def card_id(strategy):
    return fingerprint({"plan": plan(), "strategy": asdict(strategy)})
