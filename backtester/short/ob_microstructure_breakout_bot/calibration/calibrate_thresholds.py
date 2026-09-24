"""Phase C/D: calibrate CoinThresholds from labeled strong/fakeout events.

Uses Phase-A strong breakouts and Phase-B fakeouts. Metrics and search are
reported for long and short separately, while proposing one shared YAML
(same mirrored thresholds the rule engine already uses).
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ob_microstructure_breakout_bot.models import (
    CoinThresholds,
    EmaSnapshot,
    MarketState,
    ObBandSnapshot,
    TradeWindowStats,
)
from ob_microstructure_breakout_bot.rule_engine import (
    classify_long_breakout,
    classify_short_breakout,
)
from ob_microstructure_breakout_bot.thresholds import load_thresholds, make_thresholds


@dataclass(frozen=True)
class LabeledEvent:
    symbol: str
    label: str  # strong_breakout | fakeout
    direction: str  # long | short
    bar_ts: datetime
    confirm: TradeWindowStats
    followthrough: TradeWindowStats
    ob: ObBandSnapshot | None
    ema: EmaSnapshot | None


@dataclass(frozen=True)
class SideMetrics:
    n: int
    n_strong: int
    n_fake: int
    strong_recall: float  # strong → BREAKOUT_CONFIRMED
    breakout_precision: float  # among BREAKOUT_CONFIRMED, share strong
    fake_reject: float  # fake → not BREAKOUT_CONFIRMED
    fake_as_fakeout: float  # fake → FAKEOUT
    score: float


@dataclass(frozen=True)
class EvalResult:
    overall: SideMetrics
    long: SideMetrics
    short: SideMetrics


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _win_from_dict(d: dict[str, Any] | None) -> TradeWindowStats:
    d = d or {}
    return TradeWindowStats(
        buy_notional=float(d.get("buy_notional") or 0.0),
        sell_notional=float(d.get("sell_notional") or 0.0),
        trade_count=int(d.get("trade_count") or 0),
        open_price=d.get("open_price"),
        close_price=d.get("close_price"),
        high=d.get("high"),
        low=d.get("low"),
    )


def _ob_from_dict(d: dict[str, Any] | None, ok: bool) -> ObBandSnapshot | None:
    if not ok or not d:
        return None
    return ObBandSnapshot(
        bid_5bps=float(d.get("bid_5bps") or 0.0),
        ask_5bps=float(d.get("ask_5bps") or 0.0),
        bid_10bps=float(d.get("bid_10bps") or 0.0),
        ask_10bps=float(d.get("ask_10bps") or 0.0),
    )


def _ema_from_dict(d: dict[str, Any] | None) -> EmaSnapshot | None:
    if not d:
        return None
    return EmaSnapshot(
        ema9=float(d["ema9"]),
        ema20=float(d["ema20"]),
        ema59=float(d["ema59"]),
        price=float(d["price"]),
        ema200=None if d.get("ema200") is None else float(d["ema200"]),
    )


def load_labeled_events(
    strong_path: Path,
    fakeout_path: Path,
) -> list[LabeledEvent]:
    out: list[LabeledEvent] = []
    for path, label in ((strong_path, "strong_breakout"), (fakeout_path, "fakeout")):
        data = json.loads(path.read_text(encoding="utf-8"))
        symbol = str(data.get("symbol") or "UNKNOWN")
        for ev in data.get("events", []):
            out.append(
                LabeledEvent(
                    symbol=symbol,
                    label=label,
                    direction=str(ev["direction"]),
                    bar_ts=_parse_ts(ev["bar_ts"]),
                    confirm=_win_from_dict(ev.get("confirm")),
                    followthrough=_win_from_dict(ev.get("followthrough")),
                    ob=_ob_from_dict(ev.get("ob"), bool(ev.get("ob_ok", True))),
                    ema=_ema_from_dict(ev.get("ema")),
                )
            )
    out.sort(key=lambda e: e.bar_ts)
    return out


def classify_event(thresholds: CoinThresholds, ev: LabeledEvent):
    kwargs = dict(
        thresholds=thresholds,
        confirm=ev.confirm,
        followthrough=ev.followthrough,
        ob_at_event=ev.ob,
        ema=ev.ema,
    )
    if ev.direction == "long":
        return classify_long_breakout(**kwargs)
    return classify_short_breakout(**kwargs)


def _side_metrics(events: list[LabeledEvent], preds: list[MarketState]) -> SideMetrics:
    n = len(events)
    if n == 0:
        return SideMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)

    strong_idx = [i for i, e in enumerate(events) if e.label == "strong_breakout"]
    fake_idx = [i for i, e in enumerate(events) if e.label == "fakeout"]
    n_strong = len(strong_idx)
    n_fake = len(fake_idx)

    strong_hits = sum(1 for i in strong_idx if preds[i] == MarketState.BREAKOUT_CONFIRMED)
    pred_break = [i for i, p in enumerate(preds) if p == MarketState.BREAKOUT_CONFIRMED]
    tp = sum(1 for i in pred_break if events[i].label == "strong_breakout")
    fake_reject = sum(1 for i in fake_idx if preds[i] != MarketState.BREAKOUT_CONFIRMED)
    fake_as_fo = sum(1 for i in fake_idx if preds[i] == MarketState.FAKEOUT)

    recall = strong_hits / n_strong if n_strong else 0.0
    precision = tp / len(pred_break) if pred_break else 0.0
    reject = fake_reject / n_fake if n_fake else 0.0
    as_fo = fake_as_fo / n_fake if n_fake else 0.0

    # Prefer catching strong moves without promoting fakeouts to breakouts.
    score = 0.40 * recall + 0.35 * precision + 0.25 * reject
    # Soft penalty if almost no strong hits while claiming high score via reject-all.
    if n_strong and recall < 0.20:
        score *= 0.5
    return SideMetrics(
        n=n,
        n_strong=n_strong,
        n_fake=n_fake,
        strong_recall=recall,
        breakout_precision=precision,
        fake_reject=reject,
        fake_as_fakeout=as_fo,
        score=score,
    )


def evaluate(thresholds: CoinThresholds, events: list[LabeledEvent]) -> EvalResult:
    preds = [classify_event(thresholds, ev).state for ev in events]
    overall = _side_metrics(events, preds)
    long_e = [(e, p) for e, p in zip(events, preds) if e.direction == "long"]
    short_e = [(e, p) for e, p in zip(events, preds) if e.direction == "short"]
    return EvalResult(
        overall=overall,
        long=_side_metrics([e for e, _ in long_e], [p for _, p in long_e]),
        short=_side_metrics([e for e, _ in short_e], [p for _, p in short_e]),
    )


def _quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    i = int(round((len(ys) - 1) * q))
    return float(ys[max(0, min(len(ys) - 1, i))])


def _candidate_grid(events: list[LabeledEvent]) -> list[CoinThresholds]:
    """Data-driven grid around strong/fake quantiles (shared long/short YAML)."""
    strong_abs = [
        abs(e.confirm.delta_notional)
        for e in events
        if e.label == "strong_breakout"
    ]
    fake_abs = [abs(e.confirm.delta_notional) for e in events if e.label == "fakeout"]
    strong_ft_against = []
    for e in events:
        if e.label != "fakeout":
            continue
        # Against the setup: long fail → negative FT; short fail → positive FT.
        ft = e.followthrough.delta_notional
        if e.direction == "long" and ft < 0:
            strong_ft_against.append(abs(ft))
        elif e.direction == "short" and ft > 0:
            strong_ft_against.append(abs(ft))

    long_ratios = []
    short_ratios = []
    for e in events:
        if e.ob is None:
            continue
        if e.direction == "long" and e.ob.ask_5bps > 0:
            long_ratios.append(e.ob.bid_5bps / e.ob.ask_5bps)
        if e.direction == "short" and e.ob.bid_5bps > 0:
            short_ratios.append(e.ob.ask_5bps / e.ob.bid_5bps)

    tier1_opts = sorted(
        {
            round(x, -3)
            for x in (
                50_000,
                75_000,
                100_000,
                125_000,
                150_000,
                _quantile(strong_abs, 0.25),
                _quantile(fake_abs, 0.75),
            )
            if x > 0
        }
    )
    tier2_opts = sorted(
        {
            round(x, -3)
            for x in (
                150_000,
                200_000,
                250_000,
                300_000,
                400_000,
                _quantile(strong_abs, 0.50),
                _quantile(strong_abs, 0.75),
            )
            if x > 0
        }
    )
    fake_max_opts = sorted(
        {
            round(x, -3)
            for x in (
                25_000,
                50_000,
                75_000,
                100_000,
                _quantile(fake_abs, 0.25),
                _quantile(fake_abs, 0.50),
            )
            if x > 0
        }
    )
    flip_opts = sorted(
        {
            -round(x, -3)
            for x in (
                50_000,
                75_000,
                100_000,
                150_000,
                200_000,
                _quantile(strong_ft_against, 0.25) if strong_ft_against else 100_000,
                _quantile(strong_ft_against, 0.50) if strong_ft_against else 100_000,
            )
            if x > 0
        }
    )
    t1_ratio_opts = sorted(
        {
            round(x, 2)
            for x in (
                1.00,
                1.05,
                1.10,
                1.15,
                1.20,
                _quantile(long_ratios, 0.25) if long_ratios else 1.05,
                _quantile(short_ratios, 0.50) if short_ratios else 1.05,
            )
            if x >= 1.0
        }
    )
    t2_ratio_opts = sorted(
        {
            round(x, 2)
            for x in (1.5, 1.8, 2.0, 2.2, 2.5, 2.8, 3.0)
            if x >= 1.2
        }
    )

    symbol = events[0].symbol if events else "DOGEUSDT"
    cands: list[CoinThresholds] = []
    for fake_max, t1, t2, flip, r1, r2 in itertools.product(
        fake_max_opts, tier1_opts, tier2_opts, flip_opts, t1_ratio_opts, t2_ratio_opts
    ):
        if not (fake_max < t1 < t2):
            continue
        if not (r1 < r2):
            continue
        if flip >= 0:
            continue
        cands.append(
            make_thresholds(
                symbol=symbol,
                fakeout_max_confirm_delta=float(fake_max),
                tier1_min_confirm_delta=float(t1),
                tier2_min_confirm_delta=float(t2),
                tier1_min_bid_ask_ratio_5bps=float(r1),
                tier2_min_bid_ask_ratio_5bps=float(r2),
                fakeout_followthrough_flip_delta=float(flip),
            )
        )
    return cands


def walk_forward_split(
    events: list[LabeledEvent], train_frac: float = 0.7
) -> tuple[list[LabeledEvent], list[LabeledEvent]]:
    if not events:
        return [], []
    cut = max(1, int(len(events) * train_frac))
    # Keep chronological split but ensure both labels appear in train when possible.
    train, test = events[:cut], events[cut:]
    return train, test


def search_thresholds(
    train: list[LabeledEvent],
    *,
    min_long_recall: float = 0.30,
    min_short_recall: float = 0.20,
) -> tuple[CoinThresholds, EvalResult, int]:
    best: CoinThresholds | None = None
    best_eval: EvalResult | None = None
    n = 0
    for th in _candidate_grid(train):
        n += 1
        ev = evaluate(th, train)
        if ev.long.n_strong and ev.long.strong_recall < min_long_recall:
            continue
        if ev.short.n_strong and ev.short.strong_recall < min_short_recall:
            continue
        # Primary: overall score; tie-break: min(long, short) score then long recall.
        key = (
            ev.overall.score,
            min(ev.long.score, ev.short.score),
            ev.long.strong_recall,
            ev.short.strong_recall,
        )
        if best is None or best_eval is None:
            best, best_eval = th, ev
            continue
        best_key = (
            best_eval.overall.score,
            min(best_eval.long.score, best_eval.short.score),
            best_eval.long.strong_recall,
            best_eval.short.strong_recall,
        )
        if key > best_key:
            best, best_eval = th, ev

    if best is None or best_eval is None:
        # Fallback: ignore side floors, take best overall.
        for th in _candidate_grid(train):
            n += 1
            ev = evaluate(th, train)
            if best is None or best_eval is None or ev.overall.score > best_eval.overall.score:
                best, best_eval = th, ev
    assert best is not None and best_eval is not None
    return best, best_eval, n


def _metrics_dict(m: SideMetrics) -> dict[str, Any]:
    return asdict(m)


def _thresholds_dict(th: CoinThresholds) -> dict[str, Any]:
    return {
        "symbol": th.symbol,
        "fakeout_max_confirm_delta": th.fakeout_max_confirm_delta,
        "tier1_min_confirm_delta": th.tier1_min_confirm_delta,
        "tier2_min_confirm_delta": th.tier2_min_confirm_delta,
        "tier1_min_bid_ask_ratio_5bps": th.tier1_min_bid_ask_ratio_5bps,
        "tier2_min_bid_ask_ratio_5bps": th.tier2_min_bid_ask_ratio_5bps,
        "fakeout_followthrough_flip_delta": th.fakeout_followthrough_flip_delta,
        "context_lookback_minutes": th.context_lookback_minutes,
        "confirm_window_minutes": th.confirm_window_minutes,
        "followthrough_candles": th.followthrough_candles,
    }


def suggest_yaml(th: CoinThresholds, *, note: str) -> str:
    return f"""# Suggested DOGEUSDT thresholds from Phase C calibration.
# {note}
#
# Review before replacing config/doge_usdt.yaml.

symbol: {th.symbol}

fakeout_max_confirm_delta: {th.fakeout_max_confirm_delta}
tier1_min_confirm_delta: {th.tier1_min_confirm_delta}
tier2_min_confirm_delta: {th.tier2_min_confirm_delta}

tier1_min_bid_ask_ratio_5bps: {th.tier1_min_bid_ask_ratio_5bps}
tier2_min_bid_ask_ratio_5bps: {th.tier2_min_bid_ask_ratio_5bps}

fakeout_followthrough_flip_delta: {th.fakeout_followthrough_flip_delta}

context_lookback_minutes: {th.context_lookback_minutes}
confirm_window_minutes: {th.confirm_window_minutes}
followthrough_candles: {th.followthrough_candles}
"""


def run_calibration(
    *,
    symbol: str,
    strong_path: Path,
    fakeout_path: Path,
    train_frac: float = 0.7,
) -> dict[str, Any]:
    events = load_labeled_events(strong_path, fakeout_path)
    if not events:
        raise RuntimeError("No labeled events loaded")

    baseline = load_thresholds(symbol)
    # Preserve window fields from current YAML on candidates.
    def with_windows(th: CoinThresholds) -> CoinThresholds:
        return replace(
            th,
            context_lookback_minutes=baseline.context_lookback_minutes,
            confirm_window_minutes=baseline.confirm_window_minutes,
            followthrough_candles=baseline.followthrough_candles,
        )

    train, test = walk_forward_split(events, train_frac=train_frac)
    best_raw, train_eval, n_tried = search_thresholds(train)
    best = with_windows(best_raw)
    train_eval = evaluate(best, train)
    test_eval = evaluate(best, test) if test else train_eval
    full_eval = evaluate(best, events)

    base_train = evaluate(baseline, train)
    base_test = evaluate(baseline, test) if test else base_train
    base_full = evaluate(baseline, events)

    better_on_test = test_eval.overall.score > base_test.overall.score + 1e-9
    full_improve = full_eval.overall.score > base_full.overall.score + 1e-9
    short_strong_in_test = test_eval.short.n_strong > 0
    # Only recommend YAML replace on clear overall test gain AND full-sample gain.
    # If the test fold has no short strong labels, require full-sample short recall
    # not to regress either.
    short_ok = (
        test_eval.short.strong_recall >= base_test.short.strong_recall - 1e-9
        if short_strong_in_test
        else full_eval.short.strong_recall >= base_full.short.strong_recall - 1e-9
    )
    recommend_replace = bool(better_on_test and full_improve and short_ok)

    notes = [
        f"test overall score calibrated={test_eval.overall.score:.3f} "
        f"vs baseline={base_test.overall.score:.3f}",
        f"full overall score calibrated={full_eval.overall.score:.3f} "
        f"vs baseline={base_full.overall.score:.3f}",
        f"test long score calibrated={test_eval.long.score:.3f} "
        f"vs baseline={base_test.long.score:.3f}",
        f"test short score calibrated={test_eval.short.score:.3f} "
        f"vs baseline={base_test.short.score:.3f}",
        f"full short strong_recall calibrated={full_eval.short.strong_recall:.3f} "
        f"vs baseline={base_full.short.strong_recall:.3f}",
    ]
    if full_eval.short.n_strong and full_eval.short.strong_recall == 0.0:
        notes.append(
            "WARNING: short strong_recall=0 under shared OB thresholds "
            "(Phase-A short dumps often lack ask-dominant 5bps books)"
        )
    if not short_strong_in_test:
        notes.append(
            "NOTE: walk-forward test fold contains no short strong labels; "
            "short OOS comparison is incomplete"
        )
    notes.append(
        "replace recommended"
        if recommend_replace
        else "keep current YAML (no clear walk-forward win for both sides)"
    )

    return {
        "symbol": symbol,
        "n_events": len(events),
        "n_train": len(train),
        "n_test": len(test),
        "n_candidates_tried": n_tried,
        "train_from": train[0].bar_ts.isoformat() if train else None,
        "train_to": train[-1].bar_ts.isoformat() if train else None,
        "test_from": test[0].bar_ts.isoformat() if test else None,
        "test_to": test[-1].bar_ts.isoformat() if test else None,
        "baseline_thresholds": _thresholds_dict(baseline),
        "calibrated_thresholds": _thresholds_dict(best),
        "baseline": {
            "train": {
                "overall": _metrics_dict(base_train.overall),
                "long": _metrics_dict(base_train.long),
                "short": _metrics_dict(base_train.short),
            },
            "test": {
                "overall": _metrics_dict(base_test.overall),
                "long": _metrics_dict(base_test.long),
                "short": _metrics_dict(base_test.short),
            },
            "full": {
                "overall": _metrics_dict(base_full.overall),
                "long": _metrics_dict(base_full.long),
                "short": _metrics_dict(base_full.short),
            },
        },
        "calibrated": {
            "train": {
                "overall": _metrics_dict(train_eval.overall),
                "long": _metrics_dict(train_eval.long),
                "short": _metrics_dict(train_eval.short),
            },
            "test": {
                "overall": _metrics_dict(test_eval.overall),
                "long": _metrics_dict(test_eval.long),
                "short": _metrics_dict(test_eval.short),
            },
            "full": {
                "overall": _metrics_dict(full_eval.overall),
                "long": _metrics_dict(full_eval.long),
                "short": _metrics_dict(full_eval.short),
            },
        },
        "recommend_replace_yaml": recommend_replace,
        "decision_notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase C/D threshold calibration")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument(
        "--strong",
        type=Path,
        default=None,
        help="Phase-A strong breakouts JSON",
    )
    parser.add_argument(
        "--fakeouts",
        type=Path,
        default=None,
        help="Phase-B fakeouts JSON",
    )
    parser.add_argument("--train-frac", type=float, default=0.7)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--write-suggested-yaml", action="store_true")
    args = parser.parse_args(argv)

    symbol = args.symbol.upper().replace("/", "")
    events_dir = Path(__file__).resolve().parent / "events"
    strong = args.strong or events_dir / f"{symbol}_strong_breakouts_phase_a.json"
    fakeouts = args.fakeouts or events_dir / f"{symbol}_fakeouts_phase_b.json"

    result = run_calibration(
        symbol=symbol,
        strong_path=strong,
        fakeout_path=fakeouts,
        train_frac=args.train_frac,
    )

    out = args.out or events_dir / f"{symbol}_calibration_phase_c.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    cal = result["calibrated_thresholds"]
    th = make_thresholds(**{k: cal[k] for k in cal if k != "symbol"}, symbol=cal["symbol"])
    if args.write_suggested_yaml or result["recommend_replace_yaml"]:
        yaml_path = events_dir / f"{symbol}_calibrated_suggestion.yaml"
        note = (
            "OUTPERFORMS baseline on walk-forward test (long+short)"
            if result["recommend_replace_yaml"]
            else "best train candidate — review carefully before replace"
        )
        yaml_path.write_text(suggest_yaml(th, note=note), encoding="utf-8")
        print(f"suggested yaml: {yaml_path}")

    print(
        f"Phase C {symbol}: tried={result['n_candidates_tried']} "
        f"train={result['n_train']} test={result['n_test']}"
    )
    print("baseline test:", json.dumps(result["baseline"]["test"], indent=2))
    print("calibrated test:", json.dumps(result["calibrated"]["test"], indent=2))
    print("calibrated thresholds:", json.dumps(result["calibrated_thresholds"], indent=2))
    for note in result["decision_notes"]:
        print(f"- {note}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
