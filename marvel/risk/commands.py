"""Public commands expose paper checks/unsent reservations, never accounting setters."""

from pathlib import Path

from ..clock import stamp, utc_now
from .model import canonical, decode, fingerprint, load_profile, reject
from .store import RiskStore, summary


def add_parser(commands):
    parser = commands.add_parser("risk", help="Independent paper sizing, reservations and financial halts; cannot trade")
    sub = parser.add_subparsers(dest="risk_command", required=True)
    sub.add_parser("status", help="Read verified risk state and projected period budgets")
    sub.add_parser("sync", help="Persist period/financial checks; cannot clear review halts")
    sub.add_parser("example", help="Write a fresh synthetic request to var/examples/btc-risk-request.json")
    for name in ("check", "reserve"):
        parser = sub.add_parser(name, help="Check a synthetic proposal" if name == "check" else "Atomically reserve paper risk/cash; sends no order")
        parser.add_argument("--input", type=Path, required=True)
    release = sub.add_parser("release", help="Release only a known unsent paper reservation")
    release.add_argument("--id", required=True)


def example_request(now=None):
    when = stamp(now or utc_now())
    return {"proposal": {"id": fingerprint({"synthetic_example": when}), "symbol": "BTCUSDT", "side": "BUY", "stop_price": "78400", "quantity": None},
            "market": {"kind": "synthetic", "bid": "79999.99", "ask": "80000", "quote_at": when,
                       "fx_lower": "1", "fx_upper": "1", "fx_at": when}}


def run(args, config):
    command = args.risk_command
    if command == "example":
        load_profile(config.workspace)
        folder = config.workspace / "var/examples"
        if not folder.resolve().is_relative_to(config.workspace):
            reject()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "btc-risk-request.json"
        if not path.resolve().is_relative_to(folder.resolve()):
            reject()
        path.write_text(canonical(example_request()) + "\n", encoding="utf-8")
        return {"example_file": "var/examples/btc-risk-request.json", "example_kind": "synthetic", "example_expires_after_seconds": config.max_quote_age_seconds}
    store = RiskStore(config)
    if command == "status":
        return {"risk": store.status()}
    if command in {"check", "reserve"}:
        path = args.input if args.input.is_absolute() else config.workspace / args.input
        try:
            with path.open("rb") as handle:
                raw = handle.read(16_385)
        except OSError:
            reject()
        if len(raw) > 16_384:
            reject()
        document = decode(raw)
        if command == "check":
            return {"risk_decision": store.check(document)}
        snapshot, result = store.change("reserve", document)
    else:
        snapshot, result = store.change("sync" if command == "sync" else "release", {} if command == "sync" else {"id": args.id})
    return {"risk_operation": result, "risk": summary(snapshot, config, store.clock())}
