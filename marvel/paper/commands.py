"""Explicit offline commands. Synthetic events cannot change risk/cost settings."""

from pathlib import Path
import shutil
import tempfile

from ..clock import utc_now
from ..config import load_config
from ..risk.commands import example_request
from ..risk.model import decode, fingerprint, reject
from ..state import StateStore
from .store import PaperStore, summary


def add_parser(commands):
    parser = commands.add_parser("paper", help="Offline synthetic broker, fills and accounting; no real trades")
    sub = parser.add_subparsers(dest="paper_command", required=True)
    sub.add_parser("status", help="Read the verified synthetic execution ledger")
    sub.add_parser("init", help="Explicitly bind the flat paper account to the execution ledger; never resets funds")
    sub.add_parser("demo", help="Exercise fills, timeout recovery and a gap stop in a disposable separate account")
    for name in ("submit", "event"):
        parser = sub.add_parser(name, help="Submit a synthetic paper request" if name == "submit" else "Apply a uniquely identified synthetic venue event")
        parser.add_argument("--input", type=Path, required=True)


def demonstration(config, configuration_path):
    # No mutation of the owner's account, even when the workspace is paused.
    scratch = config.workspace / ".test-tmp"
    if not scratch.resolve().is_relative_to(config.workspace):
        reject()
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paper-demo-", dir=scratch) as name:
        root = Path(name).resolve()
        if not root.is_relative_to(scratch.resolve()):
            reject()
        (root / "config").mkdir()
        shutil.copyfile(configuration_path, root / "config/paper.json")
        shutil.copyfile(config.workspace / "config/btc-paper-risk.json", root / "config/btc-paper-risk.json")
        isolated = load_config(root / "config/paper.json", root)
        if isolated.fingerprint != config.fingerprint:
            reject()
        now = utc_now()
        StateStore(isolated, clock=lambda: now).initialize()
        paper = PaperStore(isolated, clock=lambda: now)
        paper.change({"action": "init"})
        document = example_request(now)
        identity = document["proposal"]["id"]
        snapshot, submitted = paper.change({"action": "submit", "request": document})
        if not submitted["paper_submitted"]:
            reject()
        quantity = submitted["risk_decision"]["plan"]["quantity"]
        steps = []
        for index, (kind, amount, market) in enumerate((
            ("timeout", None, None), ("recover", None, document["market"]),
            ("buy_fill", quantity, document["market"]),
            ("quote", None, {**document["market"], "bid": "65000", "ask": "65000.01"}),
        )):
            event = {"id": fingerprint({"demo": index}), "kind": kind, "order_id": None if kind == "quote" else identity,
                     "quantity": amount, "market": market}
            snapshot, result = paper.change({"action": "event", "event": event})
            steps.append({"kind": kind, "result": result})
        return {"disposable_account": True, "owner_account_used": False, "synthetic_performance_only": True,
                "steps": steps, "paper": summary(snapshot, isolated, now)}


def run(args, config):
    command = args.paper_command
    if command == "demo":
        configuration_path = args.config if args.config.is_absolute() else config.workspace / args.config
        return {"paper_demo": demonstration(config, configuration_path)}
    store = PaperStore(config)
    if command == "status":
        return {"paper": store.status()}
    if command == "init":
        operation = {"action": "init"}
    else:
        path = args.input if args.input.is_absolute() else config.workspace / args.input
        try:
            with path.open("rb") as handle:
                raw = handle.read(16_385)
        except OSError:
            reject()
        if len(raw) > 16_384:
            reject()
        operation = {"action": command, "request" if command == "submit" else "event": decode(raw)}
    snapshot, result = store.change(operation)
    return {"paper_operation": result, "paper": summary(snapshot, config, store.risks.clock())}
