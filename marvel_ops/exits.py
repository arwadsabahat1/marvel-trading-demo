"""Full-fill feasibility and residual accounting; never sell baseline inventory."""

from decimal import Decimal, localcontext

from marvel.risk.model import ceil_step, floor_step, text
from .common import number, require


def exit_plan(owned, price, rules):
    with localcontext() as context:
        context.prec = 64
        owned, price = number(owned), number(price, positive=True)
        quantity = floor_step(owned, number(rules["step"], positive=True))
        residual = owned - quantity
        valid = (quantity > 0 and quantity >= number(rules["minimum_qty"])
                 and quantity <= number(rules["maximum_qty"], positive=True)
                 and quantity * price >= number(rules["minimum_quote"])
                 and quantity * price <= number(rules["maximum_quote"], positive=True))
        return {"quantity": text(quantity) if valid else "0", "residual": text(residual if valid else owned),
                "complete": bool(valid and residual == 0), "tradable": bool(valid),
                "reason": None if valid and residual == 0 else "RESIDUAL_REQUIRES_REVIEW"}


def entry_exit_check(quantity, entry, stop, rules, rate, fee_asset):
    with localcontext() as context:
        context.prec = 64
        quantity, entry, stop, rate = number(quantity, positive=True), number(entry, positive=True), number(stop, positive=True), number(rate)
        require(rate < Decimal("0.1") and fee_asset in {"BTC", "USDT"})
        fee = ceil_step(quantity * rate, number(rules["base_quantum"], positive=True)) if fee_asset == "BTC" else Decimal(0)
        plan = exit_plan(text(quantity - fee), text(stop), rules)
        return {**plan, "entry_fee_btc": text(fee), "entry_allowed": plan["complete"],
                "gross_notional_quote": text(quantity * entry)}
