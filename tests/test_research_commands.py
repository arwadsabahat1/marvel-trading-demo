from hashlib import sha256
import json

from tests import test_cli as command_fixtures
from tests.helpers import WorkspaceCase
from tests.data_helpers import encoded, params, rows, PROVENANCE
from marvel.data.store import SnapshotStore


class BaselineCommandTests(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def test_plan_is_read_only_and_has_no_risk_fee_or_holdout_override(self):
        result = self.payload("research", "plan")
        self.assertEqual(len(result["research_plan"]["strategies"]), 3)
        self.assertFalse((self.root / "var/research-runs").exists())
        self.assertFalse(self.config().database.exists())
        self.assertEqual(self.run_cli("research", "screen", "--dataset", "a" * 64, "--risk", "5").returncode, 2)
        self.assertEqual(self.run_cli("research", "promote").returncode, 2)
        self.assertEqual(self.run_cli("research", "holdout", "--force").returncode, 2)

    def test_demo_records_all_variants_without_owner_account_or_real_market_claim(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        before = sha256(self.config().database.read_bytes()).hexdigest()
        result = self.payload("research", "demo")
        screen = result["research_screen"]
        self.assertEqual(screen["data_kind"], "synthetic_engineering_only")
        self.assertFalse(screen["owner_account_used"])
        self.assertFalse(screen["phase5_historical_gate_passed"])
        self.assertFalse(screen["holdout_evaluated"])
        self.assertEqual(len(screen["candidates"]), 3)
        listing = self.payload("research", "list")["research_runs"]
        self.assertEqual(len(listing), 1)
        study = self.payload("research", "inspect", "--run", screen["run_id"])["research_study"]
        self.assertEqual(study["holdout"]["range"], [576, 720])
        self.assertEqual(sha256(self.config().database.read_bytes()).hexdigest(), before)
        self.assertEqual(self.payload("status")["halt_reasons"], ["operator"])

    def test_repeating_screen_preserves_one_identical_record_and_report(self):
        first = self.payload("research", "demo")["research_screen"]
        second = self.payload("research", "screen", "--dataset", first["dataset_id"])["research_screen"]
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(self.payload("research", "list")["research_runs"]), 1)
        self.assertFalse(self.config().database.exists())

    def test_missing_small_and_wrong_interval_snapshots_fail_without_account_creation(self):
        result = self.run_cli("research", "screen", "--dataset", "a" * 64)
        self.assertEqual(result.returncode, 2)
        for count, interval in ((72, "1h"), (120, "4h")):
            snapshot = SnapshotStore(self.root).save(encoded(rows(count, interval=interval)), params(count, interval=interval), provenance=PROVENANCE)
            result = self.run_cli("research", "screen", "--dataset", snapshot["snapshot_id"])
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "RESEARCH_DATA")
        self.assertFalse(self.config().database.exists())

    def test_malformed_identifiers_are_sanitized_and_cannot_escape_workspace(self):
        secret = "SECRET_SENTINEL_NEVER_ECHO_513"
        for command, field in (("screen", "--dataset"), ("inspect", "--run")):
            result = self.run_cli("research", command, field, "../../" + secret)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")

    def test_tampered_human_report_is_not_silently_regenerated(self):
        first = self.payload("research", "demo")["research_screen"]
        report = self.root / first["report_file"]
        report.write_text("made up profit", encoding="utf-8")
        result = self.run_cli("research", "inspect", "--run", first["run_id"])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "RESEARCH_RECORD")
        self.assertEqual(report.read_text(encoding="utf-8"), "made up profit")
