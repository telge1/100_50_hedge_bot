#!/usr/bin/env python3
"""Reproduce frozen long_geometry_ladder24_be100_v1 metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from . import config as C
from .metrics import aggregate, classify_managed
from .strategy import run_period

PKG = Path(__file__).resolve().parent

EXPECTED = {
    "jun-jul": {
        "managed": {
            "trades": 94,
            "TP": 59,
            "SL": 16,
            "BE": 19,
            "pnl_sum_pct": 112.3443,
            "profit_factor": 12.4312,
            "max_drawdown_pct": 1.907,
            "max_loss_streak": 3,
        },
        "ladder_baseline_no_be": {
            "trades": 94,
            "TP": 68,
            "SL": 26,
            "pnl_sum_pct": 129.3675,
            "profit_factor": 8.3238,
            "max_drawdown_pct": 2.9833,
            "max_loss_streak": 3,
        },
    },
    "apr-may": {
        "managed": {
            "trades": 74,
            "TP": 47,
            "SL": 22,
            "BE": 4,
            "pnl_sum_pct": 78.1428,
            "profit_factor": 6.9087,
            "max_drawdown_pct": 3.1552,
            "max_loss_streak": 4,
        },
        "per_coin": {
            "XRPUSDT": {"pnl_sum_pct": 17.6493, "max_drawdown_pct": 1.5569},
            "ADAUSDT": {"pnl_sum_pct": 11.1497, "max_drawdown_pct": 1.4615},
            "DOGEUSDT": {"pnl_sum_pct": 49.3438, "max_drawdown_pct": 2.1972},
        },
    },
}


def _close(a: float | None, b: float | None, tol: float) -> bool:
    if a is None or b is None:
        return a == b
    return abs(float(a) - float(b)) <= tol


def _compare(actual: dict, expected: dict, ints: tuple[str, ...], floats: tuple[str, ...]) -> list[str]:
    errs = []
    for k in ints:
        if actual.get(k) != expected.get(k):
            errs.append(f"{k}: got {actual.get(k)} expected {expected.get(k)}")
    tol = C.PNL_TOLERANCE_PCT
    for k in floats:
        if k == "max_drawdown_pct":
            tol = C.DD_TOLERANCE_PCT
        if not _close(actual.get(k), expected.get(k), tol):
            errs.append(f"{k}: got {actual.get(k)} expected {expected.get(k)} (tol {tol})")
    return errs


def _baseline_aggregate(trades: list[dict]) -> dict:
    rows = []
    for t in trades:
        rows.append({**t, "managed_pnl_pct": t["baseline_pnl_pct"], "managed_outcome": t["baseline_first_hit"]})
    return aggregate(rows, outcome_fn=lambda x: "TP" if x["baseline_first_hit"] == "TP" else ("SL" if x["baseline_first_hit"] == "SL" else "other"))


def verify_hashes() -> bool:
    manifest_path = PKG / "manifest.json"
    hash_path = PKG / "hashes.sha256"
    if not hash_path.exists():
        return False
    lines = hash_path.read_text().strip().splitlines()
    ok = True
    for line in lines:
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip()
        if name == "hashes.sha256":
            continue
        fp = PKG / name
        if not fp.exists():
            ok = False
            continue
        got = hashlib.sha256(fp.read_bytes()).hexdigest()
        if got != digest:
            ok = False
    return ok


def write_hashes() -> None:
    files = sorted(
        p.relative_to(PKG).as_posix()
        for p in PKG.rglob("*")
        if p.is_file() and p.name != "hashes.sha256"
    )
    lines = []
    for name in files:
        digest = hashlib.sha256((PKG / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    digest_self = hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()
    lines.append(f"{digest_self}  hashes.sha256")
    (PKG / "hashes.sha256").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reproduce long_geometry_ladder24_be100_v1")
    parser.add_argument("--period", choices=list(C.PERIODS), required=False)
    parser.add_argument("--write-hashes", action="store_true")
    parser.add_argument("--verify-hashes", action="store_true")
    args = parser.parse_args(argv)

    if args.write_hashes:
        write_hashes()
        print("Wrote hashes.sha256")
        return 0
    if args.verify_hashes:
        ok = verify_hashes()
        print("HASH_VERIFY_PASS" if ok else "HASH_VERIFY_FAIL")
        return 0 if ok else 1

    if not args.period:
        parser.error("--period is required unless using --write-hashes or --verify-hashes")
    trades, meta = run_period(args.period)
    managed = aggregate(trades)
    baseline = _baseline_aggregate(trades)

    out = {
        "period": args.period,
        "managed": managed,
        "ladder_baseline_no_be": baseline,
        "causality": meta,
    }
    print(json.dumps(out, indent=2))

    exp = EXPECTED[args.period]
    errs = _compare(managed, exp["managed"], ("trades", "TP", "SL", "BE", "max_loss_streak"), ("pnl_sum_pct", "profit_factor", "max_drawdown_pct"))
    if "ladder_baseline_no_be" in exp:
        b = exp["ladder_baseline_no_be"]
        errs += _compare(
            baseline,
            b,
            ("trades", "TP", "SL", "max_loss_streak"),
            ("pnl_sum_pct", "profit_factor", "max_drawdown_pct"),
        )
    if args.period == "apr-may" and "per_coin" in exp:
        for sym, ex in exp["per_coin"].items():
            sub = aggregate([t for t in trades if t["symbol"] == sym])
            if not _close(sub["pnl_sum_pct"], ex["pnl_sum_pct"], C.PNL_TOLERANCE_PCT):
                errs.append(f"{sym} pnl {sub['pnl_sum_pct']} vs {ex['pnl_sum_pct']}")
            if not _close(sub["max_drawdown_pct"], ex["max_drawdown_pct"], C.DD_TOLERANCE_PCT):
                errs.append(f"{sym} dd {sub['max_drawdown_pct']} vs {ex['max_drawdown_pct']}")

    if errs:
        print("REPRODUCTION_FAIL")
        for e in errs:
            print(" -", e)
        return 1
    print("REPRODUCTION_PASS")
    print("CAUSALITY_PASS" if meta.get("causality_pass") else "CAUSALITY_FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
