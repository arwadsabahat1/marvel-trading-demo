"""Replayable synthetic venue, fills and accounting in the risk transaction.

Only this reducer derives accounting reports. A fixture controls timing/liquidity;
prices and fees are derived from the bound profile and fresh synthetic contexts.
"""

from copy import deepcopy
from decimal import Decimal

from ..clock import stamp
from ..risk.engine import active_claims, active_halts, latch
from ..risk.model import (ceil_step, fingerprint, floor_step, history_limit, identifier,
                          keys, number, reject, request, text, timestamp)

ZERO = Decimal("0")
DONE = {"filled", "cancelled", "rejected"}
KINDS = {"ack", "reject", "timeout", "recover", "cancel", "cancelled", "buy_fill", "sell_fill", "quote", "close"}


def fresh_market(market, now, config, profile, *, context_kind="synthetic"):
    # Reuse the closed context schema; no arbitrary execution prices or FX setters.
    request({"proposal": {"id": "0" * 64, "symbol": "BTCUSDT", "side": "BUY", "stop_price": "1", "quantity": None}, "market": market}, context_kind=context_kind)
    if any(not 0 <= (now - timestamp(market[field])).total_seconds() <= config.max_quote_age_seconds for field in ("quote_at", "fx_at")):
        reject()
    if not profile.n("min_price") <= Decimal(market["bid"]) <= Decimal(market["ask"]) <= profile.n("max_price"):
        reject()
    return deepcopy(market)


def order_by_id(book, value):
    identifier(value)
    matches = [order for order in book["orders"] if order["id"] == value]
    if len(matches) != 1:
        reject("RISK_TRANSITION")
    return matches[0]


def cycle_by_id(book, value):
    return next(cycle for cycle in book["cycles"] if cycle["id"] == value)


def venue_status(order, status):
    order["broker_status"] = status
    if order["status"] != "unknown":
        order["status"] = status


def new_order(identity, cycle_id, side, quantity, *, status="open"):
    return {"id": identity, "cycle_id": cycle_id, "side": side, "quantity": text(quantity),
            "filled_quantity": "0", "status": status, "broker_status": "open", "fills": []}


def sellable(cycle, profile):
    return floor_step(Decimal(cycle["holding_btc"]), profile.n("exit_step"))


def exit_price(market, profile):
    return floor_step(Decimal(market["bid"]) * (1 - profile.n("exit_slippage_bps") / 10_000), profile.n("tick_size"))


def proceeds(quantity, price, market, profile):
    gross = quantity * price
    fee = ceil_step(gross * profile.n("exit_fee_rate"), profile.n("quote_fee_quantum")) if gross else ZERO
    return max(ZERO, gross - fee) * Decimal(market["fx_lower"]), fee


def fill(book, order, quantity, market, profile, fill_id, when):
    quantity = number(quantity)
    step = profile.n("entry_step" if order["side"] == "BUY" else "exit_step")
    remaining = Decimal(order["quantity"]) - Decimal(order["filled_quantity"])
    if (order["broker_status"] in DONE or quantity > remaining or quantity != floor_step(quantity, step)):
        reject("RISK_TRANSITION")
    cycle = cycle_by_id(book, order["cycle_id"])
    if order["side"] == "BUY":
        price = ceil_step(Decimal(market["ask"]) * (1 + profile.n("entry_slippage_bps") / 10_000), profile.n("tick_size"))
        if price > profile.n("max_price"):
            reject()
        base_fee = (ceil_step(quantity * profile.n("entry_fee_rate"), profile.n("base_fee_quantum"))
                    if profile.document["entry_fee_asset"] == "BTC" else ZERO)
        quote_fee = (ceil_step(quantity * price * profile.n("entry_fee_rate"), profile.n("quote_fee_quantum"))
                     if profile.document["entry_fee_asset"] == "USDT" else ZERO)
        cost = (quantity * price + quote_fee) * Decimal(market["fx_upper"])
        fee_usd = (base_fee * price + quote_fee) * Decimal(market["fx_upper"])
        if base_fee >= quantity or cost > Decimal(book["cash_usd"]):
            reject()  # This spot simulator cannot borrow or invent cash.
        book["cash_usd"] = text(Decimal(book["cash_usd"]) - cost)
        cycle["entry_cost_usd"] = text(Decimal(cycle["entry_cost_usd"]) + cost)
        cycle["holding_btc"] = text(Decimal(cycle["holding_btc"]) + quantity - base_fee)
        cycle["bought_btc"] = text(Decimal(cycle["bought_btc"]) + quantity)
    else:
        if quantity > Decimal(cycle["holding_btc"]):
            reject("RISK_TRANSITION")
        price = exit_price(market, profile)
        if price < profile.n("min_price"):
            reject()
        net, quote_fee = proceeds(quantity, price, market, profile)
        base_fee, fee_usd = ZERO, quote_fee * Decimal(market["fx_lower"])
        book["cash_usd"] = text(Decimal(book["cash_usd"]) + net)
        cycle["exit_proceeds_usd"] = text(Decimal(cycle["exit_proceeds_usd"]) + net)
        cycle["holding_btc"] = text(Decimal(cycle["holding_btc"]) - quantity)
    order["filled_quantity"] = text(Decimal(order["filled_quantity"]) + quantity)
    next_status = ("filled" if order["filled_quantity"] == order["quantity"] else
                   "cancel_pending" if order["broker_status"] == "cancel_pending" else "partially_filled")
    venue_status(order, next_status)
    if order["broker_status"] == "filled":
        order["status"] = "filled"  # A complete fill is authoritative terminal evidence.
    book["fees_usd"] = text(Decimal(book["fees_usd"]) + fee_usd)
    order["fills"].append({"id": fill_id, "at": when, "quantity": text(quantity), "price": text(price),
                           "fee_btc": text(base_fee), "fee_usdt": text(quote_fee), "fee_usd": text(fee_usd)})


def make_exit(book, cycle, profile, market, event_id):
    existing = [order for order in book["orders"] if order["cycle_id"] == cycle["id"] and order["side"] == "SELL" and order["broker_status"] not in DONE]
    if existing:
        return existing[0]
    quantity, price = sellable(cycle, profile), exit_price(market, profile)
    if (not profile.n("exit_min_qty") <= quantity <= profile.n("exit_max_qty")
            or price < profile.n("min_price")
            or not profile.n("exit_min_notional_quote") <= quantity * price <= profile.n("exit_max_notional_quote")):
        return None
    identity = fingerprint({"paper_exit": cycle["id"], "event": event_id})
    order = new_order(identity, cycle["id"], "SELL", quantity)
    book["orders"].append(order)
    return order


def cancel_entry(book, cycle):
    entry = order_by_id(book, cycle["id"])
    if entry["broker_status"] not in DONE:
        # The in-process synthetic venue confirms its own cancellation/fill ledger.
        entry["broker_status"] = "cancelled"
    entry["status"] = entry["broker_status"]


def accounting(state, book, event_id, stage, now, config, profile):
    from ..risk.ledger import reconcile
    market = book["market"]
    value = notional = stop_value = unrealized = ZERO
    current = [cycle for cycle in book["cycles"] if not cycle["settled"]]
    positions, pending, position_id = 0, 0, None
    closed, flat, no_fill = [], [], []
    for cycle in current:
        orders = [order for order in book["orders"] if order["cycle_id"] == cycle["id"]]
        entry = order_by_id(book, cycle["id"])
        pending += int(entry["status"] not in DONE)
        holding = Decimal(cycle["holding_btc"])
        if holding:
            positions, position_id = 1, cycle["id"]
            quantity = sellable(cycle, profile)
            # Nontradeable residuals have zero recovery; ownership still remains.
            price = exit_price(market, profile)
            active_exit = any(order["side"] == "SELL" and order["broker_status"] not in DONE
                              and Decimal(order["quantity"]) - Decimal(order["filled_quantity"]) >= quantity for order in orders)
            liquid = active_exit or (quantity >= profile.n("exit_min_qty") and quantity * price >= profile.n("exit_min_notional_quote"))
            value = proceeds(quantity, price, market, profile)[0] if liquid else ZERO
            notional = holding * Decimal(market["bid"]) * Decimal(market["fx_lower"])
            stop_price = min(price, floor_step(Decimal(cycle["stop_trigger"]) * (1 - profile.n("exit_slippage_bps") / 10_000), profile.n("tick_size")))
            stop_value = (proceeds(quantity, stop_price, market, profile)[0]
                          if active_exit or quantity >= profile.n("exit_min_qty") and quantity * stop_price >= profile.n("exit_min_notional_quote") else ZERO)
        if not holding and all(order["status"] in DONE for order in orders):
            cycle["settled"] = True
            flat.append(cycle["id"])
            if Decimal(cycle["bought_btc"]):
                pnl = Decimal(cycle["exit_proceeds_usd"]) - Decimal(cycle["entry_cost_usd"])
                outcome = {"id": fingerprint({"paper_trade": cycle["id"]}), "reservation_id": cycle["id"], "net_pnl_usd": text(pnl)}
                closed.append(outcome)
                book["closed_trades"].append(outcome)
            else:
                no_fill.append(cycle["id"])
        elif holding:
            unrealized += value + Decimal(cycle["exit_proceeds_usd"]) - Decimal(cycle["entry_cost_usd"])
    report = {"id": fingerprint({"paper_report": event_id, "stage": stage}),
              "cash_usd": book["cash_usd"], "position_value_usd": text(value), "position_notional_usd": text(notional),
              "open_risk_usd": text(max(ZERO, value - stop_value)), "position_count": positions,
              "pending_entry_count": pending, "position_reservation_id": position_id,
              "unrealized_net_pnl_usd": text(unrealized), "total_fees_usd": book["fees_usd"],
              "closed_trades": closed, "confirmed_flat_ids": flat, "no_fill_ids": no_fill}
    reconcile(state, report, now, config)
    return report


def protect(state, book, market, event_id, now, config, profile, manual_halts):
    for cycle in book["cycles"]:
        if cycle["settled"]:
            continue
        holding = Decimal(cycle["holding_btc"])
        triggered = Decimal(market["bid"]) <= Decimal(cycle["stop_trigger"])
        if not (triggered or manual_halts or active_halts(state)):
            continue
        cancel_entry(book, cycle)
        if holding:
            order = make_exit(book, cycle, profile, market, event_id)
            if order is None:
                latch(state, "PAPER_LIQUIDATION_BLOCKED")
            else:
                # The fixture's stop/emergency policy assumes full liquidity here.
                # Prices gap with the quote; no fill at an obsolete stop price.
                remaining = Decimal(order["quantity"]) - Decimal(order["filled_quantity"])
                fill(book, order, text(remaining), market, profile, fingerprint({"stop_fill": event_id}), stamp(now))
                order["status"] = order["broker_status"]
                if Decimal(cycle["holding_btc"]):
                    latch(state, "PAPER_LIQUIDATION_BLOCKED")


def reduce(state, data, when, config, profile, manual_halts=(), history=None, *, context_kind="synthetic"):
    """Called only by the audited risk reducer inside its fixed Decimal context."""
    from functools import partial
    from ..risk.ledger import reduce as base_risk_reduce
    risk_reduce = partial(base_risk_reduce, context_kind=context_kind)
    now = timestamp(when)
    if not isinstance(data, dict) or not isinstance(data.get("action"), str) or data["action"] not in {"init", "submit", "event"}:
        reject()
    action = data["action"]
    if action == "init":
        keys(data, {"action"})
        if "paper_state" in state:
            if state["paper_state"]["mode"] != context_kind:
                reject()
            return state, {"paper_initialized": True, "paper_initialization_reused": True}
        if active_claims(state) or state["position_count"] or state["pending_entry_count"]:
            reject("RISK_TRANSITION")
        state["paper_state"] = {"schema_version": 1, "mode": context_kind, "initialized_at": when,
                                "origin_cash_usd": state["cash_usd"], "cash_usd": state["cash_usd"],
                                "origin_fees_usd": state["total_fees_usd"], "fees_usd": state["total_fees_usd"],
                                "orders": [], "cycles": [], "closed_trades": [], "events": {}, "market": None}
        return state, {"paper_initialized": True, "paper_initialization_reused": False}
    if "paper_state" not in state:
        reject("RISK_TRANSITION")
    book = state.pop("paper_state")
    if book["mode"] != context_kind:
        reject()
    if action == "submit":
        keys(data, {"action", "request"})
        document = request(data["request"], context_kind=context_kind)
        event_id = document["proposal"]["id"]
    else:
        keys(data, {"action", "event"})
        event = keys(data["event"], {"id", "kind", "order_id", "quantity", "market"})
        event_id = identifier(event["id"])
        if not isinstance(event["kind"], str) or event["kind"] not in KINDS:
            reject()
    signature = fingerprint(data)
    if event_id in book["events"]:
        if book["events"][event_id] != signature:
            reject("RISK_ID_CONFLICT")
        state["paper_state"] = book
        return state, {"paper_event_reused": True, "event_id": event_id}
    result = {"paper_event_reused": False, "event_id": event_id}
    if action == "submit":
        if len(book["events"]) + len(book["orders"]) >= history_limit(context_kind):
            reject("RISK_HISTORY_FULL")
        state, assessed = risk_reduce(state, "reserve", document, when, config, profile, manual_halts, history)
        result["risk_decision"] = assessed
        result["paper_submitted"] = assessed["risk_check_passed"]
        if assessed["risk_check_passed"]:
            state, _ = risk_reduce(state, "submitted", {"id": event_id, "market": document["market"]}, when, config, profile, manual_halts, history)
            plan = next(claim["plan"] for claim in state["claims"] if claim["id"] == event_id)
            book["market"] = fresh_market(document["market"], now, config, profile, context_kind=context_kind)
            book["orders"].append(new_order(event_id, event_id, "BUY", Decimal(plan["quantity"]), status="pending"))
            book["cycles"].append({"id": event_id, "stop_trigger": plan["stop_trigger"], "entry_cost_usd": "0",
                                   "exit_proceeds_usd": "0", "holding_btc": "0", "bought_btc": "0", "settled": False})
            result["order_id"] = event_id
    else:
        kind, quantity = event["kind"], event["quantity"]
        if (kind == "quote") != (event["order_id"] is None):
            reject()
        order = None if kind == "quote" else order_by_id(book, event["order_id"])
        if (kind in {"buy_fill", "sell_fill"}) != (quantity is not None):
            reject()
        if event["market"] is not None:
            book["market"] = fresh_market(event["market"], now, config, profile, context_kind=context_kind)
        elif kind not in {"ack", "reject", "timeout", "recover", "cancel", "cancelled"} or (kind != "timeout" and any(Decimal(c["holding_btc"]) for c in book["cycles"])):
            reject()  # Never refresh an open-position valuation with an old quote.
        if kind in {"buy_fill", "sell_fill"}:
            if order["side"] != ("BUY" if kind == "buy_fill" else "SELL"):
                reject("RISK_TRANSITION")
            fill(book, order, quantity, book["market"], profile, event_id, when)
        elif kind == "timeout":
            if order["status"] in DONE:
                reject("RISK_TRANSITION")
            order["status"] = "unknown"
            state, _ = risk_reduce(state, "unknown", {"id": order["cycle_id"]}, when, config, profile, manual_halts, history)
        elif kind == "recover":
            if order["status"] != "unknown":
                reject("RISK_TRANSITION")
            order["status"] = order["broker_status"]
        elif kind == "ack":
            if order["status"] == "unknown":
                reject("RISK_TRANSITION")
            if order["status"] == "pending":
                order["status"] = order["broker_status"]
        elif kind == "cancel":
            if order["status"] == "unknown" or order["broker_status"] in DONE:
                reject("RISK_TRANSITION")
            venue_status(order, "cancel_pending")
        elif kind == "cancelled":
            if order["broker_status"] == "filled":
                result["cancel_lost_fill_race"] = True
            elif order["broker_status"] != "cancel_pending":
                reject("RISK_TRANSITION")
            else:
                venue_status(order, "cancelled")
        elif kind == "reject":
            if Decimal(order["filled_quantity"]) or order["broker_status"] in DONE:
                reject("RISK_TRANSITION")
            venue_status(order, "rejected")
        elif kind == "close":
            if order["side"] != "BUY":
                reject("RISK_TRANSITION")
            cycle = cycle_by_id(book, order["cycle_id"])
            if cycle["settled"] or not Decimal(cycle["holding_btc"]):
                reject("RISK_TRANSITION")
            cancel_entry(book, cycle)
            exit_order = make_exit(book, cycle, profile, book["market"], event_id)
            if exit_order is None:
                latch(state, "PAPER_LIQUIDATION_BLOCKED")
            else:
                result["exit_order_id"] = exit_order["id"]
    book["events"][event_id] = signature
    # Unknown-without-data records uncertainty without fabricating a new mark.
    no_quote_timeout = action == "event" and data["event"]["kind"] == "timeout" and data["event"]["market"] is None
    accepted_submit = action == "submit" and result["paper_submitted"]
    if accepted_submit or action == "event" and not no_quote_timeout:
        from ..risk.engine import financial_halts
        accounting(state, book, event_id, 1, now, config, profile)
        financial_halts(state, config, history)
        if accepted_submit or data.get("event", {}).get("market") is not None:
            protect(state, book, book["market"], event_id, now, config, profile, manual_halts)
            accounting(state, book, event_id, 2, now, config, profile)
    state["paper_state"] = book
    return state, result
