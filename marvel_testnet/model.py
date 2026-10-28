"""Strict Testnet observations and hypothetical allocation planning."""

from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path
import re

from marvel.clock import stamp
from marvel.research.feasibility import largest_complete_quantity
from marvel.research.study import source_hashes as core_hashes
from marvel.risk.engine import advance, evaluate
from marvel.risk.model import PRECISION, ceil_step, fingerprint, floor_step, load_profile, parse_profile, text

HOST = "https://testnet.binance.vision"
ZERO = Decimal(0)
TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
STATUSES = TERMINAL | {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}


class DemoError(Exception):
    def __init__(self, code, *, uncertain=False):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


def fail(code="TESTNET_RESPONSE_INVALID", *, uncertain=False):
    raise DemoError(code, uncertain=uncertain) from None


def number(value, *, positive=False):
    if type(value) is not str or len(value) > 40 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
        fail()
    result = Decimal(value)
    if not (0 < result <= 10**12 if positive else 0 <= result <= 10**12):
        fail()
    return result


def integer(value):
    if type(value) is not int or not 0 <= value < 10**16:
        fail()
    return value


def sources():
    root = Path(__file__).resolve().parent
    return {"core/" + k: v for k, v in core_hashes().items()} | {
        "testnet/" + p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.py"))}


def financial_hashes(config):
    return {name: sha256((config.workspace / name).read_bytes()).hexdigest()
        for name in ("config/paper.json", "config/btc-paper-risk.json")}


def symbol(document):
    try:
        rows = document["symbols"]
        if type(rows) is not list or len(rows) != 1:
            fail()
        row = rows[0]
        if (row["symbol"] != "BTCUSDT" or row["baseAsset"] != "BTC" or row["quoteAsset"] != "USDT"
                or row["status"] != "TRADING" or row["isSpotTradingAllowed"] is not True
                or not {"LIMIT", "STOP_LOSS"} <= set(row["orderTypes"])):
            fail("TESTNET_ORDER_TYPES_UNSUPPORTED")
        filters = {item["filterType"]: item for item in row["filters"]}
        if len(filters) != len(row["filters"]) or len(filters) > 100:
            fail()
        lot, price = filters["LOT_SIZE"], filters["PRICE_FILTER"]
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL"))
        values = {"step": lot["stepSize"], "minimum_qty": lot["minQty"], "maximum_qty": lot["maxQty"],
            "tick": price["tickSize"], "min_price": price["minPrice"], "max_price": price["maxPrice"],
            "minimum_quote": notional["minNotional"], "maximum_quote": notional.get("maxNotional", "1000000")}
        for value in values.values():
            number(value, positive=True)
        base_precision = integer(row["baseCommissionPrecision"])
        quote_precision = integer(row["quoteCommissionPrecision"])
        if max(base_precision, quote_precision) > 12:
            fail()
        return {**values, "base_quantum": text(Decimal(1).scaleb(-base_precision)),
            "quote_quantum": text(Decimal(1).scaleb(-quote_precision))}
    except (KeyError, TypeError):
        fail()


def quote(document):
    try:
        if document["symbol"] != "BTCUSDT":
            fail()
        bid, ask = number(document["bidPrice"], positive=True), number(document["askPrice"], positive=True)
        if bid > ask:
            fail()
        return {"bid": text(bid), "ask": text(ask)}
    except (KeyError, TypeError):
        fail()


def account(document, salt):
    try:
        if document["canTrade"] is not True or document["accountType"] != "SPOT":
            fail("TESTNET_ACCOUNT_UNSUPPORTED")
        uid = integer(document["uid"])
        if type(document["balances"]) is not list or len(document["balances"]) > 10000:
            fail()
        amounts = {}
        for row in document["balances"]:
            if row.get("asset") not in {"BTC", "USDT", "BNB"}:
                continue
            asset = row["asset"]
            if asset in amounts:
                fail()
            amounts[asset] = {k: text(number(row[k])) for k in ("free", "locked")}
        if not {"BTC", "USDT"} <= amounts.keys():
            fail()
        amounts.setdefault("BNB", {"free": "0", "locked": "0"})
        return {"binding": sha256(f"{salt}:testnet-uid:{uid}".encode("ascii")).hexdigest(), "balances": amounts}
    except (KeyError, TypeError):
        fail()


def fees(document):
    try:
        if document["symbol"] != "BTCUSDT":
            fail()
        rates = {}
        with localcontext() as ctx:
            ctx.prec = PRECISION
            for side, role in (("buy", "buyer"), ("sell", "seller")):
                rates[side] = text(sum((number(document[c]["taker"]) + number(document[c][role])
                    for c in ("standardCommission", "specialCommission", "taxCommission")), ZERO))
                if Decimal(rates[side]) >= Decimal("0.1"):
                    fail()
        discount = document["discount"]
        if any(type(discount[k]) is not bool for k in ("enabledForAccount", "enabledForSymbol")):
            fail()
        rates["bnb_possible"] = discount["enabledForAccount"] and discount["enabledForSymbol"]
        return rates
    except (KeyError, TypeError):
        fail()


def profile(config, rules, rates):
    values = dict(load_profile(config.workspace).document)
    values.update(tick_size=rules["tick"], min_price=rules["min_price"], max_price=rules["max_price"],
        entry_step=rules["step"], exit_step=rules["step"], entry_min_qty=rules["minimum_qty"], exit_min_qty=rules["minimum_qty"],
        entry_max_qty=rules["maximum_qty"], exit_max_qty=rules["maximum_qty"],
        entry_min_notional_quote=rules["minimum_quote"], exit_min_notional_quote=rules["minimum_quote"],
        entry_max_notional_quote=rules["maximum_quote"], exit_max_notional_quote=rules["maximum_quote"],
        base_fee_quantum=rules["base_quantum"], quote_fee_quantum=rules["quote_quantum"],
        entry_fee_rate=rates["buy"], exit_fee_rate=rates["sell"], entry_fee_asset="BTC", exit_fee_asset="USDT")
    return parse_profile(values)


def plan(config, ledger, observed, now):
    """Project Testnet observations into the existing hypothetical sizing gate; no real permission."""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        risk = advance(ledger["risk"], config, now, ledger["history"])
        risk["last_accounting_at"] = stamp(now)  # Demo allocation is known flat before a new cycle.
        market = {"kind": "synthetic", **observed["quote"], "quote_at": stamp(now), "fx_at": stamp(now),
            "fx_lower": "0.995", "fx_upper": "1.005"}
        modeled = profile(config, observed["rules"], observed["fees"])
        stop = floor_step(Decimal(market["ask"]) * Decimal("0.99"), modeled.n("tick_size"))
        request = {"proposal": {"id": fingerprint({"testnet": ledger["protocol"]["nonce"], "at": stamp(now)}), "symbol": "BTCUSDT", "side": "BUY",
            "stop_price": text(stop), "quantity": None}, "market": market}
        checked = evaluate(risk, config, modeled, request, now, history=ledger["history"])
        if checked["risk_check_passed"] and Decimal(checked["plan"]["dust_quantity"]):
            quantity, _ = largest_complete_quantity(modeled, Decimal(checked["plan"]["quantity"]))
            if quantity is None:
                return {"risk_check_passed": False, "reasons": ["TESTNET_FULL_EXIT_GRID_BLOCKED"], "plan": None}
            request["proposal"]["quantity"] = text(quantity)
            checked = evaluate(risk, config, modeled, request, now, history=ledger["history"])
        return checked


def order(document, client_id, side, kind, quantity):
    try:
        if (document["symbol"] != "BTCUSDT" or document["clientOrderId"] != client_id
                or document["side"] != side or document["type"] != kind or document["status"] not in STATUSES
                or number(document["origQty"]) != Decimal(quantity)):
            fail("TESTNET_ORDER_MISMATCH")
        order_id = integer(document["orderId"])
        executed, cost = number(document["executedQty"]), number(document["cummulativeQuoteQty"])
        if executed > Decimal(quantity):
            fail()
        if document["status"] == "FILLED" and executed != Decimal(quantity):
            fail()
        return {"order_id": order_id, "client_id": client_id, "status": document["status"], "side": side,
            "type": kind, "quantity": quantity, "executed_btc": text(executed), "quote": text(cost)}
    except (KeyError, TypeError):
        fail()


def fills(document, observed_order):
    if type(document) is not list or len(document) >= 1000:
        fail("TESTNET_TRADES_INCOMPLETE")
    result, seen = [], set()
    with localcontext() as ctx:
        ctx.prec = PRECISION
        for row in document:
            try:
                identity = integer(row["id"])
                if (identity in seen or row["symbol"] != "BTCUSDT" or row["orderId"] != observed_order["order_id"]
                        or type(row["isBuyer"]) is not bool or row["isBuyer"] != (observed_order["side"] == "BUY")
                        or type(row["commissionAsset"]) is not str or re.fullmatch(r"[A-Z0-9]{1,20}", row["commissionAsset"]) is None):
                    fail()
                seen.add(identity)
                quantity, cost, fee = number(row["qty"], positive=True), number(row["quoteQty"], positive=True), number(row["commission"])
                result.append({"trade_id": identity, "quantity": text(quantity), "quote": text(cost),
                    "commission": text(fee), "commission_asset": row["commissionAsset"], "time_ms": integer(row["time"])})
            except (KeyError, TypeError):
                fail()
        if (sum((Decimal(r["quantity"]) for r in result), ZERO) != Decimal(observed_order["executed_btc"])
                or sum((Decimal(r["quote"]) for r in result), ZERO) != Decimal(observed_order["quote"])):
            fail("TESTNET_TRADES_INCOMPLETE")
    return result
