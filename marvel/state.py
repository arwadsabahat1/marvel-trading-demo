"""Atomic paper account, manual halts and audited risk/synthetic execution state."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import sqlite3
from typing import Callable, Iterator

from .clock import parse_stamp, stamp, utc_now
from .config import Config, amount
from .errors import StateError

APPLICATION_ID = 0x4D415256
SCHEMA_VERSION = 3
PAUSE_REASONS = frozenset({"operator", "maintenance", "incident"})
MIGRATIONS = {1: "Initial paper account and audit journal", 2: "Account revision and migration history",
              3: "Independent paper risk ledger and bound instrument assumptions"}


def _bad_state() -> StateError:
    return StateError("STATE_INVALID", "Saved state is damaged or inconsistent; preserve it for review. No reset was performed.")


def _payload(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _audit_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate audit field")
        result[key] = value
    return result


@dataclass(frozen=True)
class Snapshot:
    initial_equity_usd: Decimal
    equity_usd: Decimal
    high_water_usd: Decimal
    halts: tuple[str, ...]
    revision: int
    created_at: str
    updated_at: str
    journal_events: int
    schema_version: int
    risk_state: dict | None = None
    risk_history: tuple = ()


class StateStore:
    def __init__(self, config: Config, clock: Callable[[], datetime] = utc_now):
        self.config = config
        self.path = config.database
        self.clock = clock

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise StateError("STATE_MISSING", "No paper state exists; use init explicitly. No account was created.")
        connection = None
        try:
            uri = self.path.as_uri() + ("?mode=rw" if write else "?mode=ro")
            connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("PRAGMA synchronous = FULL")
            else:
                connection.execute("PRAGMA query_only = ON")
            yield connection
        except sqlite3.Error as exc:
            raise StateError("STATE_IO", "Database operation failed; no automatic reset or recovery was attempted.") from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _transaction(self, connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def initialize(self) -> tuple[Snapshot, bool]:
        """Create only on explicit init; existing files must validate, never reset."""
        if self.path.exists():
            return self.read(), False
        from .risk.model import load_profile
        load_profile(self.config.workspace)  # Fail before creating a file if assumptions are missing.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=False)
        except FileExistsError:
            return self.read(), False
        except OSError as exc:
            raise StateError("STATE_IO", "Cannot create paper state in the configured runtime directory.") from exc
        timestamp = stamp(self.clock())
        with self._connect(write=True) as connection:
            with self._transaction(connection):
                self._create_v1(connection, timestamp)
                self._apply_v2(connection, timestamp)
                self._apply_v3(connection, timestamp)
                result = self._validate(connection)
        return result, True

    def _create_v1(self, connection: sqlite3.Connection, timestamp: str) -> None:
        # Individual statements keep all DDL inside the explicit transaction.
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("""CREATE TABLE account (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            initial_equity TEXT NOT NULL, equity TEXT NOT NULL, high_water TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        connection.execute("CREATE TABLE halts (code TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
        connection.execute("""CREATE TABLE journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
            event TEXT NOT NULL, payload TEXT NOT NULL)""")
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
            ("config_hash", self.config.fingerprint), ("mode", "paper"), ("created_at", timestamp),
        ])
        money = format(self.config.starting_equity_usd, "f")
        connection.execute("INSERT INTO account VALUES (1, ?, ?, ?, ?, ?)", (money, money, money, timestamp, timestamp))
        self._journal(connection, timestamp, "initialized", {
            "mode": "paper", "starting_equity_usd": money, "config_hash": self.config.fingerprint,
        })
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 1")

    def _apply_v2(self, connection: sqlite3.Connection, timestamp: str) -> None:
        created = connection.execute("SELECT created_at FROM account WHERE singleton = 1").fetchone()[0]
        connection.execute("ALTER TABLE account ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        pauses = connection.execute("SELECT COUNT(*) FROM journal WHERE event = 'paused'").fetchone()[0]
        connection.execute("UPDATE account SET revision = ?, updated_at = ? WHERE singleton = 1", (pauses, timestamp))
        connection.execute("""CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)""")
        connection.executemany("INSERT INTO schema_migrations VALUES (?, ?, ?)", [
            (1, created, MIGRATIONS[1]), (2, timestamp, MIGRATIONS[2]),
        ])
        self._journal(connection, timestamp, "schema_migrated", {"from": 1, "to": 2})
        connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _journal(connection: sqlite3.Connection, timestamp: str, event: str, payload: dict) -> None:
        connection.execute("INSERT INTO journal (occurred_at, event, payload) VALUES (?, ?, ?)", (timestamp, event, _payload(payload)))

    def _apply_v3(self, connection: sqlite3.Connection, timestamp: str) -> None:
        from .risk.engine import seed
        from .risk.model import canonical, fingerprint, load_profile
        profile = load_profile(self.config.workspace)
        account = connection.execute("SELECT equity, high_water FROM account WHERE singleton = 1").fetchone()
        document = seed(self.config, profile, Decimal(account["equity"]), Decimal(account["high_water"]), timestamp)
        connection.execute("CREATE TABLE risk_state (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), seed TEXT NOT NULL, document TEXT NOT NULL)")
        connection.execute("INSERT INTO risk_state VALUES (1, ?, ?)", (canonical(document), canonical(document)))
        connection.execute("INSERT INTO schema_migrations VALUES (3, ?, ?)", (timestamp, MIGRATIONS[3]))
        connection.execute("UPDATE account SET updated_at = ? WHERE singleton = 1", (timestamp,))
        self._journal(connection, timestamp, "schema_migrated", {"from": 2, "to": 3, "seed_sha256": fingerprint(document), "profile_hash": profile.fingerprint})
        connection.execute("PRAGMA user_version = 3")

    def read(self) -> Snapshot:
        with self._connect() as connection:
            # A stable read transaction avoids mixing snapshots with concurrent pauses.
            connection.execute("BEGIN")
            try:
                return self._validate(connection)
            finally:
                connection.rollback()

    def pause(self, reason: str) -> Snapshot:
        if reason not in PAUSE_REASONS:
            raise StateError("PAUSE_INVALID", "Pause reason must be operator, maintenance or incident.")
        with self._connect(write=True) as connection:
            with self._transaction(connection):
                before = self._validate(connection)
                timestamp = stamp(self.clock())
                if parse_stamp(timestamp) < parse_stamp(before.updated_at):
                    raise StateError("CLOCK_REGRESSION", "Clock moved backwards; no state change was made.")
                if reason in before.halts:
                    return before
                connection.execute("INSERT INTO halts VALUES (?, ?)", (reason, timestamp))
                revision = before.revision + 1
                connection.execute("UPDATE account SET revision = ?, updated_at = ? WHERE singleton = 1", (revision, timestamp))
                self._journal(connection, timestamp, "paused", {"reason": reason, "revision": revision})
                result = self._validate(connection)
        return result

    def migrate(self) -> tuple[Snapshot, bool]:
        """Explicit known 1 -> 2 -> 3 migrations preserve account values and halts."""
        with self._connect(write=True) as connection:
            with self._transaction(connection):
                before = self._validate(connection, allow_v1=True)
                if before.schema_version == SCHEMA_VERSION:
                    return before, False
                timestamp = stamp(self.clock())
                if parse_stamp(timestamp) < parse_stamp(before.updated_at):
                    raise StateError("CLOCK_REGRESSION", "Clock moved backwards; no migration was performed.")
                if before.schema_version == 1:
                    self._apply_v2(connection, timestamp)
                self._apply_v3(connection, timestamp)
                result = self._validate(connection)
        return result, True

    def _validate(self, connection: sqlite3.Connection, *, allow_v1: bool = False) -> Snapshot:
        try:
            return self._validate_inner(connection, allow_v1=allow_v1)
        except StateError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, RecursionError, OverflowError) as exc:
            raise _bad_state() from exc

    def _validate_inner(self, connection: sqlite3.Connection, *, allow_v1: bool) -> Snapshot:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise _bad_state()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2, SCHEMA_VERSION):
            raise StateError("SCHEMA_UNSUPPORTED", "Database schema is unsupported; no migration or reset was attempted.")
        if version < SCHEMA_VERSION and not allow_v1:
            raise StateError("MIGRATION_REQUIRED", "A known schema migration is required; use migrate explicitly.")
        if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise _bad_state()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        expected = {"metadata", "account", "halts", "journal"}
        if version >= 2:
            expected.add("schema_migrations")
        if version >= 3:
            expected.add("risk_state")
        if tables != expected:
            raise _bad_state()
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if set(metadata) != {"config_hash", "mode", "created_at"} or metadata["mode"] != "paper":
            raise _bad_state()
        if metadata["config_hash"] != self.config.fingerprint:
            raise StateError("CONFIG_MISMATCH", "Configuration differs from this account's saved policy; no state was changed.")
        rows = connection.execute("SELECT * FROM account").fetchall()
        if len(rows) != 1 or rows[0]["singleton"] != 1:
            raise _bad_state()
        account = rows[0]
        initial = amount(account["initial_equity"], maximum=Decimal("50"))
        equity = amount(account["equity"], zero=True)
        high_water = amount(account["high_water"])
        created = parse_stamp(account["created_at"])
        updated = parse_stamp(account["updated_at"])
        if initial != self.config.starting_equity_usd or high_water < max(initial, equity) or updated < created:
            raise _bad_state()
        if metadata["created_at"] != account["created_at"]:
            raise _bad_state()
        halts = {}
        for row in connection.execute("SELECT code, created_at FROM halts ORDER BY code"):
            if row["code"] not in PAUSE_REASONS or not created <= parse_stamp(row["created_at"]) <= updated:
                raise _bad_state()
            halts[row["code"]] = row["created_at"]
        journal = connection.execute("SELECT * FROM journal ORDER BY id").fetchall()
        initialized = 0
        migrations_seen = []
        pause_records = {}
        risk_records = []
        expected_revision = 0
        migration_v3 = None
        last_time = created
        for row in journal:
            when = parse_stamp(row["occurred_at"])
            if not last_time <= when <= updated:
                raise _bad_state()
            last_time = when
            data = json.loads(row["payload"], object_pairs_hook=_audit_pairs)
            if row["event"] == "initialized":
                initialized += 1
                if when != created or data != {"mode": "paper", "starting_equity_usd": account["initial_equity"], "config_hash": self.config.fingerprint}:
                    raise _bad_state()
            elif row["event"] == "paused":
                if not isinstance(data, dict) or set(data) != {"reason", "revision"}:
                    raise _bad_state()
                reason = data["reason"]
                expected_revision += 1
                if reason not in PAUSE_REASONS or reason in pause_records or type(data["revision"]) is not int or data["revision"] != expected_revision:
                    raise _bad_state()
                pause_records[reason] = row["occurred_at"]
            elif row["event"] == "schema_migrated":
                if not isinstance(data, dict) or type(data.get("from")) is not int or type(data.get("to")) is not int:
                    raise _bad_state()
                if data == {"from": 1, "to": 2}:
                    migrations_seen.append((1, 2))
                elif version == 3 and set(data) == {"from", "to", "seed_sha256", "profile_hash"} and data["from"] == 2 and data["to"] == 3:
                    migrations_seen.append((2, 3))
                    migration_v3 = data
                else:
                    raise _bad_state()
            elif row["event"] == "risk_updated" and version == 3 and migration_v3 is not None:
                expected_revision += 1
                if (not isinstance(data, dict) or set(data) != {"revision", "kind", "input"}
                        or type(data["revision"]) is not int or data["revision"] != expected_revision):
                    raise _bad_state()
                risk_records.append((row["occurred_at"], data, tuple(pause_records)))
            else:
                raise _bad_state()
        expected_migrations = [] if version == 1 else ([(1, 2)] if version == 2 else [(1, 2), (2, 3)])
        if initialized != 1 or migrations_seen != expected_migrations or pause_records != halts:
            raise _bad_state()
        revision = len(pause_records) if version == 1 else account["revision"]
        if type(revision) is not int or revision != expected_revision:
            raise _bad_state()
        if version >= 2:
            migrations = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            if len(migrations) != version:
                raise _bad_state()
            previous = created
            for item, number in zip(migrations, range(1, version + 1)):
                when = parse_stamp(item["applied_at"])
                if item["version"] != number or item["description"] != MIGRATIONS[number] or not previous <= when <= updated:
                    raise _bad_state()
                previous = when
        risk_state = None
        risk_history = ()
        if version == 3:
            from .risk.engine import equity as risk_equity
            from .risk.ledger import audit
            from .risk.model import decode, load_profile
            risk_rows = connection.execute("SELECT * FROM risk_state").fetchall()
            if len(risk_rows) != 1 or risk_rows[0]["singleton"] != 1:
                raise _bad_state()
            risk_state, risk_history = audit(decode(risk_rows[0]["seed"]), decode(risk_rows[0]["document"]), risk_records,
                                             migration_v3, self.config, load_profile(self.config.workspace))
            if risk_equity(risk_state) != equity or Decimal(risk_state["high_water_usd"]) != high_water:
                raise _bad_state()
        return Snapshot(initial, equity, high_water, tuple(sorted(halts)), revision, account["created_at"], account["updated_at"], len(journal), version, risk_state, risk_history)
