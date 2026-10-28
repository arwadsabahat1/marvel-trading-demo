from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path
import re

from marvel.clock import parse_stamp, stamp, utc_now
from marvel.risk.model import canonical, fingerprint, signed, text


class Stop(Exception):
    def __init__(self, code, *, uncertain=False):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


def require(condition, code="OPS_INVALID"):
    if not condition:
        raise Stop(code)


def number(value, *, positive=False):
    try:
        require(type(value) is str and re.fullmatch(r"-?\d+(?:\.\d+)?", value) is not None)
        result = Decimal(value)
        require(result.is_finite() and abs(result) <= Decimal("1000000000000"))
        require(result > 0 if positive else result >= 0)
        return result
    except (ValueError, ArithmeticError):
        raise Stop("OPS_NUMBER_INVALID") from None


def bindings(config):
    root = Path(__file__).resolve().parents[1]
    paths = [p for folder in ("marvel", "marvel_testnet", "marvel_ops") for p in (root / folder).rglob("*.py")]
    return {"config": config.fingerprint,
            "profile": sha256((config.workspace / "config/btc-paper-risk.json").read_bytes()).hexdigest(),
            "source": fingerprint({p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() for p in sorted(paths)})}
