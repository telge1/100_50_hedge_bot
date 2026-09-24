"""German display copy for Live Orderbook cards (UI-only, no analysis engine).

Maps already-computed view fields into simple German headlines + metric labels.
Does not invent runner outputs that are missing.
"""

from __future__ import annotations

from typing import Any


Tone = str  # "positive" | "negative" | "warning" | "neutral" | "mixed"


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_num(v: Any, digits: int = 4) -> str:
    n = _f(v)
    if n is None:
        return "N/A"
    return f"{n:.{digits}f}"


def _fmt_pct(v: Any, digits: int = 2) -> str:
    n = _f(v)
    if n is None:
        return "N/A"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.{digits}f}%"


def _fmt_notional(v: Any) -> str:
    n = _f(v)
    if n is None:
        return "N/A"
    if abs(n) >= 1000:
        return f"{n / 1000.0:.1f}k"
    return f"{n:.1f}"


def _window_label(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "1m"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def _fmt_zone_num(v: Any, digits: int = 4) -> str:
    n = _f(v)
    if n is None:
        return "—"
    return f"{n:.{digits}f}"


def _fmt_move_pct(v: Any, digits: int = 2) -> str:
    """Unsigned-looking move percent without forced plus (keeps minus if negative)."""
    n = _f(v)
    if n is None:
        return "NOCH KEIN VERGLEICH"
    # show absolute magnitude with sign only when non-zero negative/positive without '+'
    if abs(n) < 0.005:
        return f"{0:.{digits}f}%"
    return f"{n:.{digits}f}%"


def _fmt_distance_badge(v: Any, digits: int = 2) -> str:
    """Distance is absolute; never show a plus sign."""
    n = _f(v)
    if n is None:
        return "ABSTAND —"
    return f"{abs(n):.{digits}f}% ENTFERNT"


def _fmt_change_pct(v: Any, digits: int = 2) -> str:
    n = _f(v)
    if n is None:
        return "SAMMELT DATEN"
    if abs(n) < 0.005:
        return f"{0:.{digits}f}%"
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.{digits}f}%"


def _zone_center_str(card: dict[str, Any]) -> str:
    cur = card.get("current_center")
    if cur is not None:
        return _fmt_zone_num(cur)
    lo, hi = card.get("zone_low"), card.get("zone_high")
    if lo is None and hi is None:
        return "NICHT ERKANNT"
    if lo is not None and hi is not None and float(lo) != float(hi):
        return f"{_fmt_zone_num(lo)}–{_fmt_zone_num(hi)}"
    return _fmt_zone_num(lo if lo is not None else hi)


def resistance_headline(card: dict[str, Any] | None) -> dict[str, Any]:
    empty_metrics = [
        {"key": "strength", "label": "Stärke", "value": "—"},
        {"key": "notional", "label": "Ask-Volumen", "value": "—"},
        {"key": "change", "label": "Änderung", "value": "SAMMELT DATEN"},
    ]
    if card is None:
        return {
            "detected": False,
            "headline": "WIDERSTAND NICHT MEHR SICHTBAR",
            "tone": "neutral",
            "distance_badge": "ABSTAND —",
            "prev_price": "NOCH KEIN VERGLEICH",
            "curr_price": "NICHT ERKANNT",
            "move_pct_display": "NOCH KEIN VERGLEICH",
            "move_line": "NOCH KEIN VERGLEICH → NICHT ERKANNT → NOCH KEIN VERGLEICH",
            "arrow": "→",
            "arrow_tone": "neutral",
            "tech": None,
            "metrics": empty_metrics,
        }

    direction = str(card.get("direction") or "unchanged")
    prev = card.get("previous_center")
    is_new = prev is None
    if is_new:
        headline, tone = "NEUER WIDERSTAND ERKANNT", "warning"
        arrow, arrow_tone = "→", "neutral"
    elif direction == "down":
        headline, tone = "WIDERSTAND KOMMT NÄHER", "negative"
        arrow, arrow_tone = "↓", "negative"
    elif direction == "up":
        headline, tone = "WIDERSTAND ENTFERNT SICH", "positive"
        arrow, arrow_tone = "↑", "positive"
    else:
        headline, tone = "WIDERSTAND BLEIBT STABIL", "neutral"
        arrow, arrow_tone = "→", "neutral"

    prev_s = "NOCH KEIN VERGLEICH" if prev is None else _fmt_zone_num(prev)
    cur_s = _zone_center_str(card)
    move_disp = _fmt_move_pct(card.get("move_pct")) if prev is not None else "NOCH KEIN VERGLEICH"

    strength = card.get("strength")
    strength_s = "—" if strength is None else f"{int(round(float(strength)))} / 100"
    notional = card.get("notional")
    notional_s = "—" if notional is None else f"{_fmt_notional(notional)} USDT"
    if str(notional_s).startswith("N/A"):
        notional_s = "—"

    return {
        "detected": True,
        "headline": headline,
        "tone": tone,
        "distance_badge": _fmt_distance_badge(card.get("distance_pct")),
        "prev_price": prev_s,
        "curr_price": cur_s,
        "move_pct_display": move_disp,
        "move_line": f"{prev_s} → {cur_s} → {move_disp}",
        "arrow": arrow,
        "arrow_tone": arrow_tone,
        "tech": None,
        "metrics": [
            {"key": "strength", "label": "Stärke", "value": strength_s},
            {"key": "notional", "label": "Ask-Volumen", "value": notional_s},
            {"key": "change", "label": "Änderung", "value": _fmt_change_pct(card.get("notional_change_pct"))},
        ],
    }


def support_headline(card: dict[str, Any] | None, *, title: str = "Support") -> dict[str, Any]:
    _ = title
    empty_metrics = [
        {"key": "strength", "label": "Stärke", "value": "—"},
        {"key": "notional", "label": "Bid-Volumen", "value": "—"},
        {"key": "change", "label": "Änderung", "value": "SAMMELT DATEN"},
    ]
    if card is None:
        return {
            "detected": False,
            "headline": "UNTERSTÜTZUNG NICHT MEHR SICHTBAR",
            "tone": "neutral",
            "distance_badge": "ABSTAND —",
            "prev_price": "NOCH KEIN VERGLEICH",
            "curr_price": "NICHT ERKANNT",
            "move_pct_display": "NOCH KEIN VERGLEICH",
            "move_line": "NOCH KEIN VERGLEICH → NICHT ERKANNT → NOCH KEIN VERGLEICH",
            "arrow": "→",
            "arrow_tone": "neutral",
            "tech": None,
            "metrics": empty_metrics,
        }

    direction = str(card.get("direction") or "unchanged")
    prev = card.get("previous_center")
    is_new = prev is None
    if is_new:
        headline, tone = "NEUE UNTERSTÜTZUNG ERKANNT", "warning"
        arrow, arrow_tone = "→", "neutral"
    elif direction == "up":
        headline, tone = "UNTERSTÜTZUNG KOMMT NÄHER", "positive"
        arrow, arrow_tone = "↑", "positive"
    elif direction == "down":
        headline, tone = "UNTERSTÜTZUNG FÄLLT ZURÜCK", "negative"
        arrow, arrow_tone = "↓", "negative"
    else:
        headline, tone = "UNTERSTÜTZUNG BLEIBT STABIL", "neutral"
        arrow, arrow_tone = "→", "neutral"

    prev_s = "NOCH KEIN VERGLEICH" if prev is None else _fmt_zone_num(prev)
    cur_s = _zone_center_str(card)
    move_disp = _fmt_move_pct(card.get("move_pct")) if prev is not None else "NOCH KEIN VERGLEICH"

    strength = card.get("strength")
    strength_s = "—" if strength is None else f"{int(round(float(strength)))} / 100"
    notional = card.get("notional")
    notional_s = "—" if notional is None else f"{_fmt_notional(notional)} USDT"
    if str(notional_s).startswith("N/A"):
        notional_s = "—"

    return {
        "detected": True,
        "headline": headline,
        "tone": tone,
        "distance_badge": _fmt_distance_badge(card.get("distance_pct")),
        "prev_price": prev_s,
        "curr_price": cur_s,
        "move_pct_display": move_disp,
        "move_line": f"{prev_s} → {cur_s} → {move_disp}",
        "arrow": arrow,
        "arrow_tone": arrow_tone,
        "tech": None,
        "metrics": [
            {"key": "strength", "label": "Stärke", "value": strength_s},
            {"key": "notional", "label": "Bid-Volumen", "value": notional_s},
            {"key": "change", "label": "Änderung", "value": _fmt_change_pct(card.get("notional_change_pct"))},
        ],
    }


def absorption_headline(data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {
            "headline": "SAMMLE ERSTE DATEN",
            "tone": "neutral",
            "tech": None,
            "metrics": [
                {"label": "Stärke", "value": "N/A"},
                {"label": "Reaktion", "value": "N/A"},
                {"label": "Zone", "value": "N/A"},
            ],
        }
    kind = str(data.get("kind") or data.get("type") or data.get("reading") or "").upper()
    if ("SELL_ABSORB" in kind or "SELL ABSORB" in kind or kind == "SELL_ABSORPTION") or (
        "SELL" in kind and "ABSORP" in kind
    ):
        headline, tone = "KÄUFER HALTEN DEN SUPPORT", "positive"
    elif ("BUY_ABSORB" in kind or "BUY ABSORB" in kind or kind == "BUY_ABSORPTION") or (
        "BUY" in kind and "ABSORP" in kind
    ):
        headline, tone = "VERKÄUFER HALTEN DIE RESISTANCE", "negative"
    elif kind in {"NONE", "NO_ABSORPTION", "CLEAR_NONE", ""} and data.get("kind") is not None:
        headline, tone = "KEINE KLARE ABSORPTION", "neutral"
    elif not kind:
        headline, tone = "KEINE KLARE ABSORPTION", "neutral"
    else:
        # Unknown payload shape — do not invent a directional claim
        headline, tone = "KEINE KLARE ABSORPTION", "neutral"

    return {
        "headline": headline,
        "tone": tone,
        "tech": data.get("kind") or data.get("type") or data.get("reading"),
        "metrics": [
            {"label": "Stärke", "value": str(data.get("strength", "N/A"))},
            {"label": "Reaktion", "value": str(data.get("reaction", "N/A"))},
            {"label": "Zone", "value": str(data.get("zone", "N/A"))},
        ],
    }


def near_price_headline(data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {
            "headline": "NOCH KEINE DATEN",
            "tone": "neutral",
            "tech": None,
            "metrics": [
                {"label": "0–10 bps", "value": "N/A"},
                {"label": "10–25 bps", "value": "N/A"},
            ],
        }
    bid_share = _f(data.get("bid_share_0_10") if "bid_share_0_10" in data else data.get("bid_share"))
    ask_share = _f(data.get("ask_share_0_10") if "ask_share_0_10" in data else data.get("ask_share"))
    if bid_share is None and ask_share is None:
        # bands list form
        bands = data.get("bands") if isinstance(data.get("bands"), list) else None
        if bands:
            first = bands[0] if isinstance(bands[0], dict) else {}
            bid_share = _f(first.get("bid_pct") or first.get("bid_share"))
            ask_share = _f(first.get("ask_pct") or first.get("ask_share"))

    bias = str(data.get("bias") or "").upper()
    if bid_share is not None and ask_share is not None:
        if bid_share - ask_share >= 5:
            headline, tone, bias = "MEHR KAUF-LIQUIDITÄT NAHE AM PREIS", "positive", bias or "BULLISH"
        elif ask_share - bid_share >= 5:
            headline, tone, bias = "MEHR VERKAUFS-LIQUIDITÄT NAHE AM PREIS", "negative", bias or "BEARISH"
        else:
            headline, tone, bias = "ORDERBOOK IST AUSGEGLICHEN", "neutral", bias or "NEUTRAL"
    elif bias in {"BULLISH", "BID"}:
        headline, tone = "MEHR KAUF-LIQUIDITÄT NAHE AM PREIS", "positive"
    elif bias in {"BEARISH", "ASK"}:
        headline, tone = "MEHR VERKAUFS-LIQUIDITÄT NAHE AM PREIS", "negative"
    elif bias in {"NEUTRAL"}:
        headline, tone = "ORDERBOOK IST AUSGEGLICHEN", "neutral"
    elif bias in {"NONE_NEAR"}:
        headline, tone = "KEINE WALLS NAHE AM PREIS", "warning"
    else:
        headline, tone = "NOCH KEINE DATEN", "neutral"

    def _band(key_bid: str, key_ask: str, fallback: str) -> str:
        b = _f(data.get(key_bid))
        a = _f(data.get(key_ask))
        if b is None or a is None:
            return fallback
        return f"Bid {b:.0f}%   Ask {a:.0f}%"

    metrics = [
        {
            "label": "0–10 bps",
            "value": _band("bid_share_0_10", "ask_share_0_10", "N/A"),
        },
        {
            "label": "10–25 bps",
            "value": _band("bid_share_10_25", "ask_share_10_25", "N/A"),
        },
    ]
    if isinstance(data.get("bands"), list):
        metrics = []
        for band in data["bands"][:3]:
            if not isinstance(band, dict):
                continue
            label = str(band.get("label") or band.get("band") or "Band")
            b = _f(band.get("bid_pct") or band.get("bid_share"))
            a = _f(band.get("ask_pct") or band.get("ask_share"))
            if b is None or a is None:
                val = "N/A"
            else:
                val = f"Bid {b:.0f}%   Ask {a:.0f}%"
            metrics.append({"label": label, "value": val})
        if not metrics:
            metrics = [{"label": "0–10 bps", "value": "N/A"}, {"label": "10–25 bps", "value": "N/A"}]

    return {
        "headline": headline,
        "tone": tone,
        "tech": bias or None,
        "metrics": metrics,
    }


def level_quality_headline(data: dict[str, Any] | None) -> dict[str, Any]:
    if data is None:
        return {
            "headline": "NOCH NICHT GENUG DATEN",
            "tone": "neutral",
            "tech": None,
            "metrics": [
                {"label": "Tests", "value": "N/A"},
                {"label": "Besteht seit", "value": "N/A"},
                {"label": "Ermüdung", "value": "N/A"},
            ],
        }
    fatigue = str(data.get("fatigue") or data.get("level") or data.get("quality") or "UNKNOWN").upper()
    if fatigue == "LOW":
        headline, tone = "LEVEL IST NOCH FRISCH", "positive"
    elif fatigue == "MEDIUM":
        headline, tone = "LEVEL WURDE MEHRFACH GETESTET", "warning"
    elif fatigue == "HIGH":
        headline, tone = "BREAK-RISIKO STEIGT", "negative"
    else:
        headline, tone = "NOCH NICHT GENUG DATEN", "neutral"
        fatigue = "UNKNOWN"

    tests = data.get("tests")
    age = data.get("age_display") or data.get("age") or data.get("besteht_seit")
    return {
        "headline": headline,
        "tone": tone,
        "tech": fatigue,
        "metrics": [
            {"label": "Tests", "value": "N/A" if tests is None else str(tests)},
            {"label": "Besteht seit", "value": "N/A" if age is None else str(age)},
            {"label": "Ermüdung", "value": fatigue},
        ],
    }


def classify_money_flow_regime(money: dict[str, Any] | None) -> str:
    """Classify flow from OI + price (delta only as soft support)."""
    if money is None or money.get("status") == "collecting":
        return "LOW_ACTIVITY"
    oi = _f(money.get("oi_change_pct"))
    px = _f(money.get("price_change_pct"))
    delta = _f(money.get("delta_notional"))
    if oi is None or px is None:
        # Without OI+price do not claim NEW_LONGS/NEW_SHORTS from delta alone
        if delta is None:
            return "LOW_ACTIVITY"
        if abs(delta) < 500:
            return "LOW_ACTIVITY"
        return "MIXED_FLOW"

    oi_up = oi > 0.02
    oi_down = oi < -0.02
    px_up = px > 0.05
    px_down = px < -0.05

    if oi_up and px_up:
        return "NEW_LONGS"
    if oi_up and px_down:
        return "NEW_SHORTS"
    if oi_down and px_up:
        return "SHORT_COVERING"
    if oi_down and px_down:
        return "LONG_UNWINDING"
    if abs(oi) <= 0.02 and abs(px) <= 0.05:
        return "LOW_ACTIVITY"
    return "MIXED_FLOW"


def money_flow_headline(money: dict[str, Any] | None) -> dict[str, Any]:
    if money is None or money.get("status") == "collecting":
        return {
            "headline": "WENIG AKTIVITÄT IM MARKT",
            "tone": "neutral",
            "tech": "LOW_ACTIVITY",
            "regime": "LOW_ACTIVITY",
            "metrics": [
                {"label": "Neue Positionen", "value": "—"},
                {"label": "Stärke", "value": "N/A"},
                {"label": "Delta", "value": "N/A"},
                {"label": "OI", "value": "N/A"},
                {"label": "Preisbewegung", "value": "N/A"},
            ],
        }
    regime = str(money.get("regime") or classify_money_flow_regime(money))
    mapping = {
        "NEW_LONGS": ("NEUE LONGS KOMMEN IN DEN MARKT", "positive"),
        "NEW_SHORTS": ("NEUE SHORTS KOMMEN IN DEN MARKT", "negative"),
        "SHORT_COVERING": ("SHORTS WERDEN GESCHLOSSEN", "positive"),
        "LONG_UNWINDING": ("LONGS WERDEN GESCHLOSSEN", "negative"),
        "MIXED_FLOW": ("KEIN KLARER GELDFLUSS", "mixed"),
        "LOW_ACTIVITY": ("WENIG AKTIVITÄT IM MARKT", "neutral"),
    }
    headline, tone = mapping.get(regime, ("KEIN KLARER GELDFLUSS", "mixed"))

    # Soft strength from |delta_ratio| if present, else N/A
    strength = money.get("strength")
    if strength is None:
        ratio = _f(money.get("delta_ratio"))
        if ratio is not None:
            strength = int(min(100, max(0, round(abs(ratio) * 100))))
    strength_s = "N/A" if strength is None else f"{int(strength)} / 100"

    positions = {
        "NEW_LONGS": "LONGS",
        "NEW_SHORTS": "SHORTS",
        "SHORT_COVERING": "SHORT-COVERING",
        "LONG_UNWINDING": "LONG-UNWINDING",
        "MIXED_FLOW": "GEMISCHT",
        "LOW_ACTIVITY": "WENIG",
    }.get(regime, "—")

    return {
        "headline": headline,
        "tone": tone,
        "tech": regime,
        "regime": regime,
        "metrics": [
            {"label": "Neue Positionen", "value": positions},
            {"label": "Stärke", "value": strength_s},
            {"label": "Delta", "value": f"{_fmt_notional(money.get('delta_notional'))} USDT"},
            {"label": "OI", "value": _fmt_pct(money.get("oi_change_pct"))},
            {"label": "Preisbewegung", "value": _fmt_pct(money.get("price_change_pct"))},
        ],
    }


def liquidations_headline(
    liq: dict[str, Any] | None,
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if liq is None or liq.get("status") == "collecting":
        return {
            "headline": "KAUM LIQUIDATIONEN",
            "tone": "neutral",
            "tech": None,
            "rising": None,
            "metrics": [
                {"label": "Longs", "value": "N/A"},
                {"label": "Shorts", "value": "N/A"},
                {"label": "Mehr betroffen", "value": "—"},
                {"label": "Steigt", "value": "—"},
            ],
        }

    # Buy liq = liquidated longs; Sell liq = liquidated shorts (project convention)
    longs = _f(liq.get("long_notional") if "long_notional" in liq else liq.get("buy_notional")) or 0.0
    shorts = _f(liq.get("short_notional") if "short_notional" in liq else liq.get("sell_notional")) or 0.0
    total = longs + shorts

    rising = liq.get("rising")
    if rising is None and previous is not None and previous.get("status") != "collecting":
        prev_longs = _f(previous.get("long_notional") if "long_notional" in previous else previous.get("buy_notional")) or 0.0
        prev_shorts = _f(previous.get("short_notional") if "short_notional" in previous else previous.get("sell_notional")) or 0.0
        prev_total = prev_longs + prev_shorts
        if prev_total > 0 or total > 0:
            rising = total > prev_total * 1.15 and total - prev_total >= 500

    if total < 100:
        headline, tone = "KAUM LIQUIDATIONEN", "neutral"
        affected = "—"
    else:
        # clear dominance if one side >= 1.5x the other and gap meaningful
        if longs >= shorts * 1.5 and longs - shorts >= 500:
            headline, tone = "VIELE LONGS WERDEN HERAUSGEDRÜCKT", "negative"
            affected = "LONGS"
        elif shorts >= longs * 1.5 and shorts - longs >= 500:
            headline, tone = "VIELE SHORTS WERDEN HERAUSGEDRÜCKT", "positive"
            affected = "SHORTS"
        else:
            headline, tone = "LONGS UND SHORTS ÄHNLICH BETROFFEN", "mixed"
            affected = "BEIDE"

    pressure = None
    if rising is True:
        pressure = "LIQUIDATIONSDRUCK NIMMT ZU"
        if tone == "neutral":
            tone = "warning"
    elif rising is False:
        pressure = "LIQUIDATIONSDRUCK NIMMT NICHT ZU"

    return {
        "headline": headline,
        "tone": tone,
        "tech": pressure,
        "rising": rising,
        "metrics": [
            {"label": "Longs", "value": f"{_fmt_notional(longs)} USDT"},
            {"label": "Shorts", "value": f"{_fmt_notional(shorts)} USDT"},
            {"label": "Mehr betroffen", "value": affected},
            {
                "label": "Steigt",
                "value": "JA" if rising is True else ("NEIN" if rising is False else "—"),
            },
        ],
    }


def _wall_side_state(wall_follow: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None, str | None]:
    ask_reading = None
    bid_reading = None
    ask_label = None
    bid_label = None
    for w in wall_follow or []:
        label = str(w.get("label") or "")
        reading = str(w.get("reading") or "").lower()
        side = str(w.get("side") or "").lower()
        if "ASK" in label.upper() or side == "ask":
            ask_reading = reading
            ask_label = label or "Ask Resistance"
        if ("BID" in label.upper() or side == "bid") and "2" not in label:
            bid_reading = reading
            bid_label = label or "Bid Support"
    return ask_reading, bid_reading, ask_label, bid_label


def wall_bias_headline(wall_follow: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = wall_follow or []
    if not rows:
        return {
            "headline": "NOCH KEINE KLARE RICHTUNG",
            "tone": "neutral",
            "tech": None,
            "metrics": [
                {"label": "Ask Resistance", "value": "N/A"},
                {"label": "Bid Support", "value": "N/A"},
                {"label": "technischer Bias", "value": "N/A"},
            ],
        }
    ask_r, bid_r, ask_label, bid_label = _wall_side_state(rows)
    ask = ask_r or ""
    bid = bid_r or ""

    ask_build = "build" in ask
    ask_weak = "weak" in ask or "disappear" in ask
    bid_build = "build" in bid
    bid_weak = "weak" in bid or "disappear" in bid

    if ask_build and bid_weak:
        headline, tone, tech = "VERKAUFSDRUCK NIMMT ZU", "negative", "ASK_BUILD + BID_WEAK"
    elif bid_build and ask_weak:
        headline, tone, tech = "KAUFDRUCK NIMMT ZU", "positive", "BID_BUILD + ASK_WEAK"
    elif ask_weak and bid_weak:
        headline, tone, tech = "BEIDE SEITEN WERDEN DÜNNER", "warning", "BOTH_WEAK"
    elif ask_build and bid_build:
        headline, tone, tech = "GEMISCHTES ORDERBOOK", "mixed", "BOTH_BUILD"
    elif ask_build:
        headline, tone, tech = "VERKAUFSDRUCK NIMMT ZU", "negative", "ASK_BUILD"
    elif bid_build:
        headline, tone, tech = "KAUFDRUCK NIMMT ZU", "positive", "BID_BUILD"
    elif ask_weak or bid_weak:
        headline, tone, tech = "GEMISCHTES ORDERBOOK", "mixed", "PARTIAL_WEAK"
    else:
        headline, tone, tech = "NOCH KEINE KLARE RICHTUNG", "neutral", "STABLE"

    return {
        "headline": headline,
        "tone": tone,
        "tech": tech,
        "metrics": [
            {"label": "Ask Resistance", "value": ask_r or "N/A"},
            {"label": "Bid Support", "value": bid_r or "N/A"},
            {"label": "technischer Bias", "value": tech or "N/A"},
        ],
    }


def overall_headline(view: dict[str, Any], display: dict[str, Any]) -> dict[str, Any]:
    """Combine card signals into a short Gesamtbild with pro/contra bullets."""
    pro: list[str] = []
    contra: list[str] = []
    scores = {"support_weak": 0, "support_strong": 0, "res_weak": 0, "res_strong": 0, "buy": 0, "sell": 0}

    res = display.get("resistance") or {}
    sup = display.get("support") or {}
    money = display.get("money_flow") or {}
    liq = display.get("liquidations") or {}
    wall = display.get("wall_bias") or {}
    abs_d = display.get("absorption") or {}
    lvl = display.get("level_quality") or {}

    if res.get("headline") == "WIDERSTAND KOMMT NÄHER":
        scores["sell"] += 1
        pro.append("Widerstand kommt näher")
    if res.get("headline") == "WIDERSTAND ENTFERNT SICH":
        scores["res_weak"] += 1
        contra.append("Widerstand entfernt sich")
    if sup.get("headline") == "UNTERSTÜTZUNG KOMMT NÄHER":
        scores["support_strong"] += 1
        pro.append("Unterstützung kommt näher")
    if sup.get("headline") == "UNTERSTÜTZUNG FÄLLT ZURÜCK":
        scores["support_weak"] += 1
        pro.append("Bid-/Support-Zone fällt zurück")

    regime = money.get("regime")
    if regime == "NEW_SHORTS":
        scores["sell"] += 2
        pro.append("neue Shorts kommen in den Markt")
    elif regime == "NEW_LONGS":
        scores["buy"] += 2
        pro.append("neue Longs kommen in den Markt")
    elif regime == "LONG_UNWINDING":
        scores["sell"] += 1
        pro.append("Longs werden geschlossen")
    elif regime == "SHORT_COVERING":
        scores["buy"] += 1
        pro.append("Shorts werden geschlossen")

    if liq.get("headline") == "VIELE LONGS WERDEN HERAUSGEDRÜCKT":
        scores["sell"] += 2
        pro.append("viele Longs werden liquidiert")
    elif liq.get("headline") == "VIELE SHORTS WERDEN HERAUSGEDRÜCKT":
        scores["buy"] += 2
        pro.append("viele Shorts werden liquidiert")

    if wall.get("headline") == "VERKAUFSDRUCK NIMMT ZU":
        scores["sell"] += 1
        pro.append("Verkaufsdruck im Orderbuch nimmt zu")
    elif wall.get("headline") == "KAUFDRUCK NIMMT ZU":
        scores["buy"] += 1
        pro.append("Kaufdruck im Orderbuch nimmt zu")

    if abs_d.get("headline") == "KÄUFER HALTEN DEN SUPPORT":
        scores["support_strong"] += 2
        contra.append("Käufer absorbieren Verkäufe")
    elif abs_d.get("headline") == "VERKÄUFER HALTEN DIE RESISTANCE":
        scores["res_strong"] += 2
        contra.append("Verkäufer halten die Resistance")

    if lvl.get("headline") == "BREAK-RISIKO STEIGT":
        scores["sell"] += 1
        pro.append("Break-Risiko steigt")

    # Deduplicate while preserving order
    def _uniq(items: list[str], limit: int) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in items:
            if it in seen:
                continue
            seen.add(it)
            out.append(it)
            if len(out) >= limit:
                break
        return out

    # Split pro/contra more carefully: "pro" = supports main thesis
    sell = scores["sell"] + scores["support_weak"]
    buy = scores["buy"] + scores["support_strong"] + scores["res_weak"]
    res_hold = scores["res_strong"]

    mixed = abs(sell - buy) <= 1 and (sell + buy) >= 2
    if mixed or (sell == 0 and buy == 0 and res_hold == 0):
        if sell == 0 and buy == 0 and res_hold == 0:
            headline = "GEMISCHTES BILD — ABWARTEN"
            decision = "ABWARTEN"
            tone = "mixed"
            reasons_for = _uniq(pro, 3) or ["noch zu wenig klare Signale"]
            reasons_against = _uniq(contra, 2)
        else:
            headline = "GEMISCHTES BILD — ABWARTEN"
            decision = "ABWARTEN"
            tone = "mixed"
            reasons_for = _uniq(pro, 3)
            reasons_against = _uniq(contra, 2) or _uniq(
                [x for x in pro if x not in reasons_for], 2
            )
    elif sell > buy and sell >= res_hold:
        if scores["support_weak"] >= 1 or regime in {"NEW_SHORTS", "LONG_UNWINDING"}:
            headline = "SUPPORT WIRD SCHWÄCHER"
        elif wall.get("headline") == "VERKAUFSDRUCK NIMMT ZU":
            headline = "VERKAUFSDRUCK NIMMT ZU"
        elif lvl.get("headline") == "BREAK-RISIKO STEIGT":
            headline = "BREAK-RISIKO STEIGT"
        else:
            headline = "VERKAUFSDRUCK NIMMT ZU"
        decision = "ABWARTEN"
        tone = "negative"
        reasons_for = _uniq(
            [x for x in pro if any(k in x.lower() for k in ("short", "longs werden", "verkauf", "fällt", "widerstand kommt", "break", "liquidiert"))],
            3,
        ) or _uniq(pro, 3)
        reasons_against = _uniq(contra, 2)
    elif buy > sell:
        if abs_d.get("headline") == "KÄUFER HALTEN DEN SUPPORT" or scores["support_strong"] >= 2:
            headline = "SUPPORT WIRD GESTÜTZT"
        elif wall.get("headline") == "KAUFDRUCK NIMMT ZU" or regime == "NEW_LONGS":
            headline = "KAUFDRUCK NIMMT ZU"
        elif res.get("headline") == "WIDERSTAND ENTFERNT SICH":
            headline = "RESISTANCE WIRD SCHWÄCHER"
        else:
            headline = "KAUFDRUCK NIMMT ZU"
        decision = "ABWARTEN"
        tone = "positive"
        reasons_for = _uniq(pro, 3)
        reasons_against = _uniq(contra, 2)
    elif res_hold > 0:
        headline = "RESISTANCE WIRD VERTEIDIGT"
        decision = "ABWARTEN"
        tone = "negative"
        reasons_for = _uniq(contra + pro, 3)
        reasons_against = []
    else:
        headline = "GEMISCHTES BILD — ABWARTEN"
        decision = "ABWARTEN"
        tone = "mixed"
        reasons_for = _uniq(pro, 3)
        reasons_against = _uniq(contra, 2)

    # If both sides have strong opposing bullets, force mixed
    if reasons_for and reasons_against and abs(sell - buy) <= 1:
        headline = "GEMISCHTES BILD — ABWARTEN"
        decision = "ABWARTEN"
        tone = "mixed"

    return {
        "headline": headline,
        "tone": tone,
        "decision": decision,
        "reasons_for": reasons_for[:3],
        "reasons_against": reasons_against[:2],
        "tech": view.get("setup") or view.get("decision"),
        "metrics": [
            {"label": "Entscheidung", "value": decision},
        ],
    }


def enrich_view_display(view: dict[str, Any]) -> dict[str, Any]:
    """Attach a `display` block with German headlines for each card."""
    money = view.get("money_flow")
    if isinstance(money, dict) and money.get("status") != "collecting":
        money = {**money, "regime": classify_money_flow_regime(money)}
        view = {**view, "money_flow": money}

    window = _window_label(view.get("report_window_seconds") or 60)
    sample_iv = view.get("sample_interval_seconds") or 5
    try:
        sample_label = f"{int(sample_iv)}s"
    except (TypeError, ValueError):
        sample_label = "5s"

    age = _f(view.get("data_age_seconds"))
    if age is None:
        age_label = "—"
    elif age < 60:
        age_label = f"{age:.0f}s"
    else:
        age_label = f"{age / 60.0:.1f}m"

    display: dict[str, Any] = {
        "window_label": window,
        "sample_label": sample_label,
        "data_age_label": age_label,
        "level_source": view.get("level_source") or "sample",
        "resistance": resistance_headline(view.get("resistance")),
        "support": support_headline(view.get("support")),
        "support2": support_headline(view.get("support2")),
        "absorption": absorption_headline(view.get("absorption")),
        "near_price": near_price_headline(view.get("near_price")),
        "level_quality": level_quality_headline(view.get("level_quality")),
        "liquidations": liquidations_headline(view.get("liquidations")),
        "money_flow": money_flow_headline(view.get("money_flow")),
        "wall_bias": wall_bias_headline(view.get("wall_follow")),
    }
    display["overall"] = overall_headline(view, display)
    # section titles with window where relevant
    display["liquidations"]["section_title"] = f"Liquidationen — letzte {window}"
    display["money_flow"]["section_title"] = f"Money Flow — letzte {window}"
    display["wall_bias"]["section_title"] = f"Wall Bias — letzte {window}"
    display["near_price"]["section_title"] = f"Orderbook nahe am Preis — {sample_label}"
    display["level_quality"]["section_title"] = f"Level-Qualität — {sample_label}"
    display["overall"]["section_title"] = "Gesamtbild"
    view = {**view, "display": display}
    return view


def _level_key(level: dict[str, Any]) -> tuple[Any, ...]:
    rank = level.get("rank_by_distance")
    price = level.get("price")
    try:
        price_k = round(float(price), 8) if price is not None else None
    except (TypeError, ValueError):
        price_k = price
    return (rank, price_k)


def _merge_compact_with_strongest(
    *,
    compact: list[dict[str, Any]],
    all_levels: list[dict[str, Any]],
    compact_n: int,
) -> list[dict[str, Any]]:
    """Compact nearest levels plus STRONGEST_RELEVANT if it sits outside compact."""
    base = list(compact) if compact else list(all_levels[:compact_n])
    strongest = next(
        (
            L
            for L in all_levels
            if isinstance(L, dict) and "STRONGEST_RELEVANT" in (L.get("policies") or [])
        ),
        None,
    )
    if strongest is None:
        return base
    keys = {_level_key(L) for L in base if isinstance(L, dict)}
    if _level_key(strongest) in keys:
        return base
    # Keep distance order (asks ascending, bids ascending by abs rank).
    out = list(base) + [strongest]
    try:
        out.sort(key=lambda L: abs(float(L.get("distance_bps") or 0.0)))
    except (TypeError, ValueError):
        pass
    return out


def _near_mid_from_walls(
    walls: list[Any] | None,
    *,
    side: str,
    mid: float | None,
    max_distance_bps: float,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Map strongest_*_walls inside the grid min-band into display rows (UI-only)."""
    if not walls or mid is None or mid <= 0:
        return []
    best_by_price: dict[float, dict[str, Any]] = {}
    for raw in walls:
        if not isinstance(raw, dict):
            continue
        try:
            price = float(raw.get("wall_price"))
            dist = float(raw.get("distance_to_mid_bps"))
            notional = float(raw.get("wall_notional") or 0.0)
            multiple = float(raw.get("wall_multiple") or 0.0)
        except (TypeError, ValueError):
            continue
        if dist < 0 or dist >= max_distance_bps:
            continue
        # Side sanity vs mid
        if side == "bid" and price >= mid:
            continue
        if side == "ask" and price <= mid:
            continue
        price_k = round(price, 8)
        prev = best_by_price.get(price_k)
        if prev is None or notional > float(prev.get("notional") or 0.0):
            signed = -abs(dist) if side == "bid" else abs(dist)
            wall_class = "STRONG_WALL" if multiple >= 1.5 else "WEAK_CANDIDATE"
            best_by_price[price_k] = {
                "rank_by_distance": "N",
                "price": price,
                "distance_bps": signed,
                "distance_bps_abs": abs(dist),
                "distance_pct": signed / 100.0,
                "notional": notional,
                "multiple": multiple,
                "percentile": raw.get("percentile"),
                "wall_class": wall_class,
                "status": "ACTIVE",
                "policies": ["NEAR_MID"],
                "resolution": raw.get("resolution"),
                "source": "strongest_walls",
            }
    rows = sorted(best_by_price.values(), key=lambda r: float(r["distance_bps_abs"]))
    # Assign N1, N2… after sort
    for i, row in enumerate(rows[:limit], start=1):
        row["rank_by_distance"] = i
        row["near_mid_id"] = f"N{i}"
    return rows[:limit]


def enrich_ob_grid_for_display(
    grid: dict[str, Any] | None,
    sample: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """UI-only: attach compact+strongest display lists and near-mid walls.

    Does not change scanner / grid extraction rules — only annotates the payload
    for the dashboard renderer.
    """
    if not isinstance(grid, dict):
        return None
    out = dict(grid)
    bid_all = list(out.get("bid_levels") or [])
    ask_all = list(out.get("ask_levels") or [])
    bid_compact = list(out.get("compact_bid_levels") or [])
    ask_compact = list(out.get("compact_ask_levels") or [])
    params = out.get("params") if isinstance(out.get("params"), dict) else {}
    band = out.get("search_band_bps") if isinstance(out.get("search_band_bps"), dict) else {}
    try:
        min_bps = float(band.get("min") if band.get("min") is not None else params.get("min_distance_bps") or 100.0)
    except (TypeError, ValueError):
        min_bps = 100.0
    try:
        compact_bid_n = int(params.get("compact_bid_n") or 4)
    except (TypeError, ValueError):
        compact_bid_n = 4
    try:
        compact_ask_n = int(params.get("compact_ask_n") or 3)
    except (TypeError, ValueError):
        compact_ask_n = 3

    out["display_bid_levels"] = _merge_compact_with_strongest(
        compact=bid_compact, all_levels=bid_all, compact_n=compact_bid_n
    )
    out["display_ask_levels"] = _merge_compact_with_strongest(
        compact=ask_compact, all_levels=ask_all, compact_n=compact_ask_n
    )

    mid = _f(out.get("mid_price"))
    if mid is None and isinstance(sample, dict):
        mid = _f(sample.get("mid_price"))
    walls_bid = (sample or {}).get("strongest_bid_walls") if isinstance(sample, dict) else None
    walls_ask = (sample or {}).get("strongest_ask_walls") if isinstance(sample, dict) else None
    out["near_mid_bid_walls"] = _near_mid_from_walls(
        walls_bid, side="bid", mid=mid, max_distance_bps=min_bps
    )
    out["near_mid_ask_walls"] = _near_mid_from_walls(
        walls_ask, side="ask", mid=mid, max_distance_bps=min_bps
    )
    return out
