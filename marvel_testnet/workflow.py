"""Bounded Testnet preflight and manual engineering round trip, with GET-only recovery."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext
import uuid

from marvel.clock import stamp, utc_now
from marvel.risk.engine import advance, equity, financial_halts, valuation_point
from marvel.risk.model import PRECISION, ceil_step, floor_step, text
from . import __version__
from . import model
from .model import DemoError, HOST, TERMINAL, ZERO, fail
from .store import Store
from marvel.data.quality import utc_us


def total(account, asset):
    with localcontext() as context:
        context.prec = PRECISION
        return sum((Decimal(v) for v in account["balances"][asset].values()), ZERO)


def public(client):
    client.request("GET", "time")
    rules = model.symbol(client.request("GET", "rules", {"symbol": "BTCUSDT"}))
    quote = model.quote(client.request("GET", "quote", {"symbol": "BTCUSDT"}))
    return {"host": HOST, "rules": rules, "quote": quote, "receipts": deepcopy(client.receipts),
        "quote_received_monotonic": client.monotonic(),
        "testnet_only": True, "production_execution_available": False}


def observe(client, state):
    observed = public(client)
    observed["account"] = model.account(client.request("GET", "account"), state["protocol"]["nonce"])
    observed["fees"] = model.fees(client.request("GET", "fees", {"symbol": "BTCUSDT"}))
    opened = client.request("GET", "open")
    if type(opened) is not list or len(opened) > 1000:
        fail()
    observed["open_orders"] = len(opened)
    observed["quote_age_seconds"] = client.monotonic() - observed["quote_received_monotonic"]
    observed.pop("quote_received_monotonic")
    return observed


def preflight(config, client, *, now=utc_now):
    store = Store(config)
    with store.locked():
        state = store.load()
        store.require_current(state)
        result = preflight_state(config, client, state, now())
        store.save(state, "preflight")
        return result


def preflight_state(config, client, state, now):
    if state["active"] is not None or state["review_reasons"]:
        fail("TESTNET_REVIEW_REQUIRED")
    observed = observe(client, state)
    binding = observed["account"]["binding"]
    if state["account_binding"] is not None and state["account_binding"] != binding:
        fail("TESTNET_ACCOUNT_CHANGED")
    state["account_binding"] = binding
    blockers = []
    if abs(client.server_ms - utc_us(stamp(now)) // 1000) > 30000:
        blockers.append("TESTNET_LOCAL_CLOCK_MISMATCH")
    if not 0 <= observed["quote_age_seconds"] <= config.max_quote_age_seconds:
        blockers.append("TESTNET_QUOTE_STALE")
    if observed["open_orders"]:
        blockers.append("TESTNET_EXISTING_OPEN_ORDERS")
    if now.hour >= 22:
        blockers.append("TESTNET_SESSION_ENTRY_BLOCK")
    if len(state["cycles"]) >= 20:
        blockers.append("TESTNET_ENGINEERING_CYCLE_LIMIT")
    rates = observed["fees"]
    if rates["bnb_possible"] and max(Decimal(rates["buy"]), Decimal(rates["sell"])) > 0 and total(observed["account"], "BNB") > 0:
        blockers.append("TESTNET_BNB_FEE_INVENTORY_UNALLOCATED")
    checked = model.plan(config, state, observed, now)
    blockers.extend(checked["reasons"])
    if checked["risk_check_passed"] and Decimal(checked["plan"]["cash_reserved_usd"]) / Decimal("1.005") > Decimal(observed["account"]["balances"]["USDT"]["free"]):
        blockers.append("TESTNET_VIRTUAL_FUNDS_INSUFFICIENT")
    result = {"observed": observed, "risk_projection": checked, "blockers": sorted(set(blockers)),
        "virtual_cycle_preflight_passed": not blockers, "production_execution_available": False,
        "actual_production_fees_qualified": False, "strategy_qualified": False}
    state["last_preflight"] = deepcopy(result)
    return result


def start_cycle(config, client, *, now=utc_now):
    if not client.allow_orders:
        fail("TESTNET_VIRTUAL_ORDER_OPT_IN_REQUIRED")
    store = Store(config)
    with store.locked(), localcontext() as context:
        context.prec = PRECISION
        state = store.load()
        store.require_current(state)
        checked = preflight_state(config, client, state, now())
        store.save(state, "cycle_preflight")
        if checked["blockers"]:
            return summary(state)
        plan = checked["risk_projection"]["plan"]
        tag = uuid.uuid4().hex[:20]
        state["risk"] = advance(state["risk"], config, now(), state["history"])
        state["active"] = {"id": tag, "started_at_utc": stamp(now()), "plan": plan,
            "rules": checked["observed"]["rules"], "rates": checked["observed"]["fees"],
            "baseline": checked["observed"]["account"], "orders": {}, "stage": "planned",
            "holding_btc": "0", "quote_delta": "0", "external_fee_assets": {},
            "entry_cost_usd": "0", "unprotected_interval_observed": True, "protection_acknowledged": False,
            "holding_quantity_fully_reconciled": False, "protection_ever_acknowledged": False}
        store.save(state, "cycle_plan")
        try:
            cycle = state["active"]
            entry = params(cycle, "entry", "BUY", "LIMIT", plan["quantity"], price=plan["entry_price_bound"])
            # Validation does not reach the matching engine. A refusal leaves the allocation unused.
            client.request("POST", "test", entry)
            client.request("POST", "test", params(cycle, "stop", "SELL", "STOP_LOSS", plan["exit_quantity"]))
            cycle["request_validation_passed"] = True
            store.save(state, "nonmatching_order_validation_passed")
            send(store, state, client, "entry", entry)
            sync(store, state, client, config, now())
            if state["active"] is not None:
                protect(store, state, client, config, now)
            if state["active"] is not None and not state["review_reasons"]:
                close_cycle(store, state, client, config, now)
        except DemoError as exc:
            if state["active"] is not None and not state["active"]["orders"]:
                state["active"] = None  # No matching-engine order intent was ever created.
                store.save(state, "pre_entry_validation_failed")
            store.review(state, exc.code)
        return summary(state)


def params(cycle, role, side, kind, quantity, *, price=None):
    result = {"symbol": "BTCUSDT", "side": side, "type": kind, "quantity": quantity,
        "newClientOrderId": f"mtd_{cycle['id']}_{role}", "newOrderRespType": "FULL"}
    if kind == "LIMIT":
        result.update(timeInForce="FOK", price=price)
    else:
        result["stopPrice"] = cycle["plan"]["stop_trigger"]
    return result


def send(store, state, client, role, request):
    store.require_current(state)
    cycle = state["active"]
    if role in cycle["orders"]:
        fail("TESTNET_ORDER_ALREADY_ATTEMPTED")
    cycle["orders"][role] = {"request": request, "observed": None, "fills": [], "intent_saved": True}
    cycle["stage"] = role + "_intent"
    store.save(state, role + "_intent")  # Durable BEFORE the only POST; never automatically retry an order.
    try:
        client.request("POST", "order", request)
    except DemoError:
        # A POST response is not settlement. Always query this same identifier next.
        pass
    refresh_order(cycle["orders"][role], client)
    store.save(state, role + "_observed")


def refresh_order(item, client):
    request = item["request"]
    raw = client.request("GET", "order", {"symbol": "BTCUSDT", "origClientOrderId": request["newClientOrderId"]})
    observed = model.order(raw, request["newClientOrderId"], request["side"], request["type"], request["quantity"])
    if (request["type"] == "LIMIT" and (model.number(raw["price"]) != Decimal(request["price"]) or raw["timeInForce"] != "FOK")
            or request["type"] == "STOP_LOSS" and model.number(raw["stopPrice"]) != Decimal(request["stopPrice"])):
        fail("TESTNET_ORDER_MISMATCH")
    if item["observed"] is not None and observed["order_id"] != item["observed"]["order_id"]:
        fail("TESTNET_ORDER_MISMATCH")
    if item["observed"] is not None and Decimal(observed["executed_btc"]) < Decimal(item["observed"]["executed_btc"]):
        fail("TESTNET_ORDER_FILL_REGRESSION")
    trades = client.request("GET", "trades", {"symbol": "BTCUSDT", "orderId": observed["order_id"], "limit": 1000})
    item["observed"], item["fills"] = observed, model.fills(trades, observed)


def balances(cycle):
    btc, quote, other = ZERO, ZERO, {}
    for item in cycle["orders"].values():
        for fill in item["fills"]:
            direction = Decimal(1) if item["request"]["side"] == "BUY" else Decimal(-1)
            btc += direction * Decimal(fill["quantity"])
            quote -= direction * Decimal(fill["quote"])
            fee, asset = Decimal(fill["commission"]), fill["commission_asset"]
            if asset == "BTC":
                btc -= fee
            elif asset == "USDT":
                quote -= fee
            elif fee:
                other[asset] = text(Decimal(other.get(asset, "0")) + fee)
    return btc, quote, other


def cash_flows(cycle):
    bought, sold = ZERO, ZERO
    for item in cycle["orders"].values():
        for fill in item["fills"]:
            quote = Decimal(fill["quote"])
            fee = Decimal(fill["commission"]) if fill["commission_asset"] == "USDT" else ZERO
            if item["request"]["side"] == "BUY":
                bought += quote + fee
            else:
                sold += quote - fee
    return bought * Decimal("1.005"), sold * (Decimal("0.995") if sold >= 0 else Decimal("1.005"))


def sync(store, state, client, config, now):
    cycle = state["active"]
    cycle["holding_quantity_fully_reconciled"] = False
    for item in cycle["orders"].values():
        refresh_order(item, client)
    btc, delta, other = balances(cycle)
    cycle.update(holding_btc=text(btc), quote_delta=text(delta), external_fee_assets=other)
    store.save(state, "fills_reconciled")  # Retain actual attributed holdings even if balance verification fails next.
    current = model.account(client.request("GET", "account"), state["protocol"]["nonce"])
    if current["binding"] != state["account_binding"]:
        fail("TESTNET_ACCOUNT_CHANGED")
    if (total(current, "BTC") != total(cycle["baseline"], "BTC") + btc
            or total(current, "USDT") != total(cycle["baseline"], "USDT") + delta):
        fail("TESTNET_BALANCE_OR_RESET_MISMATCH")
    if other:
        fail("TESTNET_UNALLOCATED_COMMISSION_ASSET")
    if btc < 0:
        fail("TESTNET_BASELINE_ASSET_CONSUMED")
    cycle["holding_quantity_fully_reconciled"] = True
    protecting = sum((Decimal(item["observed"]["quantity"]) - Decimal(item["observed"]["executed_btc"])
        for item in cycle["orders"].values() if item["observed"] and item["observed"]["type"] == "STOP_LOSS"
        and item["observed"]["status"] in {"NEW", "PARTIALLY_FILLED"}), ZERO)
    cycle["protection_acknowledged"] = bool(btc > 0 and protecting == btc
        and Decimal(current["balances"]["BTC"]["locked"]) >= protecting)
    if cycle["protection_acknowledged"]:
        cycle["protection_ever_acknowledged"] = True
    orders_terminal = all(item["observed"] and item["observed"]["status"] in TERMINAL for item in cycle["orders"].values())
    if btc == 0 and orders_terminal:
        if "entry" not in cycle["orders"]:
            state["active"] = None
            store.save(state, "unsubmitted_plan_released")
        else:
            settle(store, state, config, now)
    else:
        mark(store, state, client, config, now)


def mark(store, state, client, config, now):
    cycle = state["active"]
    observed_quote = model.quote(client.request("GET", "quote", {"symbol": "BTCUSDT"}))
    btc, delta = Decimal(cycle["holding_btc"]), Decimal(cycle["quote_delta"])
    buy_cost, received = cash_flows(cycle)
    cycle["entry_cost_usd"] = text(buy_cost)
    risk = advance(state["risk"], config, now, state["history"])
    fee_rate = Decimal(cycle["rates"]["sell"])
    rules = cycle["rules"]
    sell_quantity = floor_step(btc, Decimal(rules["step"]))
    sell_price = floor_step(Decimal(observed_quote["bid"]) * Decimal("0.998"), Decimal(rules["tick"]))
    sell_quote = sell_quantity * sell_price
    fee = ceil_step(sell_quote * fee_rate, Decimal(rules["quote_quantum"]))
    value = max(ZERO, sell_quote - fee) * Decimal("0.995") if (
        sell_quantity >= Decimal(rules["minimum_qty"]) and sell_quote >= Decimal(rules["minimum_quote"])) else ZERO
    risk.update(cash_usd=text(config.starting_equity_usd + Decimal(risk["realized_net_pnl_usd"]) - buy_cost + received),
        unrealized_net_pnl_usd=text(received + value - buy_cost),
        position_count=int(btc > 0), pending_entry_count=int(any(i["observed"] and i["observed"]["side"] == "BUY" and i["observed"]["status"] not in TERMINAL for i in cycle["orders"].values())),
        position_value_usd=text(value), position_notional_usd=text(btc * Decimal(observed_quote["bid"]) * Decimal("1.005")),
        open_risk_usd=text(max(ZERO, Decimal(cycle["plan"]["loss_reserved_usd"]) + received + value - buy_cost)),
        last_accounting_at=stamp(now))
    risk["high_water_usd"] = text(max(Decimal(risk["high_water_usd"]), equity(risk)))
    financial_halts(risk, config, state["history"])
    state["risk"] = risk
    state["history"].append(valuation_point(risk))
    store.save(state, "allocation_mark")


def sellable(cycle, quote):
    rules, holding = cycle["rules"], Decimal(cycle["holding_btc"])
    quantity = floor_step(holding, Decimal(rules["step"]))
    if (quantity != holding or quantity < Decimal(rules["minimum_qty"])
            or quantity * Decimal(quote["bid"]) < Decimal(rules["minimum_quote"])
            or quantity * Decimal(cycle["plan"]["stop_trigger"]) < Decimal(rules["minimum_quote"])):
        fail("TESTNET_RESIDUAL_OR_EXIT_MINIMUM")
    return text(quantity)


def protect(store, state, client, config, now):
    cycle = state["active"]
    entry = cycle["orders"]["entry"]["observed"]
    if entry is None or entry["status"] not in TERMINAL:
        fail("TESTNET_ENTRY_NOT_TERMINAL")
    quote = model.quote(client.request("GET", "quote", {"symbol": "BTCUSDT"}))
    quantity = sellable(cycle, quote)
    if Decimal(quote["bid"]) <= Decimal(cycle["plan"]["stop_trigger"]):
        fail("TESTNET_STOP_WOULD_ALREADY_TRIGGER")
    send(store, state, client, "stop", params(cycle, "stop", "SELL", "STOP_LOSS", quantity))
    sync(store, state, client, config, now())
    if state["active"] is not None:
        item = state["active"]["orders"]["stop"]["observed"]
        if item["status"] not in {"NEW", "PARTIALLY_FILLED"} or not state["active"]["protection_acknowledged"]:
            fail("TESTNET_PROTECTION_NOT_ACTIVE")
        state["active"]["stage"] = "protected"
        store.save(state, "protection_acknowledged")


def close_cycle(store, state, client, config, now):
    cycle = state["active"]
    quote = model.quote(client.request("GET", "quote", {"symbol": "BTCUSDT"}))
    quantity = sellable(cycle, quote)
    price = floor_step(Decimal(quote["bid"]) * Decimal("0.998"), Decimal(cycle["rules"]["tick"]))
    if price < Decimal(cycle["plan"]["exit_price_estimate"]):
        fail("TESTNET_CLOSE_PRICE_BELOW_RESERVED_BOUND")
    stop = cycle["orders"]["stop"]
    cycle["stage"] = "stop_cancel_intent"
    cycle["protection_acknowledged"] = False
    store.save(state, "stop_cancel_intent")
    try:
        client.request("DELETE", "order", {"symbol": "BTCUSDT", "origClientOrderId": stop["request"]["newClientOrderId"]})
    except DemoError:
        pass
    sync(store, state, client, config, now())  # A stop can fill while cancellation is in flight.
    if state["active"] is None:
        return
    cycle = state["active"]
    if cycle["orders"]["stop"]["observed"]["status"] not in TERMINAL:
        fail("TESTNET_STOP_CANCEL_UNCONFIRMED")
    quantity = sellable(cycle, quote)  # Use only the remaining cycle-owned BTC; never faucet inventory.
    price = floor_step(Decimal(quote["bid"]) * Decimal("0.998"), Decimal(cycle["rules"]["tick"]))
    send(store, state, client, "exit", params(cycle, "exit", "SELL", "LIMIT", quantity, price=text(price)))
    sync(store, state, client, config, now())
    if state["active"] is not None:
        cycle = state["active"]
        quote = model.quote(client.request("GET", "quote", {"symbol": "BTCUSDT"}))
        quantity = sellable(cycle, quote)
        send(store, state, client, "rearm", params(cycle, "rearm", "SELL", "STOP_LOSS", quantity))
        sync(store, state, client, config, now())
        if state["active"] is not None:
            fail("TESTNET_EXIT_NOT_FILLED_PROTECTION_RECHECK_REQUIRED")


def settle(store, state, config, now):
    cycle = state["active"]
    entry_quote = sum((Decimal(f["quote"]) + (Decimal(f["commission"]) if f["commission_asset"] == "USDT" else ZERO)
        for f in cycle["orders"]["entry"]["fills"]), ZERO)
    spent_usd, received_usd = cash_flows(cycle)
    pnl = received_usd - spent_usd
    risk = state["risk"]
    risk["realized_net_pnl_usd"] = text(Decimal(risk["realized_net_pnl_usd"]) + pnl)
    risk.update(unrealized_net_pnl_usd="0", position_count=0, pending_entry_count=0, position_value_usd="0",
        position_notional_usd="0", open_risk_usd="0", last_accounting_at=stamp(now),
        cash_usd=text(config.starting_equity_usd + Decimal(risk["realized_net_pnl_usd"])))
    risk["losing_streak"] = risk["losing_streak"] + 1 if pnl < 0 else 0 if pnl > 0 else risk["losing_streak"]
    risk = advance(risk, config, now, state["history"])
    risk["high_water_usd"] = text(max(Decimal(risk["high_water_usd"]), equity(risk)))
    financial_halts(risk, config, state["history"])
    state["risk"] = risk
    state["history"].append(valuation_point(risk))
    cycle.update(stage="settled", closed_at_utc=stamp(now), modeled_usd_pnl=text(pnl), holding_btc="0")
    cycle["entry_filled"] = entry_quote > 0
    state["cycles"].append(cycle)
    state["active"] = None
    store.save(state, "cycle_settled")


def reconcile(config, client, *, now=utc_now):
    store = Store(config)
    with store.locked(), localcontext() as context:
        context.prec = PRECISION
        state = store.load()
        store.require_current(state)
        if state["active"] is not None:
            client.request("GET", "time")
            try:
                sync(store, state, client, config, now())
            except DemoError as exc:
                store.review(state, exc.code)
        return summary(state)


def recover(config, client, *, now=utc_now):
    """Explicit bounded reduction only. Never submit a BUY or clear a review/financial halt."""
    if not client.allow_orders:
        fail("TESTNET_VIRTUAL_ORDER_OPT_IN_REQUIRED")
    store = Store(config)
    with store.locked(), localcontext() as context:
        context.prec = PRECISION
        state = store.load()
        store.require_current(state)
        if state["active"] is None:
            return summary(state)
        client.request("GET", "time")
        try:
            sync(store, state, client, config, now())
            if state["active"] is None:
                return summary(state)
            for role in list(state["active"]["orders"]):
                item = state["active"]["orders"][role]
                if item["observed"]["status"] not in TERMINAL:
                    state["active"]["stage"] = role + "_recovery_cancel_intent"
                    if item["observed"]["type"] == "STOP_LOSS":
                        state["active"]["protection_acknowledged"] = False
                    store.save(state, "recovery_cancel_intent")
                    try:
                        client.request("DELETE", "order", {"symbol": "BTCUSDT", "origClientOrderId": item["request"]["newClientOrderId"]})
                    except DemoError:
                        pass
                    sync(store, state, client, config, now())
                    if state["active"] is None:
                        return summary(state)
                    if state["active"]["orders"][role]["observed"]["status"] not in TERMINAL:
                        fail("TESTNET_RECOVERY_CANCEL_UNCONFIRMED")
            cycle = state["active"]
            role = "recovery" if "exit" in cycle["orders"] else "exit"
            if role in cycle["orders"]:
                fail("TESTNET_RECOVERY_ALREADY_ATTEMPTED")
            quote = model.quote(client.request("GET", "quote", {"symbol": "BTCUSDT"}))
            quantity = sellable(cycle, quote)
            price = floor_step(Decimal(quote["bid"]) * Decimal("0.998"), Decimal(cycle["rules"]["tick"]))
            if price < Decimal(cycle["plan"]["exit_price_estimate"]):
                fail("TESTNET_CLOSE_PRICE_BELOW_RESERVED_BOUND")
            send(store, state, client, role, params(cycle, role, "SELL", "LIMIT", quantity, price=text(price)))
            sync(store, state, client, config, now())
            if state["active"] is not None:
                fail("TESTNET_RECOVERY_HOLDING_REMAINS")
        except DemoError as exc:
            store.review(state, exc.code)
        return summary(state)


def summary(state):
    active = state["active"]
    return {"adapter_version": __version__, "host": HOST, "virtual_allocation_only": True,
        "revision": state["revision"], "virtual_initial_equity_usd": state["protocol"]["initial_equity_usd"],
        "virtual_equity_usd": text(equity(state["risk"])), "risk_halts": state["risk"]["halts"],
        "review_reasons": state["review_reasons"], "settled_engineering_cycles": sum(c["entry_filled"] for c in state["cycles"]),
        "terminal_engineering_attempts": len(state["cycles"]),
        "active_cycle": None if active is None else {"id": active["id"], "stage": active["stage"],
            "holding_btc": active["holding_btc"], "protection_acknowledged": active["protection_acknowledged"],
            "holding_quantity_fully_reconciled": active["holding_quantity_fully_reconciled"],
            "cash_reserved_usd": active["plan"]["cash_reserved_usd"],
            "external_fee_assets": active["external_fee_assets"]},
        "last_preflight_blockers": state["last_preflight"]["blockers"] if state["last_preflight"] else None,
        "production_execution_available": False, "actual_production_fees_qualified": False,
        "strategy_qualified": False, "qualifying_strategy_trades": 0, "phase5_passed": False}
