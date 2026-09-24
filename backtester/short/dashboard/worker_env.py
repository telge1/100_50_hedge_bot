from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILE = Path("/home/telgenbuescher/projects/wave_fade_gold_live_bot/.env")
PINNED_GOLD_ROOT = Path("/home/telgenbuescher/projects/wave_fade_gold_f16ae32")
PINNED_SG_PYTHON = PINNED_GOLD_ROOT / ".venv" / "bin" / "python"
CLICKHOUSE_KEYS = (
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_SECURE",
    "CLICKHOUSE_VERIFY",
)


def secure_env_file(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_DASHBOARD_ENV_FILE") or "").strip()
    return Path(override) if override else DEFAULT_ENV_FILE


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'").strip()
        if key:
            values[key] = value
    return values


def inject_worker_env(environ: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, object]]:
    target = environ if environ is not None else dict(os.environ)
    env_file = secure_env_file(target)
    file_values = _parse_env_file(env_file)
    loaded: list[str] = []
    missing: list[str] = []
    for key in CLICKHOUSE_KEYS:
        if target.get(key):
            loaded.append(key)
            continue
        if file_values.get(key):
            target[key] = file_values[key]
            loaded.append(key)
        elif key in ("CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"):
            missing.append(key)
    if not str(target.get("STOCH_FADE_SG_PYTHON") or "").strip():
        target["STOCH_FADE_SG_PYTHON"] = str(PINNED_SG_PYTHON)
    if not str(target.get("STOCH_FADE_SIGNAL_GENERATOR_ROOT") or "").strip():
        target["STOCH_FADE_SIGNAL_GENERATOR_ROOT"] = str(PINNED_GOLD_ROOT)
    return target, {
        "env_file_exists": env_file.is_file(),
        "env_file_path": str(env_file),
        "loaded_keys": loaded,
        "missing_required": missing,
        "sg_python": str(target.get("STOCH_FADE_SG_PYTHON") or ""),
        "signal_generator_root": str(target.get("STOCH_FADE_SIGNAL_GENERATOR_ROOT") or ""),
    }


def sg_python_preflight(environ: dict[str, str] | None = None) -> dict[str, object]:
    """Fail-closed Gold interpreter check. No PATH/python3 fallback."""
    env = environ if environ is not None else os.environ
    raw = str(env.get("STOCH_FADE_SG_PYTHON") or "").strip()
    path = Path(raw) if raw else PINNED_SG_PYTHON
    if raw in ("python", "python3") or (raw and not path.is_absolute()):
        return {"ok": False, "error_code": "MISSING_SG_PYTHON"}
    given = str(path)
    real = os.path.realpath(path) if path.exists() else given
    if "Signal_Generator_Ralf" in given or "Signal_Generator_Ralf" in real:
        return {"ok": False, "error_code": "GOLD_VENV_IMPORT_ORIGIN_FAIL"}
    if not path.is_file() or not os.access(path, os.X_OK):
        return {"ok": False, "error_code": "MISSING_SG_PYTHON"}
    return {
        "ok": True,
        "error_code": None,
        "python_path": given,
        "runtime_root": str(PINNED_GOLD_ROOT),
    }


def clickhouse_preflight(environ: dict[str, str] | None = None) -> dict[str, object]:
    env, meta = inject_worker_env(environ)
    if meta["missing_required"]:
        return {"ok": False, "error_code": "MISSING_CLICKHOUSE_ENV", **meta}
    from research_charts.clickhouse_config import load_clickhouse_config
    import clickhouse_connect

    cfg = load_clickhouse_config(env)
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        one = client.query("SELECT 1").result_rows
        tables = client.query(
            "SELECT count() FROM system.tables WHERE database = currentDatabase() AND name = 'candles_1m'"
        ).result_rows
    finally:
        client.close()
    return {
        "ok": True,
        "error_code": None,
        "select_1_ok": bool(one and one[0][0] == 1),
        "candles_1m_visible": bool(tables and int(tables[0][0]) >= 1),
        **meta,
    }
