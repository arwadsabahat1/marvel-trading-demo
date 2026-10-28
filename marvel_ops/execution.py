"""Persistent Spot lifecycle with real fills, own-inventory reconciliation and risk gates."""

from copy import deepcopy
from decimal import Decimal, localcontext
import uuid

from marvel.risk.engine import advance, evaluate, seed, valuation_point
from marvel.risk.ledger import reduce as risk_change
from marvel.risk.model import fingerprint, floor_step, parse_profile, text
from marvel_testnet import model
from .common import Stop, bindings, require, stamp, utc_now
from .exits import exit_plan
from .validation import readiness

TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


class Trader:
    def __init__(self, store, client, *, now=utc_now):
        require(store.mode == client.environment and store.mode in {"testnet", "live"})
        self.store, self.client, self.config, self.now = store, client, store.config, now
        client.authorize = self.authorize

    def authorize(self, method, values):
        state = self.store.load()
        active = state["active"]
        require(active is not None, "OPS_NO_DURABLE_INTENT")
        identity = values.get("newClientOrderId", values.get("origClientOrderId"))
        items = [v for v in active["orders"].values() if v["request"]["newClientOrderId"] == identity]
        require(len(items) == 1, "OPS_ORDER_NOT_OWNED")
        item = items[0]
        if method == "POST":
            require(item["request"] == values and item["sent"] is False, "OPS_DUPLICATE_SUBMISSION")
            if values["side"] == "BUY" and self.store.mode == "live":
                require(readiness(self.store, state, self.now())["live_entries_allowed"], "OPS_LIVE_NOT_QUALIFIED")
            if values["side"] == "SELL":
                require(active["reconciled"] and Decimal(values["quantity"]) <= Decimal(active["owned_btc"]), "OPS_SELL_EXCEEDS_OWNERSHIP")
        else:
            require(item["observed"] is not None, "OPS_CANCEL_UNCONFIRMED_ORDER")

    def observe(self, state):
        client = self.client
        client.request("GET", "time")
        rules = model.symbol(client.request("GET", "rules", {"symbol": "BTCUSDT"}))
        quote = model.quote(client.request("GET", "quote", {"symbol": "BTCUSDT"}))
        age = client.clock()
        account = model.account(client.request("GET", "account"), state["started_at"] + self.store.mode)
        rates = model.fees(client.request("GET", "fees", {"symbol": "BTCUSDT"}))
        opened = client.request("GET", "open")
        require(type(opened) is list and len(opened) <= 1000)
        require(abs(client.server_ms - int(self.now().timestamp() * 1000)) <= 30000, "OPS_CLOCK_MISMATCH")
        require(0 <= client.clock() - age <= self.config.max_quote_age_seconds, "OPS_STALE_QUOTE")
        require((Decimal(quote["ask"]) - Decimal(quote["bid"])) / Decimal(quote["bid"]) * 10000 <= self.config.max_spread_bps,
                "OPS_SPREAD_TOO_WIDE")
        if state["account_binding"] is not None:
            require(state["account_binding"] == account["binding"], "OPS_ACCOUNT_CHANGED")
        state["account_binding"] = account["binding"]
        profile = model.profile(self.config, rules, rates)
        if "profile" in state:
            require(state["profile"] == dict(profile.document), "OPS_FEE_OR_FILTER_CHANGED")
        else:
            state["profile"] = dict(profile.document)
            state["risk"] = seed(self.config, profile, self.config.starting_equity_usd, self.config.starting_equity_usd, stamp(self.now()))
            state["risk_history"] = [valuation_point(state["risk"])]
        return {"rules": rules, "quote": quote, "account": account, "rates": rates, "opened": opened, "profile": profile}

    def plan(self, state, observed, stop_price):
        require(not state["active"] and not state["halts"], "OPS_HALT_OR_POSITION")
        require(not observed["opened"], "OPS_OUTSIDE_OPEN_ORDERS")
        require(self.now().hour < 22, "OPS_SESSION_ENTRY_BLOCK")
        balances = observed["account"]["balances"]
        require(not (observed["rates"]["bnb_possible"] and sum(Decimal(v) for v in balances["BNB"].values()) > 0
                     and max(Decimal(observed["rates"][s]) for s in ("buy", "sell")) > 0), "OPS_BNB_FEE_UNALLOCATED")
        if state["baseline"] is not None:
            self.check_balances(state, observed["account"])
        # This is an explicitly hypothetical sizing projection. Actual provider
        # provenance stays on the separate observed evidence and is never changed.
        market = {"kind": "synthetic", **observed["quote"], "quote_at": stamp(self.now()),
                  "fx_at": stamp(self.now()), "fx_lower": "0.995", "fx_upper": "1.005"}
        document = {"proposal": {"id": fingerprint({"revision": state["revision"], "at": stamp(self.now())}),
            "symbol": "BTCUSDT", "side": "BUY", "stop_price": stop_price, "quantity": None}, "market": market}
        state["risk"] = advance(state["risk"], self.config, self.now(), state["risk_history"])
        checked = evaluate(state["risk"], self.config, observed["profile"], document,
            self.now(), history=state["risk_history"])
        if checked["risk_check_passed"] and Decimal(checked["plan"]["dust_quantity"]):
            from marvel.research.feasibility import largest_complete_quantity
            quantity, _ = largest_complete_quantity(observed["profile"], Decimal(checked["plan"]["quantity"]))
            if quantity is None:
                return {"risk_check_passed": False, "reasons": ["OPS_FULL_EXIT_GRID_BLOCKED"]}, document
            document["proposal"]["quantity"] = text(quantity)
            checked = evaluate(state["risk"], self.config, observed["profile"], document,
                self.now(), history=state["risk_history"])
        if checked["risk_check_passed"]:
            require(Decimal(checked["plan"]["entry_cash_cost_usd"]) / Decimal("1.005") <= Decimal(balances["USDT"]["free"]), "OPS_FUNDS_INSUFFICIENT")
        return checked, document

    def preflight(self, stop_price):
        with self.store.lock(), localcontext() as context:
            context.prec = 256
            state = self.store.load()
            observed = self.observe(state)
            checked, _ = self.plan(state, observed, stop_price)
            self.store.save(state, "preflight")
            return checked

    def enter(self, stop_price, strategy, *, regime_context="range"):
        from .learning import CARDS
        require(strategy in CARDS, "OPS_STRATEGY_UNKNOWN")
        require(regime_context in {"trend", "range", "volatile"}, "OPS_REGIME_INVALID")
        with self.store.lock(), localcontext() as context:
            context.prec = 256
            state = self.store.load()
            if self.store.mode == "live":
                gate = readiness(self.store, state, self.now())
                require(gate["live_entries_allowed"] and strategy in gate["qualified_strategies"], "OPS_LIVE_NOT_QUALIFIED")
            observed = self.observe(state)
            checked, document = self.plan(state, observed, stop_price)
            if not checked["risk_check_passed"]:
                self.store.save(state, "entry_refused")
                return {"submitted": False, "reasons": checked["reasons"]}
            document["proposal"]["quantity"] = checked["plan"]["quantity"]
            risk, result = risk_change(state["risk"], "reserve", document, stamp(self.now()), self.config,
                observed["profile"], history=state["risk_history"])
            require(result["risk_check_passed"])
            risk, _ = risk_change(risk, "submitted", {"id": document["proposal"]["id"], "market": document["market"]},
                stamp(self.now()), self.config, observed["profile"], history=state["risk_history"])
            state["risk"] = risk
            state["baseline"] = state["baseline"] or observed["account"]["balances"]
            state["active"] = {"id": uuid.uuid4().hex[:20], "claim": document["proposal"]["id"],
                "strategy": strategy, "regime": regime_context, "plan": checked["plan"], "rules": observed["rules"], "orders": {},
                "owned_btc": "0", "quote_delta": "0", "reconciled": False, "protected": False,
                "spent_usd": "0", "received_usd": "0", "fees_usd": "0", "unknown_fee_assets": {},
                "started_at": stamp(self.now()), "ever_protected": False}
            self.store.save(state, "entry_plan_and_claim")
            try:
                self.send(state, "entry", "BUY", "LIMIT", checked["plan"]["quantity"], checked["plan"]["entry_price_bound"])
                self.sync(state)
                if state["active"]:
                    self.protect(state)
            except Exception as exc:
                self.halt(state, getattr(exc, "code", "OPS_EXECUTION_UNCERTAIN"))
                raise Stop("OPS_REVIEW_REQUIRED") from None
            return {"submitted": True, "active": state["active"] is not None}

    def send(self, state, role, side, kind, quantity, price):
        cycle = state["active"]
        require(role not in cycle["orders"], "OPS_DUPLICATE_ORDER_INTENT")
        request = {"symbol": "BTCUSDT", "side": side, "type": kind, "quantity": quantity,
                   "newClientOrderId": "mvo_" + cycle["id"] + "_" + role, "newOrderRespType": "FULL"}
        request.update({"price": price, "timeInForce": "FOK"} if kind == "LIMIT" else {"stopPrice": price})
        cycle["orders"][role] = {"request": request, "sent": False, "observed": None, "fills": []}
        self.store.save(state, role + "_intent")
        try:
            self.client.request("POST", "order", request)
        except Stop:
            # Reconciliation, not a second submission, decides what happened.
            pass
        cycle["orders"][role]["sent"] = True
        self.store.save(state, role + "_attempted")

    def check_balances(self, state, account):
        baseline = state["baseline"]
        if baseline is None:
            return
        active = state["active"]
        quote = sum((Decimal(c["quote_delta"]) for c in state["closed"]), Decimal(0))
        quote += Decimal(active["quote_delta"]) if active else 0
        owned = Decimal(active["owned_btc"]) if active else Decimal(0)
        for asset, delta in (("BTC", owned), ("USDT", quote), ("BNB", Decimal(0))):
            actual = sum(Decimal(v) for v in account["balances"][asset].values())
            require(actual == sum(Decimal(v) for v in baseline[asset].values()) + delta, "OPS_BALANCE_MISMATCH")

    def sync(self, state):
        cycle = state["active"]
        if cycle is None:
            return
        cycle["reconciled"] = cycle["protected"] = False
        owned = quote_delta = spent = received = fees = Decimal(0)
        for item in cycle["orders"].values():
            request = item["request"]
            raw = self.client.request("GET", "order", {"symbol": "BTCUSDT", "origClientOrderId": request["newClientOrderId"]})
            observed = model.order(raw, request["newClientOrderId"], request["side"], request["type"], request["quantity"])
            fills = model.fills(self.client.request("GET", "trades", {"symbol": "BTCUSDT", "orderId": observed["order_id"], "limit": 1000}), observed)
            item.update(observed=observed, fills=fills, sent=True)
            for fill in fills:
                qty, cost, fee = (Decimal(fill[k]) for k in ("quantity", "quote", "commission"))
                asset, buy = fill["commission_asset"], request["side"] == "BUY"
                if asset not in {"BTC", "USDT"} and fee:
                    cycle["unknown_fee_assets"][asset] = text(fee)
                owned += qty if buy else -qty
                quote_delta += -cost if buy else cost
                if asset == "BTC":
                    owned -= fee
                elif asset == "USDT":
                    quote_delta -= fee
                if buy:
                    spent += (cost + (fee if asset == "USDT" else 0)) * Decimal("1.005")
                else:
                    received += (cost - (fee if asset == "USDT" else 0)) * Decimal("0.995")
                fees += fee * (cost / qty if asset == "BTC" else 1) * Decimal("1.005") if asset in {"BTC", "USDT"} else 0
        require(owned >= 0, "OPS_OVERSELL_DETECTED")
        cycle.update(owned_btc=text(owned), quote_delta=text(quote_delta), spent_usd=text(spent), received_usd=text(received), fees_usd=text(fees))
        require(not cycle["unknown_fee_assets"], "OPS_UNPRICED_FEE_ASSET")
        account = model.account(self.client.request("GET", "account"), state["started_at"] + self.store.mode)
        require(account["binding"] == state["account_binding"], "OPS_ACCOUNT_CHANGED")
        self.check_balances(state, account)
        cycle["reconciled"] = True
        protected_quantity = sum((Decimal(i["observed"]["quantity"]) - Decimal(i["observed"]["executed_btc"])
            for i in cycle["orders"].values() if i["request"]["type"] == "STOP_LOSS"
            and i["observed"]["status"] in {"NEW", "PARTIALLY_FILLED"}), Decimal(0))
        cycle["protected"] = bool(owned > 0 and protected_quantity == owned and Decimal(account["balances"]["BTC"]["locked"]) >= owned)
        cycle["ever_protected"] |= cycle["protected"]
        terminal = all(i["observed"]["status"] in TERMINAL for i in cycle["orders"].values())
        quote = model.quote(self.client.request("GET", "quote", {"symbol": "BTCUSDT"}))
        plan = exit_plan(text(owned), quote["bid"], cycle["rules"]) if owned else {"complete": True}
        profile = parse_profile(state["profile"])
        value = owned * Decimal(quote["bid"]) * Decimal("0.995") * (1 - profile.n("exit_fee_rate")) if plan["complete"] else Decimal(0)
        realized = Decimal(state["risk"]["realized_net_pnl_usd"])
        pnl = received - spent
        cash = self.config.starting_equity_usd + realized + pnl
        require(cash >= 0, "OPS_ALLOCATION_EXHAUSTED")
        flat = owned == 0 and terminal
        has_fill = Decimal(cycle["orders"]["entry"]["observed"]["executed_btc"]) > 0
        report = {"id": fingerprint({"cycle": cycle, "at": stamp(self.now())}), "cash_usd": text(cash),
            "position_value_usd": text(value), "position_notional_usd": text(owned * Decimal(quote["bid"]) * Decimal("1.005")),
            "open_risk_usd": cycle["plan"]["loss_reserved_usd"] if owned else "0", "position_count": int(owned > 0),
            "pending_entry_count": int(cycle["orders"]["entry"]["observed"]["status"] not in TERMINAL),
            "position_reservation_id": cycle["claim"] if owned else None, "unrealized_net_pnl_usd": "0" if flat else text(pnl + value),
            "total_fees_usd": text(fees + sum((Decimal(c["fees_usd"]) for c in state["closed"]), Decimal(0))),
            "closed_trades": [{"id": fingerprint({"cycle": cycle["id"]}), "reservation_id": cycle["claim"], "net_pnl_usd": text(pnl)}] if flat and has_fill else [],
            "confirmed_flat_ids": [cycle["claim"]] if flat else [], "no_fill_ids": [cycle["claim"]] if flat and not has_fill else []}
        state["risk"], _ = risk_change(state["risk"], "reconcile", report, stamp(self.now()), self.config, profile,
            history=state["risk_history"])
        state["risk_history"].append(valuation_point(state["risk"]))
        state["equity_usd"] = text(cash + value)
        state["high_water_usd"] = state["risk"]["high_water_usd"]
        if flat:
            cycle["closed_at"] = stamp(self.now())
            state["closed"].append(cycle)
            state["active"] = None
            self.store.record({"kind": "exchange_settlement", "bindings": bindings(self.config), "at": stamp(self.now()),
                "environment": self.store.mode, "provenance": self.client.provenance, "owned_btc": "0", "balanced": True,
                "had_fill": has_fill, "net_pnl_usd": text(pnl), "strategy": cycle["strategy"],
                "cycle": cycle["id"], "receipts": self.client.receipts[-20:]})
        self.store.save(state, "reconciled")

    def protect(self, state, role="stop"):
        cycle = state["active"]
        require(cycle and cycle["reconciled"], "OPS_OWNERSHIP_UNCERTAIN")
        quote = model.quote(self.client.request("GET", "quote", {"symbol": "BTCUSDT"}))
        plan = exit_plan(cycle["owned_btc"], cycle["plan"]["exit_price_estimate"], cycle["rules"])
        require(plan["complete"], "OPS_RESIDUAL_EXIT_BLOCKED")
        if Decimal(quote["bid"]) <= Decimal(cycle["plan"]["stop_trigger"]):
            price = text(floor_step(Decimal(quote["bid"]) * Decimal("0.998"), Decimal(cycle["rules"]["tick"])))
            self.send(state, "exit", "SELL", "LIMIT", plan["quantity"], price)
            self.sync(state)
            require(state["active"] is None, "OPS_EMERGENCY_EXIT_UNFILLED")
            return
        self.send(state, role, "SELL", "STOP_LOSS", plan["quantity"], cycle["plan"]["stop_trigger"])
        self.sync(state)
        require(state["active"] is None or state["active"]["protected"], "OPS_PROTECTION_UNCONFIRMED")

    def reconcile(self):
        with self.store.lock(), localcontext() as context:
            context.prec = 256
            state = self.store.load()
            self.client.request("GET", "time")
            try:
                self.sync(state)
            except Exception as exc:
                self.halt(state, getattr(exc, "code", "OPS_RECONCILE_UNCERTAIN"))
                raise Stop("OPS_REVIEW_REQUIRED") from None
            return state

    def close(self):
        with self.store.lock(), localcontext() as context:
            context.prec = 256
            state = self.store.load()
            self.client.request("GET", "time")
            try:
                self.sync(state)
                if state["active"] is None:
                    return state
                for role, item in list(state["active"]["orders"].items()):
                    if item["observed"]["status"] not in TERMINAL:
                        self.store.save(state, role + "_cancel_intent")
                        try:
                            self.client.request("DELETE", "order", {"symbol": "BTCUSDT", "origClientOrderId": item["request"]["newClientOrderId"]})
                        except Stop:
                            pass
                        self.sync(state)
                        if state["active"] is None:
                            return state
                        require(state["active"]["orders"][role]["observed"]["status"] in TERMINAL, "OPS_CANCEL_UNCONFIRMED")
                cycle = state["active"]
                quote = model.quote(self.client.request("GET", "quote", {"symbol": "BTCUSDT"}))
                price = text(floor_step(Decimal(quote["bid"]) * Decimal("0.998"), Decimal(cycle["rules"]["tick"])))
                plan = exit_plan(cycle["owned_btc"], price, cycle["rules"])
                require(plan["complete"], "OPS_RESIDUAL_EXIT_BLOCKED")
                self.send(state, "exit", "SELL", "LIMIT", plan["quantity"], price)
                self.sync(state)
                if state["active"]:
                    self.protect(state, "rearm")
                    self.halt(state, "OPS_EXIT_REMAINS_OPEN")
            except Exception as exc:
                self.halt(state, getattr(exc, "code", "OPS_CLOSE_UNCERTAIN"))
                raise Stop("OPS_REVIEW_REQUIRED") from None
            return state

    def halt(self, state, reason):
        if reason not in state["halts"]:
            state["halts"].append(reason)
        self.store.save(state, "review_halt")
