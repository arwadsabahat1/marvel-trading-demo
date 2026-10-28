"""Content-addressed research snapshots, separate from account state."""

from pathlib import Path
import re
import tempfile

from .quality import MAX_BYTES, History, canonical, digest, fail, parse_history, strict_json

TERMS = "https://raw.githubusercontent.com/binance/binance-public-data/master/TERMS_AND_CONDITIONS.md"
ATTRIBUTION = {"provider": "Binance Vision", "terms_url": TERMS,
               "license": "CC BY-NC-SA 4.0 plus Binance Vision Dataset Terms",
               "use": "Personal non-production research only; no production data/model rights established"}
PARAMETERS = {"format", "unit", "interval", "start_us", "end_us", "as_of_us", "checksum"}
SYNTHETIC_ATTRIBUTION = {"provider": "Local synthetic engineering fixture", "use": "Test input; no real market or profit claims"}


class SnapshotStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / "var/research-data"
        if not self.root.resolve().is_relative_to(self.workspace):
            fail("DATA_SNAPSHOT")

    def _path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or not resolved.is_relative_to(self.workspace):
            fail("DATA_SNAPSHOT")
        return path

    def _folder(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{64}", identifier):
            fail("DATA_SNAPSHOT")
        return self._path(self.root / identifier)

    def _read(self, path: Path) -> bytes:
        try:
            with self._path(path).open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                fail("DATA_SNAPSHOT")
            return raw
        except OSError:
            fail("DATA_SNAPSHOT")

    def save(self, raw: bytes, parameters: dict, *, provenance: dict) -> dict:
        if (set(parameters) != PARAMETERS or provenance.get("origin") not in {"local_unverified", "public_https_get"}
                or provenance.get("data_kind") not in {"synthetic", "binance_spot"}):
            fail()
        history = parse_history(raw, **parameters)
        encoded = history.encoded()
        if len(encoded) > MAX_BYTES:
            fail()
        manifest = {"schema_version": 1, "symbol": "BTCUSDT", "calendar": "BTC_UTC_24X7_NO_GAP_FILL",
                    "parameters": parameters, "provenance": provenance,
                    "raw_sha256": digest(raw), "candles_sha256": digest(encoded),
                    "completed_candles": len(history.candles), "excluded_unfinished": history.excluded_unfinished,
                    "attribution": SYNTHETIC_ATTRIBUTION if provenance["data_kind"] == "synthetic" else ATTRIBUTION,
                    "research_only": True, "execution_eligible": False}
        identifier = digest(canonical(manifest))
        manifest["snapshot_id"] = identifier
        target = self._folder(identifier)
        if target.exists():
            self.load(identifier)  # Never silently repair or replace a damaged snapshot.
            return manifest
        self.root.mkdir(parents=True, exist_ok=True)
        # Publish a whole validated directory. Interrupted writes remain unpublished scratch data.
        with tempfile.TemporaryDirectory(prefix=".incoming-", dir=self.root) as scratch:
            staging = self._path(Path(scratch))
            (staging / "raw.bin").write_bytes(raw)
            (staging / "candles.jsonl").write_bytes(encoded)
            (staging / "manifest.json").write_bytes(canonical(manifest))
            try:
                staging.rename(target)
            except OSError:
                if not target.exists():
                    fail("DATA_SNAPSHOT")
                self.load(identifier)
            # TemporaryDirectory tolerates a staging directory already moved away.
        return manifest

    def load(self, identifier: str) -> tuple[dict, History]:
        folder = self._folder(identifier)
        try:
            manifest = strict_json(self._read(folder / "manifest.json"))
            expected = {"schema_version", "symbol", "calendar", "parameters", "provenance", "raw_sha256",
                        "candles_sha256", "completed_candles", "excluded_unfinished", "attribution",
                        "research_only", "execution_eligible", "snapshot_id"}
            if not isinstance(manifest, dict) or set(manifest) != expected:
                fail("DATA_SNAPSHOT")
            body = {key: value for key, value in manifest.items() if key != "snapshot_id"}
            if (manifest["snapshot_id"] != identifier or digest(canonical(body)) != identifier
                    or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                    or manifest["symbol"] != "BTCUSDT" or manifest["calendar"] != "BTC_UTC_24X7_NO_GAP_FILL"
                    or not isinstance(manifest["provenance"], dict)
                    or manifest["provenance"].get("origin") not in {"local_unverified", "public_https_get"}
                    or manifest["provenance"].get("data_kind") not in {"synthetic", "binance_spot"}
                    or manifest["attribution"] != (SYNTHETIC_ATTRIBUTION if manifest["provenance"]["data_kind"] == "synthetic" else ATTRIBUTION)
                    or manifest["research_only"] is not True
                    or manifest["execution_eligible"] is not False or set(manifest["parameters"]) != PARAMETERS):
                fail("DATA_SNAPSHOT")
            raw = self._read(folder / "raw.bin")
            encoded = self._read(folder / "candles.jsonl")
            if digest(raw) != manifest["raw_sha256"] or digest(encoded) != manifest["candles_sha256"]:
                fail("DATA_SNAPSHOT")
            history = parse_history(raw, **manifest["parameters"])
            if (encoded != history.encoded() or type(manifest["completed_candles"]) is not int
                    or type(manifest["excluded_unfinished"]) is not int
                    or len(history.candles) != manifest["completed_candles"]
                    or history.excluded_unfinished != manifest["excluded_unfinished"]):
                fail("DATA_SNAPSHOT")
            return manifest, history
        except (KeyError, TypeError, ValueError):
            fail("DATA_SNAPSHOT")

    def inventory(self) -> list[dict]:
        if not self.root.exists():
            return []
        result = []
        for path in sorted(self.root.iterdir()):
            if re.fullmatch(r"[a-f0-9]{64}", path.name):
                manifest, _ = self.load(path.name)
                result.append({key: manifest[key] for key in ("snapshot_id", "symbol", "completed_candles", "research_only", "execution_eligible")})
        return result

    def save_observation(self, document: dict) -> str:
        raw = canonical({**document, "attribution": ATTRIBUTION, "research_only": True,
                         "execution_eligible": False, "entries_allowed": False})
        if len(raw) > MAX_BYTES:
            fail()
        identifier = digest(raw)
        folder = self._path(self.root / "observations")
        folder.mkdir(parents=True, exist_ok=True)
        path = self._path(folder / (identifier + ".json"))
        try:
            with path.open("xb") as handle:
                handle.write(raw)
        except FileExistsError:
            if self._read(path) != raw:
                fail("DATA_SNAPSHOT")
        return identifier

    def load_observation(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{64}", identifier):
            fail("DATA_SNAPSHOT")
        raw = self._read(self.root / "observations" / (identifier + ".json"))
        document = strict_json(raw)
        if (digest(raw) != identifier or not isinstance(document, dict)
                or document.get("attribution") != ATTRIBUTION or document.get("research_only") is not True
                or document.get("execution_eligible") is not False or document.get("entries_allowed") is not False):
            fail("DATA_SNAPSHOT")
        return document
