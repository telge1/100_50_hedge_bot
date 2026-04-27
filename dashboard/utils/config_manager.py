"""
Config management utilities
"""
import os
import yaml
from pathlib import Path


def _get_project_root() -> Path:
    """Project root: from env (set by dashboard app) or from this file's path."""
    env_root = os.environ.get("BURN_REENTRY_PROJECT_ROOT", "").strip()
    if env_root and Path(env_root).is_dir():
        return Path(env_root).resolve()
    # dashboard/utils/config_manager.py -> parent.parent.parent = project root
    return Path(__file__).resolve().parent.parent.parent


def _normalize_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    s = str(symbol).strip().upper()
    return s or None


def _bot_type_to_prefix(bot_type: str) -> str:
    bt = (bot_type or "long").strip().lower()
    return "short" if bt == "short" else "long"


def _profile_to_config_label(profile: str | None) -> str:
    """Profil-Label für Config-Header: Main | Bot 1 | Bot 2"""
    if profile == "bot_1":
        return "Bot 1"
    if profile == "bot_2":
        return "Bot 2"
    return "Main"


def get_config_header_comment(profile: str | None, bot_type: str, filename: str) -> str:
    """Erzeugt die Titelzeile für Config-Dateien: # Main Long-Config (long_config_TONUSDT.yaml)"""
    label = _profile_to_config_label(profile)
    prefix = "Long" if (bot_type or "long").strip().lower() == "long" else "Short"
    return f"# {label} {prefix}-Config ({filename})\n"


def get_config_path(
    *,
    bot_type: str = "long",
    symbol: str | None = None,
    profile: str | None = None,
) -> Path:
    """Returns the config path for bot_type (+ optional per-symbol suffix).

    Default (Main):
      config/long_config_XRPUSDT.yaml, config/short_config_XRPUSDT.yaml

    Mit Profil (bot_1 / bot_2):
      config/bot_1/long_config_XRPUSDT.yaml, config/bot_1/short_config_XRPUSDT.yaml
    """
    project_dir = _get_project_root()
    prefix = _bot_type_to_prefix(bot_type)
    sym = _normalize_symbol(symbol)

    base_dir = project_dir / "config"
    prof = (profile or "").strip().lower()
    if prof in ("bot_1", "bot_2"):
        base_dir = base_dir / prof

    if sym:
        return base_dir / f"{prefix}_config_{sym}.yaml"
    return base_dir / f"{prefix}_config.yaml"


def load_config(
    symbol: str | None = None,
    bot_type: str = "long",
    *,
    fallback_to_global: bool = True,
    profile: str | None = None,
) -> dict:
    """
    Load config.
    - If symbol is provided: try `config/{bot}_config_<SYMBOL>.yaml`.
    - If missing and fallback_to_global: fall back to global `config/{bot}_config.yaml`.
    """
    symbol = _normalize_symbol(symbol)
    symbol_path = get_config_path(bot_type=bot_type, symbol=symbol, profile=profile) if symbol else None
    global_path = get_config_path(bot_type=bot_type, symbol=None, profile=profile)

    path = None
    used_fallback = False
    if symbol_path and symbol_path.exists():
        path = symbol_path
    elif fallback_to_global and global_path.exists():
        path = global_path
        used_fallback = True
    else:
        return {}

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # Fallback: global Config kann Symbol eines anderen Coins enthalten – stets das angeforderte setzen
    if used_fallback and symbol and isinstance(cfg, dict):
        cfg = {**cfg, "symbol": symbol}
    return cfg


def save_config(
    symbol: str | None = None,
    config: dict | None = None,
    bot_type: str = "long",
    *,
    create_if_missing: bool = True,
    fallback_to_global_template_on_create: bool = True,
    profile: str | None = None,
) -> bool:
    """
    Save config.
    - If symbol is provided: saves to `config/{bot}_config_<SYMBOL>.yaml` (per-coin).
    - If symbol is None: saves to global `config/{bot}_config.yaml` (template/default).

    create_if_missing:
    - If False and the per-symbol file doesn't exist -> returns False (used for start/restart blocking).
    - If True and missing -> create it (optionally based on global template) and then apply updates.
    """
    if not isinstance(config, dict) or config is None:
        config = {}

    symbol = _normalize_symbol(symbol)
    config_file = get_config_path(bot_type=bot_type, symbol=symbol, profile=profile)
    global_template_file = get_config_path(bot_type=bot_type, symbol=None, profile=profile)

    if symbol and not config_file.exists() and not create_if_missing:
        return False
    
    try:
        # Load existing config to preserve other values
        existing_config = {}
        if config_file.exists():
            with open(config_file, 'r') as f:
                existing_config = yaml.safe_load(f) or {}
        elif symbol and create_if_missing:
            # Bootstrap per-symbol config von globaler Template-Datei (falls vorhanden)
            if fallback_to_global_template_on_create and global_template_file.exists():
                with open(global_template_file, "r", encoding="utf-8") as f:
                    existing_config = yaml.safe_load(f) or {}
            else:
                # Fallback: nutze eine bestehende per-coin Config als Template.
                # Für long-Bots bevorzugen wir explizit DOGEUSDT (so wie von dir gewünscht),
                # damit neue Coins dieselbe Struktur wie long_config_DOGEUSDT.yaml haben.
                cfg_dir = config_file.parent
                try:
                    template_path = None
                    if _bot_type_to_prefix(bot_type) == "long":
                        doge_tpl = cfg_dir / "long_config_DOGEUSDT.yaml"
                        if doge_tpl.exists():
                            template_path = doge_tpl
                    if template_path is None:
                        prefix = _bot_type_to_prefix(bot_type)
                        for tpl in sorted(cfg_dir.glob(f"{prefix}_config_*.yaml")):
                            if tpl == config_file:
                                continue
                            template_path = tpl
                            break
                    if template_path and template_path.exists():
                        with open(template_path, "r", encoding="utf-8") as f:
                            existing_config = yaml.safe_load(f) or {}
                    else:
                        existing_config = {}
                except Exception:
                    existing_config = {}

        # Ergänze fehlende Defaults aus Template (wichtig für neue Coins wie DASHUSDT)
        try:
            template_cfg = {}
            project_dir = _get_project_root()
            if _bot_type_to_prefix(bot_type) == "long":
                doge_tpl = project_dir / "config" / "long_config_DOGEUSDT.yaml"
                if doge_tpl.exists():
                    with open(doge_tpl, "r", encoding="utf-8") as f:
                        template_cfg = yaml.safe_load(f) or {}
                elif global_template_file.exists():
                    with open(global_template_file, "r", encoding="utf-8") as f:
                        template_cfg = yaml.safe_load(f) or {}
            elif global_template_file.exists():
                with open(global_template_file, "r", encoding="utf-8") as f:
                    template_cfg = yaml.safe_load(f) or {}
            if isinstance(template_cfg, dict) and template_cfg:
                for k, v in template_cfg.items():
                    # Nur fehlende Keys setzen, nichts überschreiben
                    existing_config.setdefault(k, v)
        except Exception:
            # Fallback: Änderung nicht abbrechen, nur Defaults nicht angewendet
            pass
        existing_config.setdefault("min_rebuy_usdt", 7.0)
        existing_config.setdefault("spread_recovery_mode", _default_spread_recovery_mode())
        _normalize_spread_recovery_mode(existing_config)

        # Prüfe, ob sich die Werte wirklich geändert haben (Zahlen vergleichen normalisiert)
        has_changes = False
        for key, value in config.items():
            old = existing_config.get(key)
            if key not in existing_config:
                has_changes = True
                break
            try:
                if isinstance(value, (int, float)) and old is not None and isinstance(old, (int, float)):
                    if float(old) != float(value):
                        has_changes = True
                        break
                elif isinstance(value, list) and key == "burn_levels":
                    # burn_levels: Listen als Zahlenfolge vergleichen (YAML kann int/float mischen)
                    if not isinstance(old, list) or len(old) != len(value):
                        has_changes = True
                        break
                    for i, v in enumerate(value):
                        try:
                            ov = float(old[i]) if i < len(old) else None
                            nv = float(v) if v is not None else None
                            if ov != nv:
                                has_changes = True
                                break
                        except (TypeError, ValueError, IndexError):
                            has_changes = True
                            break
                    if has_changes:
                        break
                elif isinstance(value, list) and key == "next_cycle_rebuys":
                    if not isinstance(old, list) or len(old) != len(value):
                        has_changes = True
                        break
                    for i, v in enumerate(value):
                        try:
                            ov = int(old[i]) if i < len(old) and old[i] is not None else None
                            nv = int(v) if v is not None else None
                            if ov != nv:
                                has_changes = True
                                break
                        except (TypeError, ValueError, IndexError):
                            has_changes = True
                            break
                    if has_changes:
                        break
                elif old != value:
                    has_changes = True
                    break
            except (TypeError, ValueError):
                if old != value:
                    has_changes = True
                    break

        # Wenn explizit burn_levels gesendet wurde (Speichern-Button), immer schreiben
        if "burn_levels" in config:
            has_changes = True
        if "next_cycle_rebuys" in config:
            has_changes = True
        # start_price immer schreiben, wenn im Update enthalten (Dashboard „Start Preis“)
        if "start_price" in config:
            has_changes = True
        if not has_changes:
            # Keine Änderungen - Datei nicht schreiben (verhindert unnötigen Config-Watcher-Trigger)
            import logging
            logger = logging.getLogger(__name__)
            logger.debug(f"ℹ️ Config unverändert: {config_file} - keine Speicherung nötig")
            return True  # Erfolgreich (keine Änderung nötig)
        
        # Merge new values with existing config
        existing_config.update(config)
        _normalize_spread_recovery_mode(existing_config)
        
        # Optionale Felder: None = aus Config entfernen (z. B. start_price leer)
        for k in list(existing_config.keys()):
            if existing_config[k] is None:
                del existing_config[k]
        
        # Entferne veraltete Keys (nur initial_short_usdt / initial_long_usdt nutzen)
        for deprecated in ('target_long_notional', 'initial_long_notional', 'target_short_notional', 'initial_short_notional'):
            existing_config.pop(deprecated, None)
        
        # Save merged config (gleiche Block-Formatierung wie long_config_UNIUSDT.yaml)
        config_file.parent.mkdir(parents=True, exist_ok=True)
        header = get_config_header_comment(profile, bot_type, config_file.name)
        content = header + format_config_with_blocks(existing_config, bot_type=bot_type)
        with open(config_file, "w", encoding="utf-8") as f:
            f.write(content)
        
        # Log successful save inkl. absolutem Pfad (zum Prüfen, ob richtige Datei getroffen wird)
        import logging
        logger = logging.getLogger(__name__)
        abs_path = config_file.resolve()
        logger.info(f"✅ Config gespeichert: {abs_path} - Werte: {config}")
        
        return True
    except Exception as e:
        import logging
        import traceback
        logger = logging.getLogger(__name__)
        error_msg = f"❌ Fehler beim Speichern der Config {config_file}: {str(e)}"
        logger.error(error_msg, exc_info=True)
        # Print to console as well for debugging
        print(f"ERROR: {error_msg}")
        print(traceback.format_exc())
        return False


# Keys that belong to "Zyklus 1" (active cycle); rest is global. Used for block-style config.
CYCLE_KEYS = frozenset({"burn_mode", "burns_before_rebuy", "burn_pct", "burn_profit_pct", "burn_levels"})
# Optional keys only used for comment lines (Von/Bis)
CYCLE1_RANGE_KEYS = frozenset({"cycle1_range_start", "cycle1_range_end"})
NEXT_CYCLE_RANGE_KEYS = frozenset({"range_start", "range_end"})

# Exakte Formatierung wie long_config_UNIUSDT.yaml: Reihenfolge und Leerzeilen 1:1
SECTION_SEP = "# " + "=" * 59


def _dump_block(data: dict) -> str:
    """YAML-Dump eines Dicts ohne trailing newline."""
    if not data:
        return ""
    return yaml.dump(data, default_flow_style=False, sort_keys=False).rstrip()


def _default_spread_recovery_mode() -> dict:
    return {
        "enabled": False,
        "trigger_min_pct": 2.0,
        "trigger_max_pct": 2.5,
        "step_divisor": 3.0,
        "tp_pct": 0.5,
        "hedge_restore_extra_drop_pct": 0.1,
    }


def _normalize_spread_recovery_mode(cfg: dict) -> None:
    current = cfg.get("spread_recovery_mode")
    defaults = _default_spread_recovery_mode()
    if not isinstance(current, dict):
        cfg["spread_recovery_mode"] = dict(defaults)
        return
    merged = dict(defaults)
    merged.update(current)
    cfg["spread_recovery_mode"] = merged


def _format_spread_recovery_mode_block(mode: dict) -> str:
    mode = dict(mode or {})
    return "\n".join([
        "spread_recovery_mode:",
        f"  enabled: {str(bool(mode.get('enabled', False))).lower()}  # Recovery-Mode aktivieren/deaktivieren",
        f"  trigger_min_pct: {mode.get('trigger_min_pct', 2.0)}  # min spread",
        f"  trigger_max_pct: {mode.get('trigger_max_pct', 2.5)}  # max spread",
        f"  step_divisor: {mode.get('step_divisor', 3.0)}  # spread / step_divisor",
        f"  tp_pct: {mode.get('tp_pct', 0.5)}  # rebuy_tp",
        f"  hedge_restore_extra_drop_pct: {mode.get('hedge_restore_extra_drop_pct', 0.1)}  # hedge rebuy ratio repair",
    ])


def format_config_with_blocks(config: dict, bot_type: str = "long") -> str:
    """
    Erzeugt YAML-Config-String im exakten Block-Format (wie long_config_UNIUSDT.yaml):
    Global / Symbol → Tp modes → Burn settings → Zyklus 1 – Erste Range (aktiv).
    Leerzeilen und Kommentarblöcke 1:1.
    """
    is_long = _bot_type_to_prefix(bot_type) == "long"
    lines = []

    # ---- Global / Symbol ----
    global_order_long = ("symbol", "initial_long_usdt", "be_target_profit", "exit_close", "min_rebuy_usdt", "start_price")
    global_order_short = ("symbol", "initial_short_usdt", "be_target_profit", "exit_close", "min_rebuy_usdt", "start_price")
    global_order = global_order_long if is_long else global_order_short
    global_keys = [k for k in global_order if k in config]
    # Weitere Keys, die in keine andere Sektion gehören
    other_global = [k for k in config if k not in global_keys and k not in (
        "long_tp_mode", "short_tp_mode", "long_tp_percentage", "short_tp_percentage",
        "long_tp_fixed_price", "short_tp_fixed_price", "exit_levels",
        "tp_atr_multiplier", "tp_atr_min_pct", "tp_atr_max_pct",
        "burn_mode", "burns_before_rebuy", "burn_distance_percentage",
        "atr_burn_enabled", "burn_atr_enabled", "burn_atr_multiplier", "burn_atr_min_pct", "burn_atr_max_pct",
        "target_net_burn_profit_pct", "min_burn_distance_pct", "max_burn_distance_pct", "fee_rate",
        "next_cycle_rebuys", "burn_pct", "burn_profit_pct", "burn_levels",
        "rebuy_profile", "spread_zones", "burn_guard", "projected_spread_trigger_pct",
        "spread_recovery_mode",
    ) and k not in CYCLE1_RANGE_KEYS and k != "next_cycles"]
    global_keys += sorted(other_global)
    if global_keys:
        lines.append(SECTION_SEP)
        lines.append("# Global / Symbol")
        lines.append(SECTION_SEP)
        block = _dump_block({k: config[k] for k in global_keys})
        if block:
            # Kommentar für exit_close: false=Auto-Restart nach Profit, true=Bot stoppt
            block_lines = block.split("\n")
            for i, bl in enumerate(block_lines):
                if bl.strip().startswith("exit_close:"):
                    block_lines[i] = bl + "  # false=Auto-Restart nach Profit, true=Bot stoppt"
                    break
            lines.append("\n".join(block_lines))
        lines.append("")

    # ---- Tp modes ----
    tp_order_long = (
        "long_tp_mode",
        "long_tp_percentage",
        "short_tp_percentage",
        "tp_atr_multiplier",
        "tp_atr_min_pct",
        "tp_atr_max_pct",
        "long_tp_fixed_price",
        "short_tp_fixed_price",
        "exit_levels",
    )
    tp_order_short = (
        "short_tp_mode",
        "short_tp_percentage",
        "long_tp_percentage",
        "tp_atr_multiplier",
        "tp_atr_min_pct",
        "tp_atr_max_pct",
        "short_tp_fixed_price",
        "long_tp_fixed_price",
        "exit_levels",
    )
    tp_order = tp_order_long if is_long else tp_order_short
    tp_keys = [k for k in tp_order if k in config]
    if tp_keys:
        lines.append(SECTION_SEP)
        lines.append("# TP modes (Options: percent / fixed_price / atr)")
        if "exit_levels" in config:
            lines.append("# exit_levels = Reentry-Preis des Hedge-Partners (Long→Short, Short→Long)")
        lines.append(SECTION_SEP)
        lines.append("")
        block = _dump_block({k: config[k] for k in tp_keys})
        if block:
            lines.append(block)
        lines.append("")

    # ---- Burn settings ----
    # next_cycle_rebuys nicht mehr ausgeben: Zyklus 2+ kommt aus next_cycles (Dashboard generiert)
    burn_top = (
        "burn_mode",
        "burns_before_rebuy",
        "burn_distance_percentage",
        "target_net_burn_profit_pct",
        "min_burn_distance_pct",
        "max_burn_distance_pct",
        "fee_rate",
        "atr_burn_enabled",
        "burn_atr_enabled",
    )
    burn_bottom = (
        "burn_pct",
        "burn_profit_pct",
        "burn_atr_multiplier",
        "burn_atr_min_pct",
        "burn_atr_max_pct",
    )
    burn_top_keys = [k for k in burn_top if k in config]
    burn_bottom_keys = [k for k in burn_bottom if k in config]
    if burn_top_keys or burn_bottom_keys:
        lines.append(SECTION_SEP)
        lines.append("# Burn settings (Options: percentage / fixed_levels / atr / dynamic_spread)")
        lines.append("# percentage = Burn-Trigger startet mit burn_distance_percentage")
        lines.append("# und kann im aktiven Guard-Bereich automatisch weiter nach außen gezogen werden")
        lines.append("# fixed_levels = Burn-Trigger kommt direkt aus burn_levels")
        lines.append("# dynamic_spread = Burn-Trigger wird dynamisch aus Profit-/Spread-Logik berechnet")
        lines.append("# burns_before_rebuy = nach wie vielen Burns der Rebuy-Zyklus startet")
        lines.append("# burn_pct = wie viel von der Verlustseite pro Burn maximal reduziert wird")
        lines.append("# burn_profit_pct = wie viel vom realisierten TP-Profit maximal für den Burn-Risikoanteil")
        lines.append("# verwendet werden darf")
        lines.append("# max_burn_distance_pct = 0.0 bedeutet: keine feste Obergrenze")
        lines.append(SECTION_SEP)
        lines.append("")
        if burn_top_keys:
            block = _dump_block({k: config[k] for k in burn_top_keys})
            if block:
                lines.append(block)
        if burn_top_keys and burn_bottom_keys:
            lines.append("")
        if burn_bottom_keys:
            block = _dump_block({k: config[k] for k in burn_bottom_keys})
            if block:
                lines.append(block)
        lines.append("")

    # ---- Spread-Control ----
    spread_block = {}
    if "projected_spread_trigger_pct" in config:
        spread_block["projected_spread_trigger_pct"] = config["projected_spread_trigger_pct"]
    if "rebuy_profile" in config:
        spread_block["rebuy_profile"] = config["rebuy_profile"]
    if "spread_zones" in config:
        spread_block["spread_zones"] = config["spread_zones"]
    if "burn_guard" in config:
        spread_block["burn_guard"] = config["burn_guard"]
    if spread_block:
        lines.append(SECTION_SEP)
        lines.append("# Spread-Control Profil (Downtrend)")
        lines.append("# projected_spread_trigger_pct = ab welchem projizierten End-Spread der Bot")
        lines.append("# schon waehrend Burn 2 defensivere Rebuy-/Hedge-Werte fuer den Zyklus lockt")
        lines.append("#")
        lines.append("# rebuy_profile = zyklusbasierte Staffelung fuer rebuy_factor und hedge_ratio")
        lines.append("# spread_zones = optionale Zusatz-Defensive je nach Spread")
        lines.append("#")
        lines.append("# burn_guard.enabled = aktiviert die Profit-Schutzlogik")
        lines.append("# burn_guard.start_cycle = ab welchem Zyklus die Schutzlogik eingreifen darf")
        lines.append("# burn_guard.min_spread_pct = ab welchem Spread die Schutzlogik aktiv wird")
        lines.append("# burn_guard.fee_buffer_pct = zusaetzlicher Puffer auf die geschaetzten Fees")
        lines.append("# Beispiel: 50.0 bedeutet Burn-Ziel = Fees + 50% Puffer")
        lines.append("#")
        lines.append("# WICHTIG:")
        lines.append("# Bei burn_mode=percentage oder dynamic_spread wird der Burn-Abstand bei Bedarf")
        lines.append("# automatisch erhöht, damit Fees + Puffer wieder erreicht werden.")
        lines.append("# Bei burn_mode=fixed_levels kann der Bot feste Level nicht automatisch verschieben.")
        lines.append(SECTION_SEP)
        lines.append("")
        block = _dump_block(spread_block)
        if block:
            lines.append(block)
        lines.append("")

    # ---- Spread-Recovery ----
    spread_recovery_block = {}
    if "spread_recovery_mode" in config:
        spread_recovery_block["spread_recovery_mode"] = config["spread_recovery_mode"]
    if spread_recovery_block:
        lines.append(SECTION_SEP)
        lines.append("# Spread-Recovery Mode")
        lines.append("# enabled = aktiviert den Recovery-Mode nach abgeschlossenem Rebuy-Zyklus")
        lines.append("# trigger_min_pct / trigger_max_pct = Spread-Fenster fuer die Aktivierung")
        lines.append("# step_divisor = Schrittweite fuer Recovery-Rebuys, z. B. spread / 3")
        lines.append("# tp_pct = TP pro Recovery-Tranche")
        lines.append("# hedge_restore_extra_drop_pct = weiterer Weg bis zum Hedge-Restore per Market")
        lines.append(SECTION_SEP)
        lines.append("")
        block = _format_spread_recovery_mode_block(spread_recovery_block.get("spread_recovery_mode", {}))
        if block:
            lines.append(block)
        lines.append("")

    # ---- Zyklus 1 – Erste Range (aktiv) ----
    if "burn_levels" in config:
        lines.append(SECTION_SEP)
        lines.append("# Zyklus 1 – Erste Range (aktiv)")
        lines.append(SECTION_SEP)
        lines.append("")
        block = _dump_block({"burn_levels": config["burn_levels"]})
        if block:
            lines.append(block)
        lines.append("")
        lines.append("")

    # ---- Zyklus 2, 3, … (next_cycles) ----
    next_cycles = config.get("next_cycles")
    if next_cycles and isinstance(next_cycles, list) and len(next_cycles) > 0:
        lines.append(SECTION_SEP)
        lines.append("# Zyklus 2 – Zweite Range (nach Abschluss von Zyklus 1)")
        lines.append(SECTION_SEP)
        lines.append("")
        lines.append("next_cycles:")
        for i, cycle in enumerate(next_cycles):
            if not isinstance(cycle, dict):
                continue
            if i > 0:
                lines.append("")  # Leerzeile vor jedem weiteren Zyklus
            block = yaml.dump([cycle], default_flow_style=False, sort_keys=False)
            for bline in block.strip().split("\n"):
                lines.append("  " + bline)
        lines.append("")
        lines.append("")

    return "\n".join(lines)


def save_config_with_cycles(
    symbol: str | None = None,
    config: dict | None = None,
    bot_type: str = "long",
    *,
    create_if_missing: bool = True,
    fallback_to_global_template_on_create: bool = True,
    profile: str | None = None,
) -> bool:
    """
    Like save_config but writes config in block format (Global, Zyklus 1, next_cycles)
    so the file stays human-readable with clear sections.
    """
    if not isinstance(config, dict):
        config = {}

    symbol = _normalize_symbol(symbol)
    config_file = get_config_path(bot_type=bot_type, symbol=symbol, profile=profile)
    global_template_file = get_config_path(bot_type=bot_type, symbol=None, profile=profile)

    if symbol and not config_file.exists() and not create_if_missing:
        return False

    try:
        existing_config = {}
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                existing_config = yaml.safe_load(f) or {}
        elif symbol and create_if_missing:
            if fallback_to_global_template_on_create and global_template_file.exists():
                with open(global_template_file, "r", encoding="utf-8") as f:
                    existing_config = yaml.safe_load(f) or {}
            else:
                cfg_dir = config_file.parent
                try:
                    template_path = None
                    if _bot_type_to_prefix(bot_type) == "long":
                        doge_tpl = cfg_dir / "long_config_DOGEUSDT.yaml"
                        if doge_tpl.exists():
                            template_path = doge_tpl
                    if template_path is None:
                        prefix = _bot_type_to_prefix(bot_type)
                        for tpl in sorted(cfg_dir.glob(f"{prefix}_config_*.yaml")):
                            if tpl != config_file:
                                template_path = tpl
                                break
                    if template_path and template_path.exists():
                        with open(template_path, "r", encoding="utf-8") as f:
                            existing_config = yaml.safe_load(f) or {}
                except Exception:
                    pass

        try:
            template_cfg = {}
            if _bot_type_to_prefix(bot_type) == "long":
                doge_tpl = get_config_path(bot_type="long", symbol="DOGEUSDT", profile=profile)
                if doge_tpl.exists():
                    with open(doge_tpl, "r", encoding="utf-8") as f:
                        template_cfg = yaml.safe_load(f) or {}
                elif global_template_file.exists():
                    with open(global_template_file, "r", encoding="utf-8") as f:
                        template_cfg = yaml.safe_load(f) or {}
                elif profile and profile in ("bot_1", "bot_2"):
                    main_global = get_config_path(bot_type="long", symbol=None, profile=None)
                    if main_global.exists():
                        with open(main_global, "r", encoding="utf-8") as f:
                            template_cfg = yaml.safe_load(f) or {}
            elif global_template_file.exists():
                with open(global_template_file, "r", encoding="utf-8") as f:
                    template_cfg = yaml.safe_load(f) or {}
            elif profile and profile in ("bot_1", "bot_2"):
                main_global = get_config_path(bot_type="short", symbol=None, profile=None)
                if main_global.exists():
                    with open(main_global, "r", encoding="utf-8") as f:
                        template_cfg = yaml.safe_load(f) or {}
            if isinstance(template_cfg, dict) and template_cfg:
                for k, v in template_cfg.items():
                    existing_config.setdefault(k, v)
        except Exception:
            pass
        existing_config.setdefault("min_rebuy_usdt", 7.0)
        existing_config.setdefault("be_target_profit", 0.5)
        existing_config.setdefault("burn_pct", 0.20)
        existing_config.setdefault("burn_profit_pct", 0.775)
        existing_config.setdefault("projected_spread_trigger_pct", 3.0)
        existing_config.setdefault("spread_recovery_mode", _default_spread_recovery_mode())
        _normalize_spread_recovery_mode(existing_config)
        if bot_type == "long":
            existing_config.setdefault("initial_long_usdt", 20.0)
        else:
            existing_config.setdefault("initial_short_usdt", 20.0)

        existing_config.update(config)
        _normalize_spread_recovery_mode(existing_config)
        for k in list(existing_config.keys()):
            if existing_config[k] is None:
                del existing_config[k]
        for deprecated in ("target_long_notional", "initial_long_notional", "target_short_notional", "initial_short_notional"):
            existing_config.pop(deprecated, None)

        config_file.parent.mkdir(parents=True, exist_ok=True)
        header = get_config_header_comment(profile, bot_type, config_file.name)
        content = header + format_config_with_blocks(existing_config, bot_type=bot_type)
        with open(config_file, "w", encoding="utf-8") as f:
            f.write(content)

        import logging
        logger = logging.getLogger(__name__)
        logger.info("✅ Config (Zyklus-Blöcke) gespeichert: %s", config_file.resolve())
        return True
    except Exception as e:
        import logging
        import traceback
        logger = logging.getLogger(__name__)
        logger.error("❌ Fehler beim Speichern der Config %s: %s", config_file, e, exc_info=True)
        print("ERROR:", e)
        print(traceback.format_exc())
        return False


def get_default_config(bot_type: str = "long") -> dict:
    """Get default config from normal config file (fallback if config doesn't exist)"""
    config_file = get_config_path(bot_type=bot_type, symbol=None)
    
    # If config file exists, return it (even if empty)
    if config_file.exists():
        try:
            with open(config_file, 'r', encoding="utf-8") as f:
                config = yaml.safe_load(f)
                return config if config else {}
        except Exception:
            return {}
    
    # If config file doesn't exist, return empty dict
    return {}

