"""Durable append-only evidence, source-bound state, locks and independent health."""

from contextlib import contextmanager, closing
from copy import deepcopy
import os
from pathlib import Path
import sqlite3

from marvel.data.quality import strict_json
from marvel.risk.model import canonical, fingerprint
from .common import Stop, bindings, require, stamp, utc_now


class Store:
    def __init__(self, config, mode="paper"):
        require(mode in {"paper", "testnet", "live"})
        self.config, self.mode = config, mode
        self.root = config.workspace / "var/operations" / mode
        require(self.root.resolve().is_relative_to(config.workspace.resolve()))
        self.path = self.root / "journal.sqlite3"
        require(self.path.resolve().is_relative_to(self.root.resolve()))

    @contextmanager
    def lock(self, name="worker.lock"):
        require(name in {"worker.lock", "service.lock"})
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / name).open("a+b") as handle:
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
                raise Stop("OPS_WORKER_BUSY") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def connect(self):
        require(self.path.exists(), "OPS_STATE_MISSING")
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def initialize(self, now=None):
        with self.lock():
            if self.path.exists():
                return self.load()
            state = {"schema": 1, "mode": self.mode, "revision": 0, "bindings": bindings(self.config),
                     "started_at": stamp(now or utc_now()), "active": None, "halts": [], "strategies": {},
                     "closed": [], "equity_usd": str(self.config.starting_equity_usd),
                     "high_water_usd": str(self.config.starting_equity_usd), "baseline": None,
                     "account_binding": None, "release": None}
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, parent TEXT NOT NULL, digest TEXT NOT NULL)")
                connection.execute("CREATE TABLE evidence (id TEXT PRIMARY KEY, seq INTEGER UNIQUE NOT NULL, body TEXT NOT NULL, parent TEXT NOT NULL, digest TEXT NOT NULL)")
                connection.execute("CREATE TABLE health (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            self.save(state, "initialize")
            return state

    def load(self, *, current=True):
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT seq,kind,body,parent,digest FROM events ORDER BY seq").fetchall()
        require(rows and len(rows) <= 100000, "OPS_JOURNAL_INVALID")
        parent, state = "0" * 64, None
        for seq, kind, raw, previous, digest in rows:
            state = strict_json(raw.encode())
            require(seq == state["revision"] and previous == parent and fingerprint(
                {"seq": seq, "kind": kind, "state": state, "parent": parent}) == digest, "OPS_JOURNAL_INVALID")
            parent = digest
        require(state["mode"] == self.mode)
        if current:
            require(state["bindings"] == bindings(self.config), "OPS_BINDINGS_CHANGED")
        return state

    def save(self, state, kind):
        require(state["bindings"] == bindings(self.config), "OPS_BINDINGS_CHANGED")
        saved = deepcopy(state)
        saved["revision"] += 1
        require(saved["revision"] <= 100000 and len(canonical(saved)) <= 2_000_000, "OPS_JOURNAL_FULL")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            tail = connection.execute("SELECT seq,digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            require((tail[0] if tail else 0) == state["revision"], "OPS_STATE_CONFLICT")
            parent = tail[1] if tail else "0" * 64
            digest = fingerprint({"seq": saved["revision"], "kind": kind, "state": saved, "parent": parent})
            connection.execute("INSERT INTO events VALUES (?,?,?,?,?)", (saved["revision"], kind, canonical(saved), parent, digest))
        state["revision"] = saved["revision"]

    def record(self, document):
        identity = fingerprint(document)
        raw = canonical(document)
        require(len(raw) <= 2_000_000, "OPS_EVIDENCE_TOO_LARGE")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT body FROM evidence WHERE id=?", (identity,)).fetchone()
            if old:
                require(old[0] == raw, "OPS_EVIDENCE_DAMAGED")
                return identity
            tail = connection.execute("SELECT seq,digest FROM evidence ORDER BY seq DESC LIMIT 1").fetchone()
            seq, parent = (tail[0] + 1, tail[1]) if tail else (1, "0" * 64)
            require(seq <= 100000, "OPS_EVIDENCE_FULL")
            digest = fingerprint({"id": identity, "seq": seq, "parent": parent})
            connection.execute("INSERT INTO evidence VALUES (?,?,?,?,?)", (identity, seq, raw, parent, digest))
        return identity

    def evidence(self):
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT id,seq,body,parent,digest FROM evidence ORDER BY seq").fetchall()
        output, previous = [], "0" * 64
        for expected, (identity, seq, raw, parent, digest) in enumerate(rows, 1):
            document = strict_json(raw.encode())
            require(fingerprint(document) == identity and seq == expected and parent == previous
                    and fingerprint({"id": identity, "seq": seq, "parent": parent}) == digest, "OPS_EVIDENCE_DAMAGED")
            previous = digest
            output.append(document)
        return output

    def heartbeat(self, status, now=None, *, failures=0):
        document = {"at": stamp(now or utc_now()), "status": status, "failures": failures}
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT INTO health VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (canonical(document),))

    def health(self, now=None):
        from .common import parse_stamp
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT body FROM health WHERE id=1").fetchone()
        if not row:
            return {"healthy": False, "reason": "NO_HEARTBEAT"}
        document = strict_json(row[0].encode())
        age = ((now or utc_now()) - parse_stamp(document["at"])).total_seconds()
        return {**document, "healthy": 0 <= age <= 120 and document["status"] == "running", "age_seconds": age}

    def backup(self):
        # A consistent online backup, never a reset or removal of journal history.
        target = self.root / "backup.sqlite3"
        require(not target.is_symlink() and target.resolve().is_relative_to(self.root.resolve()))
        with closing(self.connect()) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
        return "var/operations/" + self.mode + "/backup.sqlite3"
