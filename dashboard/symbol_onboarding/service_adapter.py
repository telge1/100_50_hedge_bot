"""Thin OnboardService factory — no duplicated onboarding logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import jobs_dir, oa_root, plans_dir
from .schemas import OnboardForm


def build_service(
    *,
    environ: dict | None = None,
    jobs_subdir: Path | None = None,
    instrument_fetcher: Callable[[str], Any] | None = None,
    enable_live_side_effects: bool = True,
    systemctl_runner: Callable[..., Any] | None = None,
):
    from orderbook_analyse.symbol_onboarding.service import OnboardService

    root = oa_root(environ)
    return OnboardService(
        oa_root=root,
        jobs_dir=jobs_subdir if jobs_subdir is not None else jobs_dir(environ),
        instrument_fetcher=instrument_fetcher,
        enable_live_side_effects=enable_live_side_effects,
        systemctl_runner=systemctl_runner,
    )


def form_to_request(form: OnboardForm, *, apply: bool, allow_live_restart: bool):
    from orderbook_analyse.symbol_onboarding.service import OnboardRequest

    skips = form.skip_flags()
    return OnboardRequest(
        symbol=form.symbol,
        days=form.days,
        apply=apply,
        with_ob1000=form.with_ob1000,
        restart_live=bool(form.restart_live and apply),
        skip_backfill=skips["skip_backfill"],
        skip_candles=skips["skip_candles"],
        skip_oi=skips["skip_oi"],
        skip_trades=skips["skip_trades"],
        allow_live_restart=allow_live_restart,
    )


def run_plan(
    form: OnboardForm,
    *,
    principal: str,
    roles: frozenset[str],
    environ: dict | None = None,
    instrument_fetcher: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    import os

    from orderbook_analyse.symbol_onboarding.service import AuthContext, OnboardRejected

    env = environ if environ is not None else os.environ
    # Offline / pre-deploy stub — never writes configs; no network.
    if str(env.get("SYMBOL_ONBOARDING_PLAN_FIXTURE") or "").strip() == "1":
        from orderbook_analyse.symbol_onboarding.instrument import InstrumentMeta

        instrument = InstrumentMeta(
            symbol=form.symbol,
            status="Trading",
            category="linear",
            quote_coin="USDT",
            contract_type="LinearPerpetual",
            tick_size="0.01",
            qty_step="0.01",
            min_order_qty="0.01",
            min_notional="5",
            orderbook_supported=True,
            base_coin=form.symbol.replace("USDT", ""),
            settle_coin="USDT",
        )
        service = build_service(
            environ=environ,
            jobs_subdir=plans_dir(environ),
            instrument_fetcher=lambda _s: instrument,
            enable_live_side_effects=False,
        )
        auth = AuthContext(principal=principal, roles=roles)
        req = form_to_request(form, apply=False, allow_live_restart=False)
        return service.run(req, auth=auth)

    service = build_service(
        environ=environ,
        jobs_subdir=plans_dir(environ),
        instrument_fetcher=instrument_fetcher,
        enable_live_side_effects=False,
    )
    auth = AuthContext(principal=principal, roles=roles)
    req = form_to_request(form, apply=False, allow_live_restart=False)
    try:
        return service.run(req, auth=auth)
    except OnboardRejected as exc:
        raise


def get_job(job_id: str, *, environ: dict | None = None) -> dict[str, Any]:
    service = build_service(environ=environ)
    return service.get_job(job_id)


def list_job_files(*, environ: dict | None = None) -> list[Path]:
    root = jobs_dir(environ)
    if not root.is_dir():
        return []
    return sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
