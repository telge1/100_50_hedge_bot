"""Derived levels: value area, high/low volume nodes, low-volume ranges."""

from __future__ import annotations

from .contracts import NodeSet, ProfileBin, ValueArea


def compute_value_area(
    bins: list[ProfileBin] | tuple[ProfileBin, ...],
    value_area_pct: float,
) -> ValueArea:
    """Expand from the POC until the band holds `value_area_pct` of volume.

    Expansion is one bin at a time toward the larger neighbour. TPO profiles
    conventionally expand in pairs of rows; for a volume profile the
    single-bin variant is the common choice and it makes the result
    independent of where the bin grid happens to start.
    """
    seq = list(bins)
    if not seq:
        raise ValueError("cannot compute a value area over zero bins")
    pct = float(value_area_pct)
    if not 0.0 < pct <= 1.0:
        raise ValueError("value_area_pct must be in (0, 1]")

    total = sum(b.volume for b in seq)
    poc_idx = max(range(len(seq)), key=lambda i: (seq[i].volume, -abs(i - len(seq) // 2)))
    poc_bin = seq[poc_idx]

    if total <= 0.0:
        return ValueArea(
            poc=poc_bin.price_mid,
            poc_volume=0.0,
            poc_bin_index=poc_bin.bin_index,
            vah=poc_bin.price_high,
            val=poc_bin.price_low,
            requested_share=pct,
            volume_share=0.0,
            bin_count=1,
        )

    target = total * pct
    lo = hi = poc_idx
    acc = poc_bin.volume
    while acc < target and (lo > 0 or hi < len(seq) - 1):
        below = seq[lo - 1].volume if lo > 0 else -1.0
        above = seq[hi + 1].volume if hi < len(seq) - 1 else -1.0
        if above >= below:
            hi += 1
            acc += seq[hi].volume
        else:
            lo -= 1
            acc += seq[lo].volume

    return ValueArea(
        poc=poc_bin.price_mid,
        poc_volume=poc_bin.volume,
        poc_bin_index=poc_bin.bin_index,
        vah=seq[hi].price_high,
        val=seq[lo].price_low,
        requested_share=pct,
        volume_share=acc / total,
        bin_count=hi - lo + 1,
    )


def _smooth(values: list[float]) -> list[float]:
    """Width-3 moving average, edges held. Suppresses single-bin jitter."""
    n = len(values)
    if n < 3:
        return list(values)
    out = [0.0] * n
    out[0] = (values[0] + values[1]) / 2.0
    out[-1] = (values[-1] + values[-2]) / 2.0
    for i in range(1, n - 1):
        out[i] = (values[i - 1] + values[i] + values[i + 1]) / 3.0
    return out


def _pick_separated(
    candidates: list[tuple[int, float]],
    *,
    min_separation: int,
    prefer_high: bool,
) -> list[int]:
    """Thin a candidate list so no two picks sit within `min_separation` bins.

    Candidates are taken strongest first, so a plateau contributes its peak
    rather than one entry per bin.
    """
    ordered = sorted(candidates, key=lambda c: c[1], reverse=prefer_high)
    chosen: list[int] = []
    for idx, _ in ordered:
        if all(abs(idx - k) >= min_separation for k in chosen):
            chosen.append(idx)
    return sorted(chosen)


def find_nodes(
    bins: list[ProfileBin] | tuple[ProfileBin, ...],
    *,
    hvn_factor: float,
    lvn_factor: float,
    min_separation_bins: int,
    single_print_frac: float,
    poc_volume: float,
) -> NodeSet:
    """Locate HVNs, LVNs and the low-volume ranges price accelerated through."""
    seq = list(bins)
    if not seq:
        return NodeSet(hvn=(), lvn=(), single_print_ranges=())

    raw = [b.volume for b in seq]
    total = sum(raw)
    if total <= 0.0:
        return NodeSet(hvn=(), lvn=(), single_print_ranges=())

    mean = total / len(seq)
    smooth = _smooth(raw)

    hvn_cand: list[tuple[int, float]] = []
    lvn_cand: list[tuple[int, float]] = []
    for i in range(1, len(seq) - 1):
        v, prev, nxt = smooth[i], smooth[i - 1], smooth[i + 1]
        if v >= hvn_factor * mean and v >= prev and v >= nxt:
            hvn_cand.append((i, v))
        if v <= lvn_factor * mean and v <= prev and v <= nxt:
            lvn_cand.append((i, v))

    hvn_idx = _pick_separated(
        hvn_cand, min_separation=min_separation_bins, prefer_high=True
    )
    lvn_idx = _pick_separated(
        lvn_cand, min_separation=min_separation_bins, prefer_high=False
    )

    # Contiguous runs of near-empty bins, merged into price ranges.
    threshold = single_print_frac * float(poc_volume)
    ranges: list[tuple[float, float]] = []
    run_start: int | None = None
    for i, b in enumerate(seq):
        if b.volume <= threshold:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            ranges.append((seq[run_start].price_low, seq[i - 1].price_high))
            run_start = None
    if run_start is not None:
        ranges.append((seq[run_start].price_low, seq[-1].price_high))

    return NodeSet(
        hvn=tuple(seq[i].price_mid for i in hvn_idx),
        lvn=tuple(seq[i].price_mid for i in lvn_idx),
        single_print_ranges=tuple(ranges),
    )
