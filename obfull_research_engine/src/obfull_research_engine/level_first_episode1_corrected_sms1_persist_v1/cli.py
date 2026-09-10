"""CLI for LEVEL_FIRST_EPISODE1_CORRECTED_SMS1_PERSIST_V1.

Default: existing persist audit.
Subcommands:
  e2e               one fresh raw→persist→readback→derived process
  prove             two independent e2e processes + oracle + A/B
  prove-independent two fresh independent-derivation e2e runs + oracle + A/B
  oracle            audit one persist directory
  causal-audit      derived causal availability audit
  touch-provenance  first_touch origin / independent-derivation check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runner import main as persist_main

SUBCOMMANDS = {"e2e", "prove", "prove-independent", "oracle", "causal-audit", "touch-provenance"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"-h", "--help"}:
        print(
            "usage: ...cli [{e2e,prove,prove-independent,oracle,causal-audit,touch-provenance}|persist-args]\n"
            "  (default without subcommand runs the corrected sms1 persist audit)"
        )
        return 0
    if not argv or argv[0] not in SUBCOMMANDS:
        return persist_main()
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "e2e":
        from .e2e import main as e2e_main

        return e2e_main(rest)
    if cmd == "prove":
        parser = argparse.ArgumentParser()
        parser.add_argument("--skip-tests", action="store_true")
        args = parser.parse_args(rest)
        from .prove import run_prove

        result = run_prove(skip_tests=args.skip_tests)
        return 0 if result.get("verdict") else 1
    if cmd == "prove-independent":
        from .prove_independent import run_prove_independent

        result = run_prove_independent()
        return 0 if result.get("verdict") == "EPISODE1_ZONE_TOUCH_DETECTION_RAW_DERIVATION_EVENT_TIME_PROVEN" else 1
    if cmd == "oracle":
        parser = argparse.ArgumentParser()
        parser.add_argument("--dir", required=True)
        args = parser.parse_args(rest)
        from ..paths import ENGINE_ROOT
        from .oracle import audit_directory

        cfg = json.loads((ENGINE_ROOT / "config/level_first_episode1_corrected_sms1_persist_v1.json").read_text(encoding="utf-8"))
        out = audit_directory(Path(args.dir), cfg=cfg)
        print(json.dumps({k: out.get(k) for k in ("derived_false_positives", "derived_false_negatives", "causality_violations", "refill_misclassifications", "walls", "refills", "touches", "detections")}, indent=2, default=str))
        return 0 if out.get("derived_false_positives") == 0 and out.get("derived_false_negatives") == 0 else 1
    if cmd == "causal-audit":
        from .causal_audit import main as causal_main

        return causal_main(rest)
    if cmd == "touch-provenance":
        from .touch_provenance import main as touch_main

        return touch_main(rest)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
