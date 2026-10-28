"""Build an audited archive from the source manifest, never the working tree."""

import json
from public_files import build_archive

if __name__ == "__main__":
    try:
        print(json.dumps(build_archive(), indent=2))
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}))
        raise SystemExit(1)
