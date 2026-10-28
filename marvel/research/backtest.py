"""Historical OHLCV assumptions, explicitly separated from synthetic/live contexts."""

from collections import Counter
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext

from ..clock import stamp
from ..data.quality import EPOCH
from ..errors import RiskError
from ..paper.ledger import DONE
from ..risk.engine import active_halts, equity, seed, valuation_point
from ..risk.ledger import reduce
from ..risk.model import PRECISION, fingerprint, floor_step, text
from .features import signal
from .feasibility import complete_entry_decision
from .model import CONTEXT, fail, plan


def at(microseconds):
    return EPOCH + timedelta(microseconds=microseconds)


def market(price, when, scenario):
    half = Decimal("0.0002" if scenario == "double_spread_slippage" else "0.0001")
    return {"kind": CONTEXT, "bid": text(price * (1 - half)), "ask": text(price * (1 + half)),
            "quote_at": stamp(at(when)), "fx_at": stamp(at(when)), "fx_lower": "0.995", "fx_upper": "1.005"}


class Broker:
    def __init__(self, config, profile, start_us, scenario, *, miss_every=0):
        if type(miss_every) is not int or not 0 <= miss_every <= 100:
            fail()
        self.miss_every, self.eligible_entry_attempts = miss_every, 0
        self.config, self.profile, self.scenario = config, profile, scenario
        self.state = seed(config, profile, config.starting_equity_usd, config.starting_equity_usd, stamp(at(start_us)))
        self.history = [valuation_point(self.state)]
        self.sequence, self.chain = 0, "0" * 64
        self.points, self.trade_closed_at, self.trade_entries = [], {}, {}
        self.apply({"action": "init"}, start_us)

    @property
    def book(self):
        return self.state["paper_state"]

    def current(self):
        return next((cycle for cycle in self.book["cycles"] if not cycle["settled"]), None)

    def apply(self, operation, when):
        self.sequence += 1
        old_closed = len(self.state["closed_trades"])
        self.state, result = reduce(self.state, "paper", operation, stamp(at(when)), self.config, self.profile,
                                    history=self.history, context_kind=CONTEXT)
        self.history.append(valuation_point(self.state))
        self.chain = fingerprint({"previous": self.chain, "at_us": when, "operation": operation})
        for trade in self.state["closed_trades"][old_closed:]:
            self.trade_closed_at[trade["id"]] = when
        self.check()
        return result

    def check(self):
        # Exact independent identities after every transition, not just at run end.
        cost = sum((Decimal(c["entry_cost_usd"]) for c in self.book["cycles"]), Decimal(0))
        sold = sum((Decimal(c["exit_proceeds_usd"]) for c in self.book["cycles"]), Decimal(0))
        if (Decimal(self.book["origin_cash_usd"]) - cost + sold != Decimal(self.state["cash_usd"])
                or Decimal(self.state["cash_usd"]) + Decimal(self.state["position_value_usd"]) != equity(self.state)):
            fail()
        for cycle in self.book["cycles"]:
            acquired = sum((Decimal(f["quantity"]) - Decimal(f["fee_btc"]) for order in self.book["orders"]
                            if order["cycle_id"] == cycle["id"] and order["side"] == "BUY" for f in order["fills"]), Decimal(0))
            exited = sum((Decimal(order["filled_quantity"]) for order in self.book["orders"]
                          if order["cycle_id"] == cycle["id"] and order["side"] == "SELL"), Decimal(0))
            if acquired - exited != Decimal(cycle["holding_btc"]):
                fail()

    def event(self, kind, when, price, *, order_id=None, quantity=None):
        event = {"id": fingerprint({"step": self.sequence, "kind": kind, "when": when}), "kind": kind,
                 "order_id": order_id, "quantity": quantity, "market": market(price, when, self.scenario)}
        return self.apply({"action": "event", "event": event}, when)

    def mark(self, when, price):
        if self.current():
            self.event("quote", when, price)
        self.point(when)

    def point(self, when):
        self.points.append({"at_us": when, "equity_usd": text(equity(self.state)),
                            "notional_usd": self.state["position_notional_usd"],
                            "holding_btc": text(sum((Decimal(c["holding_btc"]) for c in self.book["cycles"]), Decimal(0))),
                            "closed_trades": len(self.state["closed_trades"])})

    def enter(self, when, price, stop, signal_at):
        identity = fingerprint({"entry": when, "signal_at": signal_at, "sequence": self.sequence})
        rounded_stop = text(floor_step(Decimal(stop), self.profile.n("tick_size")))
        if Decimal(rounded_stop) <= 0:
            return {"paper_submitted": False, "risk_decision": {"risk_check_passed": False, "reasons": ["PRICE_OR_STOP_INVALID"]}}
        request = {"proposal": {"id": identity, "symbol": "BTCUSDT", "side": "BUY", "stop_price": rounded_stop, "quantity": None},
                   "market": market(price, when, self.scenario)}
        checked = complete_entry_decision(self.state, self.config, self.profile, request, at(when), self.history)
        if not checked["risk_check_passed"]:
            return {"paper_submitted": False, "risk_decision": checked}
        if self.miss_every:
            self.eligible_entry_attempts += 1
            if self.eligible_entry_attempts % self.miss_every == 0:
                return {"paper_submitted": False, "risk_decision": {
                    "risk_check_passed": False, "reasons": ["MODELED_ENTRY_MISS"]}}
        request["proposal"]["quantity"] = checked["plan"]["quantity"]
        result = self.apply({"action": "submit", "request": request}, when)
        if result["paper_submitted"]:
            self.trade_entries[identity] = {"signal_at_us": signal_at, "entry_at_us": when}
            self.event("buy_fill", when, price, order_id=identity, quantity=result["risk_decision"]["plan"]["quantity"])
        return result

    def close(self, when, price):
        cycle = self.current()
        if not cycle or not Decimal(cycle["holding_btc"]):
            return
        result = self.event("close", when, price, order_id=cycle["id"])
        if result.get("exit_order_id"):
            order = next(order for order in self.book["orders"] if order["id"] == result["exit_order_id"])
            if order["status"] not in DONE:
                self.event("sell_fill", when, price, order_id=order["id"], quantity=text(Decimal(order["quantity"]) - Decimal(order["filled_quantity"])))

    def frozen(self):
        current = self.current()
        if current and Decimal(current["holding_btc"]) < self.profile.n("exit_step"):
            return bool(active_halts(self.state))  # Lot dust cannot be sold at any later price under this model.
        return bool(active_halts(self.state)) and current is None


def run(candles, strategy, config, profile, start, end, scenario="baseline", *, miss_every=0, signal_function=None):
    """One continuous portfolio through all requested windows; never reset a halt."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        broker = Broker(config, profile, candles[start].open_us, scenario, miss_every=miss_every)
        decide = signal if signal_function is None else signal_function
        rejected, ready, pending, terminated = Counter(), 0, None, None
        initial = decide(candles[:start], strategy)
        if initial["ready"] and initial["enter"]:
            pending = (start + 1, initial)
        broker.point(candles[start].open_us)
        curve = []
        for index in range(start, end):
            candle = candles[index]
            hour = at(candle.open_us).hour
            if broker.frozen() or terminated:
                # Cash/zero-recovery lot dust cannot generate any later model P&L.
                broker.point(candle.end_us)
                # Economic exposure still moves with BTC even when step dust has zero recovery.
                broker.points[-1]["notional_usd"] = text(Decimal(broker.points[-1]["holding_btc"]) * Decimal(market(candle.close, candle.end_us, scenario)["bid"]) * Decimal("0.995"))
                curve.append(deepcopy(broker.points[-1]))
                continue
            broker.mark(candle.open_us, candle.open)
            if hour == 23:
                broker.close(candle.open_us, candle.open)
                pending = None
            elif pending and pending[0] == index:
                decision = pending[1]
                pending = None
                if broker.current() and decision["exit"]:
                    broker.close(candle.open_us, candle.open)
                elif not broker.current() and decision["enter"]:
                    if hour >= 22:
                        rejected["SESSION_ENTRY_BLOCK"] += 1
                    else:
                        try:
                            result = broker.enter(candle.open_us, candle.open, decision["stop_price"], decision["signal_at_us"])
                            rejected.update(result["risk_decision"]["reasons"])
                        except RiskError as exc:
                            if exc.code != "RISK_HISTORY_FULL":
                                raise
                            terminated = exc.code
            # These are hypothetical intrabar observations, not exchange event timestamps.
            for when, price in ((candle.open_us + 900_000_000, candle.high),
                                (candle.open_us + 1_800_000_000, candle.low)):
                broker.mark(when, price)
            if index == end - 1:
                broker.close(candle.end_us - 1, candle.close)
            broker.mark(candle.end_us - 1, candle.close)
            curve.append({**deepcopy(broker.points[-1]), "at_us": candle.end_us})
            decision = decide(candles[:index + 1], strategy)
            ready += int(decision["ready"])
            if pending is None and decision["ready"] and (decision["enter"] if broker.current() is None else decision["exit"]):
                pending = (index + 2, decision)
        broker.check()
        trades = []
        for trade in broker.state["closed_trades"]:
            cycle = next(c for c in broker.book["cycles"] if c["id"] == trade["reservation_id"])
            trades.append({**trade, **broker.trade_entries[cycle["id"]], "closed_at_us": broker.trade_closed_at[trade["id"]],
                           "entry_cost_usd": cycle["entry_cost_usd"],
                           "net_return_fraction": text(Decimal(trade["net_pnl_usd"]) / Decimal(cycle["entry_cost_usd"]))})
        return {"strategy": strategy.identity, "scenario": scenario, "start_index": start, "end_index_exclusive": end,
                "ready_signal_bars": ready, "rejections": dict(sorted(rejected.items())), "termination": terminated,
                "halt_reasons": active_halts(broker.state), "curve": curve, "intrabar_marks": broker.points,
                "trades": trades, "ledger": deepcopy(broker.book), "final_equity_usd": text(equity(broker.state)),
                "open_position_count": broker.state["position_count"], "unrealized_net_pnl_usd": broker.state["unrealized_net_pnl_usd"],
                "event_count": broker.sequence, "event_chain_sha256": broker.chain,
                "final_state_sha256": fingerprint(broker.state), "execution_model": CONTEXT,
                "availability_assumption": plan()["latency"], "reconciliation_checked": True}
