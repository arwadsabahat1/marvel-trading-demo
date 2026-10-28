"""Prospective public-price paper observations, separated from synthetic training."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

from marvel.data.quality import Candle, INTERVALS, utc_us
from marvel.research.backtest import Broker
from marvel.research.features import signal as baseline
from marvel.research.model import STRATEGIES
from marvel.research.rules import signal as additional
from marvel.risk.model import fingerprint, load_profile, text
from .common import bindings, require, stamp, utc_now
from .learning import CARDS, Learner, regime

BROKER_FIELDS = ("state", "history", "sequence", "chain", "points", "trade_closed_at", "trade_entries",
                 "miss_every", "eligible_entry_attempts")
HOUR = INTERVALS["1h"]


def bars_from_rest(rows, now):
    require(type(rows) is list and 25 <= len(rows) <= 600, "OPS_BARS_INVALID")
    output = []
    end = int(now.timestamp() * 1_000_000)
    for row in rows:
        require(type(row) is list and len(row) == 12 and type(row[0]) is int)
        require(all(type(row[i]) is str for i in (1, 2, 3, 4, 5, 7, 9, 10)), "OPS_BARS_INVALID")
        opened = row[0] * 1000
        if opened + HOUR > end:
            continue
        values = [Decimal(row[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10)]
        require(all(v.is_finite() and v >= 0 for v in values) and all(v > 0 for v in values[:4]))
        require(values[1] >= max(values[0], values[3]) and values[2] <= min(values[0], values[3]))
        require(opened % HOUR == 0 and (not output or opened == output[-1].end_us), "OPS_BARS_GAP")
        require(type(row[8]) is int and row[8] >= 0 and row[6] == (opened + HOUR) // 1000 - 1)
        output.append(Candle(opened, opened + HOUR, *values[:5], values[5], row[8], *values[6:]))
    require(len(output) >= 24 and 0 <= end - output[-1].end_us < HOUR, "OPS_BARS_STALE")
    return output


def tick(store, bars, *, observed_at=None, provenance="public_network"):
    now = observed_at or utc_now()
    received = int(now.timestamp() * 1_000_000)
    require(provenance in {"public_network", "synthetic"})
    require(store.mode == "paper" and len(bars) >= 25)
    with store.lock():
        state = store.load()
        profile = load_profile(store.config.workspace)
        forward = state.get("forward")
        if forward is None:
            broker = Broker(store.config, profile, received, "baseline")
            forward = {"started_at": stamp(now), "last_end": bars[-1].end_us, "last_price": text(bars[-1].close),
                       "last_signal": {}, "pending": None, "active_strategy": None, "active_regime": None,
                       "learner": {"seen": [], "contexts": {}}, "provenance": provenance,
                       "broker": {name: deepcopy(getattr(broker, name)) for name in BROKER_FIELDS}}
            state["forward"] = forward
            store.save(state, "forward_frozen")
            return {"warmup_only": True, "entered": False}
        require(forward["provenance"] == provenance, "OPS_FORWARD_PROVENANCE_CHANGED")
        if bars[-1].end_us <= forward["last_end"]:
            return {"duplicate": True, "entered": False}
        gap = bars[-1].end_us != forward["last_end"] + HOUR
        timely = 0 <= received - bars[-1].end_us <= 180_000_000 and not gap
        broker = Broker.__new__(Broker)
        broker.config, broker.profile, broker.scenario = store.config, profile, "baseline"
        for name in BROKER_FIELDS:
            setattr(broker, name, deepcopy(forward["broker"][name]))
        # JSON object keys are text, including trade timestamp maps after restart.
        learner = Learner(forward["learner"])
        context = regime(bars)
        if timely:
            for identity, previous in forward["last_signal"].items():
                if previous.get("enter"):
                    cost = profile.n("entry_fee_rate") + profile.n("exit_fee_rate")
                    cost += (profile.n("entry_slippage_bps") + profile.n("exit_slippage_bps") + 2) / 10000
                    reward = bars[-1].close / Decimal(forward["last_price"]) - 1 - cost
                    learner.observe(fingerprint({"bar": bars[-1].end_us, "strategy": identity, "source": provenance}),
                        identity, previous["regime"], text(max(Decimal(-1), min(Decimal(1), reward))), "1",
                        complete=True, provenance="synthetic" if provenance == "synthetic" else "forward_paper")
        before = len(broker.state["closed_trades"])
        broker.mark(received, bars[-1].close)
        pending = forward["pending"]
        entered, reasons = False, []
        if broker.current() and (not timely or now.hour >= 22 or state["halts"]):
            broker.close(received, bars[-1].close)
        elif pending and pending["due_end"] == bars[-1].end_us and timely and not state["halts"]:
            if broker.current() and pending.get("exit"):
                broker.close(received, bars[-1].close)
            elif broker.current() is None and pending.get("enter") and now.hour < 22:
                result = broker.enter(received, bars[-1].close, pending["stop_price"], pending["signal_at_us"])
                entered = result["paper_submitted"]
                reasons = result["risk_decision"]["reasons"]
                if entered:
                    forward["active_strategy"], forward["active_regime"] = pending["strategy"], pending["regime"]
        for trade in broker.state["closed_trades"][before:]:
            strategy = forward["active_strategy"]
            if strategy:
                # Shadow one-bar samples never become completed portfolio trades.
                store.record({"kind": "forward_paper_trade", "bindings": bindings(store.config), "at": stamp(now),
                    "provenance": provenance, "timely": timely, "closed": True,
                    "net_pnl_usd": trade["net_pnl_usd"], "strategy": strategy,
                    "trade": trade["id"], "execution": "hypothetical_ohlcv_model"})
        decisions = {}
        for identity, card in CARDS.items():
            decision = baseline(bars, card) if card in STRATEGIES else additional(bars, card)
            decisions[identity] = {**decision, "regime": context}
        selected = learner.select(context, CARDS, provenance="synthetic" if provenance == "synthetic" else "forward_paper")
        if broker.current() and forward["active_strategy"]:
            selected = forward["active_strategy"]
        next_decision = decisions.get(selected)
        forward.update(last_end=bars[-1].end_us, last_price=text(bars[-1].close), last_signal=decisions,
            pending={**next_decision, "due_end": bars[-1].end_us + 2 * HOUR, "strategy": selected} if next_decision and timely else None,
            broker={name: deepcopy(getattr(broker, name)) for name in BROKER_FIELDS})
        # Pending decisions wait an entire bar; retain an earlier decision until due.
        if pending and pending["due_end"] > bars[-1].end_us and timely:
            forward["pending"] = pending
        state["equity_usd"] = text(Decimal(broker.state["cash_usd"]) + Decimal(broker.state["position_value_usd"]))
        state["high_water_usd"] = broker.state["high_water_usd"]
        store.record({"kind": "forward_observation", "bindings": bindings(store.config), "at": stamp(now),
            "bar_end_us": bars[-1].end_us, "timely": timely, "gap": gap, "provenance": provenance,
            "selected": selected, "entered": entered, "reasons": reasons})
        store.save(state, "forward_step")
        return {"timely": timely, "gap": gap, "entered": entered, "selected": selected,
                "reasons": reasons, "equity_usd": state["equity_usd"]}
