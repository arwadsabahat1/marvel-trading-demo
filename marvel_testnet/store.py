"""One persistent virtual allocation, durable order intents and an audited hash chain."""

from contextlib import closing, contextmanager
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import uuid

from marvel.clock import stamp
from marvel.data.quality import strict_json
from marvel.risk.engine import seed, valuation_point
from marvel.risk.model import canonical, fingerprint, load_profile
from .model import HOST, fail, financial_hashes, sources


class Store:
    def __init__(self, config):
        self.config = config
        self.root = (config.workspace / "var/testnet-demo").resolve()
        if not self.root.is_relative_to(config.workspace.resolve()):
            fail("TESTNET_STORAGE_INVALID")
        self.database = self.root / "allocation.sqlite3"
        if not self.database.resolve().is_relative_to(self.root):
            fail("TESTNET_STORAGE_INVALID")

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "worker.lock").open("a+b") as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fail("TESTNET_BUSY")
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self, now):
        with self.locked():
            if self.database.exists():
                return self.load()
            risk = seed(self.config, load_profile(self.config.workspace), self.config.starting_equity_usd,
                self.config.starting_equity_usd, stamp(now))
            state = {"kind": "isolated_testnet_virtual_allocation_v1", "revision": 0,
                "protocol": {"host": HOST, "nonce": uuid.uuid4().hex, "started_at_utc": stamp(now),
                    "config_fingerprint": self.config.fingerprint, "financial_sha256": financial_hashes(self.config),
                    "source_sha256": sources(), "initial_equity_usd": str(self.config.starting_equity_usd)},
                "account_binding": None, "risk": risk, "history": [valuation_point(risk)],
                "active": None, "cycles": [], "review_reasons": [], "last_preflight": None,
                "production_execution_available": False, "strategy_qualified": False, "phase5_passed": False}
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL)")
                connection.execute("PRAGMA user_version=1")
            self.save(state, "initialize", expected_revision=0)
            return self.load()

    def load(self):
        if not self.database.exists():
            fail("TESTNET_STATE_MISSING")
        try:
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    fail("TESTNET_STATE_INVALID")
                rows = connection.execute("SELECT sequence,kind,body,previous,digest FROM events ORDER BY sequence LIMIT 2002").fetchall()
            if not rows or len(rows) > 2000:
                fail("TESTNET_STATE_INVALID")
            previous = "0" * 64
            for index, (sequence, kind, raw, parent, digest) in enumerate(rows, 1):
                if type(raw) is not str or len(raw) > 2_000_000:
                    fail("TESTNET_STATE_INVALID")
                state = strict_json(raw.encode("ascii"))
                body = {"sequence": sequence, "kind": kind, "state": state, "previous": parent}
                if (sequence != state["revision"] or sequence != index
                        or parent != previous or fingerprint(body) != digest
                        or state["kind"] != "isolated_testnet_virtual_allocation_v1" or state["protocol"]["host"] != HOST
                        or any(state[k] is not False for k in ("production_execution_available", "strategy_qualified", "phase5_passed"))):
                    fail("TESTNET_STATE_INVALID")
                previous = digest
            return state
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            fail("TESTNET_STATE_INVALID")

    def require_current(self, state):
        if (state["protocol"]["config_fingerprint"] != self.config.fingerprint
                or state["protocol"]["financial_sha256"] != financial_hashes(self.config)
                or state["protocol"]["source_sha256"] != sources()):
            fail("TESTNET_BINDINGS_CHANGED")

    def save(self, state, kind, *, expected_revision=None):
        expected = state["revision"] if expected_revision is None else expected_revision
        saved = deepcopy(state)
        saved["revision"] = expected + 1
        if saved["revision"] > 2000 or len(canonical(saved)) > 2_000_000:
            fail("TESTNET_JOURNAL_LIMIT")
        try:
            with closing(sqlite3.connect(self.database, timeout=1)) as connection, connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                tail = connection.execute("SELECT sequence,digest FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
                if (tail[0] if tail else 0) != expected:
                    fail("TESTNET_STATE_CONFLICT")
                parent = tail[1] if tail else "0" * 64
                body = {"sequence": saved["revision"], "kind": kind, "state": saved, "previous": parent}
                connection.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                    (saved["revision"], kind, canonical(saved), parent, fingerprint(body)))
            state["revision"] = saved["revision"]
        except sqlite3.Error:
            fail("TESTNET_STORAGE_INVALID")

    def review(self, state, code):
        if code not in state["review_reasons"]:
            state["review_reasons"].append(code)
        self.save(state, "review_halt")
