from copy import deepcopy
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import subprocess
import sys
from unittest.mock import patch

from marvel.errors import MarvelError
from marvel.research import conversion, cycle
from marvel.risk.engine import active_claims, evaluate
from marvel.risk.model import canonical, fingerprint, load_profile, timestamp
from tests import test_cli as command_fixtures
from tests.helpers import ROOT, WorkspaceCase


class CycleTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg, self.profile = self.config(), load_profile(self.root)

    def start(self, entry="1", exit="1"):
        return cycle.new(self.cfg, self.profile, entry_fraction=entry, exit_fraction=exit)

    def apply(self, journal, item):
        return cycle.apply(journal, item, self.cfg, self.profile)

    def restore(self, journal):
        return cycle.restore(canonical(journal).encode("ascii"), self.cfg, self.profile)

    def prepared(self, entry="1", exit="1", *, cancel=True):
        journal = self.start(entry, exit)
        for kind, at in (("buy_fill", 0), ("entry_cancel", 1), ("exit_open", 2)):
            journal = self.apply(journal, cycle.event(kind, at))
        if entry == "1":
            if exit != "0":
                journal = self.apply(journal, cycle.event("sell_fill", 3))
            if cancel:
                journal = self.apply(journal, cycle.event("exit_cancel", 4))
        return journal

    def cleanup(self, journal, kind, at, **changes):
        return self.apply(journal, cycle.cleanup_event(journal, kind, at, **changes))

    def accepted(self, entry="1", exit="1"):
        journal = self.prepared(entry, exit)
        return self.cleanup(self.cleanup(journal, "quote", 5), "accept", 6)

    def closed(self):
        journal = self.accepted()
        return self.cleanup(self.cleanup(journal, "status", 7), "balances", 8)

    def test_each_prefix_restores_the_same_original_claim_and_cash(self):
        journal = self.start()
        claim = journal["state"]["risk"]["claims"][0]
        for kind, at in (("buy_fill", 0), ("entry_cancel", 1), ("exit_open", 2), ("sell_fill", 3), ("exit_cancel", 4)):
            journal = self.apply(journal, cycle.event(kind, at))
            self.assertEqual(journal, self.restore(journal))
            current = journal["state"]["risk"]["claims"][0]
            self.assertEqual(current["id"], claim["id"])
            self.assertEqual(current["plan"], claim["plan"])
            self.assertEqual(len(active_claims(journal["state"]["risk"])), 1)
        self.assertLessEqual(Decimal(claim["plan"]["loss_reserved_usd"]), 1)
        self.assertLessEqual(Decimal(claim["plan"]["notional_usd"]), 10)

    def test_settlement_closes_once_with_derived_pnl_not_conversion_proceeds_as_profit(self):
        journal = self.closed()
        state, book = journal["state"], journal["state"]["book"]
        risk, trade = state["risk"], book["cycles"][0]
        self.assertEqual(trade["holding_btc"], "0")
        self.assertEqual(risk["claims"][0]["status"], "closed")
        self.assertEqual(len(risk["closed_trades"]), 1)
        self.assertEqual(Decimal(risk["realized_net_pnl_usd"]), Decimal(trade["exit_proceeds_usd"]) - Decimal(trade["entry_cost_usd"]))
        self.assertEqual(Decimal(book["cash_usd"]), 50 + Decimal(risk["realized_net_pnl_usd"]))
        self.assertEqual(risk["losing_streak"], 1)
        self.assertLess(Decimal(risk["realized_net_pnl_usd"]), 0)
        self.assertGreater(Decimal(risk["total_fees_usd"]), 0)

    def test_pending_exit_order_blocks_cleanup_until_cancel_confirmed(self):
        journal = self.prepared(exit="0.5", cancel=False)
        blocked = self.cleanup(journal, "quote", 4)
        self.assertIsNone(blocked["state"]["conversion"])
        self.assertIn("SPOT_ORDERS_NOT_TERMINAL", blocked["state"]["rejections"])
        journal = self.apply(blocked, cycle.event("exit_cancel", 5))
        journal = self.cleanup(journal, "quote", 6)
        self.assertEqual(journal["state"]["conversion"]["state"]["phase"], "quoted")

    def test_pending_entry_blocks_cleanup(self):
        journal = self.apply(self.start("0.5"), cycle.event("buy_fill", 0))
        journal = self.cleanup(journal, "quote", 1)
        self.assertIn("SPOT_ORDERS_NOT_TERMINAL", journal["state"]["rejections"])
        self.assertIsNone(journal["state"]["conversion"])

    def test_partial_entry_below_spot_minimum_keeps_ownership_and_halts(self):
        journal = self.prepared("0.5", "0")
        state = journal["state"]
        self.assertIn("SPOT_EXIT_BELOW_MINIMUM", state["rejections"])
        self.assertEqual(state["book"]["cycles"][0]["holding_btc"], "0.00004995")
        self.assertTrue(active_claims(state["risk"]))
        self.assertIn("PEAK_DRAWDOWN", {h["code"] for h in state["risk"]["halts"]})
        self.assertEqual(state["risk"]["closed_trades"], [])

    def test_timeout_blocks_second_acceptance_and_survives_restart(self):
        journal = self.cleanup(self.accepted(), "timeout", 7)
        before = journal["state"]["book"]["cash_usd"]
        journal = self.cleanup(self.restore(journal), "accept", 8)
        journal = self.cleanup(journal, "quote", 9)
        self.assertEqual(journal["state"]["conversion"]["state"]["acceptance_intents"], 1)
        self.assertEqual(journal["state"]["book"]["cash_usd"], before)
        self.assertEqual(journal["state"]["risk"]["claims"][0]["status"], "unknown")

    def test_unknown_journal_restores_in_a_new_process_and_settles_only_once(self):
        journal = self.cleanup(self.accepted(), "timeout", 7)
        path = self.root / "cycle.json"
        path.write_text(canonical(journal), encoding="ascii")
        script = """
import json, sys
from pathlib import Path
from marvel.config import load_config
from marvel.risk.model import load_profile
from marvel.research.cycle import apply, cleanup_event, event, restore
root = Path(sys.argv[1])
config, profile = load_config(root / 'config/paper.json', root), load_profile(root)
j = restore((root / 'cycle.json').read_bytes(), config, profile)
assert j['state']['risk']['claims'][0]['status'] == 'unknown'
for kind, at in [('status', 20), ('balances', 21)]:
    j = apply(j, cleanup_event(j, kind, at), config, profile)
assert not j['state']['conversion_credited']
j = apply(j, event('mark', 22), config, profile)
j = apply(j, cleanup_event(j, 'balances', 23), config, profile)
print(json.dumps({'closed': len(j['state']['risk']['closed_trades']),
                  'cash': j['state']['book']['cash_usd']}))
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"closed": 1, "cash": self.closed()["state"]["book"]["cash_usd"]})
        self.assertFalse(self.cfg.database.exists())

    def test_failed_conversion_does_not_release_original_trade_claim(self):
        journal = self.accepted()
        holding = journal["state"]["book"]["cycles"][0]["holding_btc"]
        journal = self.cleanup(journal, "status", 7, status="FAIL")
        journal = self.cleanup(journal, "balances", 8, holding_btc=holding, cash_usdt="0")
        self.assertFalse(journal["state"]["conversion"]["state"]["claim_held"])
        self.assertTrue(active_claims(journal["state"]["risk"]))
        self.assertEqual(journal["state"]["book"]["cycles"][0]["holding_btc"], holding)
        self.assertEqual(journal["state"]["risk"]["closed_trades"], [])

    def test_processing_receipt_or_balance_alone_never_credits(self):
        base = self.accepted()
        for kind, changes in (("status", {"status": "PROCESS"}), ("status", {"status": "ACCEPT_SUCCESS"}),
                              ("status", {}), ("balances", {})):
            with self.subTest(kind=kind, changes=changes):
                journal = self.cleanup(base, kind, 7, **changes)
                self.assertEqual(journal["state"]["book"]["cash_usd"], base["state"]["book"]["cash_usd"])
                self.assertTrue(active_claims(journal["state"]["risk"]))

    def test_wrong_cash_residual_or_order_latches_review_and_preserves_claim(self):
        base = self.cleanup(self.accepted(), "status", 7)
        for changes in ({"cash_usdt": "0"}, {"holding_btc": "0.00000001"}, {"order_id": fingerprint("wrong")}):
            with self.subTest(changes=changes):
                journal = self.cleanup(base, "balances", 8, **changes)
                self.assertTrue(journal["state"]["review_required"])
                self.assertTrue(active_claims(journal["state"]["risk"]))
                self.assertEqual(journal["state"]["book"]["cash_usd"], base["state"]["book"]["cash_usd"])

    def test_duplicate_delivery_cannot_credit_or_close_twice(self):
        journal = self.closed()
        self.assertEqual(self.apply(journal, journal["events"][-1]), journal)
        journal = self.cleanup(journal, "balances", 9, label="redelivery")
        self.assertEqual(len(journal["state"]["risk"]["closed_trades"]), 1)
        self.assertEqual(journal["state"]["book"]["cash_usd"], self.closed()["state"]["book"]["cash_usd"])

    def test_below_floor_quote_cannot_spend_more_than_original_reservation(self):
        journal = self.prepared(exit="0")
        floor = cycle._floor(journal["state"])
        journal = self.cleanup(journal, "quote", 5, to_usdt=str(floor - Decimal("0.00000001")))
        self.assertIn("ORIGINAL_RESERVATION_FLOOR_FAILED", journal["state"]["rejections"])
        self.assertEqual(journal["state"]["conversion"]["state"]["acceptance_intents"], 0)

    def test_acceptance_rechecks_floor_after_fx_change(self):
        journal = self.cleanup(self.prepared(exit="0"), "quote", 5)
        journal = self.apply(journal, cycle.event("depeg_mark", 6))
        journal = self.cleanup(journal, "accept", 7)
        self.assertIn("ORIGINAL_RESERVATION_FLOOR_FAILED", journal["state"]["rejections"])
        self.assertEqual(journal["state"]["conversion"]["state"]["acceptance_intents"], 0)

    def test_stale_fx_refuses_quote(self):
        journal = self.cleanup(self.prepared(), "quote", 20)
        self.assertIn("FX_STALE", journal["state"]["rejections"])
        self.assertIsNone(journal["state"]["conversion"]["state"]["quote"])

    def test_quote_reference_cannot_invent_a_different_price_or_timestamp(self):
        base = self.prepared()
        for changes in ({"reference_bid": "90000"}, {"reference_at": 5}):
            with self.subTest(changes=changes):
                journal = self.cleanup(base, "quote", 5, **changes)
                self.assertIn("REFERENCE_NOT_BOUND_TO_MARK", journal["state"]["rejections"])
                self.assertIsNone(journal["state"]["conversion"]["state"]["quote"])

    def test_expired_acceptance_keeps_original_holding_and_cash(self):
        base = self.cleanup(self.prepared(), "quote", 5, valid_until=6)
        journal = self.cleanup(base, "accept", 6)
        self.assertEqual(journal["state"]["conversion"]["state"]["acceptance_intents"], 0)
        self.assertIn("QUOTE_EXPIRED", journal["state"]["conversion"]["state"]["rejections"])
        self.assertEqual(journal["state"]["book"], base["state"]["book"])

    def test_no_fill_terminal_cancel_closes_without_inventing_a_trade(self):
        journal = self.apply(self.start(), cycle.event("entry_cancel", 0))
        self.assertFalse(active_claims(journal["state"]["risk"]))
        self.assertEqual(journal["state"]["risk"]["closed_trades"], [])
        self.assertEqual(journal["state"]["book"]["cash_usd"], "50")
        with self.assertRaises(MarvelError):
            self.apply(journal, cycle.event("buy_fill", 1))

    def test_contradictory_post_settlement_evidence_latches_review_without_double_credit(self):
        base = self.closed()
        journal = self.cleanup(base, "status", 9, status="FAIL")
        self.assertTrue(journal["state"]["review_required"])
        self.assertIn("CONVERSION_REVIEW", {h["code"] for h in journal["state"]["risk"]["halts"]})
        self.assertEqual(journal["state"]["book"]["cash_usd"], base["state"]["book"]["cash_usd"])
        self.assertEqual(len(journal["state"]["risk"]["closed_trades"]), 1)

    def test_late_settlement_waits_for_fx_and_keeps_boundary_review(self):
        journal = self.accepted()
        before = journal["state"]["book"]["cash_usd"]
        journal = self.cleanup(journal, "status", 20)
        journal = self.cleanup(journal, "balances", 21)
        self.assertGreater(Decimal(journal["state"]["pending_conversion_usdt"]), 0)
        self.assertEqual(journal["state"]["book"]["cash_usd"], before)
        self.assertTrue(active_claims(journal["state"]["risk"]))
        self.assertEqual(journal, self.restore(journal))
        journal = self.apply(journal, cycle.event("mark", 22))
        self.assertFalse(active_claims(journal["state"]["risk"]))
        self.assertIn("PERIOD_REVIEW", {h["code"] for h in journal["state"]["risk"]["halts"]})
        self.assertEqual(journal["state"]["risk"]["day_start_equity"], "50")

    def test_settlement_fx_breach_is_recorded_not_hidden_by_old_floor(self):
        journal = self.accepted(exit="0")
        journal = self.cleanup(journal, "status", 20)
        journal = self.cleanup(journal, "balances", 21)
        journal = self.apply(journal, cycle.event("depeg_mark", 22))
        risk = journal["state"]["risk"]
        self.assertLess(Decimal(risk["realized_net_pnl_usd"]), -Decimal(risk["claims"][0]["plan"]["loss_reserved_usd"]))
        self.assertTrue({"WEEKLY_LOSS", "REALIZED_RESERVATION_BREACH"} <= {h["code"] for h in risk["halts"]})
        self.assertEqual(risk["claims"][0]["status"], "closed")

    def test_unresolved_claim_blocks_another_entry(self):
        journal = self.accepted()
        recipe = journal["recipe"]
        request = {"proposal": {"id": fingerprint("second"), "symbol": "BTCUSDT", "side": "BUY", "quantity": None, "stop_price": "78000"},
                   "market": cycle._market(recipe, 6)}
        decision = evaluate(journal["state"]["risk"], self.cfg, self.profile, request, timestamp(cycle._when(6)), history=journal["state"]["history"])
        self.assertFalse(decision["risk_check_passed"])
        self.assertIn("POSITION_OR_PENDING_LIMIT", decision["reasons"])

    def test_changed_profile_or_config_cannot_restore_existing_journal(self):
        journal = self.prepared()
        self.document["risk"]["per_trade_cap_usd"] = "0.5"
        self.write_config()
        with self.assertRaises(MarvelError):
            cycle.audit(journal, self.config(), self.profile)

    def test_forged_state_rehashed_outer_record_fails_replay(self):
        journal = self.accepted()
        journal["state"]["risk"]["claims"][0]["status"] = "closed"
        journal["record_id"] = fingerprint({k: v for k, v in journal.items() if k != "record_id"})
        with self.assertRaises(MarvelError):
            self.restore(journal)

    def test_malformed_conflicting_and_regressing_events_are_atomic(self):
        journal = self.prepared()
        before = deepcopy(journal)
        items = [cycle.event("wait", 0), cycle.event("wait", True), cycle.event("buy_fill", 5),
                 {**cycle.event("wait", 5), "payload": {"cash_usd": "500"}},
                 {**journal["events"][-1], "at": 5}]
        for item in items:
            with self.subTest(item=item), self.assertRaises(MarvelError):
                self.apply(journal, item)
            self.assertEqual(journal, before)

    def test_journal_is_bounded_and_strict_json(self):
        journal = self.start()
        for at in range(cycle.MAX_EVENTS):
            journal = self.apply(journal, cycle.event("wait", at))
        with self.assertRaises(MarvelError):
            self.apply(journal, cycle.event("wait", cycle.MAX_EVENTS))
        for raw in (b" " * (cycle.MAX_BYTES + 1), b'{"a":1,"a":2}', b"[]"):
            with self.assertRaises(MarvelError):
                cycle.restore(raw, self.cfg, self.profile)

    def test_original_v1_converter_and_bounded_v2_are_compatible(self):
        old = conversion.new()
        self.assertEqual(old["recipe"]["schema_version"], 1)
        self.assertEqual(conversion.restore(canonical(old).encode("ascii")), old)
        new = conversion.new(initial_btc="0.00004995")
        self.assertEqual(conversion.restore(canonical(new).encode("ascii")), new)
        for amount in ("0", "0.00010001", "0.000009999", "NaN", None):
            if amount is None:
                continue
            with self.assertRaises(MarvelError):
                conversion.new(initial_btc=amount)

    def test_low_decimal_precision_does_not_change_cycle_accounting(self):
        expected = self.closed()
        with localcontext() as context:
            context.prec = 6
            actual = self.start()
            for item in expected["events"]:
                actual = self.apply(actual, item)
        self.assertEqual(actual, expected)

    def test_demo_has_only_invented_outcomes_and_never_accesses_network_or_owner_db(self):
        with patch("socket.socket", side_effect=AssertionError("network")), patch("sqlite3.connect", side_effect=AssertionError("owner db")):
            result = cycle.demo(self.cfg, self.profile)
        self.assertEqual(result["case_count"], 32)
        self.assertEqual(result["out_of_sample_strategy_trades"], 0)
        for field in ("owner_account_used", "phase5_historical_gate_passed", "actual_complete_exit_qualified", "entries_allowed", "usd_fx_accounting_qualified"):
            self.assertFalse(result[field])
        self.assertFalse(self.cfg.database.exists())


class CycleCommandTests(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def test_cycle_demo_preserves_existing_account_and_halt(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        before = sha256(self.config().database.read_bytes()).hexdigest()
        result = self.payload("research", "cycle-demo")
        self.assertEqual(result["cycle_demo"]["case_count"], 32)
        self.assertFalse(result["entries_allowed"])
        self.assertEqual(sha256(self.config().database.read_bytes()).hexdigest(), before)
        self.assertEqual(self.payload("status")["halt_reasons"], ["operator"])

    def test_cycle_command_has_no_execution_or_input_override(self):
        for flag in ("--allow-network", "--live", "--accept", "--input", "--risk"):
            self.assertEqual(self.run_cli("research", "cycle-demo", flag).returncode, 2)
        self.assertFalse(self.config().database.exists())
