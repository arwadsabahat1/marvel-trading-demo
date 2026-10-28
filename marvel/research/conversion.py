"""Invented Convert cleanup events, isolated from accounts, strategies and transport.

This is an accounting experiment, not a Binance adapter. All amounts, precision,
quotes and balance evidence below are synthetic. A claim here belongs only to
this experiment; it is not an integrated paper/live risk reservation.
"""

from copy import deepcopy
from decimal import Decimal, localcontext
import re

from ..risk.model import PRECISION, canonical, fingerprint, text
from .model import fail
from .store import decode_record

MAX_EVENTS = 100
MAX_BYTES = 262144
POLICY = {"source_min_btc": "0.00000013", "destination_min_usdt": "0.01",
          "assumed_asset_decimals": 8, "reference_max_age_seconds": 10,
          "quote_max_age_seconds": 10, "maximum_validity_seconds": 60,
          "maximum_quote_deviation_usdt": "0.02"}
RECIPE = {"schema_version": 1, "kind": "synthetic_convert_cleanup_v1",
          "initial_btc": "0.00000989", "initial_usdt": "0",
          "policy": POLICY, "policy_id": fingerprint(POLICY)}


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        fail()


def _number(value, zero=False):
    if (type(value) is not str or len(value) > 40
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None):
        fail()
    result = Decimal(value)
    if result > Decimal("1000000000000") or result < 0 or (not zero and result == 0):
        fail()
    return result


def _time(value):
    # Seconds from an invented clock origin, never exchange timestamps.
    if type(value) is not int or not 0 <= value <= 86400:
        fail()
    return value


def _id(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        fail()


def _precision(value):
    return Decimal(value) % Decimal("0.00000001") == 0


def _recipe(initial_btc=None):
    if initial_btc is None:
        return deepcopy(RECIPE)
    amount = _number(initial_btc)
    if amount > Decimal("0.0001") or not _precision(initial_btc):
        fail()
    return {**deepcopy(RECIPE), "schema_version": 2, "kind": "synthetic_convert_cleanup_v2",
            "initial_btc": text(amount)}


def _seed(recipe=RECIPE):
    return {"phase": "idle", "holding_btc": recipe["initial_btc"], "cash_usdt": "0",
            "claim_held": False, "review_required": False, "quote": None,
            "quote_history": {},
            "order_id": None, "terminal_status": None, "consumed_quotes": [],
            "acceptance_intents": 0, "rejections": [], "at": 0}


def _reject(state, reason):
    state["rejections"].append(reason)


def _review(state, reason):
    state["phase"], state["review_required"] = "review", True
    if state["acceptance_intents"] and Decimal(state["holding_btc"]) > 0:
        state["claim_held"] = True
    _reject(state, reason)


def _event_shape(event):
    _keys(event, {"id", "at", "kind", "payload"})
    _id(event["id"])
    _time(event["at"])
    kind, body = event["kind"], event["payload"]
    if type(kind) is not str:
        fail()
    fields = {
        "quote": {"quote_id", "from_btc", "to_usdt", "issued_at", "valid_until", "reference_bid", "reference_at"},
        "accept": {"quote_id"}, "timeout": {"quote_id"},
        "status": {"quote_id", "order_id", "status", "from_btc", "to_usdt"},
        "balances": {"quote_id", "order_id", "holding_btc", "cash_usdt"},
    }
    if kind not in fields:
        fail()
    _keys(body, fields[kind])
    _id(body["quote_id"])
    if kind in {"status", "balances"}:
        _id(body["order_id"])
    if kind == "quote":
        for name in ("from_btc", "to_usdt", "reference_bid"):
            _number(body[name])
        for name in ("issued_at", "valid_until", "reference_at"):
            _time(body[name])
    if kind == "status":
        if type(body["status"]) is not str or body["status"] not in {"PROCESS", "ACCEPT_SUCCESS", "SUCCESS", "FAIL"}:
            fail()
        _number(body["from_btc"])
        _number(body["to_usdt"])
    if kind == "balances":
        _number(body["holding_btc"], zero=True)
        _number(body["cash_usdt"], zero=True)


def _quote_reason(body, at, holding):
    issued, expires, reference = body["issued_at"], body["valid_until"], body["reference_at"]
    if issued > at or reference > at or not issued < expires <= issued + 60:
        return "QUOTE_TIME_INVALID"
    if at >= expires or at - issued > 10:
        return "QUOTE_EXPIRED"
    if at - reference > 10:
        return "REFERENCE_STALE"
    if Decimal(body["from_btc"]) != Decimal(holding):
        return "ENTIRE_HOLDING_REQUIRED"
    if not _precision(body["from_btc"]) or not _precision(body["to_usdt"]):
        return "ASSUMED_PRECISION_FAILED"
    if Decimal(body["from_btc"]) < Decimal(POLICY["source_min_btc"]):
        return "SOURCE_MINIMUM_FAILED"
    if Decimal(body["to_usdt"]) < Decimal(POLICY["destination_min_usdt"]):
        return "DESTINATION_MINIMUM_FAILED"
    difference = Decimal(body["from_btc"]) * Decimal(body["reference_bid"]) - Decimal(body["to_usdt"])
    if difference.copy_abs() > Decimal(POLICY["maximum_quote_deviation_usdt"]):
        return "QUOTE_DEVIATION_EXCEEDED"
    return None


def _reduce(state, event):
    _event_shape(event)
    at, kind, body = event["at"], event["kind"], event["payload"]
    if at < state["at"]:
        fail()
    state["at"] = at
    if state["review_required"]:
        _reject(state, "REVIEW_REQUIRED")
        return
    if kind == "quote":
        if state["claim_held"] or state["phase"] not in {"idle", "quoted"}:
            _reject(state, "CLEANUP_NOT_AVAILABLE")
            return
        if body["quote_id"] in state["consumed_quotes"]:
            _reject(state, "QUOTE_ALREADY_CONSUMED")
            return
        previous = state["quote_history"].get(body["quote_id"])
        if previous is not None and canonical(previous) != canonical(body):
            _review(state, "QUOTE_ID_CONFLICT")
            return
        reason = _quote_reason(body, at, state["holding_btc"])
        if reason:
            _reject(state, reason)
            return
        state.update(phase="quoted", quote=deepcopy(body), order_id=None, terminal_status=None)
        state["quote_history"][body["quote_id"]] = deepcopy(body)
        return
    quote = state["quote"]
    if quote is None or body["quote_id"] != quote["quote_id"]:
        if (state["claim_held"] or state["phase"] == "settled"
                or body["quote_id"] in state["consumed_quotes"]):
            _review(state, "QUOTE_BINDING_MISMATCH")
        else:
            _reject(state, "QUOTE_NOT_AVAILABLE")
        return
    if kind == "accept":
        if state["phase"] != "quoted" or state["claim_held"]:
            _reject(state, "ACCEPTANCE_ALREADY_ATTEMPTED")
            return
        reason = _quote_reason(quote, at, state["holding_btc"])
        if reason:
            _reject(state, reason)
            return
        # Intent is recorded before any imagined dispatch. No dispatch exists.
        state.update(phase="pending", claim_held=True)
        state["consumed_quotes"].append(quote["quote_id"])
        state["acceptance_intents"] += 1
        return
    if not state["claim_held"] and state["phase"] != "settled" and state["terminal_status"] is None:
        _reject(state, "ACCEPTANCE_REQUIRED")
        return
    if kind == "timeout":
        if state["phase"] in {"pending", "unknown"}:
            state["phase"] = "unknown"
        else:
            _reject(state, "TERMINAL_EVIDENCE_RETAINED")
        return
    if state["order_id"] is not None and body["order_id"] != state["order_id"]:
        _review(state, "ORDER_BINDING_MISMATCH")
        return
    if kind == "status":
        if (Decimal(body["from_btc"]) != Decimal(quote["from_btc"])
                or Decimal(body["to_usdt"]) != Decimal(quote["to_usdt"])):
            _review(state, "RECEIPT_AMOUNT_MISMATCH")
            return
        state["order_id"] = body["order_id"]
        status, terminal = body["status"], state["terminal_status"]
        if terminal is not None:
            if status in {"SUCCESS", "FAIL"} and status != terminal:
                _review(state, "CONFLICTING_TERMINAL_STATUS")
            elif status not in {"SUCCESS", "FAIL"}:
                _reject(state, "OLDER_STATUS_IGNORED")
            return
        if status in {"SUCCESS", "FAIL"}:
            state["terminal_status"] = status
            state["phase"] = "awaiting_success_balance" if status == "SUCCESS" else "awaiting_failure_balance"
        else:
            state["phase"] = "pending"
        return
    # A balance observation alone cannot prove success, absence of fills, or flatness.
    if state["terminal_status"] is None:
        _reject(state, "TERMINAL_RECEIPT_REQUIRED")
        return
    if state["phase"] == "settled":
        if Decimal(body["holding_btc"]) != 0 or Decimal(body["cash_usdt"]) != Decimal(state["cash_usdt"]):
            _review(state, "BALANCE_MISMATCH")
        return
    success = state["terminal_status"] == "SUCCESS"
    btc = Decimal("0") if success else Decimal(state["holding_btc"])
    cash = Decimal(state["cash_usdt"]) + (Decimal(quote["to_usdt"]) if success else Decimal("0"))
    if Decimal(body["holding_btc"]) != btc or Decimal(body["cash_usdt"]) != cash:
        _review(state, "BALANCE_MISMATCH")
        return
    state.update(holding_btc=text(btc), cash_usdt=text(cash), claim_held=False,
                 phase="settled" if success else "idle")


def _seal(events, state, recipe=RECIPE):
    record = {"recipe": deepcopy(recipe), "events": events, "state": state}
    return {**record, "record_id": fingerprint(record)}


def new(*, initial_btc=None):
    """Optional bounded synthetic holding; the original v1 journal stays unchanged."""
    recipe = _recipe(initial_btc)
    return _seal([], _seed(recipe), recipe)


def audit(journal):
    """Replay bounded evidence; hashes detect damage, not authenticate a venue."""
    try:
        _keys(journal, {"recipe", "events", "state", "record_id"})
        recipe = journal["recipe"]
        expected = RECIPE if recipe == RECIPE else _recipe(recipe["initial_btc"])
        if (len(canonical(journal).encode("ascii")) > MAX_BYTES
                or canonical(recipe) != canonical(expected)
                or type(journal["events"]) is not list or len(journal["events"]) > MAX_EVENTS):
            fail("RESEARCH_RECORD")
        state, seen = _seed(recipe), set()
        with localcontext() as context:
            context.prec = PRECISION
            for event in journal["events"]:
                _event_shape(event)
                if event["id"] in seen:
                    fail("RESEARCH_RECORD")
                seen.add(event["id"])
                _reduce(state, event)
        if canonical(_seal(journal["events"], state, recipe)) != canonical(journal):
            fail("RESEARCH_RECORD")
    except (KeyError, TypeError, ValueError, RecursionError):
        fail("RESEARCH_RECORD")
    return deepcopy(state)


def apply(journal, event):
    """Return a new journal; malformed input or conflicts never mutate the old one."""
    state = audit(journal)
    _event_shape(event)
    for previous in journal["events"]:
        if previous["id"] == event["id"]:
            if canonical(previous) != canonical(event):
                fail()
            return deepcopy(journal)
    if len(journal["events"]) >= MAX_EVENTS:
        fail("RESEARCH_RECORD")
    with localcontext() as context:
        context.prec = PRECISION
        _reduce(state, event)
    result = _seal([*deepcopy(journal["events"]), deepcopy(event)], state, journal["recipe"])
    if len(canonical(result).encode("ascii")) > MAX_BYTES:
        fail("RESEARCH_RECORD")
    return result


def restore(raw):
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        fail("RESEARCH_RECORD")
    journal = decode_record(raw)
    audit(journal)
    return journal


def fixture_event(kind, at, *, label=None, **changes):
    """Invented receipt fields; never a representation of retrieved owner data."""
    qid, oid = fingerprint("invented quote"), fingerprint("invented order")
    payload = {
        "quote": {"quote_id": qid, "from_btc": "0.00000989", "to_usdt": "0.78",
                  "issued_at": 0, "valid_until": 10, "reference_bid": "80000", "reference_at": 0},
        "accept": {"quote_id": qid}, "timeout": {"quote_id": qid},
        "status": {"quote_id": qid, "order_id": oid, "status": "SUCCESS", "from_btc": "0.00000989", "to_usdt": "0.78"},
        "balances": {"quote_id": qid, "order_id": oid, "holding_btc": "0", "cash_usdt": "0.78"},
    }[kind]
    payload.update(changes)
    return {"id": fingerprint({"kind": kind, "at": at, "label": label, "payload": payload}),
            "kind": kind, "at": at, "payload": payload}


def demo():
    q, accept = fixture_event("quote", 0), fixture_event("accept", 1)
    status, balances = fixture_event("status", 3), fixture_event("balances", 4)
    cases = {
        "success_after_receipt_and_balances": [q, accept, status, balances, balances],
        "success_receipt_alone": [q, accept, status],
        "expired_before_acceptance": [q, fixture_event("accept", 10)],
        "excessive_quote_cost": [fixture_event("quote", 0, to_usdt="0.70")],
        "stale_reference": [fixture_event("quote", 11, issued_at=11, valid_until=21)],
        "destination_below_minimum": [fixture_event("quote", 0, to_usdt="0.009")],
        "partial_holding_quote": [fixture_event("quote", 0, from_btc="0.000009")],
        "assumed_precision_mismatch": [fixture_event("quote", 0, to_usdt="0.780000001")],
        "processing_not_settled": [q, accept, fixture_event("status", 2, status="PROCESS")],
        "accepted_not_settled": [q, accept, fixture_event("status", 2, status="ACCEPT_SUCCESS")],
        "balance_without_final_receipt": [q, accept, fixture_event("balances", 2)],
        "timeout_keeps_claim_and_blocks_retry": [q, accept, fixture_event("timeout", 2),
                                                 fixture_event("accept", 3), fixture_event("quote", 4)],
        "late_success_after_timeout": [q, accept, fixture_event("timeout", 2), fixture_event("status", 50), fixture_event("balances", 51)],
        "wrong_receipt_amount": [q, accept, fixture_event("status", 3, to_usdt="0.77")],
        "wrong_order_binding": [q, accept, fixture_event("status", 2, status="PROCESS"),
                                fixture_event("status", 3, order_id=fingerprint("other order"))],
        "residual_balance_keeps_claim": [q, accept, status, fixture_event("balances", 4, holding_btc="0.00000001")],
        "wrong_cash_keeps_claim": [q, accept, status, fixture_event("balances", 4, cash_usdt="0.77")],
        "failure_receipt_alone": [q, accept, fixture_event("status", 3, status="FAIL")],
        "failure_and_unchanged_balances": [q, accept, fixture_event("status", 3, status="FAIL"),
                                          fixture_event("balances", 4, holding_btc="0.00000989", cash_usdt="0")],
    }
    results = {}
    for name, events in cases.items():
        journal = new()
        for event in events:
            journal = apply(journal, event)
        restored = restore(canonical(journal).encode("ascii"))
        results[name] = {"state": audit(restored), "record_id": restored["record_id"],
                         "events": restored["events"]}
    return {"kind": "synthetic_convert_cleanup_demo", "recipe": deepcopy(RECIPE), "cases": results,
            "real_orders_or_conversion_requests": 0, "network_used": False, "owner_account_used": False,
            "strategy_integration_available": False, "conversion_execution_available": False,
            "actual_account_fees_qualified": False, "asset_precision_qualified": False,
            "real_balance_reconciliation_qualified": False, "partial_fill_liquidation_qualified": False,
            "profit_demonstrated": False, "phase5_historical_gate_passed": False,
            "limitations": ["All quotes, statuses, clocks and balance evidence are invented.",
                            "Recovered USDT is proceeds of an existing holding, not profit or USD equity.",
                            "The 0.02 USDT quote-deviation bound is isolated test policy, not a trading risk limit.",
                            "No network, authenticated adapter, protective exit or integrated risk-claim release exists.",
                            "Journal hashes detect damaged local evidence; they do not authenticate exchange truth."]}
