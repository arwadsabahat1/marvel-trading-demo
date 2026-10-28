"""One SQLite transaction binds risk reservations, balances, halts and audit history."""

from ..clock import parse_stamp, stamp, utc_now
from ..errors import StateError
from ..state import StateStore
from .engine import active_halts, advance, budgets, equity, evaluate
from .ledger import reduce
from .model import canonical, load_profile, text


class RiskStore:
    def __init__(self, config, clock=utc_now):
        self.config, self.clock = config, clock
        self.foundation = StateStore(config, clock)
        self.profile = load_profile(config.workspace)

    def status(self):
        snapshot = self.foundation.read()
        return summary(snapshot, self.config, self.clock())

    def check(self, document):
        snapshot = self.foundation.read()
        _clock_check(snapshot, self.clock())
        return evaluate(snapshot.risk_state, self.config, self.profile, document, self.clock(), snapshot.halts, snapshot.risk_history)

    def change(self, kind, data):
        with self.foundation._connect(write=True) as connection:
            with self.foundation._transaction(connection):
                before = self.foundation._validate(connection)
                when = stamp(self.clock())
                _clock_check(before, parse_stamp(when))
                state, result = reduce(before.risk_state, kind, data, when, self.config, self.profile, before.halts, before.risk_history)
                revision = before.revision + 1
                connection.execute("UPDATE risk_state SET document = ? WHERE singleton = 1", (canonical(state),))
                connection.execute("UPDATE account SET equity = ?, high_water = ?, updated_at = ?, revision = ? WHERE singleton = 1",
                                   (text(equity(state)), state["high_water_usd"], when, revision))
                self.foundation._journal(connection, when, "risk_updated", {"revision": revision, "kind": kind, "input": data})
                after = self.foundation._validate(connection)
        return after, result


def summary(snapshot, config, now):
    _clock_check(snapshot, now)
    state = advance(snapshot.risk_state, config, now, snapshot.risk_history)
    limits = budgets(state, config, snapshot.risk_history)
    return {"budgets": limits, "automatic_halt_reasons": active_halts(state),
            "halt_history": state["halts"], "losing_streak": state["losing_streak"],
            "closed_trades": len(state["closed_trades"]), "total_fees_usd": state["total_fees_usd"],
            "day_utc": state["day"], "weekly_window_start_utc": limits["weekly_window_start_utc"],
            "reservations": [{"id": c["id"], "status": c["status"], "has_position": c["has_position"], "plan": c["plan"]} for c in state["claims"]],
            "profile_hash": state["profile_hash"], "profile_qualification": "illustrative_not_venue_qualified",
            "period_projection_persisted": state == snapshot.risk_state,
            "entries_allowed": False, "execution_eligible": False}


def _clock_check(snapshot, now):
    if now < parse_stamp(snapshot.updated_at):
        raise StateError("CLOCK_REGRESSION", "Clock moved backwards; no risk state change was made.")
