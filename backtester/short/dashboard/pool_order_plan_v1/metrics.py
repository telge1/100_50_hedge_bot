"""Paired baseline vs pool metrics. No dashboard publish."""

from __future__ import annotations

from typing import Any, Iterable

from .schema import STATUS_NO_PLAN, STATUS_READY


def dashboard_style_summary(rows: list[dict[str, Any]], *, pnl_key: str = "pnl_pct") -> dict[str, Any]:
    """Match collector summarize_trade_views: gross, WIN/LOSS from result, 1-decimal display later."""
    signals = len(rows)
    wins = losses = opens = 0
    gp = gl = 0.0
    for r in rows:
        fr = str(r.get("result") or r.get("display_result") or "OPEN").upper()
        pnl = r.get(pnl_key)
        if fr == "WIN":
            wins += 1
            if pnl is not None and float(pnl) > 0:
                gp += float(pnl)
            elif pnl is not None:
                gl += float(pnl)
        elif fr == "LOSS":
            losses += 1
            if pnl is not None and float(pnl) < 0:
                gl += float(pnl)
            elif pnl is not None and float(pnl) > 0:
                gp += float(pnl)
        else:
            opens += 1
    closed = wins + losses
    return {
        "signals": signals,
        "wins": wins,
        "losses": losses,
        "open": opens,
        "win_rate_pct": (100.0 * wins / closed) if closed else None,
        "gross_profit_pct": gp,
        "gross_loss_pct": gl,
        "total_pnl_pct": gp + gl,
        "pnl_basis": "gross",
    }


def display_round(summary: dict[str, Any]) -> dict[str, Any]:
    wr = summary.get("win_rate_pct")
    return {
        "signals": summary.get("signals"),
        "wins": summary.get("wins"),
        "losses": summary.get("losses"),
        "open": summary.get("open"),
        "win_rate_pct_1dp": None if wr is None else round(float(wr) + 1e-12, 1),
        "gross_profit_pct_1dp": round(float(summary.get("gross_profit_pct") or 0.0), 1),
        "gross_loss_pct_1dp": round(float(summary.get("gross_loss_pct") or 0.0), 1),
        "total_pnl_pct_1dp": round(float(summary.get("total_pnl_pct") or 0.0), 1),
    }


def _pool_result(row: dict[str, Any]) -> str:
    if row.get("plan_status") == STATUS_NO_PLAN or not row.get("plan_status"):
        return "NO_PLAN"
    oc = str(row.get("outcome") or "OPEN").upper()
    if oc == "OPEN":
        return "OPEN"
    gross = row.get("gross_pnl_pct")
    if gross is None:
        return oc
    if float(gross) > 0:
        return "WIN"
    if float(gross) < 0:
        return "LOSS"
    return "FLAT"


def strategy_stats(rows: Iterable[dict[str, Any]], *, kind: str) -> dict[str, Any]:
    """kind=baseline uses result/pnl_pct; kind=pool uses plan/outcome/gross/net. NO_PLAN pnl=0 in all-set."""
    rows = list(rows)
    trades = []
    wins = losses = opens = no_plan = 0
    gp = gl = 0.0
    fees = 0.0
    net_sum = 0.0
    gross_sum = 0.0
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    win_pnls: list[float] = []
    loss_pnls: list[float] = []
    ordered = sorted(rows, key=lambda r: str(r.get("entry_time") or ""))
    for r in ordered:
        if kind == "pool":
            status = r.get("plan_status")
            if status == STATUS_NO_PLAN:
                no_plan += 1
                pnl_g = 0.0
                pnl_n = 0.0
                fee = 0.0
                is_trade = False
            else:
                oc = str(r.get("outcome") or "OPEN").upper()
                pnl_g = float(r.get("gross_pnl_pct") or 0.0) if oc != "OPEN" else 0.0
                fee = float(r.get("fees_pct") or 0.0) if oc != "OPEN" else 0.0
                pnl_n = float(r.get("net_pnl_pct") or 0.0) if oc != "OPEN" else 0.0
                is_trade = oc != "OPEN"
                if oc == "OPEN":
                    opens += 1
        else:
            fr = str(r.get("result") or "OPEN").upper()
            pnl_g = float(r.get("pnl_pct") or 0.0) if fr in ("WIN", "LOSS") else 0.0
            fee = 0.0
            pnl_n = pnl_g
            is_trade = fr in ("WIN", "LOSS")
            no_plan += 0
            if fr == "OPEN":
                opens += 1
        if is_trade:
            trades.append(r)
            gross_sum += pnl_g
            net_sum += pnl_n
            fees += fee
            if pnl_g > 0:
                wins += 1
                gp += pnl_g
                win_pnls.append(pnl_g)
            elif pnl_g < 0:
                losses += 1
                gl += pnl_g
                loss_pnls.append(pnl_g)
        eq += pnl_n if kind == "pool" else pnl_g
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    closed = wins + losses
    pf = (gp / abs(gl)) if gl < 0 else None
    return {
        "raw_or_set_n": len(rows),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "open": opens,
        "no_plan": no_plan,
        "win_rate_pct": (100.0 * wins / closed) if closed else None,
        "gross_profit_pct": gp,
        "gross_loss_pct": gl,
        "gross_pnl_pct": gross_sum,
        "fees_pct": fees,
        "net_pnl_pct": net_sum,
        "avg_per_trade": (gross_sum / len(trades)) if trades else None,
        "profit_factor": pf,
        "avg_winner": (sum(win_pnls) / len(win_pnls)) if win_pnls else None,
        "avg_loser": (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else None,
        "max_drawdown_pct": max_dd,
    }


def decide(baseline_net_or_gross: float, pool_net: float, *, coverage_ready: float) -> str:
    if coverage_ready < 0.3:
        return "INCONCLUSIVE"
    delta = pool_net - baseline_net_or_gross
    if abs(delta) < 0.25:
        return "INCONCLUSIVE"
    return "POOL_V1_BETTER" if delta > 0 else "BASELINE_BETTER"
