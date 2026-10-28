"""Bounded, deterministic online strategy selection from completed net outcomes."""

from decimal import Decimal, localcontext

from marvel.research.features import signal as baseline
from marvel.research.model import STRATEGIES
from marvel.research.rules import RULES, signal as additional
from marvel.risk.model import text
from .common import number, require

CARDS = {card.identity: card for card in (*STRATEGIES, *RULES)}


def regime(bars):
    if len(bars) < 25:
        return "unready"
    with localcontext() as context:
        context.prec = 64
        tail = bars[-24:]
        volatility = sum((b.high - b.low) / b.close for b in tail) / len(tail)
        change = tail[-1].close / tail[0].open - 1
        return "volatile" if volatility > Decimal("0.03") else "trend" if abs(change) > Decimal("0.02") else "range"


class Learner:
    def __init__(self, document=None):
        self.document = document if document is not None else {"seen": [], "contexts": {}}

    def observe(self, identity, strategy, context, net_pnl, capital, *, complete, provenance):
        require(strategy in CARDS and context in {"trend", "range", "volatile"})
        require(type(identity) is str and len(identity) == 64)
        require(provenance in {"synthetic", "historical", "forward_paper", "testnet", "live"})
        require(complete is True, "OPS_INCOMPLETE_TRADE_CANNOT_TRAIN")
        if identity in self.document["seen"]:
            return False
        with localcontext() as arithmetic:
            arithmetic.prec = 64
            require(type(net_pnl) is str, "OPS_REWARD_INVALID")
            reward = Decimal(net_pnl) / number(capital, positive=True)
            require(reward.is_finite() and abs(reward) <= 1, "OPS_REWARD_INVALID")
        key = provenance + ":" + context + ":" + strategy
        samples = self.document["contexts"].setdefault(key, [])
        samples.append(text(reward))
        del samples[:-200]
        self.document["seen"].append(identity)
        require(len(self.document["seen"]) <= 10000, "OPS_LEARNER_HISTORY_FULL")
        return True

    def select(self, context, qualified, *, provenance):
        """No money-risking exploration: an uncertain arm is always skipped."""
        choices = []
        with localcontext() as arithmetic:
            arithmetic.prec = 64
            for identity in sorted(set(qualified) & CARDS.keys()):
                values = [Decimal(v) for v in self.document["contexts"].get(provenance + ":" + context + ":" + identity, [])]
                if len(values) < 30:
                    continue
                mean = sum(values) / len(values)
                variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
                # Conservative score, not a selection-adjusted statistical guarantee.
                score = mean - 3 * (variance / len(values)).sqrt()
                if score > 0:
                    choices.append((score, identity))
        return max(choices)[1] if choices else None

    def propose(self, bars, qualified, *, provenance):
        context = regime(bars)
        selected = self.select(context, qualified, provenance=provenance)
        if selected is None:
            return {"strategy": None, "regime": context, "enter": False, "reason": "NO_QUALIFIED_POSITIVE_ARM"}
        card = CARDS[selected]
        decision = baseline(bars, card) if card in STRATEGIES else additional(bars, card)
        return {**decision, "strategy": selected, "regime": context}

    def champion(self, context, qualified, *, provenance):
        """Use the reviewed champion only during initial feedback collection.

        Once its context has 30 outcomes, failure of the positive-score test
        means cash, not an automatic switch to an unobserved challenger.
        """
        selected = self.select(context, qualified, provenance=provenance)
        if selected:
            return selected
        qualified = sorted(set(qualified) & CARDS.keys())
        if not qualified:
            return None
        baseline = qualified[0]
        values = self.document["contexts"].get(provenance + ":" + context + ":" + baseline, [])
        return baseline if len(values) < 30 else None
