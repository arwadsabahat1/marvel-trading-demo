import json
import subprocess
import sys
from unittest.mock import patch

from marvel.cli import main
from marvel.errors import LogError
from marvel.events import EventLog
from tests.helpers import ROOT, WorkspaceCase


class CommandTests(WorkspaceCase):
    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "marvel", "--workspace", str(self.root), *arguments], cwd=ROOT, capture_output=True, text=True, timeout=15)

    def payload(self, *arguments):
        result = self.run_cli(*arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_validate_has_no_runtime_side_effects(self):
        result = self.payload("validate")
        self.assertTrue(result["configuration_valid"])
        self.assertFalse(result["entries_allowed"])
        self.assertFalse(result["live_execution_available"])
        self.assertFalse((self.root / "var").exists())

    def test_init_status_and_pause_across_processes(self):
        created = self.payload("init")
        self.assertTrue(created["new_account_created"])
        self.assertFalse(self.payload("init")["new_account_created"])
        self.payload("pause", "--reason", "maintenance")
        status = self.payload("status")
        self.assertEqual(status["account"]["equity"], "50")
        self.assertEqual(status["halt_reasons"], ["maintenance"])
        self.assertFalse(status["entries_allowed"])

    def test_status_missing_database_does_not_create_it(self):
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "STATE_MISSING")
        self.assertFalse(self.config().database.exists())

    def test_no_live_or_resume_command_exists(self):
        for command in ["live", "trade", "resume", "reset"]:
            with self.subTest(command=command):
                result = self.run_cli(command)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(json.loads(result.stdout)["entries_allowed"])

    def test_secret_in_invalid_config_or_argv_is_not_echoed(self):
        secret = "SECRET_SENTINEL_DO_NOT_LOG_ABC123"
        self.document["api_key"] = secret
        self.write_config()
        rejected = self.run_cli("init")
        self.assertEqual(rejected.returncode, 2)
        self.assertNotIn(secret, rejected.stdout + rejected.stderr)
        self.assertFalse((self.root / "var").exists())
        rejected = self.run_cli("status", "--api-key", secret)
        self.assertEqual(rejected.returncode, 2)
        self.assertNotIn(secret, rejected.stdout + rejected.stderr)

    def test_database_error_text_is_not_logged(self):
        self.payload("init")
        self.sql("""CREATE TRIGGER reject_secret BEFORE INSERT ON journal
            BEGIN SELECT RAISE(ABORT, 'SECRET_FROM_DATABASE_ERROR'); END""")
        result = self.run_cli("pause")
        self.assertEqual(result.returncode, 2)
        text = result.stdout + result.stderr + self.config().log.read_text(encoding="utf-8")
        self.assertNotIn("SECRET_FROM_DATABASE_ERROR", text)
        self.assertEqual(self.payload("status")["account"]["revision"], 0)

    def test_structured_logs_have_only_allowed_fields(self):
        self.payload("init")
        self.payload("status")
        records = [json.loads(line) for line in self.config().log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 4)
        self.assertEqual(set(records[0]), {"timestamp_utc", "level", "event", "command", "code"})

    def test_log_rotation_and_unapproved_error_code(self):
        log = EventLog(self.config().log, max_bytes=220)
        try:
            for _ in range(20):
                log.emit("command_completed", "status")
            with self.assertRaises(LogError):
                log.emit("command_failed", "status", code="SECRET_UPPERCASE_TOKEN")
        finally:
            log.close()
        files = list(self.config().log.parent.glob("marvel.jsonl*"))
        self.assertLessEqual(len(files), 4)
        self.assertGreater(len(files), 1)
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET_UPPERCASE_TOKEN", text)
            for line in text.splitlines():
                json.loads(line)

    def test_no_network_needed_for_foundation(self):
        import contextlib
        import io
        with patch("socket.socket", side_effect=AssertionError("Network is forbidden")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--workspace", str(self.root), "init"]), 0)
                self.assertEqual(main(["--workspace", str(self.root), "status"]), 0)

    def test_log_storage_failure_does_not_initialize_database(self):
        self.config().log.parent.parent.mkdir(parents=True)
        self.config().log.parent.write_text("BLOCK_LOG_DIRECTORY", encoding="utf-8")
        result = self.run_cli("init")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "LOG_IO")
        self.assertFalse(self.config().database.exists())
