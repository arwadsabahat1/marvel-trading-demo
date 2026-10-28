from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from marvel.config import load_config
from marvel.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="phase1-", dir=temp_root)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(temp_root.resolve()))
        self.addCleanup(self.temp.cleanup)
        (self.root / "config").mkdir()
        self.path = self.root / "config/paper.json"
        (self.root / "config/btc-paper-risk.json").write_bytes((ROOT / "config/btc-paper-risk.json").read_bytes())
        self.document = json.loads((ROOT / "config/paper.json").read_text(encoding="utf-8"))
        self.write_config()

    def write_config(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def config(self):
        return load_config(self.path, self.root)

    def store(self, clock=lambda: NOW):
        return StateStore(self.config(), clock)

    def sql(self, query, parameters=()):
        with closing(sqlite3.connect(self.config().database)) as connection, connection:
            connection.execute(query, parameters)

    def legacy_database(self):
        config = self.config()
        config.database.parent.mkdir(parents=True, exist_ok=True)
        fixture = (ROOT / "tests/fixtures/schema-v1.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(config.database)) as connection, connection:
            connection.executescript(fixture.replace("CONFIG_HASH", config.fingerprint))
