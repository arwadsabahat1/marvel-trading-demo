from copy import deepcopy
from decimal import Decimal

from marvel.clock import stamp
from marvel.risk.commands import example_request
from marvel.risk.engine import seed
from marvel.risk.model import fingerprint, load_profile, text
from marvel.risk.store import RiskStore
from tests.helpers import NOW, WorkspaceCase


def token(value):
    return fingerprint({"synthetic_identifier": value})


def proposal(now=NOW, price="100", stop="98", quantity=None, identity="test"):
    document = example_request(now)
    document["proposal"].update({"id": token(identity), "stop_price": stop, "quantity": quantity})
    document["market"].update({"bid": text(Decimal(price) - Decimal("0.0001")), "ask": price})
    return document


class RiskCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        if hasattr(self, "limits"):
            self.document["risk"].update(self.limits)
            self.write_config()
        self.now = NOW
        self.profile = load_profile(self.root)
        self.risks = RiskStore(self.config(), clock=lambda: self.now)
        self.risks.foundation.initialize()

    def current(self):
        return self.risks.foundation.read().risk_state

    def reserve(self, identity="test", **changes):
        document = proposal(self.now, identity=identity, **changes)
        snapshot, result = self.risks.change("reserve", document)
        self.assertTrue(result["risk_check_passed"], result)
        return document["proposal"]["id"], result["plan"]

    def submitted(self, identity="test", **changes):
        claim, plan = self.reserve(identity, **changes)
        self.risks.change("submitted", {"id": claim, "market": proposal(self.now, **changes)["market"]})
        return claim, plan

    def report(self, *, claim=None, pnl="0", unrealized="0", value="0", positions=0, pending=0,
               fee="0", identity="report", flat=False, notional=None, open_risk="0"):
        state = self.current()
        total = Decimal(state["origin_equity"]) + Decimal(state["realized_net_pnl_usd"]) + Decimal(pnl) + Decimal(unrealized)
        return {"id": token(identity), "cash_usd": text(total - Decimal(value)), "position_value_usd": value,
                "position_notional_usd": value if notional is None else notional, "open_risk_usd": open_risk,
                "position_count": positions, "pending_entry_count": pending, "position_reservation_id": claim if positions else None,
                "unrealized_net_pnl_usd": unrealized, "total_fees_usd": fee,
                "closed_trades": [{"id": token(identity + "trade"), "reservation_id": claim, "net_pnl_usd": pnl}] if flat else [],
                "confirmed_flat_ids": [claim] if flat else [], "no_fill_ids": []}

    def close_trade(self, pnl, identity="trade", fee="0"):
        claim, _ = self.submitted(identity)
        self.risks.change("reconcile", self.report(claim=claim, pnl=pnl, identity=identity + "report", flat=True, fee=fee))
        return claim
