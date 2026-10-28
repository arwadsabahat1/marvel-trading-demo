"""Replayable risk operations; accounting input belongs to a trusted future adapter."""

from copy import deepcopy
from decimal import Decimal, localcontext

from ..clock import stamp
from .engine import (active_claims, active_halts, advance, equity, evaluate, financial_halts,
                     latch, seed, valuation_point)
from .model import (ACTIVE, PRECISION, canonical, fingerprint, identifier,
                    history_limit, integer, keys, number, reject, request, signed, text, timestamp)


def claim_by_id(state, value):
    identifier(value)
    matches = [item for item in state["claims"] if item["id"] == value]
    if len(matches) != 1:
        reject("RISK_TRANSITION")
    return matches[0]


def reconcile(state, data, now, config):
    keys(data, {"id", "cash_usd", "position_value_usd", "position_notional_usd", "open_risk_usd",
                "position_count", "pending_entry_count", "position_reservation_id", "unrealized_net_pnl_usd",
                "total_fees_usd", "closed_trades", "confirmed_flat_ids", "no_fill_ids"})
    identifier(data["id"])
    signature = fingerprint(data)
    if data["id"] in state["reports"]:
        if state["reports"][data["id"]] != signature:
            reject("RISK_ID_CONFLICT")
        return {"accounting_report_reused": True}
    values = {key: number(data[key], zero=True) for key in ("cash_usd", "position_value_usd", "position_notional_usd", "open_risk_usd", "total_fees_usd")}
    unrealized = signed(data["unrealized_net_pnl_usd"])
    positions, pending = integer(data["position_count"]), integer(data["pending_entry_count"])
    if data["position_reservation_id"] is not None:
        identifier(data["position_reservation_id"])
    if values["position_value_usd"] > values["position_notional_usd"] or values["total_fees_usd"] < Decimal(state["total_fees_usd"]):
        reject()
    if positions == 0 and (unrealized or values["position_value_usd"] or values["position_notional_usd"] or values["open_risk_usd"] or data["position_reservation_id"] is not None):
        reject()
    closed, flat, no_fill = data["closed_trades"], data["confirmed_flat_ids"], data["no_fill_ids"]
    if any(not isinstance(items, list) or len(items) > 100 for items in (closed, flat, no_fill)):
        reject()
    flat_ids = {identifier(value) for value in flat}
    no_fill_ids = {identifier(value) for value in no_fill}
    if len(flat_ids) != len(flat) or len(no_fill_ids) != len(no_fill) or not no_fill_ids <= flat_ids:
        reject()
    if flat and (positions or pending):
        reject("RISK_TRANSITION")
    seen = {trade["id"] for trade in state["closed_trades"]}
    used_claims = set()
    realized = Decimal(state["realized_net_pnl_usd"])
    for trade in closed:
        keys(trade, {"id", "reservation_id", "net_pnl_usd"})
        identifier(trade["id"])
        identifier(trade["reservation_id"])
        if trade["id"] in seen or trade["reservation_id"] in used_claims or trade["reservation_id"] not in flat:
            reject("RISK_ID_CONFLICT")
        claim = claim_by_id(state, trade["reservation_id"])
        if claim["status"] not in {"submitted", "unknown"}:
            reject("RISK_TRANSITION")
        pnl = signed(trade["net_pnl_usd"])
        realized += pnl
        state["losing_streak"] = state["losing_streak"] + 1 if pnl < 0 else (0 if pnl > 0 else state["losing_streak"])
        if state["losing_streak"] >= config.risk.losing_streak_limit:
            latch(state, "LOSING_STREAK")
        seen.add(trade["id"])
        used_claims.add(trade["reservation_id"])
    if used_claims & no_fill_ids or used_claims | no_fill_ids != flat_ids:
        reject("RISK_TRANSITION")
    if no_fill_ids and not closed and values["total_fees_usd"] != Decimal(state["total_fees_usd"]):
        reject()
    resulting_equity = Decimal(state["origin_equity"]) + realized + unrealized
    if resulting_equity < 0 or resulting_equity > Decimal("1000000000000") or values["cash_usd"] + values["position_value_usd"] != resulting_equity:
        reject()  # No deposit, withdrawal, leverage, or arbitrary equity-reset operation.
    for value in flat:
        claim = claim_by_id(state, value)
        if claim["status"] not in {"submitted", "unknown"}:
            reject("RISK_TRANSITION")
        if value in no_fill_ids and claim["has_position"]:
            reject("RISK_TRANSITION")
        if value in used_claims:
            claim["has_position"] = True
        claim["status"] = "closed"
    active = {c["id"]: c for c in active_claims(state)}
    position_id = data["position_reservation_id"]
    if (positions and (position_id not in active or active[position_id]["status"] == "reserved")) or (pending and not any(c["status"] in {"submitted", "unknown"} for c in active.values())):
        latch(state, "UNRESERVED_EXPOSURE")
    if positions and position_id in active:
        active[position_id]["has_position"] = True  # Sticky across later flat marks/restarts.
    state.update({key: text(value) for key, value in values.items()})
    state.update({"position_count": positions, "pending_entry_count": pending, "position_reservation_id": position_id,
                  "unrealized_net_pnl_usd": text(unrealized), "realized_net_pnl_usd": text(realized),
                  "high_water_usd": text(max(Decimal(state["high_water_usd"]), resulting_equity)),
                  "last_accounting_at": stamp(now)})
    state["closed_trades"].extend(deepcopy(closed))
    state["reports"][data["id"]] = signature
    return {"accounting_report_reused": False}


def reduce(state, kind, data, when, config, profile, manual_halts=(), history=None, *, context_kind="synthetic"):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        now = timestamp(when)
        state = advance(state, config, now, history)
        if "paper_state" in state and kind not in {"paper", "sync"}:
            reject("RISK_TRANSITION")  # Execution owns its fills, claims and accounting.
        if kind == "reserve" and len(state["claims"]) + len(state["reports"]) >= history_limit(context_kind):
            reject("RISK_HISTORY_FULL")
        result = {}
        if kind == "paper":
            from ..paper.ledger import reduce as paper_reduce
            state, result = paper_reduce(state, data, when, config, profile, manual_halts, history, context_kind=context_kind)
        elif kind == "sync":
            keys(data, set())
        elif kind == "reserve":
            root = request(data, context_kind=context_kind)
            existing = [c for c in state["claims"] if c["id"] == root["proposal"]["id"]]
            if existing:
                claim = existing[0]
                if claim["signature"] != fingerprint(root) or claim["status"] not in ACTIVE:
                    reject("RISK_ID_CONFLICT")
                # Reuse acknowledges an existing claim, never a fresh authorization to submit it.
                result = {"reservation_reused": True, "reservation_id": claim["id"], "reservation_status": claim["status"],
                          "risk_check_passed": False, "reasons": ["EXISTING_RESERVATION_REQUIRES_FRESH_SUBMISSION_CHECK"], "plan": claim["plan"]}
            else:
                result = evaluate(state, config, profile, root, now, manual_halts, history, context_kind=context_kind)
                if result["risk_check_passed"]:
                    claim = {"id": root["proposal"]["id"], "signature": fingerprint(root), "plan": result["plan"],
                             "status": "reserved", "created_at": when, "has_position": False}
                    state["claims"].append(claim)
                    result.update({"reservation_reused": False, "reservation_id": claim["id"], "reservation_status": "reserved"})
        elif kind in {"release", "submitted", "unknown"}:
            keys(data, {"id", "market"} if kind == "submitted" else {"id"})
            claim = claim_by_id(state, data["id"])
            if kind == "release":
                if claim["status"] != "reserved":
                    reject("RISK_TRANSITION")
                claim["status"] = "released"
            elif kind == "submitted":
                if claim["status"] != "reserved" or manual_halts or active_halts(state):
                    reject("RISK_TRANSITION")
                recheck_state = deepcopy(state)
                recheck_state["claims"] = [item for item in recheck_state["claims"] if item["id"] != claim["id"]]
                recheck = evaluate(recheck_state, config, profile,
                                   {"proposal": {"id": claim["id"], "symbol": "BTCUSDT", "side": "BUY",
                                                 "stop_price": claim["plan"]["stop_trigger"], "quantity": claim["plan"]["quantity"]},
                                    "market": data["market"]}, now, manual_halts, history, context_kind=context_kind)
                if not recheck["risk_check_passed"]:
                    reject("RISK_TRANSITION")
                claim["plan"] = recheck["plan"]
                # A future trusted adapter journals this before dispatch; no order is sent here.
                claim["status"] = "submitted"
            else:
                if claim["status"] not in {"submitted", "unknown"}:
                    reject("RISK_TRANSITION")
                claim["status"] = "unknown"
            result = {"reservation_id": claim["id"], "reservation_status": claim["status"]}
            if kind == "submitted":
                result["submission_risk_rechecked"] = True
        elif kind == "reconcile":
            result = reconcile(state, data, now, config)
        else:
            reject()
        financial_halts(state, config, history)
        return state, result


def audit(seed_document, document, records, migration, config, profile):
    """Recompute the saved ledger from its seed and every accepted operation."""
    try:
        if seed_document["profile_hash"] != profile.fingerprint:
            reject("RISK_PROFILE_MISMATCH")
        original = number(seed_document["origin_equity"], zero=True)
        high = number(seed_document["origin_high_water"])
        if high < max(original, config.starting_equity_usd):
            reject()
        expected_seed = seed(config, profile, original, high, seed_document["last_at"])
        if canonical(seed_document) != canonical(expected_seed):
            reject()
        if migration != {"from": 2, "to": 3, "seed_sha256": fingerprint(expected_seed), "profile_hash": profile.fingerprint}:
            reject()
        expected = expected_seed
        history = [valuation_point(expected_seed)]
        for when, payload, manual_halts in records:
            expected, _ = reduce(expected, payload["kind"], payload["input"], when, config, profile, manual_halts, history)
            history.append(valuation_point(expected))
        if canonical(document) != canonical(expected):
            reject()
        return expected, tuple(history)
    except (KeyError, ValueError, TypeError, IndexError, OverflowError):
        reject()
