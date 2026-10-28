from copy import deepcopy
from hashlib import sha256
import json

from marvel.clock import utc_now
from tests import test_cli as command_fixtures
from tests.helpers import WorkspaceCase
from tests.risk_helpers import proposal, token


class PaperCommandTests(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def input_file(self, document):
        path = self.root / "input.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def test_read_only_status_does_not_initialize_execution_or_create_missing_state(self):
        self.assertEqual(self.run_cli("paper", "status").returncode, 2)
        self.assertFalse(self.config().database.exists())
        self.payload("init")
        before = self.config().database.read_bytes()
        result = self.payload("paper", "status")
        self.assertFalse(result["paper"]["paper_initialized"])
        self.assertEqual(self.config().database.read_bytes(), before)

    def test_disposable_demo_uses_no_owner_balance_and_keeps_live_disabled(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        before = sha256(self.config().database.read_bytes()).hexdigest()
        result = self.payload("paper", "demo")
        self.assertTrue(result["paper_demo"]["disposable_account"])
        self.assertFalse(result["paper_demo"]["owner_account_used"])
        self.assertFalse(result["entries_allowed"])
        self.assertFalse(result["live_execution_available"])
        self.assertIn("PAPER_LIQUIDATION_BLOCKED", result["paper_demo"]["paper"]["risk"]["automatic_halt_reasons"])
        self.assertEqual(sha256(self.config().database.read_bytes()).hexdigest(), before)
        self.assertEqual(self.payload("status")["halt_reasons"], ["operator"])

    def test_order_and_events_survive_actual_command_process_restarts(self):
        self.payload("init")
        self.payload("paper", "init")
        request = proposal(utc_now(), quantity="0.09")
        identity = request["proposal"]["id"]
        submitted = self.payload("paper", "submit", "--input", self.input_file(request))
        self.assertTrue(submitted["paper_operation"]["paper_submitted"])
        event = {"id": token("unknown"), "kind": "timeout", "order_id": identity, "quantity": None, "market": None}
        self.payload("paper", "event", "--input", self.input_file(event))
        self.assertTrue(self.payload("paper", "status")["paper"]["recovery_required"])
        event.update(id=token("recovered"), kind="recover", market=proposal(utc_now())["market"])
        self.payload("paper", "event", "--input", self.input_file(event))
        event.update(id=token("filled"), kind="buy_fill", quantity="0.09", market=proposal(utc_now())["market"])
        self.payload("paper", "event", "--input", self.input_file(event))
        final = self.payload("paper", "status")["paper"]
        self.assertFalse(final["recovery_required"])
        self.assertEqual(final["ledger"]["orders"][0]["status"], "filled")
        self.assertEqual(final["risk"]["closed_trades"], 0)

    def test_submission_requires_explicit_paper_initialization(self):
        self.payload("init")
        before = self.config().database.read_bytes()
        result = self.run_cli("paper", "submit", "--input", self.input_file(proposal(utc_now())))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "RISK_TRANSITION")
        self.assertEqual(self.config().database.read_bytes(), before)

    def test_malformed_event_fields_are_fixed_errors_without_tracebacks_or_mutation(self):
        self.payload("init")
        self.payload("paper", "init")
        baseline = {"id": token("invalid"), "kind": "quote", "order_id": None, "quantity": None, "market": proposal(utc_now())["market"]}
        for name, value in (("id", []), ("kind", {}), ("quantity", []), ("market", []), ("order_id", {})):
            event = deepcopy(baseline)
            event[name] = value
            before = self.config().database.read_bytes()
            result = self.run_cli("paper", "event", "--input", self.input_file(event))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertNotIn("Traceback", result.stdout)
            self.assertEqual(self.config().database.read_bytes(), before)

    def test_oversized_and_duplicate_key_inputs_fail_before_mutation(self):
        self.payload("init")
        self.payload("paper", "init")
        path = self.root / "input.json"
        for raw in ('{"id":"first","id":"second"}', " " * 16_385):
            path.write_text(raw, encoding="utf-8")
            before = self.config().database.read_bytes()
            result = self.run_cli("paper", "event", "--input", str(path))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(self.config().database.read_bytes(), before)

    def test_existing_unsent_claim_prevents_execution_initialization(self):
        self.payload("init")
        request = proposal(utc_now())
        self.payload("risk", "reserve", "--input", self.input_file(request))
        before = self.config().database.read_bytes()
        result = self.run_cli("paper", "init")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "RISK_TRANSITION")
        self.assertEqual(self.config().database.read_bytes(), before)
