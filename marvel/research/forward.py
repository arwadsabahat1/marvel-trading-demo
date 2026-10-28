"""Prospective public observations, never retrospective fills or owner-account trading."""

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile
import time

from ..clock import stamp, utc_now
from ..config import load_config
from ..data.client import PublicClient
from ..data.quality import INTERVALS, utc_us
from ..data.store import SnapshotStore
from ..errors import ERROR_CODES, MarvelError
from ..risk.model import canonical, fingerprint, load_profile
from .features import signal as baseline_signal
from .feasibility import report as route_report
from .model import STRATEGIES, fail
from .proxy import ProxyStore, REAL_DATASETS
from .rules import RULES, signal as additional_signal
from .store import decode_record
from .study import source_hashes

HOUR = INTERVALS["1h"]
MAX_RECORDS = 800
RECORD_FIELDS = {"session_id", "sequence", "previous", "slot_us", "attempted_at_utc", "captured_at_utc", "kind",
    "failure_code", "dataset_id", "observation_id", "bar_end_us", "eligible_prospective_signal", "eligibility_reasons",
    "signals", "route", "public_get_receipts", "missed_hours", "completed_paper_trades", "entries_allowed",
    "live_execution_available", "owner_account_used"}


def eligibility(plan, end, received):
    reasons = []
    if end < plan["first_eligible_bar_end_us"]:
        reasons.append("PRE_START_WARMUP_ONLY")
    if received - end > plan["maximum_signal_delay_seconds"] * 1_000_000:
        reasons.append("LATE_CAPTURE_NOT_A_TIMELY_DECISION")
    if received >= plan["expires_at_us"]:
        reasons.append("SESSION_EXPIRED_DURING_CAPTURE")
    return reasons


def policy():
    return {"schema_version": 1, "kind": "btc_prospective_observation_protocol_v1",
        "symbol": "BTCUSDT", "strategies": [asdict(s) for s in (*STRATEGIES, *RULES)],
        "warmup_hours": 528, "duration_days": 30, "maximum_records": MAX_RECORDS,
        "maximum_signal_delay_seconds": 180, "maximum_consecutive_capture_failures": 3,
        "protected_through_us": max(utc_us(row[2]) for row in REAL_DATASETS.values()),
        "sampling": "At most one attempt per UTC hour; latest completed bar only. Missed hours are recorded, never replayed as timely decisions.",
        "warmup": "Pre-start bars provide features only; no retrospective scoring or fills.",
        "execution": "Observation only. No orders, conversion, fill simulator, API credentials, account access or performance estimates.",
        "qualification": "Actual fees, complete protective/partial exits, FX, lawful provider/funding access and production rights remain unqualified.",
        "risk_policy": "Original configuration/profile are bound; source or financial-file changes prevent further collection.",
        "promotion": "No automatic strategy selection. Observed signals and public quotes are not completed paper trades or profitability evidence.",
        "entries_allowed": False, "live_execution_available": False, "phase5_historical_gate_passed": False}


def freeze(config, profile, now):
    when = utc_us(stamp(now))
    fixed = policy()
    if when // HOUR * HOUR - fixed["warmup_hours"] * HOUR < fixed["protected_through_us"]:
        fail("RESEARCH_DATA")
    return {**fixed, "started_at_utc": stamp(now), "expires_at_us": when + fixed["duration_days"] * 24 * HOUR,
        "first_eligible_bar_end_us": (when // HOUR + 1) * HOUR,
        "config_fingerprint": config.fingerprint, "original_profile_hash": profile.fingerprint,
        "financial_file_sha256": financial_hashes(config),
        "source_sha256": source_hashes()}


def financial_hashes(config):
    return {name: sha256((config.workspace / name).read_bytes()).hexdigest()
        for name in ("config/paper.json", "config/btc-paper-risk.json")}


class ForwardStore(ProxyStore):
    directory = "var/forward-observations"

    def protocol(self, identity):
        identity = self.identity(identity)
        try:
            plan = decode_record(self.read(self.root / "plans" / (identity + ".json")))
            extra = {"started_at_utc", "expires_at_us", "first_eligible_bar_end_us", "config_fingerprint", "original_profile_hash", "financial_file_sha256", "source_sha256"}
            if (fingerprint(plan) != identity or set(plan) != set(policy()) | extra
                    or any(plan.get(k) != v for k, v in policy().items())
                    or any(plan[k] is not False for k in ("entries_allowed", "live_execution_available", "phase5_historical_gate_passed"))):
                fail("RESEARCH_RECORD")
            when = utc_us(plan["started_at_utc"])
            if (plan["first_eligible_bar_end_us"] != (when // HOUR + 1) * HOUR
                    or plan["expires_at_us"] != when + 30 * 24 * HOUR
                    or when // HOUR * HOUR - 528 * HOUR < plan["protected_through_us"]
                    or not isinstance(plan["source_sha256"], dict) or not plan["source_sha256"]):
                fail("RESEARCH_RECORD")
            return plan
        except (KeyError, TypeError, ValueError):
            fail("RESEARCH_RECORD")

    def folder(self, identity):
        return self.path(self.root / self.identity(identity))

    @contextmanager
    def locked(self, identity):
        folder = self.folder(identity)
        folder.mkdir(parents=True, exist_ok=True)
        with self.path(folder / "capture.lock").open("a+b") as handle:
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
                fail("RESEARCH_RECORD")
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def records(self, identity):
        plan = self.protocol(identity)
        folder = self.path(self.folder(identity) / "captures")
        paths = sorted(folder.glob("*.json"))
        if len(paths) > MAX_RECORDS:
            fail("RESEARCH_RECORD")
        result, previous = [], "0" * 64
        for index, path in enumerate(paths, 1):
            try:
                document = decode_record(self.read(path))
                body = {k: v for k, v in document.items() if k != "record_id"}
                if (set(body) != RECORD_FIELDS or path.name != f"{index:06d}.json" or document["record_id"] != fingerprint(body)
                        or body["session_id"] != identity or body["sequence"] != index or body["previous"] != previous
                        or body["kind"] not in {"observation", "capture_failure"}
                        or body["failure_code"] is not None and body["failure_code"] not in ERROR_CODES
                        or any(body[k] is not False for k in ("entries_allowed", "live_execution_available", "owner_account_used"))
                        or body["completed_paper_trades"] != 0 or type(body["completed_paper_trades"]) is not int
                        or body["eligible_prospective_signal"] is not (not body["eligibility_reasons"])
                        or result and body["slot_us"] <= result[-1]["slot_us"]):
                    fail("RESEARCH_RECORD")
                attempted, received = utc_us(body["attempted_at_utc"]), utc_us(body["captured_at_utc"])
                expected_missed = max(0, (body["slot_us"] - result[-1]["slot_us"]) // HOUR - 1) if result else 0
                if (attempted < utc_us(plan["started_at_utc"]) or received < attempted
                        or type(body["slot_us"]) is not int or body["slot_us"] != attempted // HOUR * HOUR
                        or type(body["missed_hours"]) is not int or body["missed_hours"] != expected_missed):
                    fail("RESEARCH_RECORD")
                if body["kind"] == "observation":
                    end = body["bar_end_us"]
                    if (type(end) is not int or end % HOUR or end > received
                            or body["failure_code"] is not None or body["eligibility_reasons"] != eligibility(plan, end, received)
                            or [s["strategy"] for s in body["signals"]] != [s.identity for s in (*STRATEGIES, *RULES)]
                            or any(s["decision"].get("signal_at_us", end) > end for s in body["signals"])
                            or re.fullmatch(r"[a-f0-9]{64}", body["dataset_id"]) is None
                            or re.fullmatch(r"[a-f0-9]{64}", body["observation_id"]) is None
                            or body["route"]["entries_allowed"] is not False):
                        fail("RESEARCH_RECORD")
                elif (body["failure_code"] not in ERROR_CODES or body["signals"] or body["route"] is not None
                        or body["eligibility_reasons"] != ["CAPTURE_FAILED"]):
                    fail("RESEARCH_RECORD")
                previous = document["record_id"]
                result.append(document)
            except (KeyError, TypeError, ValueError):
                fail("RESEARCH_RECORD")
        return result

    def append(self, identity, body):
        folder = self.path(self.folder(identity) / "captures")
        folder.mkdir(parents=True, exist_ok=True)
        destination = self.path(folder / f"{body['sequence']:06d}.json")
        if destination.exists():
            fail("RESEARCH_RECORD")
        raw = (canonical({**body, "record_id": fingerprint(body)}) + "\n").encode("ascii")
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=folder) as scratch:
            staging = self.path(Path(scratch) / "capture.json")
            staging.write_bytes(raw)
            staging.rename(destination)
        self.records(identity)


def bindings_match(plan, config):
    try:
        return (plan["config_fingerprint"] == config.fingerprint == load_config(config.workspace / "config/paper.json", config.workspace).fingerprint
            and plan["original_profile_hash"] == load_profile(config.workspace).fingerprint
            and plan["financial_file_sha256"] == financial_hashes(config)
            and plan["source_sha256"] == source_hashes())
    except (MarvelError, OSError):
        return False


def start(config, *, now=None):
    store = ForwardStore(config.workspace)
    frozen = freeze(config, load_profile(config.workspace), now or utc_now())
    identity = store.freeze(frozen)  # Before the first external market observation.
    store.folder(identity).mkdir(parents=True, exist_ok=True)
    return status(config, identity)


def stop(config, identity, *, now=None):
    store = ForwardStore(config.workspace)
    store.protocol(identity)
    with store.locked(identity):
        path = store.path(store.folder(identity) / "stopped.json")
        raw = (canonical({"session_id": identity, "stopped_at_utc": stamp(now or utc_now())}) + "\n").encode("ascii")
        try:
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            decode_record(store.read(path))
    return status(config, identity)


def status(config, identity, *, now=None):
    store = ForwardStore(config.workspace)
    plan, records = store.protocol(identity), store.records(identity)
    current = utc_us(stamp(now or utc_now()))
    failures = 0
    for row in reversed(records):
        if row["kind"] != "capture_failure":
            break
        failures += 1
    stopped = store.path(store.folder(identity) / "stopped.json").exists()
    matches = bindings_match(plan, config)
    collection_allowed = matches and not stopped and current < plan["expires_at_us"] and failures < 3 and len(records) < MAX_RECORDS
    return {"session_id": identity, "kind": "prospective_observations_only", "started_at_utc": plan["started_at_utc"],
        "first_eligible_bar_end_us": plan["first_eligible_bar_end_us"], "expires_at_us": plan["expires_at_us"],
        "captures": len(records), "eligible_signal_captures": sum(row["eligible_prospective_signal"] for row in records),
        "failed_captures": sum(row["kind"] == "capture_failure" for row in records), "consecutive_capture_failures": failures,
        "missed_hours": sum(row["missed_hours"] for row in records), "bindings_match": matches,
        "collection_allowed": collection_allowed, "stopped": stopped, "last_capture": records[-1] if records else None,
        "completed_paper_trades": 0, "qualifying_strategy_trades": 0,
        "actual_route_qualified": False, "entries_allowed": False, "live_execution_available": False,
        "owner_account_used": False, "note": "Forward signals/public quotes are observations; no paper fills or profitability claim."}


def capture(config, identity, *, allow_network=False, clock=utc_now, client_factory=PublicClient):
    # Construction enforces opt-in even if there would be no work this hour.
    client = client_factory(allow_network=allow_network, clock=clock)
    store = ForwardStore(config.workspace)
    with store.locked(identity):
        now = clock()
        current, plan = status(config, identity, now=now), store.protocol(identity)
        records = store.records(identity)
        if not current["collection_allowed"]:
            fail("RESEARCH_RECORD")
        slot = utc_us(stamp(now)) // HOUR * HOUR
        if records and slot == records[-1]["slot_us"]:
            return {**current, "capture_skipped": "ALREADY_ATTEMPTED_THIS_UTC_HOUR"}
        if records and slot < records[-1]["slot_us"] or utc_us(stamp(now)) < utc_us(plan["started_at_utc"]):
            fail("RESEARCH_RECORD")
        body = {"session_id": identity, "sequence": len(records) + 1, "previous": records[-1]["record_id"] if records else "0" * 64,
            "slot_us": slot, "attempted_at_utc": stamp(now), "captured_at_utc": None,
            "kind": "capture_failure", "failure_code": None, "dataset_id": None, "observation_id": None,
            "bar_end_us": None, "eligible_prospective_signal": False, "eligibility_reasons": ["CAPTURE_FAILED"],
            "signals": [], "route": None, "public_get_receipts": [],
            "missed_hours": max(0, (slot - records[-1]["slot_us"]) // HOUR - 1) if records else 0,
            "completed_paper_trades": 0, "entries_allowed": False, "live_execution_available": False, "owner_account_used": False}
        try:
            as_of = client.server_time()
            end = as_of // HOUR * HOUR
            begin = end - plan["warmup_hours"] * HOUR
            if begin < plan["protected_through_us"]:
                fail("RESEARCH_DATA")
            raw, parameters = client.rest(interval="1h", start_us=begin, end_us=end, as_of_us=as_of)
            snapshots = SnapshotStore(config.workspace)
            manifest = snapshots.save(raw, parameters, provenance=client.provenance())
            _, history = snapshots.load(manifest["snapshot_id"])
            observation = client.observe(max_age_seconds=config.max_quote_age_seconds, max_spread_bps=config.max_spread_bps)
            observation_id = snapshots.save_observation(observation)
            received = utc_us(stamp(clock()))
            if history.candles[-1].end_us != end or end > received or received < utc_us(plan["started_at_utc"]):
                fail("RESEARCH_DATA")
            reasons = eligibility(plan, end, received)
            signals = [{"strategy": rule.identity, "decision": fn(history.candles, rule)}
                for rules, fn in ((STRATEGIES, baseline_signal), (RULES, additional_signal)) for rule in rules]
            body.update(kind="observation", dataset_id=manifest["snapshot_id"], observation_id=observation_id,
                bar_end_us=end, eligible_prospective_signal=not reasons, eligibility_reasons=reasons, signals=signals,
                route=route_report(config, load_profile(config.workspace), observation))
        except MarvelError as exc:
            body["failure_code"] = exc.code  # Never retain exception text, private data or fabricated fills.
        if not bindings_match(plan, config):
            fail("RESEARCH_RECORD")
        body["captured_at_utc"] = stamp(clock())
        if body["kind"] == "observation":
            body["eligibility_reasons"] = eligibility(plan, body["bar_end_us"], utc_us(body["captured_at_utc"]))
            body["eligible_prospective_signal"] = not body["eligibility_reasons"]
        body["public_get_receipts"] = client.provenance()["requests"]
        store.append(identity, body)
    return status(config, identity)


def watch(config, identity, *, allow_network=False, clock=utc_now, sleeper=time.sleep):
    if allow_network is not True:
        PublicClient(allow_network=allow_network)
    while True:
        current = status(config, identity, now=clock())
        if not current["collection_allowed"]:
            return current
        now = utc_us(stamp(clock()))
        previous = current["last_capture"]
        if previous is None or now // HOUR * HOUR > previous["slot_us"]:
            capture(config, identity, allow_network=True, clock=clock)
        sleeper(20)  # Bounded, interruptible worker checks; at most one network cycle per UTC hour.
