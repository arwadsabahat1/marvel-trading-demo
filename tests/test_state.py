from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
import sqlite3

from marvel.errors import StateError
from marvel.state import SCHEMA_VERSION
from tests.helpers import NOW, WorkspaceCase


class StateTests(WorkspaceCase):
    def test_read_missing_state_does_not_initialize(self):
        with self.assertRaisesRegex(StateError, "No paper state"):
            self.store().read()
        self.assertFalse(self.config().database.exists())

    def test_initialization_is_durable_and_idempotent(self):
        first, created = self.store().initialize()
        second, created_again = self.store().initialize()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, second)
        self.assertEqual(first.equity_usd, Decimal("50"))
        self.assertEqual(first.schema_version, SCHEMA_VERSION)
        self.assertEqual(first.journal_events, 3)

    def test_pause_survives_restart_and_day_change(self):
        self.store().initialize()
        paused = self.store().pause("operator")
        tomorrow = self.store(lambda: NOW + timedelta(days=1))
        self.assertEqual(tomorrow.read(), paused)
        self.assertIn("operator", tomorrow.read().halts)
        again = tomorrow.pause("operator")
        self.assertEqual(again, paused)

    def test_config_change_rejects_existing_state_without_reset(self):
        self.store().initialize()
        self.store().pause("incident")
        original_bytes = self.config().database.read_bytes()
        self.document["risk"]["per_trade_cap_usd"] = "0.5"
        self.write_config()
        for action in [lambda: self.store().read(), lambda: self.store().initialize(), lambda: self.store().pause("operator")]:
            with self.assertRaises(StateError) as caught:
                action()
            self.assertEqual(caught.exception.code, "CONFIG_MISMATCH")
        self.assertEqual(self.config().database.read_bytes(), original_bytes)

    def test_corrupt_or_empty_existing_file_is_never_replaced(self):
        for contents in [b"", b"THIS_IS_NOT_A_DATABASE"]:
            with self.subTest(contents=bool(contents)):
                path = self.config().database
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(contents)
                with self.assertRaises(StateError):
                    self.store().initialize()
                self.assertEqual(path.read_bytes(), contents)

    def test_unknown_newer_schema_is_not_downgraded(self):
        self.store().initialize()
        self.sql("PRAGMA user_version = 99")
        before = self.config().database.read_bytes()
        for method in [self.store().read, self.store().migrate]:
            with self.assertRaises(StateError) as caught:
                method()
            self.assertEqual(caught.exception.code, "SCHEMA_UNSUPPORTED")
        self.assertEqual(before, self.config().database.read_bytes())

    def test_wrong_application_database_is_rejected(self):
        self.store().initialize()
        self.sql("PRAGMA application_id = 123")
        with self.assertRaises(StateError):
            self.store().read()

    def test_invalid_stored_money_fails_closed(self):
        self.store().initialize()
        for value in ["NaN", "Infinity", "-1", "1e9"]:
            with self.subTest(value=value):
                self.sql("UPDATE account SET equity = ?", (value,))
                with self.assertRaises(StateError):
                    self.store().read()

    def test_deleted_halt_detected_from_journal(self):
        self.store().initialize()
        self.store().pause("incident")
        self.sql("DELETE FROM halts")
        with self.assertRaises(StateError):
            self.store().read()

    def test_malformed_journal_payload_detected(self):
        self.store().initialize()
        self.sql("UPDATE journal SET payload = ? WHERE event = 'initialized'", ("{SECRET_VALUE",))
        with self.assertRaises(StateError):
            self.store().read()

    def test_duplicate_audit_keys_and_boolean_schema_versions_rejected(self):
        self.store().initialize()
        for payload in ['{"from":99,"from":1,"to":2}', '{"from":true,"to":2}']:
            with self.subTest(payload=payload):
                self.sql("UPDATE journal SET payload = ? WHERE event = 'schema_migrated'", (payload,))
                with self.assertRaises(StateError):
                    self.store().read()

    def test_failed_journal_write_rolls_back_halt_and_revision(self):
        self.store().initialize()
        before = self.store().read()
        self.sql("""CREATE TRIGGER reject_audit BEFORE INSERT ON journal
            BEGIN SELECT RAISE(ABORT, 'TEST_WRITE_FAILURE_SECRET'); END""")
        with self.assertRaises(StateError) as caught:
            self.store().pause("operator")
        self.assertEqual(caught.exception.code, "STATE_IO")
        self.assertEqual(self.store().read(), before)

    def test_clock_regression_does_not_mutate_state(self):
        self.store().initialize()
        backwards = self.store(lambda: NOW - timedelta(seconds=1))
        with self.assertRaises(StateError) as caught:
            backwards.pause("operator")
        self.assertEqual(caught.exception.code, "CLOCK_REGRESSION")
        self.assertEqual(self.store().read().revision, 0)

    def test_unknown_pause_reason_is_not_written(self):
        self.store().initialize()
        with self.assertRaises(StateError):
            self.store().pause("PRIVATE_SECRET_REASON")
        self.assertEqual(self.store().read().revision, 0)

    def test_concurrent_pauses_do_not_lose_a_halt(self):
        self.store().initialize()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.store().pause, reason) for reason in ("operator", "incident")]
            for future in futures:
                future.result(timeout=10)
        state = self.store().read()
        self.assertEqual(set(state.halts), {"operator", "incident"})
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.journal_events, 5)

    def test_explicit_v1_migration_preserves_lower_equity_and_halt(self):
        self.legacy_database()
        with self.assertRaises(StateError) as caught:
            self.store().read()
        self.assertEqual(caught.exception.code, "MIGRATION_REQUIRED")
        after, changed = self.store().migrate()
        self.assertTrue(changed)
        self.assertEqual(after.equity_usd, Decimal("45"))
        self.assertEqual(after.high_water_usd, Decimal("50"))
        self.assertEqual(after.halts, ("operator",))
        self.assertEqual(after.revision, 1)
        self.assertEqual(self.store().read(), after)
        self.assertFalse(self.store().migrate()[1])

    def test_migration_failure_rolls_back_schema_change(self):
        self.legacy_database()
        self.sql("""CREATE TRIGGER reject_migration BEFORE INSERT ON journal
            WHEN NEW.event = 'schema_migrated'
            BEGIN SELECT RAISE(ABORT, 'TEST_MIGRATION_FAILURE'); END""")
        with self.assertRaises(StateError):
            self.store().migrate()
        with closing(sqlite3.connect(self.config().database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(account)")}
            self.assertNotIn("revision", columns)
            self.assertEqual(connection.execute("SELECT equity FROM account").fetchone()[0], "45")
