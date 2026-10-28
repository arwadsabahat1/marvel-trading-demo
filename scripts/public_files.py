"""Shared source-only publication policy. No account or credential access."""

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = {"README.md", "SECURITY.md", "pyproject.toml", "requirements.txt",
             ".python-version", ".gitignore", ".gitattributes", "PUBLIC_FILES.txt"}
EXACT_PATHS = {"config/paper.json", "config/btc-paper-risk.json",
               "tests/fixtures/schema-v1.sql", "tests/fixtures/btc-synthetic-ms.json",
               "docs/ARCHITECTURE.md", "docs/ROADMAP.md", "docs/TESTNET.md",
               "ci/offline-checks.yml", "docs/OPERATIONS.md", "deploy/marvel-paper.service", "deploy/run-paper.ps1"}
CODE_FOLDERS = {"marvel", "marvel_testnet", "marvel_ops", "tests", "tests_testnet", "tests_public", "tests_ops"}
SCRIPT_NAMES = {"public_files.py", "check_publish.py", "package_demo.py",
                "demo_workspace.py", "check_demo.py", "run_demo.py"}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "cloud_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "literal_api_credential": re.compile(
        r'''(?i)(?:api[_-]?(?:key|secret)|hmac[_-]?secret)["']?\s*[:=]\s*["'][A-Za-z0-9+/=_-]{32,}["']'''),
    "personal_windows_path": re.compile(r"(?i)[a-z]:[\\/]users[\\/][^\s\"'<>]+"),
    "private_ipv4": re.compile(r"(?<![\d.])(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])"),
}


def allowed_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or path.as_posix() != name or any(p in {".", "..", "__pycache__"} for p in path.parts)):
        return False
    return (name in TOP_LEVEL or name in EXACT_PATHS
            or (len(path.parts) >= 2 and path.parts[0] in CODE_FOLDERS and path.suffix == ".py")
            or (len(path.parts) == 2 and path.parts[0] == "scripts" and path.name in SCRIPT_NAMES))


def parse_manifest(text):
    names = text.splitlines()
    if not names or names != sorted(set(names)) or any(not allowed_name(name) for name in names):
        raise ValueError("Publication manifest contains duplicate, unordered or unapproved paths")
    return names


def plain_file(root, name):
    root = root.resolve()
    path = root / name
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("Publication refuses symbolic links and junctions")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError("A reviewed public file is missing or outside the repository")
    return path


def findings(text):
    # Return labels only, never a matched secret or private path.
    return sorted(name for name, pattern in PATTERNS.items() if pattern.search(text))


def public_paths(root=ROOT):
    manifest = plain_file(root, "PUBLIC_FILES.txt")
    return [(name, plain_file(root, name)) for name in parse_manifest(manifest.read_text(encoding="utf-8"))]


def tracked_paths(root):
    if shutil.which("git") is None:
        return []
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                            capture_output=True, text=True, timeout=10)
    if result.returncode or Path(result.stdout.strip()).resolve() != root.resolve():
        return []
    result = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=root,
                            capture_output=True, timeout=10, check=True)
    return result.stdout.decode("utf-8").split("\0")[:-1]


def audit(root=ROOT):
    paths = public_paths(root)
    names = {name for name, _ in paths}
    if any(name not in names for name in tracked_paths(root)):
        raise ValueError("Git tracks files outside the public manifest; review the staged files")
    for name, path in paths:
        raw = path.read_bytes()
        if len(raw) > 2_000_000:
            raise ValueError("Public source file exceeds the review size limit")
        text = raw.decode("utf-8")
        matches = findings(text)
        if matches:
            raise ValueError(f"Public-file check failed in {name}: {', '.join(matches)}")
        if path.suffix == ".md":
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                target = target.strip("<>").split("#", 1)[0]
                if target and "://" not in target:
                    resolved = (path.parent / target).resolve()
                    if not resolved.is_relative_to(root.resolve()) or resolved.relative_to(root.resolve()).as_posix() not in names:
                        raise ValueError("Documentation links to an unpublished local file")
    return {"status": "passed", "public_files": len(paths),
            "runtime_and_private_files_included": False,
            "scope": "Reviewed manifest and selected text patterns; manual change review still required"}


def build_archive(root=ROOT):
    audit(root)
    directory = root / "dist"
    if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
        raise ValueError("Archive destination must be a local directory")
    directory.mkdir(exist_ok=True)
    destination = directory / "marvel-trading-demo.zip"
    if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("Archive destination is outside the repository")
    paths = public_paths(root)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for name, path in paths:
            info = ZipInfo("marvel-trading-demo/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return {"status": "passed", "archive": "dist/marvel-trading-demo.zip", "public_files": len(paths),
            "bytes": destination.stat().st_size, "sha256": sha256(destination.read_bytes()).hexdigest()}
