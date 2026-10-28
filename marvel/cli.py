import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .clock import DISPLAY_TIMEZONE, stamp, utc_now
from .config import MARKETS, Config, load_config
from .errors import MarvelError
from .events import EventLog
from .state import PAUSE_REASONS, SCHEMA_VERSION, Snapshot, StateStore
from .data import commands as data_commands
from .risk import commands as risk_commands
from .paper import commands as paper_commands
from .research import commands as research_commands


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default includes raw argv values, which could contain a secret.
        raise MarvelError("ARGUMENTS_INVALID", "Invalid command arguments; use --help for supported commands.")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Marvel Phase 5: offline BTC hypothesis screening, risk controls and paper execution.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Project/runtime root (default: current directory)")
    parser.add_argument("--config", type=Path, default=Path("config/paper.json"), help="Configuration path relative to workspace, unless absolute")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Validate settings without creating runtime files")
    commands.add_parser("init", help="Explicitly initialize paper state; never reset an existing account")
    commands.add_parser("status", help="Read and validate saved state; cannot create an account")
    pause = commands.add_parser("pause", help="Persist a manual halt; no resume command exists in Phase 1")
    pause.add_argument("--reason", choices=sorted(PAUSE_REASONS), default="operator")
    commands.add_parser("migrate", help="Apply a known schema migration explicitly")
    data_commands.add_parser(commands)
    risk_commands.add_parser(commands)
    paper_commands.add_parser(commands)
    research_commands.add_parser(commands)
    return parser


def _base(config: Config) -> dict:
    now = utc_now()
    risk = {name: str(value) if not isinstance(value, int) else value for name, value in asdict(config.risk).items()}
    return {
        "application_version": __version__, "phase": 5, "mode": "paper", "network_enabled": False,
        "execution_network_enabled": False, "research_network_opt_in": False,
        "live_execution_available": False, "entries_allowed": False,
        "blocking_reasons": ["EXECUTION_DATA_NOT_QUALIFIED", "REAL_ORDER_EXECUTION_NOT_IMPLEMENTED", "STRATEGY_NOT_QUALIFIED", "LIVE_FEASIBILITY_NOT_PASSED"],
        "timestamp_utc": stamp(now), "display_time_utc": now.astimezone(DISPLAY_TIMEZONE).isoformat(),
        "daily_accounting_timezone": "UTC", "config_fingerprint": config.fingerprint,
        "risk_settings": risk,
        "risk_settings_enforced_on_orders": False,
        "risk_engine_available": True, "paper_reservations_enforced": True,
        "paper_execution_available": True, "paper_execution_kind": "synthetic_only",
        "risk_settings_enforced_on_paper_orders": True,
        "historical_research_available": True, "strategy_qualified": False,
        "research_full_fill_liquidation_preflight": True,
        "actual_account_fees_qualified": False, "partial_fill_liquidation_qualified": False,
        "conversion_research_simulator_available": True, "conversion_execution_available": False,
        "read_only_account_research_available": True,
        "offline_exit_cost_audit_available": True,
        "isolated_risk_bound_cycle_available": True,
        "convert_quote_research_available": True, "convert_quote_acceptance_available": False,
        "limit_precedence_note": "Tighter weekly/drawdown/streak/exposure controls take precedence over trade and daily ceilings.",
        "data_settings": {"max_quote_age_seconds": config.max_quote_age_seconds, "max_spread_bps": str(config.max_spread_bps), "validated_for_trading": False},
        "research_data_pipeline_available": ["BTCUSDT"],
        "markets": [{"id": market, "data_validation": "research_pipeline_available" if market == "BTC" else "pending", "live_qualified": False} for market in config.markets],
    }


def _status(config: Config, snapshot: Snapshot) -> dict:
    result = _base(config)
    result.update({
        "state_initialized": True, "schema_version": snapshot.schema_version,
        "paper_initialized": "paper_state" in (snapshot.risk_state or {}),
        "account": {"currency": "USD", "initial_equity": str(snapshot.initial_equity_usd), "equity": str(snapshot.equity_usd), "high_water": str(snapshot.high_water_usd), "revision": snapshot.revision, "journal_events": snapshot.journal_events, "created_at": snapshot.created_at, "updated_at": snapshot.updated_at},
        "halted": bool(snapshot.halts), "halt_reasons": list(snapshot.halts),
    })
    if snapshot.halts:
        result["blocking_reasons"].append("PERSISTENT_MANUAL_HALT")
    if snapshot.risk_state is not None:
        from .risk.store import summary
        result["risk"] = summary(snapshot, config, utc_now())
        automatic = result["risk"]["automatic_halt_reasons"]
        result["halt_reasons"] = sorted(set(result["halt_reasons"]) | set(automatic))
        result["halted"] = bool(result["halt_reasons"])
        if automatic:
            result["blocking_reasons"].append("PERSISTENT_FINANCIAL_HALT")
    return result


def _print(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    log = None
    args = None
    try:
        if sys.version_info[:2] != (3, 12) or sys.implementation.name != "cpython":
            raise MarvelError("PYTHON_UNSUPPORTED", "This build is qualified for CPython 3.12.x; use the documented interpreter.")
        args = _parser().parse_args(argv)
        config_path = args.config if args.config.is_absolute() else args.workspace / args.config
        config = load_config(config_path, args.workspace)
        if args.command == "validate":
            result = _base(config)
            result.update({"configuration_valid": True, "state_checked": False, "python_version": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version, "available_market_ids": list(MARKETS), "supported_schema_version": SCHEMA_VERSION})
            _print(result)
            return 0
        log = EventLog(config.log)
        log.emit("command_started", args.command)
        if args.command == "data":
            result = _base(config)
            result.update(data_commands.run(args, config))
            result["research_only"] = True
            result["execution_eligible"] = False
            log.emit("command_completed", args.command)
            _print(result)
            return 0
        if args.command == "risk":
            result = _base(config)
            result.update(risk_commands.run(args, config))
            log.emit("command_completed", args.command)
            _print(result)
            return 0
        if args.command == "paper":
            result = _base(config)
            result.update(paper_commands.run(args, config))
            log.emit("command_completed", args.command)
            _print(result)
            return 0
        if args.command == "research":
            result = _base(config)
            result.update(research_commands.run(args, config))
            result["research_only"] = True
            result["execution_eligible"] = False
            log.emit("command_completed", args.command)
            _print(result)
            return 0
        store = StateStore(config)
        if args.command == "init":
            snapshot, created = store.initialize()
            result = _status(config, snapshot)
            result["new_account_created"] = created
        elif args.command == "status":
            result = _status(config, store.read())
        elif args.command == "pause":
            result = _status(config, store.pause(args.reason))
        else:
            snapshot, changed = store.migrate()
            result = _status(config, snapshot)
            result["schema_changed"] = changed
        log.emit("command_completed", args.command)
        _print(result)
        return 0
    except MarvelError as exc:
        if log is not None and args is not None:
            try:
                log.emit("command_failed", args.command, code=exc.code)
            except MarvelError:
                pass
        _print({"error": {"code": exc.code, "message": exc.public_message}, "entries_allowed": False, "live_execution_available": False})
        return 2
    except (OSError, ValueError, sqlite3.Error):
        # Never echo OS/database text, raw settings, argv or a traceback.
        _print({"error": {"code": "OPERATION_FAILED", "message": "Local operation failed safely; preserve state and review the setup."}, "entries_allowed": False, "live_execution_available": False})
        return 2
    finally:
        if log is not None:
            log.close()
