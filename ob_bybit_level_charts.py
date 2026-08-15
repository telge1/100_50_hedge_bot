#!/usr/bin/env python3

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


HOST = "127.0.0.1"
PORT = 8765


def build_test_levels() -> dict[str, Any]:
    """
    Später ersetzen wir diese Testwerte durch echte Level
    aus unserer Orderbook-Level-Engine.
    """

    return {
        "symbol": "1000BONKUSDT",
        "generated_at": time.time(),
        "levels": [
            {
                "id": "ask_strong_1",
                "price": "0.009617",
                "side": "ask",
                "strength": "strong",
                "label": "STRONG ASK",
            },
            {
                "id": "ask_weak_1",
                "price": "0.009474",
                "side": "ask",
                "strength": "weak",
                "label": "WEAK ASK",
            },
            {
                "id": "bid_strong_1",
                "price": "0.009183",
                "side": "bid",
                "strength": "strong",
                "label": "STRONG BID",
            },
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))

        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Access-Control-Request-Private-Network",
        )
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")

        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Access-Control-Request-Private-Network",
        )
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path in {"/", "/health"}:
            self.send_json(
                {
                    "status": "ok",
                    "service": "ob-level-test-server",
                }
            )
            return

        if self.path.startswith("/levels"):
            self.send_json(build_test_levels())
            return

        self.send_json(
            {
                "status": "error",
                "message": "Not found",
            },
            status=404,
        )

    def log_message(self, format: str, *args: object) -> None:
        print(
            f"[{self.log_date_time_string()}] "
            f"{self.client_address[0]} "
            f"{format % args}"
        )


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    print("OB-Level-Testserver läuft")
    print(f"Health: http://{HOST}:{PORT}/health")
    print(f"Levels: http://{HOST}:{PORT}/levels")
    print("Beenden mit Strg+C")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer wird beendet.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()




'''

(() => {
  "use strict";

  const CONFIG = {
    rootId: "ob-live-api-overlay",
    apiUrl: "http://127.0.0.1:8765/levels",
    refreshMs: 2000,
    requestTimeoutMs: 10000,
  };

  // Alte Version entfernen
  try {
    window.__obLiveApiCleanup?.();
  } catch (_) {}

  document.getElementById(CONFIG.rootId)?.remove();

  function findChartElement() {
    const candidates = [
      ...document.querySelectorAll("canvas"),
      ...document.querySelectorAll("iframe"),
      ...document.querySelectorAll('[class*="chart"]'),
    ]
      .map((element) => {
        const rect = element.getBoundingClientRect();

        return {
          element,
          rect,
          area: rect.width * rect.height,
        };
      })
      .filter(({ rect }) => {
        return (
          rect.width >= 300 &&
          rect.height >= 150 &&
          rect.bottom > 0 &&
          rect.right > 0 &&
          rect.top < window.innerHeight &&
          rect.left < window.innerWidth
        );
      })
      .sort((a, b) => b.area - a.area);

    if (!candidates.length) {
      return null;
    }

    let current = candidates[0].element;
    let best = current;

    for (let depth = 0; depth < 6; depth += 1) {
      if (!current.parentElement) {
        break;
      }

      current = current.parentElement;
      const rect = current.getBoundingClientRect();

      if (
        rect.width >= 400 &&
        rect.height >= 200 &&
        rect.width <= window.innerWidth * 1.1 &&
        rect.height <= window.innerHeight
      ) {
        best = current;
      }
    }

    return best;
  }

  const chart = findChartElement();

  if (!chart) {
    alert("Der Bybit-Chart wurde nicht gefunden.");
    return;
  }

  function parsePrice(value) {
    const text = String(value)
      .trim()
      .replace(/\s+/g, "")
      .replace(",", ".");

    if (!/^\d+(?:\.\d+)?$/.test(text)) {
      return null;
    }

    const number = Number(text);

    if (!Number.isFinite(number) || number <= 0) {
      return null;
    }

    return { text, number };
  }

  function createButton(text) {
    const button = document.createElement("button");

    button.type = "button";
    button.textContent = text;

    Object.assign(button.style, {
      boxSizing: "border-box",
      width: "100%",
      minHeight: "38px",
      marginBottom: "7px",
      border: "1px solid #596273",
      borderRadius: "5px",
      background: "#262c36",
      color: "#ffffff",
      cursor: "pointer",
      fontSize: "13px",
    });

    return button;
  }

  function levelStyle(level) {
    const side = String(level.side || "").toLowerCase();
    const strength = String(level.strength || "").toLowerCase();

    if (side === "ask" && strength === "strong") {
      return {
        color: "#ff3030",
        width: 3,
        name: "STRONG ASK",
      };
    }

    if (side === "ask" && strength === "medium") {
      return {
        color: "#ff9f1a",
        width: 2,
        name: "MEDIUM ASK",
      };
    }

    if (side === "ask" && strength === "weak") {
      return {
        color: "#1ea7fd",
        width: 2,
        name: "WEAK ASK",
      };
    }

    if (side === "bid" && strength === "strong") {
      return {
        color: "#00c878",
        width: 3,
        name: "STRONG BID",
      };
    }

    if (side === "bid" && strength === "medium") {
      return {
        color: "#5dd39e",
        width: 2,
        name: "MEDIUM BID",
      };
    }

    if (side === "bid" && strength === "weak") {
      return {
        color: "#69a7ff",
        width: 2,
        name: "WEAK BID",
      };
    }

    return {
      color: "#b56cff",
      width: 2,
      name: "LEVEL",
    };
  }

  const root = document.createElement("div");
  root.id = CONFIG.rootId;

  Object.assign(root.style, {
    position: "fixed",
    inset: "0",
    zIndex: "2147483646",
    pointerEvents: "none",
    fontFamily: "Arial, sans-serif",
  });

  const overlay = document.createElement("div");

  Object.assign(overlay.style, {
    position: "fixed",
    overflow: "visible",
    pointerEvents: "none",
  });

  const panel = document.createElement("div");

  Object.assign(panel.style, {
    position: "fixed",
    top: "70px",
    left: "15px",
    width: "360px",
    maxHeight: "82vh",
    overflowY: "auto",
    boxSizing: "border-box",
    padding: "12px",
    border: "1px solid #596273",
    borderRadius: "8px",
    background: "rgba(15,18,24,0.97)",
    color: "#ffffff",
    boxShadow: "0 8px 30px rgba(0,0,0,0.45)",
    pointerEvents: "auto",
    fontSize: "13px",
  });

  panel.innerHTML = `
    <div style="font-size:16px;font-weight:700;margin-bottom:7px">
      OB Live-Level
    </div>

    <div style="color:#bac2cf;line-height:1.4;margin-bottom:10px">
      Aktuelle Walls werden automatisch von der API geladen.
    </div>
  `;

  const calibrateTopButton = createButton(
    "1. Oberen Preis kalibrieren"
  );

  const calibrateBottomButton = createButton(
    "2. Unteren Preis kalibrieren"
  );

  const recalibrateButton = createButton(
    "Nach Zoom neu kalibrieren"
  );

  recalibrateButton.style.background = "#1677ff";

  const refreshButton = createButton(
    "Level jetzt neu laden"
  );

  const pauseButton = createButton(
    "Automatische Aktualisierung pausieren"
  );

  const removeButton = createButton(
    "Overlay vollständig entfernen"
  );

  const levelList = document.createElement("div");

  Object.assign(levelList.style, {
    marginTop: "8px",
    padding: "8px",
    borderRadius: "5px",
    background: "rgba(255,255,255,0.05)",
    color: "#d8dee9",
    lineHeight: "1.5",
  });

  levelList.textContent = "Noch keine Level geladen.";

  const status = document.createElement("div");

  Object.assign(status.style, {
    marginTop: "8px",
    padding: "8px",
    borderRadius: "5px",
    background: "rgba(255,255,255,0.08)",
    color: "#d8dee9",
    lineHeight: "1.45",
    whiteSpace: "pre-wrap",
  });

  status.textContent = "Status: Noch nicht kalibriert.";

  panel.append(
    calibrateTopButton,
    calibrateBottomButton,
    recalibrateButton,
    refreshButton,
    pauseButton,
    removeButton,
    levelList,
    status
  );

  root.append(overlay, panel);
  document.body.appendChild(root);

  const state = {
    calibrationMode: null,
    recalibrationStep: null,

    topPoint: null,
    bottomPoint: null,

    levels: [],
    renderedLines: [],

    timer: null,
    requestRunning: false,
    paused: false,
    destroyed: false,

    symbol: null,
    generatedAt: null,
    lastUpdate: null,
  };

  function setStatus(message) {
    status.textContent = `Status: ${message}`;
  }

  function updateOverlayBounds() {
    if (state.destroyed) {
      return;
    }

    const rect = chart.getBoundingClientRect();

    Object.assign(overlay.style, {
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
    });

    redrawLevels();
  }

  function priceToY(price) {
    const top = state.topPoint;
    const bottom = state.bottomPoint;

    if (!top || !bottom) {
      throw new Error("Kalibrierung fehlt.");
    }

    if (top.price === bottom.price) {
      throw new Error(
        "Die Kalibrierungspreise dürfen nicht identisch sein."
      );
    }

    return (
      top.y +
      ((price - top.price) * (bottom.y - top.y)) /
        (bottom.price - top.price)
    );
  }

  function clearRenderedLines() {
    for (const item of state.renderedLines) {
      item.line.remove();
      item.label.remove();
    }

    state.renderedLines = [];
  }

  function createLevelLine(level) {
    const parsedPrice = parsePrice(level.price);

    if (!parsedPrice) {
      return {
        success: false,
        reason: "invalid_price",
      };
    }

    const chartRect = chart.getBoundingClientRect();
    const y = priceToY(parsedPrice.number);

    if (y < 0 || y > chartRect.height) {
      return {
        success: false,
        reason: "outside_view",
      };
    }

    const style = levelStyle(level);

    const line = document.createElement("div");

    Object.assign(line.style, {
      position: "absolute",
      left: "0",
      right: "0",
      top: `${y}px`,
      height: `${style.width}px`,
      background: style.color,
      boxShadow: `0 0 5px ${style.color}`,
      pointerEvents: "none",
      opacity: "0.92",
    });

    const label = document.createElement("div");

    Object.assign(label.style, {
      position: "absolute",
      right: "4px",
      top: `${y}px`,
      transform: "translateY(-50%)",
      padding: "3px 7px",
      borderRadius: "4px",
      background: style.color,
      color: "#ffffff",
      fontSize: "12px",
      fontWeight: "700",
      whiteSpace: "nowrap",
      pointerEvents: "none",
    });

    const suppliedLabel = String(level.label || "").trim();
    const labelText = suppliedLabel || style.name;

    label.textContent = `${labelText} · ${parsedPrice.text}`;

    overlay.append(line, label);

    state.renderedLines.push({
      id: level.id,
      line,
      label,
    });

    return {
      success: true,
      y,
    };
  }

  function redrawLevels() {
    // Alte Walls immer vollständig entfernen
    clearRenderedLines();

    if (
      !state.topPoint ||
      !state.bottomPoint ||
      !state.levels.length
    ) {
      return;
    }

    let visible = 0;
    let outside = 0;
    let invalid = 0;

    for (const level of state.levels) {
      const result = createLevelLine(level);

      if (result.success) {
        visible += 1;
      } else if (result.reason === "outside_view") {
        outside += 1;
      } else {
        invalid += 1;
      }
    }

    const updateText = state.lastUpdate
      ? new Date(state.lastUpdate).toLocaleTimeString()
      : "–";

    setStatus(
      `Symbol: ${state.symbol || "unbekannt"}\n` +
      `Geladen: ${state.levels.length}\n` +
      `Im Chart sichtbar: ${visible}\n` +
      `Außerhalb des Charts: ${outside}\n` +
      `Ungültig: ${invalid}\n` +
      `Letztes Update: ${updateText}`
    );
  }

  function updateLevelList() {
    if (!state.levels.length) {
      levelList.textContent = "Keine aktiven Walls vorhanden.";
      return;
    }

    levelList.innerHTML = "";

    for (const level of state.levels) {
      const row = document.createElement("div");
      const style = levelStyle(level);
      const price = parsePrice(level.price);

      Object.assign(row.style, {
        display: "flex",
        alignItems: "center",
        gap: "7px",
        marginBottom: "5px",
        minWidth: "0",
      });

      const colorBox = document.createElement("span");

      Object.assign(colorBox.style, {
        display: "inline-block",
        flex: "0 0 auto",
        width: "11px",
        height: "11px",
        borderRadius: "2px",
        background: style.color,
      });

      const text = document.createElement("span");

      Object.assign(text.style, {
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      });

      text.textContent =
        `${level.label || style.name}: ` +
        `${price?.text || level.price}`;

      row.append(colorBox, text);
      levelList.appendChild(row);
    }
  }

  async function fetchWithTimeout(url, timeoutMs) {
    const controller = new AbortController();

    const timeoutId = window.setTimeout(() => {
      controller.abort();
    }, timeoutMs);

    try {
      return await fetch(url, {
        method: "GET",
        cache: "no-store",
        signal: controller.signal,
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  function normalizeLevels(payload) {
    if (!payload || !Array.isArray(payload.levels)) {
      throw new Error(
        "API-Antwort enthält kein gültiges levels-Array."
      );
    }

    return payload.levels
      .map((level, index) => {
        const price = parsePrice(level?.price);

        if (!price) {
          return null;
        }

        return {
          id:
            String(level.id || "").trim() ||
            `level_${index}_${price.text}`,

          price: price.text,

          side: String(level.side || "unknown").toLowerCase(),

          strength: String(
            level.strength || "unknown"
          ).toLowerCase(),

          label: String(level.label || "").trim(),
        };
      })
      .filter(Boolean);
  }

  async function loadLevels() {
    if (
      state.destroyed ||
      state.paused ||
      state.requestRunning
    ) {
      return;
    }

    state.requestRunning = true;

    try {
      const response = await fetchWithTimeout(
        `${CONFIG.apiUrl}?t=${Date.now()}`,
        CONFIG.requestTimeoutMs
      );

      if (!response.ok) {
        throw new Error(
          `HTTP ${response.status} ${response.statusText}`
        );
      }

      const payload = await response.json();
      const normalizedLevels = normalizeLevels(payload);

      // Aktuelle API-Liste ersetzt die alte vollständig
      state.levels = normalizedLevels;
      state.symbol = payload.symbol || null;
      state.generatedAt = payload.generated_at || null;
      state.lastUpdate = Date.now();

      updateLevelList();
      redrawLevels();
    } catch (error) {
      console.error(
        "[OB LIVE API] Laden fehlgeschlagen:",
        error
      );

      setStatus(
        `API konnte nicht geladen werden.\n` +
        `${error?.message || error}\n\n` +
        `Prüfe API oder SSH-Tunnel:\n` +
        CONFIG.apiUrl
      );
    } finally {
      state.requestRunning = false;
    }
  }

  function beginCalibration(mode) {
    state.calibrationMode = mode;

    overlay.style.pointerEvents = "auto";
    overlay.style.cursor = "crosshair";

    setStatus(
      mode === "top"
        ? "Klicke auf einen oberen sichtbaren Skalenpreis."
        : "Klicke auf einen unteren sichtbaren Skalenpreis."
    );
  }

  function endCalibration() {
    state.calibrationMode = null;

    overlay.style.pointerEvents = "none";
    overlay.style.cursor = "default";
  }

  calibrateTopButton.addEventListener("click", () => {
    beginCalibration("top");
  });

  calibrateBottomButton.addEventListener("click", () => {
    beginCalibration("bottom");
  });

  recalibrateButton.addEventListener("click", () => {
    state.recalibrationStep = "top";
    beginCalibration("top");

    setStatus(
      "Neukalibrierung gestartet.\n" +
      "Klicke zuerst auf einen oberen sichtbaren Preis."
    );
  });

  function handleCalibrationClick(event) {
    if (!state.calibrationMode) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    const chartRect = chart.getBoundingClientRect();
    const y = event.clientY - chartRect.top;

    const entered = window.prompt(
      state.calibrationMode === "top"
        ? "Welcher obere Preis liegt auf dieser Höhe?"
        : "Welcher untere Preis liegt auf dieser Höhe?"
    );

    if (entered === null) {
      state.recalibrationStep = null;
      endCalibration();
      setStatus("Kalibrierung abgebrochen.");
      return;
    }

    const parsed = parsePrice(entered);

    if (!parsed) {
      state.recalibrationStep = null;
      endCalibration();
      setStatus("Ungültiger Preis.");
      return;
    }

    const point = {
      y,
      price: parsed.number,
      text: parsed.text,
    };

    if (state.calibrationMode === "top") {
      state.topPoint = point;
    } else {
      state.bottomPoint = point;
    }

    if (state.recalibrationStep === "top") {
      state.recalibrationStep = "bottom";
      state.calibrationMode = "bottom";

      setStatus(
        `Oberer Punkt gespeichert: ${parsed.text}\n` +
        "Klicke jetzt auf einen unteren sichtbaren Preis."
      );

      return;
    }

    if (state.recalibrationStep === "bottom") {
      state.recalibrationStep = null;
      endCalibration();

      redrawLevels();

      setStatus(
        `Neukalibrierung abgeschlossen.\n` +
        `Oben: ${state.topPoint.text}\n` +
        `Unten: ${state.bottomPoint.text}\n\n` +
        "Aktuelle Walls wurden automatisch neu positioniert."
      );

      return;
    }

    endCalibration();

    if (state.topPoint && state.bottomPoint) {
      redrawLevels();

      setStatus(
        `Kalibrierung vollständig.\n` +
        `Oben: ${state.topPoint.text}\n` +
        `Unten: ${state.bottomPoint.text}`
      );
    } else {
      setStatus(
        "Erster Kalibrierungspunkt gespeichert. " +
        "Jetzt den zweiten Punkt kalibrieren."
      );
    }
  }

  overlay.addEventListener(
    "click",
    handleCalibrationClick,
    true
  );

  refreshButton.addEventListener("click", async () => {
    const wasPaused = state.paused;
    state.paused = false;

    try {
      await loadLevels();
    } finally {
      state.paused = wasPaused;
    }
  });

  pauseButton.addEventListener("click", () => {
    state.paused = !state.paused;

    pauseButton.textContent = state.paused
      ? "Automatische Aktualisierung fortsetzen"
      : "Automatische Aktualisierung pausieren";

    setStatus(
      state.paused
        ? "Automatische Aktualisierung pausiert."
        : "Automatische Aktualisierung läuft wieder."
    );
  });

  function cleanup() {
    state.destroyed = true;

    if (state.timer !== null) {
      window.clearInterval(state.timer);
      state.timer = null;
    }

    clearRenderedLines();

    window.removeEventListener(
      "resize",
      updateOverlayBounds
    );

    window.removeEventListener(
      "scroll",
      updateOverlayBounds,
      true
    );

    overlay.removeEventListener(
      "click",
      handleCalibrationClick,
      true
    );

    root.remove();

    delete window.__obLiveApiCleanup;
  }

  removeButton.addEventListener("click", cleanup);

  window.addEventListener(
    "resize",
    updateOverlayBounds
  );

  window.addEventListener(
    "scroll",
    updateOverlayBounds,
    true
  );

  window.__obLiveApiCleanup = cleanup;

  updateOverlayBounds();

  state.timer = window.setInterval(() => {
    void loadLevels();
  }, CONFIG.refreshMs);

  void loadLevels();

  setStatus(
    "API-Verbindung wird aufgebaut.\n" +
    "Danach oben und unten kalibrieren."
  );

  console.log("[OB LIVE API] Overlay gestartet.", {
    chart,
    apiUrl: CONFIG.apiUrl,
    refreshMs: CONFIG.refreshMs,
  });
})();

'''