"""Print a human-readable fill walkthrough from audit artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from .manual_order_timeline import POLICIES, build_policy_timeline, format_terminal_walkthrough

DEFAULT_AUDIT_DIR = (
    Path(__file__).resolve().parent / "results" / "apt_full_order_audit_20260725"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print chronological fill walkthrough for Cobertura net-BE audit"
    )
    parser.add_argument(
        "--policy",
        default="all",
        choices=[*POLICIES, "all"],
        help="Policy to print (default: all)",
    )
    parser.add_argument(
        "--audit-dir",
        type=Path,
        default=DEFAULT_AUDIT_DIR,
        help="Existing apt_full_order_audit directory",
    )
    args = parser.parse_args(argv)

    audit_dir = Path(args.audit_dir)
    if not audit_dir.exists():
        raise SystemExit(f"audit dir not found: {audit_dir}")

    policies = list(POLICIES) if args.policy == "all" else [args.policy]
    for policy in policies:
        data = build_policy_timeline(policy=policy, audit_dir=audit_dir)
        print(format_terminal_walkthrough(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
