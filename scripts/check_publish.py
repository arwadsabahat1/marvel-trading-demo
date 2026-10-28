"""Audit only the public manifest; never open local account records."""

import json
from public_files import audit

if __name__ == "__main__":
    try:
        print(json.dumps(audit(), indent=2))
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}))
        raise SystemExit(1)
