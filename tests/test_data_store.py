import json
from unittest.mock import patch

from marvel.data.quality import digest
from marvel.data.store import SnapshotStore
from marvel.errors import DataError
from tests.data_helpers import PROVENANCE, encoded, params, rows
from tests.helpers import WorkspaceCase


class SnapshotTests(WorkspaceCase):
    def snapshots(self):
        return SnapshotStore(self.root)

    def save(self):
        return self.snapshots().save(encoded(rows()), params(), provenance=PROVENANCE)

    def test_roundtrip_and_repeated_import_are_identical(self):
        manifest = self.save()
        self.assertEqual(manifest, self.save())
        loaded, history = self.snapshots().load(manifest["snapshot_id"])
        self.assertEqual(loaded, manifest)
        self.assertEqual(len(history.candles), 72)
        self.assertEqual(len(self.snapshots().inventory()), 1)
        self.assertFalse((self.root / "var/paper.sqlite3").exists())
        self.assertEqual(manifest["attribution"]["provider"], "Local synthetic engineering fixture")

    def test_raw_normalized_and_manifest_corruption_rejected_without_repair(self):
        for filename in ("raw.bin", "candles.jsonl", "manifest.json"):
            manifest = self.save()
            path = self.snapshots().root / manifest["snapshot_id"] / filename
            original = path.read_bytes()
            damaged = original + b" "
            path.write_bytes(damaged)
            # Manifest whitespace has no semantic effect; damage a value instead.
            if filename == "manifest.json":
                damaged = original.replace(b'"completed_candles":72', b'"completed_candles":73')
                path.write_bytes(damaged)
            for action in (lambda: self.snapshots().load(manifest["snapshot_id"]), self.save):
                with self.assertRaises(DataError):
                    action()
            self.assertEqual(path.read_bytes(), damaged)
            path.write_bytes(original)

    def test_identifier_path_traversal_and_missing_snapshot_rejected(self):
        for identifier in ("../config/paper.json", "a" * 63, "0" * 64):
            with self.assertRaises(DataError):
                self.snapshots().load(identifier)

    def test_validation_failure_publishes_no_snapshot(self):
        with self.assertRaises(DataError):
            self.snapshots().save(b"[]", params(), provenance=PROVENANCE)
        self.assertEqual(self.snapshots().inventory(), [])

    def test_publication_failure_leaves_no_visible_snapshot(self):
        with patch("marvel.data.store.Path.rename", side_effect=OSError("SECRET_STORAGE_ERROR")):
            with self.assertRaises(DataError):
                self.save()
        self.assertEqual(self.snapshots().inventory(), [])
        self.assertEqual(list(self.snapshots().root.iterdir()), [])

    def test_snapshot_io_cannot_change_account_or_halts(self):
        store = self.store()
        store.initialize()
        store.pause("operator")
        before = store.read()
        self.save()
        self.snapshots().inventory()
        self.assertEqual(store.read(), before)

    def test_observations_are_separate_content_addressed_research_files(self):
        identifier = self.snapshots().save_observation({"symbol": "BTCUSDT"})
        self.assertEqual(identifier, self.snapshots().save_observation({"symbol": "BTCUSDT"}))
        path = self.snapshots().root / "observations" / (identifier + ".json")
        self.assertEqual(digest(path.read_bytes()), identifier)
        self.assertFalse(json.loads(path.read_bytes())["execution_eligible"])
