#!/usr/bin/env python3
"""Build room-to-target config v1 report and run unit tests."""

from __future__ import annotations

import json
import sys

from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.report import (
    build_report,
    run_tests_and_persist,
)


def main() -> int:
    report = build_report()
    tests = run_tests_and_persist()
    report["tests"] = tests
    if not tests["passed"]:
        report["verdict"] = "MIN_TARGET_DISTANCE_CONFIG_V1_TEST_FAILURE"
    print(json.dumps({"verdict": report["verdict"], "tests_passed": tests["passed"]}, indent=2))
    return 0 if tests["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
