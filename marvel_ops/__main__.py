"""Explicit operational CLI. Paper is default; no credentials in arguments or files."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sqlite3
import sys
import warnings

from marvel.config import load_config
from marvel.errors import MarvelError
from .common import Stop, require, utc_now
from .store import Store
from .transport import Spot
from .execution import Trader
from .service import Worker
from .validation import validate, finalize, readiness, release


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Stop("OPS_ARGUMENTS_INVALID")


def main(argv=None):
    try:
        require(sys.version_info[:2] == (3, 12) and sys.implementation.name == "cpython", "OPS_PYTHON_UNSUPPORTED")
        parser = Parser(description="Evidence-driven strategy operations; paper by default.")
        parser.add_argument("--workspace", type=Path, default=Path.cwd())
        parser.add_argument("--mode", choices=("paper", "testnet", "live"), default="paper")
        sub = parser.add_subparsers(dest="command", required=True)
        for command in ("init", "status", "readiness", "health", "backup", "pause"):
            sub.add_parser(command)
        validation = sub.add_parser("validate")
        validation.add_argument("--dataset", required=True)
        final = sub.add_parser("finalize-validation")
        final.add_argument("--development-record", required=True)
        final.add_argument("--acknowledge-holdout", action="store_true")
        review = sub.add_parser("release")
        review.add_argument("--acknowledge-live-risk", action="store_true")
        review.add_argument("--confirm-provider-data-cost-fx-review", action="store_true")
        for command in ("worker", "preflight", "enter", "reconcile", "close"):
            action = sub.add_parser(command)
            action.add_argument("--allow-network", action="store_true")
            action.add_argument("--prompt-credentials", action="store_true")
            action.add_argument("--process-credentials", action="store_true")
            action.add_argument("--acknowledge-orders", action="store_true")
            if command in {"preflight", "enter"}:
                action.add_argument("--stop-price", required=True)
            if command == "enter":
                action.add_argument("--strategy", required=True)
            if command == "worker":
                action.add_argument("--max-ticks", type=int)
        args = parser.parse_args(argv)
        config = load_config(args.workspace.resolve() / "config/paper.json", args.workspace.resolve())
        store = Store(config, args.mode)
        if args.command == "init":
            state = store.initialize()
            result = {"initialized": True, "revision": state["revision"], "mode": args.mode}
        elif args.command in {"status", "health", "readiness"}:
            state = store.load()
            result = readiness(store, state) if args.command == "readiness" else store.health() if args.command == "health" else {
                "revision": state["revision"], "mode": args.mode, "equity_usd": state["equity_usd"],
                "halts": state["halts"], "active": None if state["active"] is None else {
                    k: state["active"][k] for k in ("owned_btc", "reconciled", "protected", "strategy")},
                "settled_cycles": len(state["closed"])}
        elif args.command == "backup":
            result = {"backup": store.backup()}
        elif args.command == "pause":
            with store.lock():
                state = store.load()
                if "OPS_OPERATOR_PAUSE" not in state["halts"]:
                    state["halts"].append("OPS_OPERATOR_PAUSE")
                    store.save(state, "operator_pause")
                result = {"paused": True, "existing_holdings_retained": True}
        elif args.command == "validate":
            result = validate(config, args.dataset, store)
        elif args.command == "finalize-validation":
            result = finalize(store, args.development_record, acknowledge_holdout=args.acknowledge_holdout)
        elif args.command == "release":
            require(args.mode == "live")
            result = release(store, acknowledge=args.acknowledge_live_risk,
                reviewed=args.confirm_provider_data_cost_fx_review)
        else:
            require(args.allow_network, "OPS_NETWORK_OPT_IN_REQUIRED")
            store.load()
            key = secret = None
            if args.mode != "paper":
                require(args.prompt_credentials != args.process_credentials, "OPS_CREDENTIAL_SOURCE_REQUIRED")
                if args.process_credentials:
                    prefix = "MARVEL_OPS_" + args.mode.upper() + "_API_"
                    key, secret = os.environ.get(prefix + "KEY"), os.environ.get(prefix + "SECRET")
                    require(key and secret, "OPS_CREDENTIALS_MISSING")
                else:
                    require(sys.stdin.isatty() and sys.stderr.isatty(), "OPS_LOCAL_HIDDEN_PROMPTS_REQUIRED")
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        key = getpass.getpass("Separate Spot " + args.mode + " key (hidden): ")
                        secret = getpass.getpass("Paired HMAC secret (hidden): ")
            def factory():
                return Spot("live" if args.mode == "paper" else args.mode, allow_network=True,
                    acknowledge_orders=args.acknowledge_orders, key=key, secret=secret)
            if args.command == "worker":
                from .runtime import step
                worker = Worker(store, lambda: step(store, factory))
                worker.install_signals()
                result = worker.run(max_ticks=args.max_ticks)
            else:
                require(args.mode != "paper", "OPS_EXCHANGE_MODE_REQUIRED")
                trader = Trader(store, factory())
                if args.command == "preflight":
                    result = trader.preflight(args.stop_price)
                elif args.command == "enter":
                    result = trader.enter(args.stop_price, args.strategy)
                else:
                    state = trader.reconcile() if args.command == "reconcile" else trader.close()
                    result = {"active_holding": state["active"] is not None, "halts": state["halts"]}
        print(json.dumps({"operations": result, "financial_success_proven": False}, indent=2, sort_keys=True))
        return 0
    except (Stop, MarvelError) as exc:
        print(json.dumps({"error": {"code": exc.code}, "financial_success_proven": False}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print(json.dumps({"error": {"code": "OPS_STOPPED_REVIEW_STATE"}, "financial_success_proven": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
