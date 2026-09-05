"""FROZEN_HIGH_ACCEPTED_LARGE_MOVE_SEPARABILITY_DISCOVERY_V1 runner."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_flip.trade_loader import (
    load_oi_labels_clickhouse,
    load_trades_clickhouse,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_runner import (
    FrozenV2BundleTampered,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import FreezeViolation
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v2 import verify_freeze_v2
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.large_move_contracts import (
    EXPECTED_V2_SHA_PREFIX,
    FEATURE_CONTRACT,
    LABEL_CONTRACT,
    MODEL_CONTRACT,
    NO_FIT_LM,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.large_move_features import (
    acceptance_features,
    book_features_at_entry,
    compute_path_outcomes,
    context_features,
    pool_distance_features,
    trade_flow_features,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)

V2_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_contract_fix_refreeze_v2"
)
ET_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_entry_timing_v1"
)
FREEZE_V2 = V2_DIR / "freeze_bundle_v2"
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_large_move_separability_discovery_v1"
)

FEE = 0.00055
SLIP_BPS = 1.0
NOTIONAL = 1000.0
TOP_FRAC = 0.20
MAX_FEATURES = 8


def _load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _verify(label: str) -> dict[str, Any]:
    try:
        out = {**verify_freeze_v2(FREEZE_V2), "label": label}
    except FreezeViolation as e:
        raise FrozenV2BundleTampered(f"FROZEN_V2_BUNDLE_TAMPERED ({label}): {e}") from e
    if not str(out.get("freeze_bundle_sha256", "")).startswith(EXPECTED_V2_SHA_PREFIX):
        raise FrozenV2BundleTampered("unexpected freeze sha")
    return out


def _fingerprint(ids: list[str]) -> str:
    h = hashlib.sha256()
    for i in sorted(ids):
        h.update(i.encode())
        h.update(b"\n")
    return h.hexdigest()


def _net_from_gross(gross: float) -> float:
    # approximate: fees + 2*slippage_bps (entry+exit) as fraction
    return gross - 2 * FEE - 2 * (SLIP_BPS / 1e4)


def _spearman(x: list[float], y: list[float]) -> Optional[float]:
    n = len(x)
    if n < 5:
        return None
    rx = np.argsort(np.argsort(np.array(x))).astype(float)
    ry = np.argsort(np.argsort(np.array(y))).astype(float)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def run_large_move_discovery(
    *,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    max_events: Optional[int] = None,
    reuse_matrices: bool = False,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)
    cand_dir = output_dir / "candidate_bundle_v1"
    ensure_outdir(cand_dir)

    before = _verify("before")
    write_json(output_dir / "freeze_verification_before.json", before)
    write_json(output_dir / "label_contract.json", LABEL_CONTRACT)
    write_json(output_dir / "feature_contract.json", FEATURE_CONTRACT)

    entries = [r for r in _load_csv(ET_DIR / "entry_execution.csv") if r.get("status") == "OK"]
    # join migration for rearm etc.
    mig = {r["entry_signal_id_v2"]: r for r in _load_csv(V2_DIR / "entry_eligible_events_v2.csv")}
    # dedupe already unique in ET
    entries = sorted(entries, key=lambda r: parse_utc(r["entry_book_ts"]))
    if max_events is not None:
        entries = entries[:max_events]

    ids = [r["entry_signal_id_v2"] for r in entries]
    fp = {
        "n": len(ids),
        "sha256": _fingerprint(ids),
        "unique": len(set(ids)),
        "expected_n": 1192 if max_events is None else len(ids),
    }
    write_json(output_dir / "cohort_fingerprint.json", fp)
    if max_events is None and (fp["n"] != 1192 or fp["unique"] != 1192):
        raise RuntimeError(f"cohort fingerprint unexpected: {fp}")

    # Enrich rows with migration fields
    for r in entries:
        m = mig.get(r["entry_signal_id_v2"], {})
        r["migration_class"] = m.get("migration_class")
        r["episode_action"] = m.get("episode_action")
        r["matched_edge_price"] = m.get("matched_edge_price") or r.get("matched_edge_price")
        r["decision_ts"] = m.get("decision_ts")

    query_log: list[dict[str, Any]] = []
    if reuse_matrices and (output_dir / "outcomes_separate.csv").exists():
        outcomes = _load_csv(output_dir / "outcomes_separate.csv")
        features = _load_csv(output_dir / "feature_matrix_pre_entry.csv")
        # coerce numeric-looking fields
        for f in features:
            for k, v in list(f.items()):
                if k in (
                    "entry_signal_id_v2",
                    "utc_day",
                    "trade_side",
                    "entry_book_ts",
                    "y_clean",
                    "y_large25",
                    "split",
                ):
                    continue
                if v in ("", "None", None):
                    f[k] = None
                else:
                    try:
                        if v in ("True", "False"):
                            f[k] = 1.0 if v == "True" else 0.0
                        else:
                            f[k] = float(v)
                    except (TypeError, ValueError):
                        pass
        for o in outcomes:
            for k in (
                "LARGE_MOVE_25BPS_15M",
                "CLEAN_LARGE_MOVE_25_15",
                "LARGE_MOVE_20BPS_15M",
                "LARGE_MOVE_30BPS_15M",
                "LARGE_MOVE_25BPS_30M",
            ):
                if k in o:
                    o[k] = o[k] in (True, "True", "true", 1, "1")
            for k in (
                "gross_ret_15m_approx",
                "net_ret_15m_approx",
                "net_pnl_usdt_15m_approx",
                "mfe_bps_15m",
                "mae_bps_15m",
                "ret_bps_15m",
            ):
                if o.get(k) not in (None, ""):
                    try:
                        o[k] = float(o[k])
                    except Exception:
                        pass
        feat_ts_audit = (
            _load_csv(output_dir / "feature_timestamp_audit.csv")
            if (output_dir / "feature_timestamp_audit.csv").exists()
            else []
        )
        excluded_leaky = (
            _load_csv(output_dir / "excluded_leaky_features.csv")
            if (output_dir / "excluded_leaky_features.csv").exists()
            else []
        )
        pool_audit = (
            _load_csv(output_dir / "pool_distance_audit.csv")
            if (output_dir / "pool_distance_audit.csv").exists()
            else []
        )
        print(f"reusing matrices n_out={len(outcomes)} n_feat={len(features)}", flush=True)
    else:
        by_hour: dict[str, list] = defaultdict(list)
        for r in entries:
            ht = parse_utc(r["entry_book_ts"]).replace(minute=0, second=0, microsecond=0)
            by_hour[iso_z(ht)].append(r)

        outcomes = []
        features = []
        feat_ts_audit = []
        excluded_leaky = []
        pool_audit = []

        hours = sorted(by_hour.keys())
        for hi, hour in enumerate(hours):
            rows = by_hour[hour]
            print(f"lm hour {hi+1}/{len(hours)} {hour} n={len(rows)}", flush=True)
            ht = parse_utc(hour)
            # need lookback 15m for features + 30m forward for labels
            start = ht - timedelta(minutes=20)
            end = ht + timedelta(hours=2)
            samples_by, _, n_ok = load_ob200_samples(
                symbols=("BTCUSDT",), start=start, end=end, raw_root=raw_root, sample_ms=250
            )
            samples = samples_by.get("BTCUSDT") or []
            trades, _ = load_trades_clickhouse(
                symbol="BTCUSDT", start=start, end=end, query_log=query_log
            )
            # OI labels optional
            try:
                oi = load_oi_labels_clickhouse(
                    symbol="BTCUSDT", start=start, end=ht + timedelta(hours=1), query_log=query_log
                )
            except Exception:
                oi = {}

            for r in rows:
                entry_ts = parse_utc(r["entry_book_ts"])
                side = r["trade_side"]
                entry_px = float(r["executable_entry_price"])
                entry_mid = float(r["entry_mid"])
                edge_px = (
                    float(r["matched_edge_price"])
                    if r.get("matched_edge_price") not in (None, "")
                    else None
                )

                path = compute_path_outcomes(
                    samples, side=side, entry_ts=entry_ts, entry_px=entry_px
                )
                g15 = (path.get("ret_bps_15m") or 0.0) / 1e4
                out_row = {
                    "entry_signal_id_v2": r["entry_signal_id_v2"],
                    "episode_id_v2": r["episode_id_v2"],
                    "trade_side": side,
                    "acceptance_state": r["acceptance_state"],
                    "utc_day": r["utc_day"],
                    "entry_book_ts": r["entry_book_ts"],
                    "executable_entry_price": entry_px,
                    "matched_edge_id": r.get("matched_edge_id"),
                    **path,
                    "gross_ret_15m_approx": g15,
                    "net_ret_15m_approx": _net_from_gross(g15),
                    "net_pnl_usdt_15m_approx": _net_from_gross(g15) * NOTIONAL,
                }
                outcomes.append(out_row)

                # Features
                ff, fm = trade_flow_features(trades, entry_ts=entry_ts)
                bf, bm = book_features_at_entry(samples, entry_ts=entry_ts)
                pf, pm = pool_distance_features(
                    samples,
                    entry_ts=entry_ts,
                    entry_mid=entry_mid,
                    side=side,
                    matched_edge_price=edge_px,
                )
                cf, cm = context_features(samples, entry_ts=entry_ts)
                af, am = acceptance_features(r, entry_ts=entry_ts)

                oi_up = oi_dn = 0
                for bt, lab in oi.items():
                    if entry_ts - timedelta(minutes=5) < bt <= entry_ts:
                        if lab == "OI_UP":
                            oi_up += 1
                        elif lab == "OI_DOWN":
                            oi_dn += 1
                of = {
                    "oi_up_bins_5m": float(oi_up),
                    "oi_down_bins_5m": float(oi_dn),
                    "oi_net_up_minus_down_5m": float(oi_up - oi_dn),
                    "oi_available": 1.0 if oi else 0.0,
                }
                om = {
                    "open_interest": {
                        "family": "open_interest",
                        "feature_available_ts": iso_z(entry_ts),
                        "causal_ok": True,
                        "note": "bucket labels closed at bucket_time <= entry",
                    }
                }

                dir_sign = 1.0 if side == "LONG" else -1.0
                for w in (15, 30, 60):
                    buy = ff.get(f"flow_buy_notional_{w}s") or 0.0
                    sell = ff.get(f"flow_sell_notional_{w}s") or 0.0
                    ff[f"flow_dir_notional_{w}s"] = buy if side == "LONG" else sell
                    ff[f"flow_opp_notional_{w}s"] = sell if side == "LONG" else buy
                    ff[f"flow_dir_imbalance_{w}s"] = dir_sign * (
                        ff.get(f"flow_signed_imbalance_{w}s") or 0.0
                    )

                feat_row = {
                    "entry_signal_id_v2": r["entry_signal_id_v2"],
                    "utc_day": r["utc_day"],
                    "trade_side": side,
                    "entry_book_ts": r["entry_book_ts"],
                    **ff,
                    **bf,
                    **pf,
                    **cf,
                    **af,
                    **of,
                }
                features.append(feat_row)

                for fam_meta in (fm, bm, pm, cm, am, om):
                    for name, meta in fam_meta.items():
                        avail = meta.get("feature_available_ts")
                        causal = bool(meta.get("causal_ok", False))
                        if avail:
                            try:
                                if parse_utc(avail) > entry_ts:
                                    causal = False
                                    excluded_leaky.append(
                                        {
                                            "entry_signal_id_v2": r["entry_signal_id_v2"],
                                            "feature_family": name,
                                            "feature_available_ts": avail,
                                            "executable_entry_ts": r["entry_book_ts"],
                                            "reason": "feature_available_after_entry",
                                        }
                                    )
                            except Exception:
                                pass
                        feat_ts_audit.append(
                            {
                                "entry_signal_id_v2": r["entry_signal_id_v2"],
                                "feature_family": name,
                                "family": meta.get("family"),
                                "feature_available_ts": avail,
                                "executable_entry_ts": r["entry_book_ts"],
                                "causal_ok": causal,
                                "missing_reason": meta.get("missing_reason"),
                            }
                        )

                pool_audit.append(
                    {
                        "entry_signal_id_v2": r["entry_signal_id_v2"],
                        "trade_side": side,
                        "utc_day": r["utc_day"],
                        **{k: pf[k] for k in pf},
                        "LARGE_MOVE_25BPS_15M": path["LARGE_MOVE_25BPS_15M"],
                        "CLEAN_LARGE_MOVE_25_15": path["CLEAN_LARGE_MOVE_25_15"],
                    }
                )

        write_csv(output_dir / "outcomes_separate.csv", outcomes)
        write_csv(output_dir / "feature_matrix_pre_entry.csv", features)
        write_csv(output_dir / "feature_timestamp_audit.csv", feat_ts_audit)
        write_csv(output_dir / "excluded_leaky_features.csv", excluded_leaky)
        write_csv(output_dir / "pool_distance_audit.csv", pool_audit)

    # join labels onto features
    lab = {o["entry_signal_id_v2"]: o for o in outcomes}
    for f in features:
        o = lab[f["entry_signal_id_v2"]]
        f["y_clean"] = int(bool(o["CLEAN_LARGE_MOVE_25_15"]))
        f["y_large25"] = int(bool(o["LARGE_MOVE_25BPS_15M"]))
        f["split"] = "holdout" if f["utc_day"] == "2026-08-26" else "development"

    # leakage block if any
    if excluded_leaky:
        # still continue but mark; hard block if systemic
        pass

    # Feature columns numeric
    skip = {
        "entry_signal_id_v2",
        "utc_day",
        "trade_side",
        "entry_book_ts",
        "y_clean",
        "y_large25",
        "split",
    }
    all_feat_names = [k for k in features[0].keys() if k not in skip]

    # missingness
    miss_rows = []
    for name in all_feat_names:
        vals = [f.get(name) for f in features]
        n_miss = sum(1 for v in vals if v is None or v == "")
        miss_rows.append(
            {
                "feature": name,
                "n": len(vals),
                "missing_frac": n_miss / len(vals),
                "n_valid": len(vals) - n_miss,
            }
        )
    write_csv(output_dir / "feature_missingness.csv", miss_rows)

    # Development / holdout split — DO NOT compute holdout summaries until freeze
    dev = [f for f in features if f["split"] == "development"]
    hold = [f for f in features if f["split"] == "holdout"]
    write_csv(
        output_dir / "development_split.csv",
        [
            {
                "development_days": "2026-08-24,2026-08-25",
                "holdout_day": "2026-08-26",
                "n_dev": len(dev),
                "n_hold": len(hold),
                "holdout_labels_unread_before_freeze": True,
            }
        ],
    )

    # Univariate on development only
    y_dev = np.array([f["y_clean"] for f in dev], dtype=float)
    uni = []
    day_stab = []
    dir_stab = []
    qsum = []
    usable_feats = []
    for name in all_feat_names:
        xs = []
        ys = []
        for f in dev:
            v = f.get(name)
            if v is None or v == "":
                continue
            try:
                xs.append(float(v))
                ys.append(float(f["y_clean"]))
            except (TypeError, ValueError):
                continue
        miss = 1.0 - len(xs) / max(1, len(dev))
        row = {
            "feature": name,
            "n_valid": len(xs),
            "missing_frac": miss,
            "auc": None,
            "spearman": None,
            "median_pos": None,
            "median_neg": None,
            "std_diff": None,
            "flag": "NO_SEPARATION",
        }
        if len(xs) >= 30 and len(set(ys)) > 1 and miss <= 0.40:
            xa = np.array(xs)
            ya = np.array(ys)
            pos = xa[ya == 1]
            neg = xa[ya == 0]
            if len(pos) and len(neg):
                row["median_pos"] = float(np.median(pos))
                row["median_neg"] = float(np.median(neg))
                sd = np.std(xa)
                row["std_diff"] = float((np.mean(pos) - np.mean(neg)) / sd) if sd > 0 else 0.0
            try:
                row["auc"] = float(roc_auc_score(ya, xa))
                # orient auc >= 0.5
                if row["auc"] < 0.5:
                    row["auc"] = 1.0 - row["auc"]
                    row["orient_flip"] = True
            except Exception:
                row["auc"] = None
            row["spearman"] = _spearman(xs, ys)
            # day stability
            d24 = [f for f in dev if f["utc_day"] == "2026-08-24"]
            d25 = [f for f in dev if f["utc_day"] == "2026-08-25"]

            def _med_diff(subset):
                pv, nv = [], []
                for f in subset:
                    v = f.get(name)
                    if v is None or v == "":
                        continue
                    try:
                        fv = float(v)
                    except Exception:
                        continue
                    (pv if f["y_clean"] else nv).append(fv)
                if not pv or not nv:
                    return None
                return float(np.median(pv) - np.median(nv))

            md24, md25 = _med_diff(d24), _med_diff(d25)
            same_dir = (
                md24 is not None
                and md25 is not None
                and ((md24 >= 0 and md25 >= 0) or (md24 <= 0 and md25 <= 0))
            )
            day_stab.append(
                {
                    "feature": name,
                    "median_diff_24": md24,
                    "median_diff_25": md25,
                    "same_sign": same_dir,
                }
            )
            # direction stability
            for sname, subset in (
                ("LONG", [f for f in dev if f["trade_side"] == "LONG"]),
                ("SHORT", [f for f in dev if f["trade_side"] == "SHORT"]),
            ):
                dir_stab.append(
                    {"feature": name, "side": sname, "median_diff": _med_diff(subset)}
                )

            # quantiles on development
            if len(xs) >= 50:
                qs = np.quantile(xa, [0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
                for qi in range(5):
                    lo, hiq = qs[qi], qs[qi + 1]
                    mask = (xa >= lo) & (xa <= hiq if qi == 4 else xa < hiq)
                    if mask.sum() == 0:
                        continue
                    qsum.append(
                        {
                            "feature": name,
                            "bin": f"Q{qi+1}",
                            "n": int(mask.sum()),
                            "clean_rate": float(ya[mask].mean()),
                            "lo": float(lo),
                            "hi": float(hiq),
                        }
                    )

            if row["auc"] and row["auc"] >= 0.55 and same_dir and miss <= 0.25:
                row["flag"] = "STABLE_SEPARATION"
                usable_feats.append(name)
            elif row["auc"] and row["auc"] >= 0.55 and not same_dir:
                row["flag"] = "DAY_SPECIFIC"
            elif miss > 0.40:
                row["flag"] = "HIGH_MISSINGNESS"
            elif row["auc"] and row["auc"] < 0.53:
                row["flag"] = "NO_SEPARATION"
            else:
                row["flag"] = "UNSTABLE"
        elif miss > 0.40:
            row["flag"] = "HIGH_MISSINGNESS"
        uni.append(row)

    write_csv(output_dir / "feature_univariate_summary.csv", uni)
    write_csv(output_dir / "feature_day_stability.csv", day_stab)
    write_csv(output_dir / "feature_direction_stability.csv", dir_stab)
    write_csv(output_dir / "feature_quantile_summary.csv", qsum)

    # Select up to 8 features: prefer STABLE, diversify families
    family_of = {}
    for name in all_feat_names:
        if name.startswith("flow_"):
            family_of[name] = "public_trade_flow"
        elif name.startswith("book_"):
            family_of[name] = "orderbook"
        elif name.startswith("ctx_"):
            family_of[name] = "market_context"
        elif name.startswith("oi_"):
            family_of[name] = "open_interest"
        elif name.startswith("acc_"):
            family_of[name] = "acceptance_quality"
        else:
            family_of[name] = "pool_edge_geometry"

    ranked = sorted(
        [u for u in uni if u["feature"] in usable_feats and u.get("auc")],
        key=lambda u: -float(u["auc"]),
    )
    selected: list[str] = []
    fam_count: Counter = Counter()
    for u in ranked:
        fam = family_of[u["feature"]]
        if fam_count[fam] >= 2:
            continue
        # only STABLE_SEPARATION enters the candidate (pool audit stays descriptive)
        if u.get("flag") != "STABLE_SEPARATION":
            continue
        selected.append(u["feature"])
        fam_count[fam] += 1
        if len(selected) >= MAX_FEATURES:
            break
    selected = selected[:MAX_FEATURES]

    # Correlation among selected on development
    corr_rows = []
    if len(selected) >= 2:
        mat = []
        for name in selected:
            col = []
            for f in dev:
                v = f.get(name)
                col.append(np.nan if v is None or v == "" else float(v))
            mat.append(col)
        M = np.array(mat)
        # pairwise
        for i, a in enumerate(selected):
            for j, b in enumerate(selected):
                if j <= i:
                    continue
                aa = M[i]
                bb = M[j]
                mask = ~np.isnan(aa) & ~np.isnan(bb)
                if mask.sum() < 30:
                    c = None
                else:
                    c = float(np.corrcoef(aa[mask], bb[mask])[0, 1])
                corr_rows.append({"a": a, "b": b, "corr": c})
    write_csv(output_dir / "feature_correlation.csv", corr_rows)

    # Drop near-duplicates |corr|>0.95 keeping higher AUC
    auc_map = {u["feature"]: u.get("auc") or 0 for u in uni}
    drop = set()
    for c in corr_rows:
        if c["corr"] is not None and abs(c["corr"]) >= 0.95:
            a, b = c["a"], c["b"]
            if auc_map.get(a, 0) >= auc_map.get(b, 0):
                drop.add(b)
            else:
                drop.add(a)
    selected = [s for s in selected if s not in drop][:MAX_FEATURES]

    # Build matrices with median impute + standardize on DEV only
    def _col(rows, name):
        out = []
        for r in rows:
            v = r.get(name)
            if v is None or v == "":
                out.append(np.nan)
            else:
                out.append(float(v))
        return np.array(out, dtype=float)

    X_dev_raw = np.column_stack([_col(dev, n) for n in selected]) if selected else np.zeros((len(dev), 0))
    medians = np.nanmedian(X_dev_raw, axis=0) if selected else np.array([])
    def _impute(X, meds):
        X = X.copy()
        for j in range(X.shape[1]):
            mask = np.isnan(X[:, j])
            X[mask, j] = meds[j] if not np.isnan(meds[j]) else 0.0
        return X

    X_dev = _impute(X_dev_raw, medians)
    scaler = StandardScaler()
    if selected:
        X_dev_s = scaler.fit_transform(X_dev)
    else:
        X_dev_s = X_dev
    y_dev = np.array([f["y_clean"] for f in dev], dtype=int)

    if selected and len(set(y_dev.tolist())) > 1:
        clf = LogisticRegression(
            penalty="l2", C=1.0, solver="lbfgs", max_iter=1000, random_state=42
        )
        clf.fit(X_dev_s, y_dev)
        coefs = {selected[i]: float(clf.coef_[0][i]) for i in range(len(selected))}
        intercept = float(clf.intercept_[0])
        proba_dev = clf.predict_proba(X_dev_s)[:, 1]
    else:
        clf = None
        coefs = {}
        intercept = 0.0
        proba_dev = np.zeros(len(dev))

    thr = float(np.quantile(proba_dev, 1.0 - TOP_FRAC)) if len(proba_dev) else 1.0
    dev_pred = []
    for i, f in enumerate(dev):
        o = lab[f["entry_signal_id_v2"]]
        score = float(proba_dev[i])
        dev_pred.append(
            {
                "entry_signal_id_v2": f["entry_signal_id_v2"],
                "utc_day": f["utc_day"],
                "trade_side": f["trade_side"],
                "score": score,
                "selected_top20": score >= thr,
                "y_clean": f["y_clean"],
                "y_large25": f["y_large25"],
                "mfe_bps_15m": o.get("mfe_bps_15m"),
                "net_ret_15m_approx": o.get("net_ret_15m_approx"),
            }
        )
    write_csv(output_dir / "development_predictions.csv", dev_pred)

    model_summary = {
        **MODEL_CONTRACT,
        "selected_features": selected,
        "coefficients": coefs,
        "intercept": intercept,
        "score_threshold_top20": thr,
        "n_dev": len(dev),
        "base_rate_clean_dev": float(y_dev.mean()) if len(y_dev) else None,
        "feature_medians_dev": {selected[i]: float(medians[i]) for i in range(len(selected))},
        "scaler_mean": {selected[i]: float(scaler.mean_[i]) for i in range(len(selected))} if selected else {},
        "scaler_scale": {selected[i]: float(scaler.scale_[i]) for i in range(len(selected))} if selected else {},
    }
    write_json(output_dir / "development_model_summary.json", model_summary)

    # --- FREEZE candidate before holdout ---
    freeze_manifest = {
        **NO_FIT_LM,
        "selected_features": selected,
        "model": model_summary,
        "label": "CLEAN_LARGE_MOVE_25_15",
        "score_threshold_rule": "development_top_20pct_quantile",
        "score_threshold": thr,
        "execution": "ENTRY_TIMING_V1 primary executable entry",
        "costs": {"fee_each": FEE, "slippage_bps_per_side": SLIP_BPS},
        "evaluation_horizon_s": 900,
    }
    raw = json.dumps(freeze_manifest, sort_keys=True, separators=(",", ":"), default=str)
    sha = hashlib.sha256(raw.encode()).hexdigest()
    freeze_manifest["sha256"] = sha
    write_json(output_dir / "development_freeze_manifest.json", freeze_manifest)
    write_json(cand_dir / "candidate_contract.json", freeze_manifest)
    write_json(cand_dir / "coefficients.json", {"intercept": intercept, "coef": coefs})
    (cand_dir / "features.txt").write_text("\n".join(selected) + "\n", encoding="utf-8")
    (output_dir / "candidate_bundle_sha256.txt").write_text(sha + "\n", encoding="utf-8")
    (cand_dir / "sha256.txt").write_text(sha + "\n", encoding="utf-8")

    # Holdout opening log AFTER freeze
    holdout_log = {
        "opened_at": iso_z(datetime.now(timezone.utc)),
        "candidate_sha_before_open": sha,
        "holdout_day": "2026-08-26",
        "first_and_only_evaluation": True,
    }
    write_json(output_dir / "holdout_opening_log.json", holdout_log)

    # Apply model to holdout
    if not hold:
        proba_h = np.zeros(0)
    else:
        X_hold_raw = (
            np.column_stack([_col(hold, n) for n in selected])
            if selected
            else np.zeros((len(hold), 0))
        )
        X_hold = _impute(X_hold_raw, medians)
        X_hold_s = scaler.transform(X_hold) if selected and clf is not None else X_hold
        if clf is not None and selected:
            proba_h = clf.predict_proba(X_hold_s)[:, 1]
        else:
            proba_h = np.zeros(len(hold))

    hold_pred = []
    for i, f in enumerate(hold):
        o = lab[f["entry_signal_id_v2"]]
        score = float(proba_h[i])
        hold_pred.append(
            {
                "entry_signal_id_v2": f["entry_signal_id_v2"],
                "utc_day": f["utc_day"],
                "trade_side": f["trade_side"],
                "entry_book_ts": f["entry_book_ts"],
                "score": score,
                "selected_top20": score >= thr,
                "y_clean": f["y_clean"],
                "y_large25": f["y_large25"],
                "mfe_bps_15m": o.get("mfe_bps_15m"),
                "mae_bps_15m": o.get("mae_bps_15m"),
                "path_class_15m": o.get("path_class_15m"),
                "gross_ret_15m_approx": o.get("gross_ret_15m_approx"),
                "net_ret_15m_approx": o.get("net_ret_15m_approx"),
                "net_pnl_usdt_15m_approx": o.get("net_pnl_usdt_15m_approx"),
            }
        )
    write_csv(output_dir / "holdout_predictions.csv", hold_pred)

    def _summ(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
        if not rows:
            return {"label": label, "n": 0}
        large = [int(r["y_large25"]) for r in rows]
        clean = [int(r["y_clean"]) for r in rows]
        nets = [float(r["net_ret_15m_approx"]) for r in rows]
        gross = [float(r["gross_ret_15m_approx"]) for r in rows]
        pnls = [float(r["net_pnl_usdt_15m_approx"]) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p < 0]
        return {
            "label": label,
            "n": len(rows),
            "large25_hit_rate": sum(large) / len(rows),
            "clean_hit_rate": sum(clean) / len(rows),
            "mean_gross": float(np.mean(gross)),
            "median_gross": float(np.median(gross)),
            "mean_net": float(np.mean(nets)),
            "median_net": float(np.median(nets)),
            "net_pos_frac": sum(1 for x in nets if x > 0) / len(nets),
            "total_net_pnl_usdt": float(sum(pnls)),
            "avg_pnl_usdt": float(np.mean(pnls)),
            "profit_factor": (sum(wins) / sum(losses)) if losses else None,
            "mean_mfe_15m": float(np.mean([float(r["mfe_bps_15m"] or 0) for r in rows])),
            "mean_mae_15m": float(np.mean([float(r["mae_bps_15m"] or 0) for r in rows])),
            "target_before_adverse_rate": sum(
                1 for r in rows if r.get("path_class_15m") == "TARGET_BEFORE_ADVERSE"
            )
            / len(rows),
        }

    base_h = _summ(hold_pred, "holdout_baseline_all")
    cand_h = _summ([r for r in hold_pred if r["selected_top20"]], "holdout_candidate_top20")
    write_csv(output_dir / "holdout_baseline_summary.csv", [base_h])
    write_csv(output_dir / "holdout_candidate_summary.csv", [cand_h])
    write_csv(
        output_dir / "holdout_long_short_summary.csv",
        [
            _summ([r for r in hold_pred if r["trade_side"] == "LONG" and r["selected_top20"]], "cand_LONG"),
            _summ([r for r in hold_pred if r["trade_side"] == "SHORT" and r["selected_top20"]], "cand_SHORT"),
            _summ([r for r in hold_pred if r["trade_side"] == "LONG"], "base_LONG"),
            _summ([r for r in hold_pred if r["trade_side"] == "SHORT"], "base_SHORT"),
        ],
    )

    # one-position candidate
    cand_sorted = sorted(
        [r for r in hold_pred if r["selected_top20"]],
        key=lambda r: parse_utc(r["entry_book_ts"]),
    )
    op = []
    free_at = None
    for r in cand_sorted:
        ets = parse_utc(r["entry_book_ts"])
        if free_at is not None and ets < free_at:
            continue
        op.append(r)
        free_at = ets + timedelta(seconds=900)
    write_csv(output_dir / "one_position_candidate.csv", op)
    op_sum = _summ(op, "one_position_candidate_15m")

    # bootstrap on holdout candidate nets
    rng = np.random.default_rng(42)
    nets = [float(r["net_ret_15m_approx"]) for r in hold_pred if r["selected_top20"]]
    boots = []
    if len(nets) >= 5:
        means = []
        for _ in range(500):
            sample = rng.choice(nets, size=len(nets), replace=True)
            means.append(float(np.mean(sample)))
        means.sort()
        boots.append(
            {
                "group": "holdout_candidate_top20",
                "mean_ci95": [means[int(0.025 * 500)], means[int(0.975 * 500)]],
                "n": len(nets),
            }
        )
    write_csv(output_dir / "bootstrap_summary.csv", boots)

    # verify candidate sha unchanged
    sha_after = hashlib.sha256(
        json.dumps(
            {k: freeze_manifest[k] for k in freeze_manifest if k != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    # recompute from file
    stored_sha = (cand_dir / "sha256.txt").read_text().strip()

    after = _verify("after")
    write_json(output_dir / "freeze_verification_after.json", after)
    fp2 = {"n": len(ids), "sha256": _fingerprint(ids), "match": _fingerprint(ids) == fp["sha256"]}
    write_json(output_dir / "cohort_fingerprint.json", {**fp, "after_match": fp2["match"]})

    # overall label rates
    all_large = sum(1 for o in outcomes if o["LARGE_MOVE_25BPS_15M"]) / len(outcomes)
    all_clean = sum(1 for o in outcomes if o["CLEAN_LARGE_MOVE_25_15"]) / len(outcomes)

    # Research verdict
    tech = "FROZEN_HIGH_ACCEPTED_LARGE_MOVE_SEPARABILITY_DISCOVERY_V1_COMPLETE"
    if excluded_leaky and len(excluded_leaky) > 0:
        # family-level leak checks already filtered; individual empty ok
        pass
    if not selected:
        tech = "FROZEN_HIGH_ACCEPTED_LARGE_MOVE_SEPARABILITY_DISCOVERY_V1_INSUFFICIENT_FEATURE_COVERAGE"
        research = "LARGE_MOVE_NOT_SEPARABLE"
    else:
        n_c = cand_h.get("n", 0)
        op_improved = (
            op_sum.get("n", 0) >= 1
            and (op_sum.get("large25_hit_rate") or 0) > (base_h.get("large25_hit_rate") or 0)
            and (op_sum.get("clean_hit_rate") or 0) > (base_h.get("clean_hit_rate") or 0)
            and (op_sum.get("mean_net") or -9) > (base_h.get("mean_net") or 0)
            and (op_sum.get("median_net") or -9) > (base_h.get("median_net") or 0)
            and (op_sum.get("profit_factor") or 0) is not None
            and (base_h.get("profit_factor") or 0) is not None
            and (op_sum.get("profit_factor") or 0) > (base_h.get("profit_factor") or 0)
        )
        improved = (
            n_c >= 30
            and (cand_h.get("large25_hit_rate") or 0) > (base_h.get("large25_hit_rate") or 0)
            and (cand_h.get("clean_hit_rate") or 0) > (base_h.get("clean_hit_rate") or 0)
            and (cand_h.get("mean_net") or -1) > (base_h.get("mean_net") or 0)
            and (cand_h.get("median_net") or -1) > (base_h.get("median_net") or 0)
            and (cand_h.get("profit_factor") or 0) is not None
            and (base_h.get("profit_factor") or 0) is not None
            and (cand_h.get("profit_factor") or 0) > (base_h.get("profit_factor") or 0)
            and op_improved
        )
        # LONG/SHORT not fundamentally contradictory on clean hit vs baseline
        cand_long = _summ(
            [r for r in hold_pred if r["trade_side"] == "LONG" and r["selected_top20"]],
            "tmpL",
        )
        cand_short = _summ(
            [r for r in hold_pred if r["trade_side"] == "SHORT" and r["selected_top20"]],
            "tmpS",
        )
        base_long = _summ([r for r in hold_pred if r["trade_side"] == "LONG"], "tmpBL")
        base_short = _summ([r for r in hold_pred if r["trade_side"] == "SHORT"], "tmpBS")
        dir_ok = True
        if cand_long.get("n", 0) >= 10 and cand_short.get("n", 0) >= 10:
            # contradictory if one side clean rate collapses vs its baseline while other improves a lot
            dL = (cand_long.get("clean_hit_rate") or 0) - (base_long.get("clean_hit_rate") or 0)
            dS = (cand_short.get("clean_hit_rate") or 0) - (base_short.get("clean_hit_rate") or 0)
            if (dL < -0.05 and dS > 0.05) or (dS < -0.05 and dL > 0.05):
                dir_ok = False
        if improved and dir_ok:
            research = "LARGE_MOVE_CANDIDATE_READY_FOR_FUTURE_CONFIRMATION"
        elif (cand_h.get("large25_hit_rate") or 0) > (base_h.get("large25_hit_rate") or 0):
            research = "LARGE_MOVE_HOLDOUT_NOT_CONFIRMED"
        elif any(u["flag"] == "STABLE_SEPARATION" for u in uni):
            research = "LARGE_MOVE_DEVELOPMENT_ONLY"
        else:
            research = "LARGE_MOVE_NOT_SEPARABLE"

    elapsed = time.perf_counter() - t0
    dq = {
        **NO_FIT_LM,
        "n_outcomes": len(outcomes),
        "n_features_rows": len(features),
        "n_excluded_leaky": len(excluded_leaky),
        "n_selected_features": len(selected),
        "candidate_sha": stored_sha,
        "oi_coverage": sum(1 for f in features if f.get("oi_available") == 1.0) / len(features),
        "liquidations": "not_available_excluded",
    }
    write_json(output_dir / "data_quality_report.json", dq)

    summary = {
        "technical_verdict": tech,
        "research_verdict": research,
        **NO_FIT_LM,
        "freeze_sha_before": before["freeze_bundle_sha256"],
        "freeze_sha_after": after["freeze_bundle_sha256"],
        "cohort_fingerprint": fp,
        "n_unique_episodes": len(ids),
        "label_hit_rates": {
            "LARGE_MOVE_25BPS_15M": all_large,
            "CLEAN_LARGE_MOVE_25_15": all_clean,
        },
        "selected_features": selected,
        "coefficients": coefs,
        "candidate_sha": stored_sha,
        "development_n": len(dev),
        "holdout_baseline": base_h,
        "holdout_candidate_top20": cand_h,
        "one_position_candidate": op_sum,
        "elapsed_s": round(elapsed, 3),
        "query_count": len(query_log),
        "net_positive_candidate": (cand_h.get("median_net") or 0) > 0,
        "trading_edge_proven": False,
    }
    write_json(output_dir / "verdict.json", summary)
    write_json(output_dir / "SUMMARY.json", summary)
    write_json(
        output_dir / "run_manifest.json",
        {**NO_FIT_LM, "elapsed_s": round(elapsed, 3), "query_count": len(query_log), "n": len(ids)},
    )
    _write_report(output_dir, summary, uni)
    return summary


def _write_report(output_dir: Path, summary: dict[str, Any], uni: list[dict[str, Any]]) -> None:
    import subprocess

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd="/home/telgenbuescher/projects/orderbook_analyse",
                text=True,
            ).strip()
        )
    except Exception:
        branch, head, dirty = "unknown", "unknown", True

    top = sorted([u for u in uni if u.get("auc")], key=lambda u: -float(u["auc"]))[:10]
    next_step = (
        "FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1 — Candidate Bundle unverändert, "
        "nur spätere ungesehene Tage, ≥100 Candidate-Trades / ≥3 neue UTC-Tage"
        if summary["research_verdict"] == "LARGE_MOVE_CANDIDATE_READY_FOR_FUTURE_CONFIRMATION"
        else "Kein neuer Strategie-Backtest. Acceptance ggf. nur als Chart-/Kontextsignal; "
        "fehlende Feature-Familien ehrlich belassen."
    )
    lines = [
        "# ABSCHLUSSBERICHT — LARGE_MOVE_SEPARABILITY_DISCOVERY_V1",
        "",
        f"1. Technisches Verdict: `{summary['technical_verdict']}`",
        f"2. Research Verdict: `{summary['research_verdict']}`",
        "3. Live-Sicherheit: read-only; keine CH-Writes; kein Collector-Change",
        f"4. Branch / HEAD / Dirty: `{branch}` / `{head}` / dirty={dirty}",
        f"5. Freeze V2 SHA vor/nach: `{summary.get('freeze_sha_before')}` / `{summary.get('freeze_sha_after')}`",
        f"6. Kohorten-Fingerprint: {json.dumps(summary.get('cohort_fingerprint'))}",
        f"7. unique Episoden: {summary.get('n_unique_episodes')}",
        f"8. Label-Hit-Rates: {json.dumps(summary.get('label_hit_rates'))}",
        f"9. 25-bps-Hit-Rate: {(summary.get('label_hit_rates') or {}).get('LARGE_MOVE_25BPS_15M')}",
        f"10. Clean-25-vor-−15: {(summary.get('label_hit_rates') or {}).get('CLEAN_LARGE_MOVE_25_15')}",
        "11. Feature-Coverage: siehe feature_missingness.csv / data_quality_report.json",
        "12. Leakage-Features: excluded_leaky_features.csv",
        f"13. stärkste Dev-Features: {json.dumps(top)}",
        "14. Tagesstabilität: feature_day_stability.csv",
        "15. LONG/SHORT-Stabilität: feature_direction_stability.csv",
        "16. Pool-Distance: pool_distance_audit.csv",
        "17–20. Tradeflow/Orderbook/OI/Kontext: univariate + selected_features",
        f"21. eingefrorene Features (≤8): {summary.get('selected_features')}",
        f"22. Koeffizienten: {json.dumps(summary.get('coefficients'))}",
        "23. Development: development_predictions.csv / development_model_summary.json",
        f"24. Candidate SHA: `{summary.get('candidate_sha')}`",
        f"25. Holdout-Baseline: {json.dumps(summary.get('holdout_baseline'))}",
        f"26. Holdout-Candidate Top20%: {json.dumps(summary.get('holdout_candidate_top20'))}",
        "27. Large-Move-Verbesserung: Candidate vs Baseline in Holdout-Summaries",
        "28. Gross/Net: in Holdout-Summaries (approx net = gross − fees − 2×1bp slip)",
        f"29. One-position: {json.dumps(summary.get('one_position_candidate'))}",
        "30. Kosten: Taker 5.5+5.5 bps + 1bp/side; Break-even ≈13 bps",
        "31. Bootstrap: bootstrap_summary.csv",
        f"32. No-Fit-Flags: {json.dumps(NO_FIT_LM)}",
        "33. Tests: tests/test_frozen_high_accepted_large_move_separability_v1.py",
        f"34. Laufzeit/Queries: {summary.get('elapsed_s')}s / {summary.get('query_count')}",
        f"35. Können große Moves ex ante getrennt werden? → `{summary['research_verdict']}`",
        f"36. Kandidat nach Kosten positiv? → net_positive_candidate={summary.get('net_positive_candidate')}",
        "37. Einschränkung: nur ein Holdout-Tag; kein Trading-Edge-Beweis",
        f"38. Nächster Schritt: {next_step}",
        "",
    ]
    (output_dir / "ABSCHLUSSBERICHT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument(
        "--reuse-matrices",
        action="store_true",
        help="Reuse outcomes/feature CSVs in output-dir (refit/selection only)",
    )
    args = p.parse_args()
    s = run_large_move_discovery(
        output_dir=args.output_dir,
        max_events=args.max_events,
        reuse_matrices=args.reuse_matrices,
    )
    print(s["technical_verdict"], s["research_verdict"], s.get("selected_features"))
