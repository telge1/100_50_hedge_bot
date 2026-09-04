"""Balance vs trend classification.

The distinction the profile is for: in a balance profile the POC is a magnet
and the value-area edges are the tradeable extremes, whereas in a trend
profile the POC is a way station and fading the extremes fights the flow.
Reading that off three lines is impossible, which is why the shape is derived
from the whole distribution.

Discriminators used here:

``va_range_share``
    (VAH-VAL) / (high-low). A balance profile concentrates volume into a
    compact core, so 70% of it fits in a small share of the range. A trend
    profile spreads volume thinly across a large range, so the same 70%
    needs most of it.
``directional_share``
    (close-open) / (high-low). Signed. Distinguishes a trend from a wide but
    non-directional range day.
``poc_position``
    Where the POC sits in the range. Drives the D/P/b letter.
``poc_concentration``
    POC volume relative to mean bin volume. Reported for calibration; not
    part of the decision yet.

Thresholds are provisional (see :class:`ShapeThresholds`). The verdict always
ships the raw metrics so the rule can be re-tuned from the artifact.
"""

from __future__ import annotations

from typing import Sequence

from .contracts import NodeSet, ProfileBin, ShapeThresholds, ShapeVerdict, ValueArea


def _letter(poc_position: float, thresholds: ShapeThresholds, double: bool) -> str:
    if double:
        return "B"
    if poc_position >= thresholds.poc_central_high:
        return "P"
    if poc_position <= thresholds.poc_central_low:
        return "b"
    return "D"


def _bin_index_at(bins: Sequence[ProfileBin], price: float) -> int:
    return min(range(len(bins)), key=lambda i: abs(bins[i].price_mid - price))


def _is_double_distribution(
    bins: Sequence[ProfileBin],
    nodes: NodeSet,
    value_area: ValueArea,
    price_low: float,
    price_high: float,
    thresholds: ShapeThresholds,
) -> bool:
    """Two areas of acceptance separated by a real area of rejection.

    Anchored on the POC as the primary cluster. A secondary cluster only
    counts if it is strong in its own right, far enough away to be a distinct
    area, and separated from the POC by a contiguous run of near-empty bins.

    Comparing the outermost HVNs instead would misfire on any trend day that
    paused at both ends of its range, which is the common case.
    """
    rng = price_high - price_low
    if rng <= 0 or len(bins) < 5 or not nodes.hvn:
        return False

    poc_vol = value_area.poc_volume
    if poc_vol <= 0:
        return False
    i_poc = _bin_index_at(bins, value_area.poc)

    for hvn in sorted(nodes.hvn, key=lambda h: -abs(h - value_area.poc)):
        if abs(hvn - value_area.poc) / rng < thresholds.double_distribution_min_separation:
            continue
        i_sec = _bin_index_at(bins, hvn)
        sec_vol = bins[i_sec].volume
        if sec_vol < thresholds.double_secondary_peak_frac * poc_vol:
            continue

        lo_i, hi_i = sorted((i_poc, i_sec))
        interior = bins[lo_i + 1 : hi_i]
        if len(interior) < thresholds.double_valley_min_bins:
            continue

        cutoff = thresholds.double_distribution_valley_frac * min(poc_vol, sec_vol)
        run = 0
        for b in interior:
            run = run + 1 if b.volume <= cutoff else 0
            if run >= thresholds.double_valley_min_bins:
                return True
    return False


def classify_shape(
    *,
    value_area: ValueArea,
    nodes: NodeSet,
    price_low: float,
    price_high: float,
    open_price: float,
    close_price: float,
    total_volume: float,
    bin_count: int,
    bins: Sequence[ProfileBin] = (),
    thresholds: ShapeThresholds | None = None,
) -> ShapeVerdict:
    th = thresholds or ShapeThresholds()
    rng = float(price_high) - float(price_low)

    if rng <= 0 or bin_count <= 0 or total_volume <= 0:
        return ShapeVerdict(
            kind="UNCLEAR",
            letter="-",
            poc_position=0.0,
            va_range_share=0.0,
            poc_concentration=0.0,
            directional_share=0.0,
            reasons=("degenerate window: no price range or no volume",),
        )

    poc_position = (value_area.poc - price_low) / rng
    va_range_share = (value_area.vah - value_area.val) / rng
    directional_share = (close_price - open_price) / rng
    mean_bin = total_volume / bin_count
    poc_concentration = (value_area.poc_volume / mean_bin) if mean_bin > 0 else 0.0

    double = _is_double_distribution(
        bins, nodes, value_area, price_low, price_high, th
    )
    reasons: list[str] = [
        f"va_range_share={va_range_share:.3f}",
        f"directional_share={directional_share:+.3f}",
        f"poc_position={poc_position:.3f}",
        f"poc_concentration={poc_concentration:.2f}x mean bin",
    ]

    if double:
        reasons.append(
            "a second strong volume cluster is separated from the POC by a "
            "contiguous rejection gap -> double distribution; treat each cluster "
            "as its own balance and the gap between them as fast territory"
        )
        kind = "DOUBLE_DISTRIBUTION"
    elif (
        abs(directional_share) >= th.trend_directional_share
        and va_range_share >= th.trend_va_range_share
    ):
        kind = "TREND_UP" if directional_share > 0 else "TREND_DOWN"
        reasons.append(
            f"closed >={th.trend_directional_share:.2f} of the range away from the "
            f"open with volume spread over >={th.trend_va_range_share:.2f} of it "
            "-> trend; POC is a way station, not a reversal level"
        )
    elif (
        abs(directional_share) <= th.balance_directional_share
        and va_range_share <= th.balance_va_range_share
        and th.poc_central_low <= poc_position <= th.poc_central_high
    ):
        kind = "BALANCE"
        reasons.append(
            f"closed within {th.balance_directional_share:.2f} of the open with "
            "volume concentrated around a central POC -> balance; the value-area "
            "edges are the tradeable extremes"
        )
    else:
        kind = "UNCLEAR"
        reasons.append(
            "metrics fall between the balance and trend cut-offs -> no verdict; "
            "thresholds are centred on observed BTCUSDT distributions but are "
            "not validated against outcomes"
        )

    return ShapeVerdict(
        kind=kind,
        letter=_letter(poc_position, th, double),
        poc_position=poc_position,
        va_range_share=va_range_share,
        poc_concentration=poc_concentration,
        directional_share=directional_share,
        reasons=tuple(reasons),
    )
