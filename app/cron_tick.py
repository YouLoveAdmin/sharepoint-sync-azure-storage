from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    request = urllib.request.Request(
        "http://127.0.0.1:8000/internal/sync-due", method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError):
        print("Cron sync check failed: local API unavailable or invalid response.", file=sys.stderr)
        return 1
    if result.get("status") not in {"disabled", "not_due"}:
        print(f"Cron sync check: {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
