"""Content-addressed immutable studies, with negative trials and source hashes."""

from pathlib import Path
import json
import re
import tempfile

from ..errors import DataError
from ..risk.model import canonical, fingerprint
from .model import fail, plan

MAX_RECORD_BYTES = 64 * 1024 * 1024


def decode_record(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("RESEARCH_RECORD")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda _: fail("RESEARCH_RECORD"))
    except (UnicodeError, ValueError, RecursionError):
        fail("RESEARCH_RECORD")


def markdown(document, identity):
    lines = ["# BTC baseline exploratory screen", "", f"Run: `{identity}`. Status: **{document['status']}**.", "",
             "Research only. Final holdout was not evaluated. No candidate is approved; no live orders or owner-account changes.", "",
             "| Fixed candidate | Scenario | Net P&L USD | Closed trades | Max drawdown | Holding BTC | Status |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for candidate in document["candidates"]:
        for scenario, result in sorted(candidate["validation"].items()):
            metrics = result["metrics"]
            lines.append(f"| {candidate['strategy']} | {scenario} | {metrics['net_pnl_usd']} | {metrics['closed_trades']} | {metrics['maximum_drawdown_fraction']} | {metrics['holding_btc']} | {candidate['status']} |")
        if not candidate["validation"]:
            lines.append(f"| {candidate['strategy']} | unavailable | — | 0 | — | — | {candidate['status']} |")
    lines.extend(["", "The JSON record includes the frozen cards, training diagnostics, six chronological validation windows, costed cash/passive and retrospective exposure-matched comparisons, bootstrap uncertainty, all scenarios and failed gates.",
                  "", "Sparse or zero trades do not prove an edge. Fee/FX/execution assumptions remain unqualified. Dust is retained; a zero-recovery residual is not a flat account.",
                  "", "[Full immutable study](study.json)", ""])
    return "\n".join(lines)


def validate(document):
    expected = {"schema_version", "application_version", "plan", "plan_id", "source_sha256", "config_fingerprint",
                "original_profile_hash", "dataset", "data_kind", "splits", "validation_portfolio", "holdout", "candidates",
                "phase5_historical_gate_passed", "status", "research_only", "entries_allowed", "execution_eligible",
                "live_execution_available", "limitations"}
    if (not isinstance(document, dict) or set(document) != expected or type(document["schema_version"]) is not int
            or document["schema_version"] != 1 or document["status"] != "blocked_historical_qualification"
            or document["research_only"] is not True or any(document[field] is not False for field in
               ("phase5_historical_gate_passed", "entries_allowed", "execution_eligible", "live_execution_available"))
            or not isinstance(document["holdout"], dict) or document["holdout"].get("evaluated") is not False
            or not isinstance(document["candidates"], list) or len(document["candidates"]) != 3
            or not isinstance(document["plan"], dict) or fingerprint(document["plan"]) != document["plan_id"]
            or not isinstance(document["dataset"], dict) or not isinstance(document["dataset"].get("snapshot_id"), str)):
        fail("RESEARCH_RECORD")
    for candidate in document["candidates"]:
        if (not isinstance(candidate, dict) or not isinstance(candidate.get("validation"), dict)
                or not isinstance(candidate.get("strategy"), str) or not isinstance(candidate.get("gates"), dict)
                or candidate["gates"].get("historical_promotion_passed") is not False
                or candidate["gates"].get("live_eligible") is not False
                or candidate.get("selected_for_deployment") is not False):
            fail("RESEARCH_RECORD")


class ResearchStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "var/research-runs"
        self.check_path(self.root)

    def check_path(self, path):
        if not path.resolve().is_relative_to(self.workspace) or not path.resolve().is_relative_to(self.root.resolve()):
            fail("RESEARCH_RECORD")
        return path

    def folder(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{64}", identifier):
            fail("RESEARCH_RECORD")
        return self.check_path(self.root / identifier)

    def freeze_plan(self):
        self.root.mkdir(parents=True, exist_ok=True)
        folder = self.check_path(self.root / "plans")
        folder.mkdir(exist_ok=True)
        document = plan()
        path = self.check_path(folder / (fingerprint(document) + ".json"))
        raw = (canonical(document) + "\n").encode("ascii")
        try:
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            if path.read_bytes() != raw:
                fail("RESEARCH_RECORD")
        return fingerprint(document)

    def save(self, document):
        validate(document)
        identity = fingerprint(document)
        raw = (canonical({**document, "run_id": identity}) + "\n").encode("ascii")
        if len(raw) > MAX_RECORD_BYTES:
            fail("RESEARCH_RECORD")
        target = self.folder(identity)
        if target.exists():
            self.load(identity)
            return identity
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=self.root) as name:
            staging = self.check_path(Path(name))
            (staging / "study.json").write_bytes(raw)
            (staging / "report.md").write_bytes(markdown(document, identity).encode("utf-8"))
            try:
                staging.rename(target)
            except OSError:
                if not target.exists():
                    fail("RESEARCH_RECORD")
                self.load(identity)
        return identity

    def load(self, identity):
        folder = self.folder(identity)
        try:
            with self.check_path(folder / "study.json").open("rb") as handle:
                raw = handle.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                fail("RESEARCH_RECORD")
            record = decode_record(raw)
            if not isinstance(record, dict) or record.get("run_id") != identity:
                fail("RESEARCH_RECORD")
            body = {key: value for key, value in record.items() if key != "run_id"}
            if fingerprint(body) != identity:
                fail("RESEARCH_RECORD")
            validate(body)
            with self.check_path(folder / "report.md").open("rb") as handle:
                report_raw = handle.read(MAX_RECORD_BYTES + 1)
            if len(report_raw) > MAX_RECORD_BYTES:
                fail("RESEARCH_RECORD")
            report = report_raw.decode("utf-8")
            if report != markdown(body, identity):
                fail("RESEARCH_RECORD")
            return record
        except (OSError, KeyError, TypeError, ValueError, DataError):
            fail("RESEARCH_RECORD")

    def inventory(self):
        if not self.root.exists():
            return []
        records = []
        for folder in sorted(self.root.iterdir()):
            if re.fullmatch(r"[a-f0-9]{64}", folder.name):
                document = self.load(folder.name)
                records.append({"run_id": folder.name, "dataset_id": document["dataset"]["snapshot_id"], "status": document["status"],
                                "holdout_evaluated": document["holdout"]["evaluated"], "candidates": len(document["candidates"])})
        return records
