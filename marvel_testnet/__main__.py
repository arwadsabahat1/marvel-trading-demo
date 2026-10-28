"""Separate CLI; no production-mode flag or host/budget override exists."""

import argparse
import getpass
import json
from pathlib import Path
import sys
import warnings

from marvel.clock import utc_now
from marvel.config import load_config
from marvel.errors import MarvelError
from . import __version__, workflow
from .model import DemoError, HOST, fail
from .store import Store
from .transport import Client, VARIABLES, credentials


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail("TESTNET_ARGUMENTS_INVALID")


def main(argv=None):
    try:
        parser = Parser(description="Isolated Binance Spot Testnet engineering; virtual funds only.")
        parser.add_argument("--workspace", type=Path, default=Path.cwd())
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser("init", help="Initialize one persistent virtual allocation; never reset it")
        sub.add_parser("status", help="Verify and inspect the local virtual ledger offline")
        for command in ("public-check", "preflight", "cycle", "reconcile", "recover"):
            action = sub.add_parser(command)
            action.add_argument("--allow-network", action="store_true")
            if command != "public-check":
                action.add_argument("--prompt-credentials", action="store_true")
            if command in {"cycle", "recover"}:
                action.add_argument("--acknowledge-virtual-orders", action="store_true")
        args = parser.parse_args(argv)
        config = load_config(args.workspace.resolve() / "config/paper.json", args.workspace.resolve())
        store = Store(config)
        if args.command == "init":
            result = workflow.summary(store.initialize(utc_now()))
        elif args.command == "status":
            state = store.load()
            result = workflow.summary(state)
            try:
                store.require_current(state)
                result["bindings_match"] = True
            except DemoError:
                result["bindings_match"] = False
        else:
            if not args.allow_network:
                fail("TESTNET_NETWORK_OPT_IN_REQUIRED")
            if args.command in {"cycle", "recover"} and not args.acknowledge_virtual_orders:
                fail("TESTNET_VIRTUAL_ORDER_OPT_IN_REQUIRED")
            values = (None, None)
            if args.command != "public-check":
                state = store.load()
                store.require_current(state)
                environment = None
                if args.prompt_credentials:
                    if not sys.stdin.isatty() or not sys.stderr.isatty():
                        fail("TESTNET_HIDDEN_TERMINAL_REQUIRED")
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("error", getpass.GetPassWarning)
                            environment = {name: getpass.getpass(label) for name, label in zip(VARIABLES,
                                ("Spot Testnet API key (hidden): ", "Spot Testnet HMAC secret (hidden): "))}
                    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
                        fail("TESTNET_HIDDEN_TERMINAL_REQUIRED")
                values = credentials(environment)
            client = Client(allow_network=True, allow_orders=args.command in {"cycle", "recover"}, key=values[0], secret=values[1])
            if args.command == "public-check":
                result = workflow.public(client)
                result.pop("quote_received_monotonic", None)
            elif args.command == "preflight":
                result = workflow.preflight(config, client)
            elif args.command == "cycle":
                result = workflow.start_cycle(config, client)
            elif args.command == "reconcile":
                result = workflow.reconcile(config, client)
            else:
                result = workflow.recover(config, client)
        print(json.dumps({"adapter_version": __version__, "host": HOST, "result": result,
            "production_execution_available": False, "strategy_qualified": False, "phase5_passed": False}, indent=2, sort_keys=True))
        return 0
    except (DemoError, MarvelError) as exc:
        print(json.dumps({"error": {"code": exc.code, "message": "Testnet request stopped; no production execution is available. Inspect the local ledger before another attempt."},
            "production_execution_available": False, "strategy_qualified": False, "phase5_passed": False}))
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"error": {"code": "TESTNET_OPERATION_FAILED", "message": "Testnet operation stopped; raw responses and credentials were not printed."},
            "production_execution_available": False, "strategy_qualified": False, "phase5_passed": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
