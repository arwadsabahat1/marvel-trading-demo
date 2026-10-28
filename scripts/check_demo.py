"""Run both implementation suites and publication checks in a fresh offline copy."""

import json
import re
from pathlib import Path
from hashlib import sha256
from demo_workspace import isolated
from public_files import ROOT, public_paths

SUITE = '''import unittest
loader = unittest.TestLoader()
suite = unittest.TestSuite(loader.discover(folder, top_level_dir=".")
    for folder in ("tests", "tests_testnet", "tests_public", "tests_ops"))
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''


def main():
    before = {name: sha256(path.read_bytes()).hexdigest() for name, path in public_paths()}
    with isolated() as (checkout, run):
        suite = run(["-c", SUITE])
        tests = int(re.search(r"Ran (\d+) tests", suite.stderr)[1])
        print(f"Offline test suites passed: {tests} tests", flush=True)
        checks = 0
        def cli(package, *arguments, expected=0):
            nonlocal checks
            data = json.loads(run(["-m", package, *arguments], expected=expected).stdout)
            checks += 1
            if data.get("live_execution_available", False) or data.get("production_execution_available", False):
                raise RuntimeError("Demo unexpectedly reported production execution")
            return data
        cli("marvel", "validate")
        cli("marvel", "status", expected=2)
        first = cli("marvel", "init")
        second = cli("marvel", "init")
        if first["account"] != second["account"] or second["new_account_created"]:
            raise RuntimeError("Initialization changed the existing simulated account")
        cli("marvel", "status")
        cli("marvel", "risk", "status")
        cli("marvel", "risk", "example")
        cli("marvel", "paper", "demo")
        cli("marvel", "research", "plan")
        research = cli("marvel", "research", "demo")["research_screen"]
        cli("marvel", "research", "proxy-screen", "--dataset", research["dataset_id"])
        cli("marvel", "research", "compare", "--dataset", research["dataset_id"])
        cli("marvel", "research", "convert-demo")
        cli("marvel", "research", "cycle-demo")
        cli("marvel_testnet", "status", expected=2)
        initialized = cli("marvel_testnet", "init")
        repeated = cli("marvel_testnet", "init")
        if initialized["result"]["revision"] != repeated["result"]["revision"]:
            raise RuntimeError("Testnet initialization changed its allocation")
        cli("marvel_testnet", "status")
        for command in ("public-check", "preflight", "cycle", "reconcile", "recover"):
            cli("marvel_testnet", command, expected=2)
        missing = cli("marvel_testnet", "preflight", "--allow-network", expected=2)
        if missing["error"]["code"] != "TESTNET_CREDENTIALS_MISSING":
            raise RuntimeError("Credential-free preflight was not rejected")
        cli("marvel_testnet", "cycle", "--allow-network", "--acknowledge-virtual-orders", expected=2)
        cli("marvel_testnet", "init", "--live", expected=2)
        cli("marvel_ops", "status", expected=2)
        cli("marvel_ops", "init")
        cli("marvel_ops", "init")
        cli("marvel_ops", "status")
        if cli("marvel_ops", "readiness")["operations"]["live_entries_allowed"]:
            raise RuntimeError("Missing operational evidence allowed live entries")
        cli("marvel_ops", "health")
        cli("marvel_ops", "backup")
        cli("marvel_ops", "validate", "--dataset", research["dataset_id"])
        cli("marvel_ops", "finalize-validation", "--development-record", "a" * 64, expected=2)
        cli("marvel_ops", "worker", expected=2)
        cli("marvel_ops", "--mode", "live", "init")
        cli("marvel_ops", "--mode", "live", "readiness")
        cli("marvel_ops", "--mode", "live", "enter", "--stop-price", "78400", "--strategy", "btc-breakout-1h-v1",
            "--allow-network", "--process-credentials", "--acknowledge-orders", expected=2)
        if cli("marvel", "status")["account"] != first["account"]:
            raise RuntimeError("Disposable demos changed the simulated main account")
        run(["scripts/run_demo.py"])
    after = {name: sha256(path.read_bytes()).hexdigest() for name, path in public_paths()}
    if before != after:
        raise RuntimeError("Source or documentation changed during verification; rerun the affected checks")
    record = {"status": "passed", "tests": tests, "command_checks": checks,
                      "fresh_source_copy": True, "network_connections_allowed": False,
                      "provider_credentials_used": False, "production_orders": 0,
                      "public_sha256": after}
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/demo-check-results.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in record.items() if k != "public_sha256"}, indent=2))


if __name__ == "__main__":
    main()
