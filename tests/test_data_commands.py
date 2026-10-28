import contextlib
import io
import json
import subprocess
import sys
from unittest.mock import patch

from marvel.cli import main
from tests.helpers import ROOT, WorkspaceCase


class ResearchCommandTests(WorkspaceCase):
    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "marvel", "--workspace", str(self.root), "data", *arguments],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)

    def invoke(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--workspace", str(self.root), "data", *arguments])
        return code, json.loads(output.getvalue())

    def import_args(self):
        return ["import", "--input", str(ROOT / "tests/fixtures/btc-synthetic-ms.json"), "--format", "rest-json",
                "--source", "synthetic", "--unit", "ms", "--start", "2026-03-07T00:00:00Z",
                "--end", "2026-03-10T00:00:00Z", "--as-of", "2026-03-10T00:00:00Z"]

    def test_import_inspect_and_replay_survive_process_restart(self):
        imported = self.run_cli(*self.import_args())
        self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
        identifier = json.loads(imported.stdout)["manifest"]["snapshot_id"]
        inspected = self.run_cli("inspect", "--dataset", identifier)
        self.assertEqual(inspected.returncode, 0, inspected.stdout)
        replayed = self.run_cli("replay", "--dataset", identifier, "--as-of", "2026-03-09T00:00:00Z")
        self.assertEqual(replayed.returncode, 0, replayed.stdout)
        result = json.loads(replayed.stdout)
        self.assertFalse(result["entries_allowed"])
        self.assertEqual(result["replay"]["available_candles"], 48)
        self.assertEqual(result["replay"]["features"]["sma"], "138.5")
        self.assertFalse(self.config().database.exists())

    def test_offline_import_list_replay_never_open_socket_or_account(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            code, imported = self.invoke(self.import_args())
            self.assertEqual(code, 0)
            self.assertEqual(self.invoke(["list"])[0], 0)
            self.assertEqual(self.invoke(["replay", "--dataset", imported["manifest"]["snapshot_id"], "--as-of", "2026-03-09T00:00:00Z"])[0], 0)
        self.assertFalse(self.config().database.exists())

    def test_network_commands_rejected_without_explicit_opt_in(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            for command in (["observe"], ["fetch-recent"], ["fetch-archive", "--month", "2026-09"],
                            ["fetch-rest", "--start", "2026-09-01T00:00:00Z", "--end", "2026-09-02T00:00:00Z"]):
                code, result = self.invoke(command)
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "NETWORK_OPT_IN_REQUIRED")

    def test_bad_input_and_secret_dataset_argument_are_redacted(self):
        secret = "SECRET_DATA_ARGUMENT"
        for arguments in (["inspect", "--dataset", secret], ["import", "--input", secret, "--format", "rest-json",
                          "--source", "synthetic", "--unit", "ms", "--start", secret, "--end", secret, "--as-of", secret]):
            response = self.run_cli(*arguments)
            self.assertEqual(response.returncode, 2)
            logs = self.config().log.read_text(encoding="utf-8")
            self.assertNotIn(secret, response.stdout + response.stderr + logs)

    def test_unsupported_market_cannot_be_requested(self):
        response = self.run_cli("fetch-recent", "--symbol", "GOLD")
        self.assertEqual(response.returncode, 2)
        self.assertEqual(json.loads(response.stdout)["error"]["code"], "ARGUMENTS_INVALID")

    def test_data_operations_preserve_saved_paper_state(self):
        store = self.store()
        store.initialize()
        store.pause("maintenance")
        before = store.read()
        self.assertEqual(self.run_cli(*self.import_args()).returncode, 0)
        self.assertEqual(self.run_cli("list").returncode, 0)
        self.assertEqual(store.read(), before)
