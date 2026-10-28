"""Connect prospective prices, bounded learning, risk-controlled execution and service."""

from copy import deepcopy
from decimal import Decimal

from marvel.research.features import signal as baseline
from marvel.research.model import STRATEGIES
from marvel.research.rules import signal as additional
from marvel.risk.model import fingerprint
from .common import Stop, require, stamp, utc_now
from .execution import Trader
from .forward import bars_from_rest, tick as paper_tick
from .learning import CARDS, Learner, regime
from .transport import Spot
from .validation import readiness


def step(store, factory, *, now=utc_now):
    client = factory()
    client.request("GET", "time")
    bars = bars_from_rest(client.request("GET", "bars", {"symbol": "BTCUSDT", "interval": "1h", "limit": 600}), now())
    if store.mode == "paper":
        return paper_tick(store, bars, observed_at=now(), provenance="synthetic" if client.provenance == "synthetic" else "public_network")
    trader = Trader(store, client, now=now)
    state = trader.reconcile()
    context = regime(bars)
    learner = Learner(state["strategies"] or None)
    for cycle in state["closed"]:
        learner.observe(fingerprint({"settled": cycle["id"]}), cycle["strategy"], cycle.get("regime", "range"),
            str(Decimal(cycle["received_usd"]) - Decimal(cycle["spent_usd"])), cycle["spent_usd"] or "1",
            complete=True, provenance=store.mode) if Decimal(cycle["spent_usd"]) else None
    state["strategies"] = learner.document
    qualified = readiness(store, state, now())["qualified_strategies"] if store.mode == "live" else sorted(CARDS)
    selected = learner.champion(context, qualified, provenance=store.mode)
    active = state["active"]
    if active:
        selected = active["strategy"]
    card = CARDS.get(selected)
    decision = (baseline(bars, card) if card in STRATEGIES else additional(bars, card)) if card else {"enter": False, "exit": False}
    late = not 0 <= int(now().timestamp() * 1_000_000) - bars[-1].end_us <= 180_000_000
    already_seen = state.get("last_decision_bar") == bars[-1].end_us
    with store.lock():
        current = store.load()
        current["strategies"] = state["strategies"]
        if not already_seen:
            current["last_decision_bar"] = bars[-1].end_us
        store.save(current, "strategy_decision")
    if active:
        if decision.get("exit") or now().hour >= 22 or state["halts"] or not active["protected"]:
            return {"closed": trader.close()["active"] is None}
        return {"holding_protected": True}
    if already_seen or late or not decision.get("enter") or state["halts"]:
        return {"entered": False, "selected": selected, "reason": "NO_NEW_ELIGIBLE_ENTRY"}
    return trader.enter(decision["stop_price"], selected, regime_context=context)
