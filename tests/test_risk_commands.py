import json
import subprocess
import sys
from unittest.mock import patch
import contextlib
import io

from marvel.cli import main
from marvel.clock import utc_now
from marvel.risk.model import canonical
from marvel.errors import StateError
from tests.helpers import ROOT, WorkspaceCase
from tests.risk_helpers import proposal


class RiskCommandTests(WorkspaceCase):
    def cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "marvel", "--workspace", str(self.root), *arguments],
                              cwd=ROOT, capture_output=True, text=True, timeout=20)

    def request_file(self):
        path = self.root / "request.json"
        path.write_text(canonical(proposal(utc_now())), encoding="utf-8")
        return str(path)

    def test_check_without_account_fails_without_initializing(self):
        result = self.cli("risk", "check", "--input", self.request_file())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "STATE_MISSING")
        self.assertFalse(self.config().database.exists())

    def test_check_is_read_only_and_reservation_persists_across_processes(self):
        self.assertEqual(self.cli("init").returncode, 0)
        before = self.config().database.read_bytes()
        path = self.request_file()
        result = self.cli("risk", "check", "--input", path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["risk_decision"]["risk_check_passed"])
        self.assertEqual(self.config().database.read_bytes(), before)
        reserved = self.cli("risk", "reserve", "--input", path)
        self.assertEqual(reserved.returncode, 0, reserved.stdout)
        claim = json.loads(reserved.stdout)["risk_operation"]["reservation_id"]
        status = json.loads(self.cli("risk", "status").stdout)
        self.assertEqual(status["risk"]["reservations"][0]["id"], claim)
        self.assertFalse(status["entries_allowed"])
        self.assertEqual(self.cli("risk", "release", "--id", claim).returncode, 0)

    def test_no_accounting_setter_submission_resume_or_live_command(self):
        for command in ("reconcile", "submitted", "unknown", "set-equity", "resume", "live"):
            response = self.cli("risk", command)
            self.assertEqual(response.returncode, 2)
            self.assertEqual(json.loads(response.stdout)["error"]["code"], "ARGUMENTS_INVALID")

    def test_secret_in_invalid_request_or_arguments_is_redacted(self):
        self.assertEqual(self.cli("init").returncode, 0)
        secret = "SECRET_RISK_VALUE_DO_NOT_ECHO"
        path = self.root / "request.json"
        path.write_text('{"api_key":"' + secret + '"}', encoding="utf-8")
        response = self.cli("risk", "reserve", "--input", str(path))
        self.assertEqual(response.returncode, 2)
        response2 = self.cli("risk", "release", "--id", secret)
        self.assertEqual(response2.returncode, 2)
        logs = self.config().log.read_text(encoding="utf-8")
        self.assertNotIn(secret, response.stdout + response.stderr + response2.stdout + response2.stderr + logs)

    def test_example_is_explicitly_synthetic_and_creates_no_account(self):
        response = self.cli("risk", "example")
        self.assertEqual(response.returncode, 0, response.stdout)
        path = self.root / "var/examples/btc-risk-request.json"
        example = json.loads(path.read_bytes())
        self.assertEqual(example["market"]["kind"], "synthetic")
        self.assertFalse(self.config().database.exists())

    def test_risk_commands_use_no_network(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--workspace", str(self.root), "init"]), 0)
            self.assertEqual(main(["--workspace", str(self.root), "risk", "check", "--input", self.request_file()]), 0)
            self.assertEqual(main(["--workspace", str(self.root), "risk", "status"]), 0)


class RiskMigrationTests(WorkspaceCase):
    def legacy_v2(self):
        from marvel.clock import stamp
        from tests.helpers import NOW
        self.legacy_database()
        store = self.store()
        with store._connect(write=True) as connection, store._transaction(connection):
            store._apply_v2(connection, stamp(NOW))

    def test_v2_requires_explicit_migration_and_preserves_loss_high_water_and_manual_halt(self):
        from decimal import Decimal
        self.legacy_v2()
        with self.assertRaisesRegex(StateError, "migration"):
            self.store().read()
        after, changed = self.store().migrate()
        self.assertTrue(changed)
        self.assertEqual(after.schema_version, 3)
        self.assertEqual(after.equity_usd, Decimal("45"))
        self.assertEqual(after.high_water_usd, Decimal("50"))
        self.assertEqual(after.halts, ("operator",))
        self.assertEqual(after.revision, 1)
        self.assertIn({"code": "PEAK_DRAWDOWN", "scope": "global"}, after.risk_state["halts"])
        self.assertFalse(self.store().migrate()[1])

    def test_v2_migration_audit_failure_rolls_back_ddl_and_financial_state(self):
        from contextlib import closing
        import sqlite3
        from marvel.errors import StateError
        self.legacy_v2()
        self.sql("""CREATE TRIGGER fail_v3 BEFORE INSERT ON journal WHEN NEW.event = 'schema_migrated'
            AND NEW.payload LIKE '%"to":3%' BEGIN SELECT RAISE(ABORT, 'SECRET_V3_FAILURE'); END""")
        with self.assertRaises(StateError):
            self.store().migrate()
        with closing(sqlite3.connect(self.config().database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'risk_state'").fetchone())
            self.assertEqual(connection.execute("SELECT equity FROM account").fetchone()[0], "45")

    def test_missing_risk_profile_cannot_initialize_or_half_migrate(self):
        from marvel.errors import RiskError
        path = self.root / "config/btc-paper-risk.json"
        raw = path.read_bytes()
        path.write_bytes(b"{}")
        with self.assertRaises(RiskError):
            self.store().initialize()
        self.assertFalse(self.config().database.exists())
        path.write_bytes(raw)
        self.legacy_v2()
        path.write_bytes(b"{}")
        with self.assertRaises(RiskError):
            self.store().migrate()
