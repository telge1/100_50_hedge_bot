"""stdin JSON -> one landscape window payload on stdout. No dashboard main import."""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    if "--ping" in sys.argv:
        json.dump({"ok": True, "pid": os.getpid()}, sys.stdout)
        return
    raw = sys.stdin.read() or "{}"
    req = json.loads(raw)
    if not isinstance(req, dict):
        raise SystemExit("window request must be a JSON object")
    from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response

    payload = get_landscape_window_response(**req)
    if payload is None:
        raise SystemExit("window build returned no payload")
    json.dump(payload, sys.stdout)


if __name__ == "__main__":
    main()
