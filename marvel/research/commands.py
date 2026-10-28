"""Offline screens use existing verified snapshots; never the owner account store."""

import json

from ..data.quality import INTERVALS, utc_us
from ..data.store import SnapshotStore
from ..risk.model import fingerprint, load_profile
from .model import fail, plan, split
from .feasibility import report as feasibility_report
from .store import ResearchStore
from .study import evaluate


def add_parser(commands):
    parser = commands.add_parser("research", help="Fixed BTC hypotheses, cost-aware historical screens and evidence; cannot trade")
    sub = parser.add_subparsers(dest="research_command", required=True)
    sub.add_parser("plan", help="Show the frozen cards, modelling assumptions, split and evidence gates")
    sub.add_parser("list", help="Verify and list immutable studies, including unsuccessful candidates")
    sub.add_parser("demo", help="Build and screen invented hourly candles; engineering demonstration only")
    sub.add_parser("convert-demo", help="Replay invented Convert cleanup failures offline; no account or strategy integration")
    sub.add_parser("cycle-demo", help="Replay an isolated virtual buy/sell/cleanup cycle and original risk claim; no owner account or network")
    proxy = sub.add_parser("proxy-screen", help="Separate hypothetical quote-fee screen on allowed Binance development prices; never Kraken performance")
    proxy.add_argument("--dataset", required=True)
    proxy_inspect = sub.add_parser("proxy-inspect", help="Verify one separate immutable proxy study offline")
    proxy_inspect.add_argument("--run", required=True)
    compare = sub.add_parser("compare", help="Compare three fixed BTC rule families with cash on protected development prefixes; cannot select a live strategy")
    compare.add_argument("--dataset", required=True)
    compare_inspect = sub.add_parser("compare-inspect", help="Verify one immutable strategy family comparison offline")
    compare_inspect.add_argument("--run", required=True)
    sub.add_parser("forward-start", help="Freeze a 30-day public-observation protocol; no network, account or simulated fills")
    for name in ("forward-status", "forward-stop", "forward-capture", "forward-watch"):
        forward = sub.add_parser(name, help="Prospective BTC public observations only; cannot place or simulate trades")
        forward.add_argument("--session", required=True)
        if name in {"forward-capture", "forward-watch"}:
            forward.add_argument("--allow-network", action="store_true")
    account = sub.add_parser("account-check", help="Opt-in GET-only account fee/precision research; never orders or conversion quotes")
    account.add_argument("--allow-network", action="store_true", help="Allow at most four fixed-host GETs using dedicated local read-only credentials")
    account.add_argument("--prompt-credentials", action="store_true", help="Read key and HMAC secret with hidden local terminal prompts; requires --allow-network and a terminal")
    account_inspect = sub.add_parser("account-inspect", help="Verify a saved sanitized account-check record offline")
    account_inspect.add_argument("--record", required=True)
    exit_audit = sub.add_parser("exit-audit", help="Offline cost floors and partial/unavailable-exit stresses; cannot qualify or submit trades")
    exit_audit.add_argument("--record", required=True, help="Saved completed account metadata record ID")
    exit_audit.add_argument("--pair-limits", required=True, help="Workspace-local saved public BTC-to-USDT Convert response")
    quote = sub.add_parser("convert-quote", help="Observe one BTC-to-USDT quote only; never accept or convert")
    quote.add_argument("--from-btc", default="0.0000099", help="Research source amount, at most 0.0001 BTC; no asset is spent")
    quote.add_argument("--allow-network", action="store_true")
    quote.add_argument("--acknowledge-trade-permission", action="store_true", help="Acknowledge Binance requires a broader TRADE-enabled key for this quote-only request")
    quote.add_argument("--prompt-credentials", action="store_true", help="Hidden local input of separate quote-research credentials; needs both opt-ins and a terminal")
    screen = sub.add_parser("screen", help="Explore development data; reserve the final holdout and never promote a candidate")
    screen.add_argument("--dataset", required=True)
    inspect = sub.add_parser("inspect", help="Verify one stored immutable study and show its complete evidence")
    inspect.add_argument("--run", required=True)
    feasibility = sub.add_parser("feasibility", help="Read saved public BTC rules and audit modeled complete liquidation; cannot qualify account fees")
    feasibility.add_argument("--observation", required=True)


def synthetic_dataset(workspace):
    first, hour, count = utc_us("2026-03-01T00:00:00Z"), INTERVALS["1h"], 720
    rows = []
    for index in range(count):
        price = 80_000 + index * 10
        rows.append([(first + index * hour) // 1000, str(price), str(price + 12), str(price - 2), str(price + 10), "10",
                     (first + (index + 1) * hour) // 1000 - 1, str(price * 10), 20, "4", str(price * 4), "0"])
    parameters = {"format": "rest-json", "unit": "ms", "interval": "1h", "start_us": first,
                  "end_us": first + count * hour, "as_of_us": first + count * hour, "checksum": None}
    provenance = {"origin": "local_unverified", "data_kind": "synthetic", "fixture": "Monotone invented BTC candles; no market/return evidence"}
    return SnapshotStore(workspace).save(json.dumps(rows).encode("ascii"), parameters, provenance=provenance)["snapshot_id"]


def summary(document):
    candidates = []
    for candidate in document["candidates"]:
        baseline = candidate["validation"].get("baseline")
        candidates.append({"candidate_id": candidate["candidate_id"], "strategy": candidate["strategy"], "status": candidate["status"],
                           "baseline_metrics": baseline["metrics"] if baseline else None, "gates": candidate["gates"]})
    return {"run_id": document["run_id"], "status": document["status"], "data_kind": document["data_kind"],
            "dataset_id": document["dataset"]["snapshot_id"], "plan_id": document["plan_id"],
            "holdout_evaluated": document["holdout"]["evaluated"], "phase5_historical_gate_passed": False,
            "candidates": candidates, "report_file": f"var/research-runs/{document['run_id']}/report.md",
            "full_evidence_file": f"var/research-runs/{document['run_id']}/study.json",
            "owner_account_used": False, "live_execution_available": False, "entries_allowed": False}


def run(args, config):
    command = args.research_command
    if command.startswith("forward-"):
        from . import forward
        action = command.removeprefix("forward-")
        if action == "start":
            result = forward.start(config)
        elif action in {"capture", "watch"}:
            result = getattr(forward, action)(config, args.session, allow_network=args.allow_network)
        else:
            result = getattr(forward, action)(config, args.session)
        return {"forward_research": result, "research_network_opt_in": action in {"capture", "watch"} and args.allow_network}
    if command == "plan":
        return {"research_plan": plan(), "research_plan_id": fingerprint(plan())}
    if command == "convert-demo":
        from .conversion import demo
        return {"conversion_demo": demo()}
    if command == "cycle-demo":
        from .cycle import demo, summary as cycle_summary
        return {"cycle_demo": cycle_summary(demo(config, load_profile(config.workspace)))}
    if command == "proxy-screen":
        from .proxy import execute
        return {"proxy_cost_screen": execute(config, args.dataset)}
    if command == "proxy-inspect":
        from .proxy import ProxyStore
        return {"proxy_cost_study": ProxyStore(config.workspace).load(args.run)}
    if command == "compare":
        from .comparison import execute
        return {"strategy_comparison": execute(config, args.dataset)}
    if command == "compare-inspect":
        from .comparison import ComparisonStore
        return {"strategy_comparison_study": ComparisonStore(config.workspace).load(args.run)}
    if command == "convert-quote":
        from .convert_quote import VARIABLES, amount, collect
        amount(args.from_btc)
        environment = None
        if args.prompt_credentials:
            import getpass
            import sys
            import warnings
            if not args.allow_network or not args.acknowledge_trade_permission or not sys.stdin.isatty() or not sys.stderr.isatty():
                fail()
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    environment = {name: getpass.getpass(label) for name, label in zip(
                        VARIABLES, ("Quote-research Binance API key (hidden): ", "HMAC secret (hidden): "))}
            except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
                fail()
        return {"convert_quote_check": collect(from_btc=args.from_btc, allow_network=args.allow_network,
                    acknowledge_trade_permission=args.acknowledge_trade_permission, environ=environment),
                "research_network_opt_in": args.allow_network}
    if command == "exit-audit":
        from .account_check import AccountEvidenceStore
        from .exit_costs import audit, load_pair
        account = AccountEvidenceStore(config.workspace).load(args.record)
        limits, source = load_pair(config.workspace, args.pair_limits)
        return {"exit_cost_audit": audit(config, load_profile(config.workspace), account, limits,
                                       account_id=args.record, pair_source=source)}
    if command in {"account-check", "account-inspect"}:
        from .account_check import AccountEvidenceStore, collect, summary as account_summary
        store = AccountEvidenceStore(config.workspace)
        if command == "account-inspect":
            document = store.load(args.record)
            identity = args.record
        else:
            environment = None
            if args.prompt_credentials:
                import getpass
                import sys
                import warnings
                from .account_check import VARIABLES
                if not args.allow_network or not sys.stdin.isatty() or not sys.stderr.isatty():
                    fail()
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        environment = {name: getpass.getpass(label) for name, label in zip(
                            VARIABLES, ("Read-only Binance API key (hidden): ", "HMAC secret (hidden): "))}
                except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
                    fail()
            document = collect(allow_network=args.allow_network, environ=environment)
            identity = store.save(document) if args.allow_network else None
        return {"account_research_check": {**account_summary(document), "record_id": identity},
                "research_network_opt_in": command == "account-check" and args.allow_network,
                "account_observation": document if command == "account-inspect" else None}
    if command == "feasibility":
        observation = SnapshotStore(config.workspace).load_observation(args.observation)
        return {"btc_feasibility": {"observation_id": args.observation,
                    "observed_at_us": observation.get("metadata_observed_us"),
                    **feasibility_report(config, load_profile(config.workspace), observation)}}
    store = ResearchStore(config.workspace)
    if command == "list":
        return {"research_runs": store.inventory()}
    if command == "inspect":
        return {"research_study": store.load(args.run)}
    identifier = synthetic_dataset(config.workspace) if command == "demo" else args.dataset
    manifest, history = SnapshotStore(config.workspace).load(identifier)
    if manifest["parameters"]["interval"] != "1h":
        fail("RESEARCH_DATA")
    split(len(history.candles))
    profile = load_profile(config.workspace)
    store.freeze_plan()  # Preserve declared rules before calculating any strategy result.
    document = evaluate(manifest, history, config, profile)
    identity = store.save(document)
    return {"research_screen": summary(store.load(identity)), "research_only": True, "execution_eligible": False}
