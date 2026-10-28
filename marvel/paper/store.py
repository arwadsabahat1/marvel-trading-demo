"""Same-database adapter; no second balance store or non-atomic order dispatch."""

from copy import deepcopy
from decimal import Decimal, localcontext

from ..risk.model import PRECISION, text
from ..risk.store import RiskStore, summary as risk_summary


class PaperStore:
    def __init__(self, config, clock=None):
        self.risks = RiskStore(config, **({"clock": clock} if clock else {}))

    def change(self, data):
        snapshot, result = self.risks.change("paper", data)
        return snapshot, result

    def status(self):
        snapshot = self.risks.foundation.read()
        return summary(snapshot, self.risks.config, self.risks.clock())


def summary(snapshot, config, now):
    book = snapshot.risk_state.get("paper_state")
    state = snapshot.risk_state
    with localcontext() as context:
        context.prec = PRECISION
        holdings = sum((Decimal(cycle["holding_btc"]) for cycle in book["cycles"]), Decimal(0)) if book else Decimal(0)
    return {"paper_initialized": book is not None, "simulation_kind": "synthetic",
            "paper_execution_available": True, "live_execution_available": False,
            "recovery_required": bool(book and any(order["status"] == "unknown" for order in book["orders"])),
            "accounting": {"cash_usd": state["cash_usd"], "holding_btc": text(holdings),
                           "net_position_value_usd": state["position_value_usd"],
                           "realized_net_pnl_usd": state["realized_net_pnl_usd"],
                           "unrealized_net_pnl_usd": state["unrealized_net_pnl_usd"],
                           "total_fees_usd": state["total_fees_usd"], "last_mark_at": state["last_accounting_at"]},
            "protection_kind": "local_synthetic_conditional_on_fresh_events",
            "ledger": deepcopy(book), "risk": risk_summary(snapshot, config, now)}
