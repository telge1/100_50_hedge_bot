"""Post-handoff APT candle replay using the existing CoberturaEngine.

Starts from the completed bundle handoff artifacts (qty-neutral seeded book)
and runs bar-by-bar from signal_available_ts forward. Does not re-run handoff.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.multicoin_price_staging_grid import atomic_write_json, atomic_write_text

from .config import CoberturaConfig
from .runner import run_cobertura

DEFAULT_HANDOFF_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "apt_cobertura_bundle_handoff_20260726"
)
DEFAULT_OUTPUT_DIR = Path(
    "research/backtests/cobertura_0_notional_strategie/results/"
    "apt_cobertura_post_handoff_replay_20260726"
)
START_TS = "2026-01-19T00:00:00+00:00"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cfg_from_handoff(handoff_dir: Path) -> CoberturaConfig:
    snap = _load_json(handoff_dir / "config_snapshot.json")
    raw = snap.get("cobertura_config") or snap
    if not isinstance(raw, dict):
        raise ValueError("handoff config_snapshot.json missing cobertura_config")
    # Full candle history for post-handoff replay (handoff used candle_limit=1).
    raw = dict(raw)
    raw["candle_limit"] = 50_000
    raw["start_timestamp"] = START_TS
    raw["end_timestamp"] = None
    tags = dict(raw.get("tags") or {})
    tags["post_handoff_replay"] = True
    tags["handoff_dir"] = str(handoff_dir)
    raw["tags"] = tags
    return CoberturaConfig.from_dict(raw)


def assert_handoff_ready(handoff_dir: Path) -> dict[str, Any]:
    inv = _load_json(handoff_dir / "handoff_invariants.json")
    decision = str(inv.get("decision") or "")
    if "PASS" not in decision:
        raise SystemExit(f"handoff not PASS: {decision}")
    after = _load_json(handoff_dir / "handoff_state_after_neutralization.json")
    pos = after.get("position") or {}
    if pos.get("net_qty") is None or abs(float(pos["net_qty"])) > 1e-9:
        raise SystemExit(f"handoff net_qty not zero: {pos.get('net_qty')}")
    return {"invariants": inv, "after": after}


def write_replay_sidecar(
    output_dir: Path,
    *,
    handoff_dir: Path,
    handoff_meta: dict[str, Any],
    result: Any,
) -> None:
    provenance = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "handoff_dir": str(handoff_dir),
        "handoff_decision": (handoff_meta.get("invariants") or {}).get("decision"),
        "start_timestamp": START_TS,
        "final_state": result.state,
        "exit_reason": result.exit_reason,
        "bars_processed": result.bars_processed,
        "recovery_rounds": result.recovery_rounds,
        "locked_spread_loss": result.locked_spread_loss,
        "seed_position": (handoff_meta.get("after") or {}).get("position"),
        "prior_economics": (handoff_meta.get("after") or {}).get("economics"),
    }
    atomic_write_json(output_dir / "post_handoff_provenance.json", provenance)

    after_pos = (handoff_meta.get("after") or {}).get("position") or {}
    lines = [
        "# APT Cobertura Post-Handoff Candle Replay",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Source",
        "",
        f"- handoff_dir: `{handoff_dir}`",
        f"- handoff_decision: `{(handoff_meta.get('invariants') or {}).get('decision')}`",
        f"- start_timestamp: `{START_TS}`",
        f"- seed long/short: `{after_pos.get('long_qty')}` / `{after_pos.get('short_qty')}`",
        f"- seed avgs: `{after_pos.get('long_avg')}` / `{after_pos.get('short_avg')}`",
        "",
        "## Replay result",
        "",
        f"- final_state: `{result.state}`",
        f"- exit_reason: `{result.exit_reason}`",
        f"- bars_processed: `{result.bars_processed}`",
        f"- recovery_rounds: `{result.recovery_rounds}`",
        f"- locked_spread_loss: `{result.locked_spread_loss}`",
        f"- overlay_short_final: `{result.ledger.overlay_short.qty}`",
        f"- realized_overlay_pnl: `{result.ledger.realized_overlay_pnl}`",
        f"- cumulative_entry_fees: `{result.ledger.cumulative_entry_fees}`",
        f"- cumulative_close_fees: `{result.ledger.cumulative_close_fees}`",
        "",
        "Standard Cobertura artifacts were written by `write_run_artifacts`.",
        "",
    ]
    atomic_write_text(output_dir / "POST_HANDOFF_REPLAY.md", "\n".join(lines) + "\n")


def run_post_handoff_replay(
    *,
    handoff_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    handoff_dir = Path(handoff_dir)
    output_dir = Path(output_dir)
    if output_dir.resolve() == handoff_dir.resolve():
        raise SystemExit("refusing to overwrite handoff output directory")

    meta = assert_handoff_ready(handoff_dir)
    cfg = load_cfg_from_handoff(handoff_dir)
    cfg.output_dir = str(output_dir)
    cfg.run_id = output_dir.name
    cfg.validate()

    result = run_cobertura(cfg, write_outputs=True)
    write_replay_sidecar(
        output_dir, handoff_dir=handoff_dir, handoff_meta=meta, result=result
    )
    return {
        "output_dir": str(output_dir),
        "state": result.state,
        "exit_reason": result.exit_reason,
        "bars_processed": result.bars_processed,
        "recovery_rounds": result.recovery_rounds,
        "locked_spread_loss": result.locked_spread_loss,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="APT post-handoff Cobertura candle replay (no handoff re-run)."
    )
    p.add_argument("--handoff-dir", type=Path, default=DEFAULT_HANDOFF_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = run_post_handoff_replay(
        handoff_dir=args.handoff_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
