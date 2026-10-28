from copy import deepcopy
from decimal import Decimal, localcontext
import json
from unittest import TestCase
from unittest.mock import patch

from marvel.errors import MarvelError
from marvel.research.conversion import apply, audit, demo, fixture_event as event, new, restore, _quote_reason
from marvel.risk.model import canonical, fingerprint
from tests import test_cli as command_fixtures
from tests.helpers import WorkspaceCase


def replay(*events):
    journal = new()
    for item in events:
        journal = apply(journal, item)
    return journal


def accepted():
    return replay(event("quote", 0), event("accept", 1))


class ConversionTests(TestCase):
    def test_only_final_status_and_matching_balances_can_release_the_holding(self):
        journal = accepted()
        for item in (event("status", 2, status="PROCESS"), event("status", 3, status="ACCEPT_SUCCESS"),
                     event("status", 4)):
            journal = apply(journal, item)
            self.assertEqual(journal["state"]["holding_btc"], "0.00000989")
            self.assertEqual(journal["state"]["cash_usdt"], "0")
            self.assertTrue(journal["state"]["claim_held"])
        journal = apply(journal, event("balances", 5))
        self.assertEqual(journal["state"]["holding_btc"], "0")
        self.assertEqual(journal["state"]["cash_usdt"], "0.78")
        self.assertFalse(journal["state"]["claim_held"])
        self.assertEqual(journal["state"]["phase"], "settled")

    def test_balance_alone_never_proves_success_or_releases_claim(self):
        journal = apply(accepted(), event("balances", 2))
        self.assertTrue(journal["state"]["claim_held"])
        self.assertEqual(journal["state"]["cash_usdt"], "0")
        self.assertIn("TERMINAL_RECEIPT_REQUIRED", journal["state"]["rejections"])

    def test_timeout_is_restartable_without_accepting_again_even_after_quote_expiry(self):
        journal = apply(accepted(), event("timeout", 2))
        journal = restore(canonical(journal).encode("ascii"))
        journal = apply(journal, event("accept", 50))
        journal = apply(journal, event("quote", 51, quote_id=fingerprint("fresh"), issued_at=51, valid_until=61, reference_at=51))
        self.assertEqual(journal["state"]["phase"], "unknown")
        self.assertEqual(journal["state"]["acceptance_intents"], 1)
        self.assertTrue(journal["state"]["claim_held"])
        journal = apply(journal, event("status", 52))
        journal = apply(journal, event("balances", 53))
        self.assertEqual(journal["state"]["phase"], "settled")

    def test_duplicate_evidence_never_credits_twice_including_new_delivery_id(self):
        receipt, balances = event("status", 3), event("balances", 4)
        journal = replay(event("quote", 0), event("accept", 1), receipt, balances)
        self.assertEqual(apply(journal, balances), journal)
        journal = apply(journal, event("status", 5, label="redelivery"))
        journal = apply(journal, event("balances", 6, label="redelivery"))
        self.assertEqual(journal["state"]["cash_usdt"], "0.78")
        self.assertEqual(journal["state"]["acceptance_intents"], 1)

    def test_reused_event_identifier_with_changed_input_is_rejected_without_mutation(self):
        journal = accepted()
        before = deepcopy(journal)
        changed = deepcopy(journal["events"][-1])
        changed["at"] = 2
        with self.assertRaises(MarvelError):
            apply(journal, changed)
        self.assertEqual(journal, before)

    def test_failure_needs_unchanged_balance_proof_before_another_attempt(self):
        journal = apply(accepted(), event("status", 3, status="FAIL"))
        journal = apply(journal, event("accept", 4))
        self.assertTrue(journal["state"]["claim_held"])
        self.assertEqual(journal["state"]["acceptance_intents"], 1)
        journal = apply(journal, event("balances", 5, holding_btc="0.00000989", cash_usdt="0"))
        self.assertFalse(journal["state"]["claim_held"])
        self.assertEqual(journal["state"]["phase"], "idle")
        journal = apply(journal, event("quote", 6))
        self.assertIn("QUOTE_ALREADY_CONSUMED", journal["state"]["rejections"])
        qid = fingerprint("second quote")
        journal = apply(journal, event("quote", 7, quote_id=qid, issued_at=7, valid_until=17, reference_at=7))
        journal = apply(journal, event("accept", 8, quote_id=qid))
        self.assertEqual(journal["state"]["acceptance_intents"], 2)
        self.assertTrue(journal["state"]["claim_held"])

    def test_failed_conversion_with_changed_balances_latches_review(self):
        journal = apply(accepted(), event("status", 3, status="FAIL"))
        journal = apply(journal, event("balances", 4))
        self.assertTrue(journal["state"]["claim_held"])
        self.assertTrue(journal["state"]["review_required"])
        self.assertEqual(journal["state"]["holding_btc"], "0.00000989")

    def test_conflicting_late_success_after_verified_failure_blocks_a_fresh_cleanup(self):
        for requote in (False, True):
            with self.subTest(requote=requote):
                journal = replay(event("quote", 0), event("accept", 1), event("status", 2, status="FAIL"),
                                 event("balances", 3, holding_btc="0.00000989", cash_usdt="0"))
                if requote:
                    journal = apply(journal, event("quote", 4, quote_id=fingerprint("new quote"),
                                                   issued_at=4, valid_until=14, reference_at=4))
                journal = apply(journal, event("status", 5))
                self.assertTrue(journal["state"]["review_required"])
                self.assertTrue(journal["state"]["claim_held"])
                self.assertEqual(journal["state"]["holding_btc"], "0.00000989")
                self.assertEqual(journal["state"]["cash_usdt"], "0")

    def test_receipt_and_balance_mismatches_preserve_exposure_and_latch_review(self):
        cases = [event("status", 3, from_btc="0.00000988"), event("status", 3, to_usdt="0.77"),
                 event("status", 3, quote_id=fingerprint("other quote")),
                 event("status", 3, order_id=fingerprint("other order")),
                 event("balances", 3, cash_usdt="0.77"),
                 event("balances", 3, holding_btc="0.00000001"),
                 event("balances", 3, order_id=fingerprint("other order"))]
        for bad in cases:
            with self.subTest(kind=bad["kind"], payload=bad["payload"]):
                journal = apply(accepted(), event("status", 2))
                journal = apply(journal, bad)
                self.assertTrue(journal["state"]["review_required"])
                self.assertTrue(journal["state"]["claim_held"])
                self.assertEqual(journal["state"]["cash_usdt"], "0")
                journal = apply(journal, event("balances", 4))
                self.assertTrue(journal["state"]["review_required"])
                self.assertEqual(journal["state"]["holding_btc"], "0.00000989")

    def test_older_status_cannot_downgrade_a_terminal_receipt(self):
        journal = apply(accepted(), event("status", 2))
        journal = apply(journal, event("status", 3, status="PROCESS"))
        self.assertEqual(journal["state"]["phase"], "awaiting_success_balance")
        journal = apply(journal, event("balances", 4))
        self.assertEqual(journal["state"]["phase"], "settled")

    def test_conflicting_terminal_evidence_does_not_reverse_or_duplicate_recovery(self):
        for settle in (False, True):
            with self.subTest(settle=settle):
                journal = apply(accepted(), event("status", 2))
                if settle:
                    journal = apply(journal, event("balances", 3))
                before = deepcopy(journal["state"])
                journal = apply(journal, event("status", 4, status="FAIL"))
                self.assertTrue(journal["state"]["review_required"])
                self.assertEqual(journal["state"]["holding_btc"], before["holding_btc"])
                self.assertEqual(journal["state"]["cash_usdt"], before["cash_usdt"])

    def test_expiry_and_reference_freshness_are_rechecked_at_acceptance(self):
        cases = [(event("quote", 0), event("accept", 10), "QUOTE_EXPIRED"),
                 (event("quote", 0, valid_until=60), event("accept", 11), "QUOTE_EXPIRED"),
                 (event("quote", 10, issued_at=10, valid_until=20), event("accept", 11), "REFERENCE_STALE")]
        for quote, accept, reason in cases:
            with self.subTest(reason=reason):
                journal = replay(quote, accept)
                self.assertEqual(journal["state"]["acceptance_intents"], 0)
                self.assertIn(reason, journal["state"]["rejections"])

    def test_quote_limits_reject_unrealistic_proceeds_and_partial_holding(self):
        cases = [({"to_usdt": "0.70"}, "QUOTE_DEVIATION_EXCEEDED"),
                 ({"to_usdt": "0.82"}, "QUOTE_DEVIATION_EXCEEDED"),
                 ({"from_btc": "0.000009"}, "ENTIRE_HOLDING_REQUIRED"),
                 ({"from_btc": "0.00001"}, "ENTIRE_HOLDING_REQUIRED"),
                 ({"to_usdt": "0.009"}, "DESTINATION_MINIMUM_FAILED"),
                 ({"to_usdt": "0.780000001"}, "ASSUMED_PRECISION_FAILED"),
                 ({"issued_at": 1}, "QUOTE_TIME_INVALID"),
                 ({"reference_at": 1}, "QUOTE_TIME_INVALID"),
                 ({"valid_until": 61}, "QUOTE_TIME_INVALID")]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                journal = replay(event("quote", 0, **changes))
                self.assertIn(reason, journal["state"]["rejections"])
                self.assertEqual(journal["state"]["phase"], "idle")
                self.assertEqual(journal["state"]["holding_btc"], "0.00000989")

    def test_source_minimum_and_tiny_base_precision_are_separate_checks(self):
        body = event("quote", 0, from_btc="0.00000012")["payload"]
        self.assertEqual(_quote_reason(body, 0, "0.00000012"), "SOURCE_MINIMUM_FAILED")
        body["from_btc"] = "0.000000129"
        self.assertEqual(_quote_reason(body, 0, "0.000000129"), "ASSUMED_PRECISION_FAILED")

    def test_same_quote_id_cannot_be_reassigned_before_acceptance(self):
        journal = replay(event("quote", 0), event("quote", 1, to_usdt="0.79"))
        self.assertTrue(journal["state"]["review_required"])
        journal = apply(journal, event("accept", 2))
        self.assertEqual(journal["state"]["acceptance_intents"], 0)

    def test_clock_regression_and_malformed_events_do_not_mutate_the_journal(self):
        journal = accepted()
        before = deepcopy(journal)
        cases = [event("status", 0), event("status", True), event("status", 2, status=[]),
                 event("quote", 2, from_btc=True), event("quote", 2, to_usdt="NaN"),
                 event("quote", 2, to_usdt="Infinity"), event("quote", 2, to_usdt="1e-8"),
                 event("quote", 2, to_usdt="1" * 41), event("quote", 2, reference_at=False),
                 event("accept", 2, quote_id=[]), event("balances", 2, cash_usdt=-1)]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(MarvelError):
                apply(journal, bad)
            self.assertEqual(journal, before)

    def test_state_tampering_is_rejected_even_if_outer_hash_is_recomputed(self):
        journal = accepted()
        journal["state"]["claim_held"] = False
        journal["record_id"] = fingerprint({key: value for key, value in journal.items() if key != "record_id"})
        with self.assertRaises(MarvelError):
            restore(canonical(journal).encode("ascii"))

    def test_recipe_hash_duplicate_history_and_boolean_schema_are_rejected(self):
        original = accepted()
        for change in ("recipe", "policy", "hash", "duplicate", "schema"):
            journal = deepcopy(original)
            if change == "recipe":
                journal["recipe"]["initial_btc"] = "0"
            elif change == "policy":
                journal["recipe"]["policy"]["maximum_quote_deviation_usdt"] = "1"
            elif change == "hash":
                journal["record_id"] = "0" * 64
            elif change == "duplicate":
                journal["events"].append(deepcopy(journal["events"][-1]))
            else:
                journal["recipe"]["schema_version"] = True
            with self.subTest(change=change), self.assertRaises(MarvelError):
                restore(canonical(journal).encode("ascii"))

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_excessive_input(self):
        for raw in (b'{"state":0,"state":1}', b'{"state":NaN}', b'\xff', b'[]', b' ' * 262145):
            with self.subTest(prefix=raw[:20]), self.assertRaises(MarvelError):
                restore(raw)

    def test_journal_budget_fails_before_history_is_truncated(self):
        journal = accepted()
        before = deepcopy(journal)
        with patch("marvel.research.conversion.MAX_EVENTS", 2), self.assertRaises(MarvelError):
            apply(journal, event("timeout", 2))
        self.assertEqual(journal, before)

    def test_decimal_context_cannot_change_the_result(self):
        expected = demo()
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(demo(), expected)

    def test_demo_discloses_invented_inputs_and_does_not_claim_profit(self):
        result = demo()
        self.assertEqual(result, demo())
        self.assertFalse(result["owner_account_used"])
        self.assertFalse(result["profit_demonstrated"])
        self.assertFalse(result["strategy_integration_available"])
        self.assertFalse(result["partial_fill_liquidation_qualified"])
        self.assertEqual(result["real_orders_or_conversion_requests"], 0)
        recovered = result["cases"]["success_after_receipt_and_balances"]["state"]
        self.assertEqual(Decimal(recovered["cash_usdt"]), Decimal("0.78"))
        self.assertEqual(result["cases"]["success_receipt_alone"]["state"]["cash_usdt"], "0")


class ConversionCommands(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def test_demo_never_creates_an_account_or_research_run(self):
        result = self.payload("research", "convert-demo")
        self.assertFalse(result["entries_allowed"])
        self.assertFalse(result["conversion_execution_available"])
        self.assertTrue(result["conversion_research_simulator_available"])
        self.assertEqual(result["conversion_demo"]["kind"], "synthetic_convert_cleanup_demo")
        self.assertFalse(self.config().database.exists())
        self.assertFalse((self.root / "var/research-runs").exists())

    def test_demo_preserves_existing_manual_halt_account_and_financial_configs(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        paths = [self.config().database, self.root / "config/paper.json", self.root / "config/btc-paper-risk.json"]
        before = [path.read_bytes() for path in paths]
        self.payload("research", "convert-demo")
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_demo_rejects_network_execution_and_risk_override_arguments(self):
        for flag in ("--allow-network", "--live", "--force", "--fee-asset", "--max-loss"):
            with self.subTest(flag=flag):
                process = self.run_cli("research", "convert-demo", flag)
                self.assertEqual(process.returncode, 2)
                self.assertEqual(json.loads(process.stdout)["error"]["code"], "ARGUMENTS_INVALID")
                self.assertNotIn("Traceback", process.stderr)
        self.assertFalse(self.config().database.exists())
