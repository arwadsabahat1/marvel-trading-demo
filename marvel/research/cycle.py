"""Replayable engineering cycle in memory, never an owner account or strategy.

Reuses Spot fills/accounting and the original risk reservation. Convert receipts
are invented inputs, not authenticated evidence. No transport or storage exists.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext

from ..clock import stamp
from ..paper.ledger import (DONE, accounting, cancel_entry, fill, fresh_market,
                            make_exit, new_order, venue_status)
from ..risk.engine import financial_halts, latch, seed, valuation_point
from ..risk.ledger import reduce as risk_reduce
from ..risk.model import PRECISION, canonical, fingerprint, floor_step, text, timestamp
from . import conversion
from .exit_costs import conversion_floor
from .model import fail
from .store import decode_record

ZERO = Decimal(0)
ORIGIN = "2026-03-01T23:59:50+00:00"
MAX_EVENTS = 60
MAX_BYTES = 262144
KINDS = {"buy_fill", "entry_cancel", "exit_open", "sell_fill", "exit_cancel",
         "mark", "depeg_mark", "wait", "cleanup"}


def _recipe(config, profile, entry_fraction, exit_fraction):
    if entry_fraction not in ("1", "0.5") or exit_fraction not in ("1", "0.5", "0"):
        fail()
    return {"kind": "synthetic_risk_bound_cycle_v1", "schema_version": 1,
            "config_fingerprint": config.fingerprint, "profile_hash": profile.fingerprint,
            "origin": ORIGIN, "entry_fraction": entry_fraction, "exit_fraction": exit_fraction,
            "ask_usdt": "80000", "bid_usdt": "79999.99", "stop_usdt": "79199.99",
            "fx_lower": "0.995", "fx_upper": "1.005", "depeg_fx_lower": "0.8",
            "conversion_policy": deepcopy(conversion.POLICY),
            "valuation": "Spot-only recovery until matching Convert settlement and fresh synthetic FX",
            "entry_guard": "Isolated failure exercise only; historical guard remains unchanged"}


def _when(at):
    return stamp(timestamp(ORIGIN) + timedelta(seconds=at))


def _market(recipe, at, *, entry=False, depeg=False):
    bid = recipe["bid_usdt"] if entry else recipe["stop_usdt"]
    return {"kind": "synthetic", "bid": bid,
            "ask": recipe["ask_usdt"] if entry else text(Decimal(bid) + Decimal("0.01")),
            "quote_at": _when(at), "fx_at": _when(at),
            "fx_lower": recipe["depeg_fx_lower"] if depeg else recipe["fx_lower"],
            "fx_upper": recipe["fx_upper"]}


def _start(recipe, config, profile):
    risk = seed(config, profile, config.starting_equity_usd, config.starting_equity_usd, ORIGIN)
    identity = fingerprint({"synthetic_cycle": recipe})
    market = _market(recipe, 0, entry=True)
    request = {"proposal": {"id": identity, "symbol": "BTCUSDT", "side": "BUY",
                            "stop_price": recipe["stop_usdt"], "quantity": None}, "market": market}
    risk, decision = risk_reduce(risk, "reserve", request, ORIGIN, config, profile)
    if not decision["risk_check_passed"]:
        fail()  # Never loosen the configured caps to make the exercise run.
    risk, _ = risk_reduce(risk, "submitted", {"id": identity, "market": market}, ORIGIN, config, profile)
    plan = risk["claims"][0]["plan"]
    cycle = {"id": identity, "stop_trigger": plan["stop_trigger"], "entry_cost_usd": "0",
             "exit_proceeds_usd": "0", "holding_btc": "0", "bought_btc": "0", "settled": False}
    book = {"cash_usd": text(config.starting_equity_usd), "fees_usd": "0", "market": market,
            "orders": [new_order(identity, identity, "BUY", Decimal(plan["quantity"]))],
            "cycles": [cycle], "closed_trades": []}
    accounting(risk, book, identity, 0, timestamp(ORIGIN), config, profile)
    return {"risk": risk, "book": book, "history": [valuation_point(risk)], "at": 0,
            "conversion": None, "conversion_credited": False, "pending_conversion_usdt": "0",
            "operations": [], "rejections": [], "review_required": False,
            "minimum_quote_usdt": None}


def event(kind, at, payload=None, *, label=None):
    body = {"kind": kind, "at": at, "payload": {} if payload is None else deepcopy(payload)}
    return {"id": fingerprint({**body, "label": label}), **body}


def _shape(item):
    conversion._keys(item, {"id", "kind", "at", "payload"})
    conversion._id(item["id"])
    conversion._time(item["at"])
    if type(item["kind"]) is not str or item["kind"] not in KINDS:
        fail()
    if item["kind"] == "cleanup":
        conversion._event_shape(item["payload"])
        if item["payload"]["at"] != item["at"]:
            fail()
    else:
        conversion._keys(item["payload"], set())


def _fresh_fx(state, at, config):
    age = (timestamp(_when(at)) - timestamp(state["book"]["market"]["fx_at"])).total_seconds()
    return 0 <= age <= config.max_quote_age_seconds


def _floor(state):
    cycle = state["book"]["cycles"][0]
    return conversion_floor(Decimal(cycle["entry_cost_usd"]), Decimal(cycle["exit_proceeds_usd"]),
        Decimal(state["risk"]["claims"][0]["plan"]["loss_reserved_usd"]),
        Decimal(state["book"]["market"]["fx_lower"]), Decimal("0.00000001"),
        Decimal(conversion.POLICY["destination_min_usdt"]))[0]


def _cleanup(state, item, config, profile):
    book, risk = state["book"], state["risk"]
    cycle = book["cycles"][0]
    if any(order["status"] not in DONE for order in book["orders"]):
        state["rejections"].append("SPOT_ORDERS_NOT_TERMINAL")
        return
    if state["conversion"] is None:
        if Decimal(cycle["holding_btc"]) <= 0:
            state["rejections"].append("NO_CLEANUP_HOLDING")
            return
        state["conversion"] = conversion.new(initial_btc=cycle["holding_btc"])
    inner = item["payload"]
    if inner["kind"] in {"quote", "accept"}:
        if not _fresh_fx(state, item["at"], config):
            state["rejections"].append("FX_STALE")
            return
        quote = inner["payload"] if inner["kind"] == "quote" else state["conversion"]["state"]["quote"]
        if quote is not None:
            if inner["kind"] == "quote" and (
                    Decimal(quote["reference_bid"]) != Decimal(book["market"]["bid"])
                    or _when(quote["reference_at"]) != book["market"]["quote_at"]):
                state["rejections"].append("REFERENCE_NOT_BOUND_TO_MARK")
                return
            state["minimum_quote_usdt"] = text(_floor(state))
            if Decimal(quote["to_usdt"]) < _floor(state):
                state["rejections"].append("ORIGINAL_RESERVATION_FLOOR_FAILED")
                return
    state["conversion"] = conversion.apply(state["conversion"], inner)
    converted = state["conversion"]["state"]
    if converted["review_required"]:
        state["review_required"] = True
        latch(risk, "CONVERSION_REVIEW")
    if converted["acceptance_intents"] and risk["claims"][0]["status"] != "closed":
        state["risk"], _ = risk_reduce(risk, "unknown", {"id": cycle["id"]}, _when(item["at"]),
                                      config, profile, history=state["history"])


def _credit(state, at, config):
    """Return true only once; fresh FX prices a net settled USDT balance."""
    if state["conversion"] is None or state["conversion_credited"] or state["review_required"]:
        return False
    converted = state["conversion"]["state"]
    if converted["phase"] != "settled":
        return False
    state["pending_conversion_usdt"] = converted["cash_usdt"]
    if not _fresh_fx(state, at, config):
        return False  # Preserve the unreconciled risk claim; never credit stale USD.
    book = state["book"]
    if any(order["status"] not in DONE for order in book["orders"]):
        fail()
    net = Decimal(converted["cash_usdt"]) * Decimal(book["market"]["fx_lower"])
    cycle = book["cycles"][0]
    cycle["holding_btc"] = "0"
    cycle["exit_proceeds_usd"] = text(Decimal(cycle["exit_proceeds_usd"]) + net)
    book["cash_usd"] = text(Decimal(book["cash_usd"]) + net)
    state["conversion_credited"], state["pending_conversion_usdt"] = True, "0"
    if Decimal(cycle["entry_cost_usd"]) - Decimal(cycle["exit_proceeds_usd"]) > Decimal(state["risk"]["claims"][0]["plan"]["loss_reserved_usd"]):
        latch(state["risk"], "REALIZED_RESERVATION_BREACH")
    return True


def _step(state, item, recipe, config, profile):
    _shape(item)
    at, kind = item["at"], item["kind"]
    if at < state["at"]:
        fail()
    state["at"] = at
    state["risk"], _ = risk_reduce(state["risk"], "sync", {}, _when(at), config, profile,
                                  history=state["history"])
    book, cycle = state["book"], state["book"]["cycles"][0]
    changed = False
    if kind in {"buy_fill", "entry_cancel", "exit_open", "sell_fill", "exit_cancel"}:
        if state["conversion"] is not None or kind in state["operations"] or cycle["settled"]:
            fail()  # Same event ID is handled idempotently outside this reducer.
        if kind == "buy_fill":
            if state["operations"]:
                fail()
            book["market"] = _market(recipe, at, entry=True)
            order = book["orders"][0]
            quantity = floor_step(Decimal(order["quantity"]) * Decimal(recipe["entry_fraction"]), profile.n("entry_step"))
            fill(book, order, text(quantity), book["market"], profile, item["id"], _when(at))
        elif kind == "entry_cancel":
            cancel_entry(book, cycle)
        elif kind == "exit_open":
            if book["orders"][0]["status"] not in DONE or not Decimal(cycle["holding_btc"]):
                fail()
            book["market"] = _market(recipe, at)
            if make_exit(book, cycle, profile, book["market"], item["id"]) is None:
                state["rejections"].append("SPOT_EXIT_BELOW_MINIMUM")
        else:
            exits = [order for order in book["orders"] if order["side"] == "SELL"]
            if len(exits) != 1:
                fail()
            order = exits[0]
            if kind == "sell_fill":
                quantity = floor_step(Decimal(order["quantity"]) * Decimal(recipe["exit_fraction"]), profile.n("exit_step"))
                if quantity <= 0:
                    fail()
                book["market"] = _market(recipe, at)
                fill(book, order, text(quantity), book["market"], profile, item["id"], _when(at))
            elif order["status"] not in DONE:
                venue_status(order, "cancelled")  # Invented terminal cancellation evidence.
        state["operations"].append(kind)
        changed = True
    elif kind in {"mark", "depeg_mark"}:
        book["market"] = _market(recipe, at, depeg=kind == "depeg_mark")
        changed = True
    elif kind == "cleanup":
        _cleanup(state, item, config, profile)
    if _credit(state, at, config):
        changed = True
    if changed:
        fresh_market(book["market"], timestamp(_when(at)), config, profile)
        accounting(state["risk"], book, item["id"], 1, timestamp(_when(at)), config, profile)
    financial_halts(state["risk"], config, state["history"])
    state["history"].append(valuation_point(state["risk"]))


def _seal(recipe, events, state):
    body = {"recipe": deepcopy(recipe), "events": deepcopy(events), "state": deepcopy(state)}
    return {**body, "record_id": fingerprint(body)}


def new(config, profile, *, entry_fraction="1", exit_fraction="1"):
    with localcontext() as context:
        context.prec = PRECISION
        recipe = _recipe(config, profile, entry_fraction, exit_fraction)
        return _seal(recipe, [], _start(recipe, config, profile))


def audit(journal, config, profile):
    try:
        conversion._keys(journal, {"recipe", "events", "state", "record_id"})
        recipe, events = journal["recipe"], journal["events"]
        expected = _recipe(config, profile, recipe["entry_fraction"], recipe["exit_fraction"])
        if (canonical(recipe) != canonical(expected) or type(events) is not list
                or len(events) > MAX_EVENTS or len(canonical(journal).encode("ascii")) > MAX_BYTES):
            fail("RESEARCH_RECORD")
        with localcontext() as context:
            context.prec = PRECISION
            state, seen = _start(recipe, config, profile), set()
            for item in events:
                _shape(item)
                if item["id"] in seen:
                    fail("RESEARCH_RECORD")
                seen.add(item["id"])
                _step(state, item, recipe, config, profile)
            if canonical(_seal(recipe, events, state)) != canonical(journal):
                fail("RESEARCH_RECORD")
        return deepcopy(state)
    except (KeyError, TypeError, ValueError, RecursionError):
        fail("RESEARCH_RECORD")


def apply(journal, item, config, profile):
    state = audit(journal, config, profile)
    _shape(item)
    for previous in journal["events"]:
        if previous["id"] == item["id"]:
            if canonical(previous) != canonical(item):
                fail()
            return deepcopy(journal)
    if len(journal["events"]) >= MAX_EVENTS:
        fail("RESEARCH_RECORD")
    with localcontext() as context:
        context.prec = PRECISION
        _step(state, item, journal["recipe"], config, profile)
        result = _seal(journal["recipe"], [*journal["events"], item], state)
    if len(canonical(result).encode("ascii")) > MAX_BYTES:
        fail("RESEARCH_RECORD")
    return result


def restore(raw, config, profile):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        fail("RESEARCH_RECORD")
    journal = decode_record(raw)
    audit(journal, config, profile)
    return journal


def cleanup_event(journal, kind, at, *, label=None, **changes):
    """Build explicitly invented evidence for the exercise's entire remaining BTC."""
    with localcontext() as context:
        context.prec = PRECISION
        return _cleanup_event(journal, kind, at, label=label, **changes)


def _cleanup_event(journal, kind, at, *, label=None, **changes):
    state = journal["state"]
    holding = (state["conversion"]["recipe"]["initial_btc"] if state["conversion"]
               else state["book"]["cycles"][0]["holding_btc"])
    quoted = text(floor_step(Decimal(holding) * Decimal(journal["recipe"]["stop_usdt"]), Decimal("0.00000001")))
    qid, oid = fingerprint({"quote": journal["recipe"]}), fingerprint({"order": journal["recipe"]})
    reference_at = int((timestamp(state["book"]["market"]["quote_at"]) - timestamp(ORIGIN)).total_seconds())
    payload = {
        "quote": {"quote_id": qid, "from_btc": holding, "to_usdt": quoted, "issued_at": at,
                  "valid_until": at + 10, "reference_bid": state["book"]["market"]["bid"], "reference_at": reference_at},
        "accept": {"quote_id": qid}, "timeout": {"quote_id": qid},
        "status": {"quote_id": qid, "order_id": oid, "status": "SUCCESS", "from_btc": holding, "to_usdt": quoted},
        "balances": {"quote_id": qid, "order_id": oid, "holding_btc": "0", "cash_usdt": quoted},
    }[kind]
    payload.update(changes)
    return event("cleanup", at, event(kind, at, payload, label=label), label=label)


def demo(config, profile):
    cases = {}
    for entry_fraction, exit_fraction in (("1", "1"), ("1", "0.5"), ("1", "0"), ("0.5", "0")):
        base = new(config, profile, entry_fraction=entry_fraction, exit_fraction=exit_fraction)
        prefix = [event("buy_fill", 0), event("entry_cancel", 1), event("exit_open", 2)]
        if entry_fraction == "1":
            if exit_fraction != "0":
                prefix.append(event("sell_fill", 3))
            prefix.append(event("exit_cancel", 4))
        for item in prefix:
            base = apply(base, item, config, profile)
        for outcome in ("settled", "unavailable", "unknown", "failed", "receipt_only", "wrong_balance", "late_settlement", "depeg"):
            journal = deepcopy(base)
            operations = [] if outcome == "unavailable" else [cleanup_event(base, "quote", 5), cleanup_event(base, "accept", 6)]
            if outcome == "unknown":
                operations += [cleanup_event(base, "timeout", 7), cleanup_event(base, "accept", 8), event("wait", 20)]
            elif outcome in {"settled", "failed", "receipt_only", "wrong_balance", "late_settlement", "depeg"}:
                late = outcome in {"late_settlement", "depeg"}
                status_at = 20 if late else 7
                operations.append(cleanup_event(base, "status", status_at, status="FAIL" if outcome == "failed" else "SUCCESS"))
                if outcome != "receipt_only":
                    overrides = {"holding_btc": base["state"]["book"]["cycles"][0]["holding_btc"], "cash_usdt": "0"} if outcome == "failed" else {}
                    if outcome == "wrong_balance":
                        overrides = {"cash_usdt": "0"}
                    operations.append(cleanup_event(base, "balances", status_at + 1, **overrides))
                if late:
                    operations.append(event("depeg_mark" if outcome == "depeg" else "mark", 22))
            for item in operations:
                journal = apply(journal, item, config, profile)
                journal = restore(canonical(journal).encode("ascii"), config, profile)
            cases[f"entry_{entry_fraction}_exit_{exit_fraction}_{outcome}"] = journal
    return {"kind": "synthetic_risk_bound_cycle_demo", "cases": cases, "case_count": len(cases),
            "network_used": False, "owner_account_used": False, "real_orders_or_conversions": 0,
            "out_of_sample_strategy_trades": 0, "historical_guard_changed": False,
            "actual_complete_exit_qualified": False, "actual_fill_costs_qualified": False,
            "usd_fx_accounting_qualified": False, "phase5_historical_gate_passed": False,
            "entries_allowed": False, "live_execution_available": False,
            "limitations": ["All fills, cancellations, quotes, settlement and FX are invented.",
                "The exercise deliberately models unqualified entries; no historical strategy invokes it.",
                "A pending USDT settlement retains stale unreconciled ledger exposure until fresh synthetic FX.",
                "Conversion proceeds are net of invented costs; their spread is not an observed fee.",
                "Risk limits do not guarantee recovery: unavailable exits and FX changes can breach the reservation.",
                "Replay hashes detect local damage; they do not authenticate a venue or prove profitability."]}


def summary(document):
    """Small CLI view; complete replay journals remain available from demo()."""
    cases = {}
    for name, journal in document["cases"].items():
        state = journal["state"]
        trade = state["book"]["cycles"][0]
        cases[name] = {"record_id": journal["record_id"],
            "original_claim_status": state["risk"]["claims"][0]["status"],
            "unreconciled_btc": trade["holding_btc"], "pending_conversion_usdt": state["pending_conversion_usdt"],
            "cash_usd": state["book"]["cash_usd"], "realized_net_pnl_usd": state["risk"]["realized_net_pnl_usd"],
            "synthetic_closed_trades": len(state["risk"]["closed_trades"]),
            "halts": state["risk"]["halts"], "review_required": state["review_required"]}
    return {**{key: deepcopy(value) for key, value in document.items() if key != "cases"}, "cases": cases}
