"""Deterministic sizing and period budgets. It never grants execution permission."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext

from ..clock import stamp
from .model import (ACTIVE, MAX_ACCOUNT_AGE_SECONDS, PRECISION, ceil_step, fingerprint,
                    floor_step, request, text, timestamp)

ZERO = Decimal("0")


def periods(now):
    return now.date().isoformat(), (now.date() - timedelta(days=now.weekday())).isoformat()


def active_claims(state):
    return [claim for claim in state["claims"] if claim["status"] in ACTIVE]


def known_flat(state):
    return state["position_count"] == state["pending_entry_count"] == 0 and all(c["status"] == "reserved" for c in active_claims(state))


def valuation_point(state):
    flat = known_flat(state)
    return {"at": state["last_at"], "equity": text(equity(state)), "flat_known": flat,
            "valuation_current": flat or state["last_accounting_at"] == state["last_at"]}


def rolling_reference(state, history=None):
    cutoff = timestamp(state["last_at"]) - timedelta(days=7)
    if not history:
        # Standalone pure checks without audited marks conservatively retain the seed anchor.
        return Decimal(state["origin_equity"]), stamp(cutoff), False
    candidates = [point for point in history if timestamp(point["at"]) <= cutoff]
    point = candidates[-1] if candidates else history[0]
    uncertain = bool(candidates) and (not point["valuation_current"] or not point["flat_known"] and timestamp(point["at"]) < cutoff)
    reference = Decimal(point["equity"])
    if uncertain:
        reference = max(reference, Decimal(state["high_water_usd"]))
    return reference, stamp(cutoff), uncertain


def equity(state):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return Decimal(state["origin_equity"]) + Decimal(state["realized_net_pnl_usd"]) + Decimal(state["unrealized_net_pnl_usd"])


def latch(state, code, scope="global"):
    item = {"code": code, "scope": scope}
    if item not in state["halts"]:
        state["halts"].append(item)
        state["halts"].sort(key=lambda value: (value["code"], value["scope"]))


def active_halts(state):
    return sorted({item["code"] for item in state["halts"]
                   if item["scope"] == "global" or item["code"] == "DAILY_LOSS" and item["scope"] == state["day"]
                   or item["code"] == "WEEKLY_LOSS" and item["scope"] == state["week"]})


def commitments(state):
    claims = active_claims(state)
    cash = sum((Decimal(c["plan"]["cash_reserved_usd"]) for c in claims), ZERO)
    notional = sum((Decimal(c["plan"]["notional_usd"]) for c in claims), ZERO)
    original_loss = sum((Decimal(c["plan"]["loss_reserved_usd"]) for c in claims), ZERO)
    current = equity(state)
    loss = sum((max(ZERO, Decimal(c["plan"]["loss_reserved_usd"])
                         + (current - Decimal(c["plan"]["reservation_equity_usd"]) if c["status"] != "reserved" else ZERO))
                for c in claims), ZERO)
    open_notional, open_loss = Decimal(state["position_notional_usd"]), Decimal(state["open_risk_usd"])
    overlaps = state["position_reservation_id"] in {c["id"] for c in claims}
    return cash, max(notional, open_notional) if overlaps else notional + open_notional, max(loss, open_loss) if overlaps else loss + open_loss, original_loss


def budgets(state, config, history=None):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        current = equity(state)
        reserved_cash, deployed, future_loss, reserved_loss = commitments(state)
        week_reference, window_start, _ = rolling_reference(state, history)
        week_cap = min(config.starting_equity_usd, week_reference) * config.risk.weekly_loss_fraction
        day_headroom = max(ZERO, config.risk.daily_loss_cap_usd + current - Decimal(state["day_start_equity"]))
        week_headroom = max(ZERO, week_cap + current - week_reference)
        drawdown_floor = Decimal(state["high_water_usd"]) * (1 - config.risk.peak_drawdown_fraction)
        drawdown_headroom = max(ZERO, current - drawdown_floor)
        notional_cap = min(config.starting_equity_usd, current) * config.risk.max_notional_fraction
        result = {
            "equity_usd": current, "daily_headroom_usd": day_headroom, "weekly_cap_usd": week_cap,
            "weekly_headroom_usd": week_headroom, "drawdown_floor_usd": drawdown_floor,
            "drawdown_headroom_usd": drawdown_headroom, "reserved_loss_usd": reserved_loss,
            "future_loss_usd": future_loss,
            "reserved_cash_usd": reserved_cash, "committed_notional_usd": deployed,
            "remaining_new_loss_usd": max(ZERO, min(config.risk.per_trade_cap_usd, day_headroom - future_loss,
                                                    week_headroom - future_loss, drawdown_headroom - future_loss)),
            "available_cash_usd": max(ZERO, Decimal(state["cash_usd"]) - reserved_cash),
            "notional_cap_usd": notional_cap, "available_notional_usd": max(ZERO, notional_cap - deployed),
        }
        return {**{key: text(value) for key, value in result.items()},
                "weekly_reference_equity_usd": text(week_reference), "weekly_window_start_utc": window_start}


def financial_halts(state, config, history=None):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        current = equity(state)
        limits = budgets(state, config, history)
        if Decimal(state["day_start_equity"]) - current >= config.risk.daily_loss_cap_usd:
            latch(state, "DAILY_LOSS", state["day"])
        if Decimal(limits["weekly_reference_equity_usd"]) - current >= Decimal(limits["weekly_cap_usd"]):
            latch(state, "WEEKLY_LOSS")  # A calendar change never clears a rolling-loss review halt.
        if current <= Decimal(limits["drawdown_floor_usd"]):
            latch(state, "PEAK_DRAWDOWN")
        if state["losing_streak"] >= config.risk.losing_streak_limit:
            latch(state, "LOSING_STREAK")
        if (Decimal(limits["committed_notional_usd"]) > Decimal(limits["notional_cap_usd"])
                or max(state["position_count"], state["pending_entry_count"], len(active_claims(state))) > config.risk.max_positions):
            latch(state, "EXPOSURE_LIMIT")
        if Decimal(limits["future_loss_usd"]) > min(config.risk.per_trade_cap_usd, *(Decimal(limits[key]) for key in ("daily_headroom_usd", "weekly_headroom_usd", "drawdown_headroom_usd"))):
            latch(state, "OPEN_RISK_BUDGET")


def seed(config, profile, current: Decimal, high_water: Decimal, when: str):
    day, week = periods(timestamp(when))
    state = {
        "schema_version": 1, "profile_hash": profile.fingerprint,
        "origin_equity": text(current), "origin_high_water": text(high_water),
        "realized_net_pnl_usd": "0", "unrealized_net_pnl_usd": "0", "cash_usd": text(current),
        "position_value_usd": "0", "position_notional_usd": "0", "open_risk_usd": "0",
        "position_count": 0, "pending_entry_count": 0, "position_reservation_id": None,
        "total_fees_usd": "0", "high_water_usd": text(high_water), "losing_streak": 0,
        "day": day, "week": week, "day_start_equity": text(current), "week_start_equity": text(current),
        "last_at": when, "last_accounting_at": when, "claims": [], "reports": {}, "closed_trades": [], "halts": [],
    }
    financial_halts(state, config)
    return state


def advance(state, config, now, history=None):
    result = deepcopy(state)
    if now < timestamp(state["last_at"]):
        from ..errors import StateError
        raise StateError("CLOCK_REGRESSION", "Clock moved backwards; no risk state change was made.")
    day, week = periods(now)
    flat = known_flat(state)
    if day != state["day"] or week != state["week"]:
        if flat:
            if day != state["day"]:
                result["day"], result["day_start_equity"] = day, text(equity(state))
            if week != state["week"]:
                result["week"], result["week_start_equity"] = week, text(equity(state))
        else:
            # A later mark cannot establish an unobserved midnight/week-open valuation.
            latch(result, "PERIOD_REVIEW")
    if flat:
        result["last_accounting_at"] = stamp(now)
    result["last_at"] = stamp(now)
    if rolling_reference(result, history)[2]:
        latch(result, "PERIOD_REVIEW")
    financial_halts(result, config, history)
    return result


def evaluate(state, config, profile, document, now, manual_halts=(), history=None, *, context_kind="synthetic"):
    with localcontext() as ctx:
        ctx.prec = PRECISION
        root = request(document, context_kind=context_kind)
        proposal, market = root["proposal"], root["market"]
        state = advance(state, config, now, history)
        limits = budgets(state, config, history)
        reasons = active_halts(state)
        if manual_halts:
            reasons.append("MANUAL_HALT")
        if "BTC" not in config.markets:
            reasons.append("MARKET_DISABLED")
        if active_claims(state) or state["position_count"] or state["pending_entry_count"]:
            reasons.append("POSITION_OR_PENDING_LIMIT")
        for time_field, prefix in (("quote_at", "QUOTE"), ("fx_at", "FX")):
            age = (now - timestamp(market[time_field])).total_seconds()
            if age < 0:
                reasons.append(prefix + "_FUTURE")
            elif age > config.max_quote_age_seconds:
                reasons.append(prefix + "_STALE")
        if (now - timestamp(state["last_accounting_at"])).total_seconds() > MAX_ACCOUNT_AGE_SECONDS:
            reasons.append("ACCOUNT_STALE")
        bid, ask, fx_low, fx_high = [Decimal(market[name]) for name in ("bid", "ask", "fx_lower", "fx_upper")]
        if (ask - bid) / ((ask + bid) / 2) * 10_000 > config.max_spread_bps:
            reasons.append("SPREAD_TOO_WIDE")
        tick = profile.n("tick_size")
        entry = ceil_step(ask * (1 + profile.n("entry_slippage_bps") / 10_000), tick)
        stop = floor_step(Decimal(proposal["stop_price"]), tick)
        exit_price = floor_step(stop * (1 - profile.n("exit_slippage_bps") / 10_000), tick)
        if not profile.n("min_price") <= exit_price <= stop < bid <= entry <= profile.n("max_price"):
            reasons.append("PRICE_OR_STOP_INVALID")
        if Decimal(limits["remaining_new_loss_usd"]) <= 0:
            reasons.append("LOSS_BUDGET_EMPTY")
        if reasons:
            return decision(False, reasons, limits)
        entry_rate, exit_rate = profile.n("entry_fee_rate"), profile.n("exit_fee_rate")
        base_fee = profile.document["entry_fee_asset"] == "BTC"
        fee_quantum, base_quantum = profile.n("quote_fee_quantum"), profile.n("base_fee_quantum")
        buffer = profile.n("cost_buffer_usd")
        entry_unit = entry * fx_high * (1 if base_fee else 1 + entry_rate)
        exit_unit = exit_price * fx_low * (1 - entry_rate if base_fee else 1) * (1 - exit_rate)
        loss_unit = entry_unit - exit_unit
        # A linear upper bound avoids assuming rounded exit proceeds are monotone.
        # It fully charges one possible exit lot of dust, fee rounding and the cost buffer.
        loss_constant = (buffer + profile.n("exit_step") * exit_price * fx_low + fee_quantum * fx_low
                         + (base_quantum * exit_price * fx_low if base_fee else fee_quantum * fx_high))
        cash_constant = buffer + (ZERO if base_fee else fee_quantum * fx_high) + fee_quantum * fx_high
        cash_unit = entry_unit + exit_price * exit_rate * fx_high  # Reserve estimated exit fee as extra cash too.
        if loss_unit <= 0 or Decimal(limits["remaining_new_loss_usd"]) <= loss_constant or Decimal(limits["available_cash_usd"]) <= cash_constant:
            return decision(False, ["COSTS_EXCEED_BUDGET"], limits)
        maximum = min(profile.n("entry_max_qty"),
                      Decimal(limits["available_notional_usd"]) / (entry * fx_high),
                      (config.risk.max_notional_fraction * (Decimal(limits["equity_usd"]) - Decimal(limits["reserved_loss_usd"]) - loss_constant)
                       - Decimal(limits["committed_notional_usd"])) / (entry * fx_high + config.risk.max_notional_fraction * loss_unit),
                      (Decimal(limits["available_cash_usd"]) - cash_constant) / cash_unit,
                      (Decimal(limits["remaining_new_loss_usd"]) - loss_constant) / loss_unit)
        quantity = floor_step(max(ZERO, maximum), profile.n("entry_step"))
        if proposal["quantity"] is not None:
            requested = Decimal(proposal["quantity"])
            if requested != floor_step(requested, profile.n("entry_step")) or requested > quantity:
                return decision(False, ["REQUESTED_QUANTITY_EXCEEDS_SAFE_SIZE"], limits)
            quantity = requested
        if quantity <= 0:
            return decision(False, ["NO_SAFE_QUANTITY"], limits)
        fee_base = ceil_step(quantity * entry_rate, base_quantum) if base_fee else ZERO
        fee_quote = ZERO if base_fee else ceil_step(quantity * entry * entry_rate, fee_quantum)
        received = quantity - fee_base
        exit_quantity = floor_step(max(ZERO, received), profile.n("exit_step"))
        entry_quote = quantity * entry
        exit_quote = exit_quantity * exit_price
        if (quantity < profile.n("entry_min_qty") or not profile.n("entry_min_notional_quote") <= entry_quote <= profile.n("entry_max_notional_quote")
                or not profile.n("exit_min_qty") <= exit_quantity <= profile.n("exit_max_qty")
                or not profile.n("exit_min_notional_quote") <= exit_quote <= profile.n("exit_max_notional_quote")):
            return decision(False, ["ENTRY_OR_PROTECTIVE_EXIT_MINIMUM"], limits)
        exit_fee = ceil_step(exit_quote * exit_rate, fee_quantum)
        entry_cost = (entry_quote + fee_quote) * fx_high
        exit_proceeds = max(ZERO, exit_quote - exit_fee) * fx_low
        loss = quantity * loss_unit + loss_constant
        cash = quantity * cash_unit + cash_constant
        notional = entry_quote * fx_high
        actual_model_loss = entry_cost - exit_proceeds + buffer  # Residual dust has zero recovery here.
        if (actual_model_loss > loss or loss > Decimal(limits["remaining_new_loss_usd"])
                or cash > Decimal(limits["available_cash_usd"]) or notional > Decimal(limits["available_notional_usd"])):
            return decision(False, ["POST_ROUNDING_BUDGET_CHECK"], limits)
        plan = {"quantity": text(quantity), "entry_price_bound": text(entry), "stop_trigger": text(stop),
                "exit_price_estimate": text(exit_price), "exit_quantity": text(exit_quantity),
                "entry_fee_base": text(fee_base), "entry_fee_quote": text(fee_quote), "exit_fee_quote": text(exit_fee),
                "dust_quantity": text(received - exit_quantity), "notional_usd": text(notional),
                "entry_cash_cost_usd": text(entry_cost), "exit_proceeds_usd": text(exit_proceeds),
                "model_loss_usd": text(actual_model_loss), "loss_reserved_usd": text(loss), "cash_reserved_usd": text(cash),
                "reservation_equity_usd": limits["equity_usd"],
                "profile_hash": profile.fingerprint, "request_hash": fingerprint(root),
                "research_only": True, "execution_eligible": False}
        return decision(True, [], limits, plan)


def decision(allowed, reasons, limits, plan=None):
    return {"risk_check_passed": allowed, "reasons": sorted(set(reasons)), "budgets": limits,
            "plan": plan, "entries_allowed": False, "execution_eligible": False, "research_only": True}
