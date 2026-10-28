"""Disposable source copies with credential-free, network-blocked Python children."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from public_files import ROOT, audit, public_paths

BOOTSTRAP = '''import socket
def _offline(*args, **kwargs):
    raise OSError("Network disabled during offline demo verification")
socket.create_connection = _offline
socket.getaddrinfo = _offline
socket.socket.connect = _offline
socket.socket.connect_ex = _offline
'''


@contextmanager
def isolated():
    if sys.version_info[:2] != (3, 12) or sys.implementation.name != "cpython":
        raise RuntimeError("Use CPython 3.12")
    audit()
    scratch = ROOT / ".test-tmp"
    if scratch.is_symlink() or getattr(scratch, "is_junction", lambda: False)():
        raise RuntimeError("Disposable workspaces must remain inside the repository")
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="offline-demo-", dir=scratch) as folder:
        temporary = Path(folder)
        checkout, bootstrap = temporary / "source", temporary / "bootstrap"
        checkout.mkdir()
        bootstrap.mkdir()
        for name, source in public_paths():
            destination = checkout / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        (bootstrap / "sitecustomize.py").write_text(BOOTSTRAP, encoding="utf-8")
        environment = {name: value for name, value in os.environ.items()
            if not any(part in name.upper() for part in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "APIKEY", "PRIVATE_KEY"))}
        for name in ("PYTHONHOME", "PYTHONSTARTUP"):
            environment.pop(name, None)
        environment.update(PYTHONPATH=str(bootstrap) + os.pathsep + str(checkout),
                           PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        def run(arguments, expected=0, timeout=600):
            result = subprocess.run([sys.executable, *arguments], cwd=checkout, env=environment,
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode != expected:
                raise RuntimeError("Offline check failed: " + " ".join(arguments)
                                   + "\n" + result.stdout + result.stderr)
            return result
        yield checkout, run
