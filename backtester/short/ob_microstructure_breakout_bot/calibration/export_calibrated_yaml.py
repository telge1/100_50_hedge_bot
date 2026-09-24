"""Write nested long/short YAML from Phase A/B event quantiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _q(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = int(round((len(ys) - 1) * p))
    return float(ys[max(0, min(len(ys) - 1, i))])


def _round_nice(x: float) -> float:
    if x >= 100_000:
        return float(round(x / 10_000) * 10_000)
    if x >= 10_000:
        return float(round(x / 1_000) * 1_000)
    return float(round(x / 100) * 100)


def _load_events(path: Path) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text(encoding="utf-8")).get("events", []))


def build_yaml_text(
    *,
    symbol: str,
    strong: list[dict[str, Any]],
    fake: list[dict[str, Any]],
) -> str:
    def abs_conf(evs: list[dict[str, Any]], direction: str) -> list[float]:
        return [abs(float(e["confirm_delta"])) for e in evs if e.get("direction") == direction]

    def ft_against(evs: list[dict[str, Any]], direction: str) -> list[float]:
        out: list[float] = []
        for e in evs:
            if e.get("direction") != direction:
                continue
            ft = float(e["followthrough_delta"])
            if direction == "long" and ft < 0:
                out.append(abs(ft))
            if direction == "short" and ft > 0:
                out.append(abs(ft))
        return out

    long_s = abs_conf(strong, "long")
    long_f = abs_conf(fake, "long")
    short_s = abs_conf(strong, "short")
    short_f = abs_conf(fake, "short")
    long_flip = ft_against(fake, "long")
    short_flip = ft_against(fake, "short")

    long_fake_max = _round_nice(_q(long_f, 0.25) or 40_000)
    long_t1 = _round_nice(_q(long_s, 0.25) or 160_000)
    long_t2 = _round_nice(_q(long_s, 0.50) or 350_000)
    if long_fake_max >= long_t1:
        long_fake_max = _round_nice(long_t1 * 0.4)
    if long_t1 >= long_t2:
        long_t2 = _round_nice(long_t1 * 1.8)
    long_ft = -_round_nice(_q(long_flip, 0.25) or 180_000)

    short_fake_max = _round_nice(_q(short_f, 0.25) or 50_000)
    short_t1 = _round_nice(_q(short_s, 0.25) or 360_000)
    short_t2 = _round_nice(_q(short_s, 0.50) or 730_000)
    if short_fake_max >= short_t1:
        short_fake_max = _round_nice(short_t1 * 0.2)
    if short_t1 >= short_t2:
        short_t2 = _round_nice(short_t1 * 1.8)
    short_ft = _round_nice(_q(short_flip, 0.25) or 170_000)

    return f"""# Auto-generated from Phase A/B labeled events.
# Review before promoting over doge_usdt.yaml.

symbol: {symbol}

context_lookback_minutes: 30
confirm_window_minutes: 5
followthrough_candles: 2

long:
  fakeout_max_confirm_delta: {long_fake_max}
  tier1_min_confirm_delta: {long_t1}
  tier2_min_confirm_delta: {long_t2}
  tier1_min_ob_ratio_5bps: 1.05
  tier2_min_ob_ratio_5bps: 1.50
  fakeout_followthrough_flip_delta: {long_ft}
  require_ob_support: true

short:
  fakeout_max_confirm_delta: {short_fake_max}
  tier1_min_confirm_delta: {short_t1}
  tier2_min_confirm_delta: {short_t2}
  tier1_min_ob_ratio_5bps: 1.00
  tier2_min_ob_ratio_5bps: 1.20
  fakeout_followthrough_flip_delta: {short_ft}
  require_ob_support: false
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export calibrated long/short YAML")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--strong", type=Path, required=False)
    parser.add_argument("--fakeouts", type=Path, required=False)
    parser.add_argument("--out", type=Path, required=False)
    args = parser.parse_args(argv)

    symbol = args.symbol.upper().replace("/", "")
    events_dir = Path(__file__).resolve().parent / "events"
    strong_path = args.strong or events_dir / f"{symbol}_strong_breakouts_phase_a.json"
    fake_path = args.fakeouts or events_dir / f"{symbol}_fakeouts_phase_b.json"
    out = args.out or (
        Path(__file__).resolve().parents[1] / "config" / "doge_usdt_calibrated.yaml"
    )

    text = build_yaml_text(
        symbol=symbol,
        strong=_load_events(strong_path),
        fake=_load_events(fake_path),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
