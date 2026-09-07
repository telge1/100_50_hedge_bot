"""Regression: Footprint candle body transparency contract (Node/vm)."""

from __future__ import annotations

import subprocess
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
FP_JS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.js"
MP_JS = DASHBOARD_DIR / "static" / "market_profile_v1" / "app.js"


def _run_node(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
    )


def _harness(body: str) -> str:
    """Load FootprintCandles with a mock candle series; run assertions in body."""
    fp_path = FP_JS.as_posix()
    return (
        """
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(%r, 'utf8');

const ORIG = {
  upColor: '#26a69a',
  downColor: '#ef5350',
  borderUpColor: '#26a69a',
  borderDownColor: '#ef5350',
  wickUpColor: '#26a69a',
  wickDownColor: '#ef5350',
  borderColor: '#378658',
  wickColor: '#737375',
  visible: true,
  priceLineVisible: true,
  lastValueVisible: true
};

function makeSeries(initial) {
  let opts = Object.assign({}, initial || ORIG);
  const applied = [];
  return {
    _applied: applied,
    options() { return Object.assign({}, opts); },
    applyOptions(patch) {
      applied.push(Object.assign({}, patch));
      opts = Object.assign({}, opts, patch);
    },
    priceToCoordinate() { return 10; },
    getOpts() { return opts; }
  };
}

const series = makeSeries();
const fakeEl = { hidden: true, getContext() { return null; }, width: 0, height: 0, style: {} };
const sandbox = {
  window: { addEventListener() {} },
  document: {
    addEventListener() {},
    hidden: false,
    getElementById(id) {
      if (id === 'fpOverlay' || id === 'fpStatus') return fakeEl;
      return null;
    }
  },
  console,
  AbortController: class {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; }
  },
  URLSearchParams,
  setInterval() { return 1; },
  clearInterval() {},
  setTimeout(cb) { if (typeof cb === 'function') cb(); return 1; },
  clearTimeout() {},
  Date,
  requestAnimationFrame(cb) { cb(); },
  fetch() {
    return Promise.resolve({
      ok: false,
      json() { return Promise.resolve({ success: false, message: 'test-skip' }); }
    });
  }
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const fp = sandbox.window.FootprintCandles;
const st = fp._state;
const T = fp._TRANSPARENT;

fp.setContext({
  candleSeries: series,
  symbol: 'BTCUSDT',
  timeframe: '5m',
  chart: {
    timeScale() {
      return {
        options() { return { barSpacing: 70 }; },
        getVisibleRange() { return { from: 1000, to: 2000 }; },
        timeToCoordinate() { return 50; }
      };
    }
  }
});

function assert(cond, msg) {
  if (!cond) { console.error('FAIL:', msg); process.exit(1); }
}

function levelsPayload(coverage) {
  return {
    success: true,
    coverage: coverage || 'COMPLETE',
    candles: [{
      time: 1000,
      high: 101,
      low: 99,
      coverage: coverage || 'COMPLETE',
      candle_delta_size: 1,
      candle_delta_notional: 10,
      levels: [{
        price_high: 100.5,
        price_low: 100.0,
        bid_notional: 100,
        ask_notional: 120,
        delta_size: 1,
        is_vpoc: true
      }]
    }]
  };
}

"""
        % fp_path
        + body
        + "\nconsole.log('ok');\n"
    )


def test_off_keeps_original_colors():
    proc = _run_node(
        _harness(
            """
assert(!fp.isEnabled(), 'default off');
fp._syncCandleBodiesForMode('off', st.gen);
assert(series.getOpts().upColor === ORIG.upColor, 'up original');
assert(series.getOpts().downColor === ORIG.downColor, 'down original');
assert(series.getOpts().wickUpColor === ORIG.wickUpColor, 'wick original');
assert(series.getOpts().priceLineVisible === true, 'price line');
assert(series.getOpts().lastValueVisible === true, 'last label');
assert(series.getOpts().visible === true, 'series visible');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_full_makes_bodies_transparent():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
st.payload = levelsPayload('COMPLETE');
const mode = fp._resolveVisibilityMode({ barSpacing: 70, levelHeight: 12 });
assert(mode === 'full', 'expect full got ' + mode);
fp._syncCandleBodiesForMode('full', st.gen);
assert(series.getOpts().upColor === T, 'up transparent');
assert(series.getOpts().downColor === T, 'down transparent');
assert(series.getOpts().borderUpColor === T, 'border up transparent');
assert(series.getOpts().borderDownColor === T, 'border down transparent');
assert(series.getOpts().wickUpColor === T, 'wick up transparent');
assert(series.getOpts().wickDownColor === T, 'wick down transparent');
assert(series.getOpts().visible === true, 'series still visible');
assert(series.getOpts().priceLineVisible === true, 'price line kept');
assert(series.getOpts().lastValueVisible === true, 'last label kept');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_delta_makes_bodies_transparent():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
st.payload = levelsPayload('COMPLETE');
const mode = fp._resolveVisibilityMode({ barSpacing: 45, levelHeight: 5 });
assert(mode === 'delta', 'expect delta got ' + mode);
fp._syncCandleBodiesForMode('delta', st.gen);
assert(series.getOpts().upColor === T, 'up transparent');
assert(series.getOpts().wickDownColor === T, 'wick transparent');
assert(series.getOpts().visible === true, 'series visible');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_zoom_fallback_restores_original():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
st.payload = levelsPayload('COMPLETE');
fp._syncCandleBodiesForMode('full', st.gen);
assert(series.getOpts().upColor === T, 'was transparent');
const mode = fp._resolveVisibilityMode({ barSpacing: 20, levelHeight: 12 });
assert(mode === 'fallback', 'expect fallback got ' + mode);
fp._syncCandleBodiesForMode('fallback', st.gen);
assert(series.getOpts().upColor === ORIG.upColor, 'restored up');
assert(series.getOpts().wickUpColor === ORIG.wickUpColor, 'restored wick');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_missing_restores_original():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
st.payload = levelsPayload('COMPLETE');
fp._syncCandleBodiesForMode('full', st.gen);
st.payload = { success: true, coverage: 'MISSING', candles: [{ time: 1, levels: [] }] };
assert(fp._resolveVisibilityMode({}) === 'missing', 'missing mode');
fp._syncCandleBodiesForMode('missing', st.gen);
assert(series.getOpts().upColor === ORIG.upColor, 'restored');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_api_error_path_restores_via_restore_hook():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
fp._syncCandleBodiesForMode('full', st.gen);
assert(series.getOpts().upColor === T, 'transparent first');
st.payload = null;
fp._restoreCandleBodies();
assert(series.getOpts().upColor === ORIG.upColor, 'api error restore');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_unsupported_symbol_restores():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
fp._syncCandleBodiesForMode('full', st.gen);
st.symbol = 'ETHUSDT';
assert(fp._resolveVisibilityMode({}) === 'unsupported', 'unsupported symbol');
fp._syncCandleBodiesForMode('unsupported', st.gen);
assert(series.getOpts().upColor === ORIG.upColor, 'restored');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_unsupported_timeframe_restores():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
fp._syncCandleBodiesForMode('delta', st.gen);
st.timeframe = '15m';
assert(fp._resolveVisibilityMode({}) === 'unsupported', 'unsupported tf');
fp._syncCandleBodiesForMode('unsupported', st.gen);
assert(series.getOpts().downColor === ORIG.downColor, 'restored');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_partial_with_levels_hides_candles():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
st.payload = levelsPayload('PARTIAL');
const mode = fp._resolveVisibilityMode({ barSpacing: 70, levelHeight: 12 });
assert(mode === 'full', 'partial still drawable');
fp._syncCandleBodiesForMode(mode, st.gen);
assert(series.getOpts().upColor === T, 'partial → transparent');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_unknown_with_levels_hides_candles():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
st.payload = levelsPayload('UNKNOWN');
const mode = fp._resolveVisibilityMode({ barSpacing: 45, levelHeight: 4 });
assert(mode === 'delta', 'unknown delta');
fp._syncCandleBodiesForMode(mode, st.gen);
assert(series.getOpts().downColor === T, 'unknown → transparent');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_rapid_toggle_end_state_original():
    proc = _run_node(
        _harness(
            """
fp.enable();
st.payload = levelsPayload('COMPLETE');
fp._syncCandleBodiesForMode('full', st.gen);
fp.disable();
fp.enable();
fp.disable();
assert(!fp.isEnabled(), 'disabled');
assert(series.getOpts().upColor === ORIG.upColor, 'final original');
assert(series.getOpts().wickDownColor === ORIG.wickDownColor, 'wick original');
assert(st.candlesTransparent === false, 'flag cleared');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_stale_generation_rejects_hide():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
const oldGen = st.gen;
st.gen += 1;
const ok = fp._hideCandleBodies(oldGen);
assert(ok === false, 'stale gen must not hide');
assert(series.getOpts().upColor === ORIG.upColor, 'still original');
assert(!st.savedCandleStyle, 'must not capture on reject');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_original_saved_once_across_toggles():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
fp._syncCandleBodiesForMode('full', st.gen);
const first = JSON.stringify(st.savedCandleStyle);
assert(!!st.savedCandleStyle, 'captured');
fp._syncCandleBodiesForMode('off', st.gen);
fp._syncCandleBodiesForMode('full', st.gen);
fp._syncCandleBodiesForMode('delta', st.gen);
fp._syncCandleBodiesForMode('full', st.gen);
assert(JSON.stringify(st.savedCandleStyle) === first, 'saved unchanged');
assert(st.savedCandleStyle.upColor === ORIG.upColor, 'saved is original not transparent');
assert(series._applied.filter(p => p.upColor === T).length >= 1, 'applied transparent');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_price_line_and_last_label_untouched():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
fp._syncCandleBodiesForMode('full', st.gen);
assert(series.getOpts().priceLineVisible === true, 'price line');
assert(series.getOpts().lastValueVisible === true, 'last value');
assert(series.getOpts().visible === true, 'visible');
const patches = series._applied;
assert(patches.every(p => p.visible === undefined), 'never patch visible');
assert(patches.every(p => p.priceLineVisible === undefined), 'never patch priceLine');
assert(patches.every(p => p.lastValueVisible === undefined), 'never patch lastValue');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_no_levels_restores():
    proc = _run_node(
        _harness(
            """
st.enabled = true;
fp._syncCandleBodiesForMode('full', st.gen);
st.payload = { coverage: 'COMPLETE', candles: [{ time: 1, coverage: 'COMPLETE', levels: [] }] };
assert(fp._resolveVisibilityMode({ barSpacing: 70, levelHeight: 12 }) === 'no_levels');
fp._syncCandleBodiesForMode('no_levels', st.gen);
assert(series.getOpts().upColor === ORIG.upColor, 'restored');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_late_response_context_guard_in_source():
    """Contract: fetch success path re-checks enabled/mode before applying payload."""
    js = FP_JS.read_text(encoding="utf-8")
    assert "if (!state.enabled || !modeSupported() || state.unsupported)" in js
    assert "gen !== state.historyGen" in js or "historyGen" in js
    assert "gen !== state.formingGen" in js or "formingGen" in js
    assert "restoreCandleBodies" in js
    assert "historyInflight" in js and "formingInflight" in js


def test_mp_hooks_remain_thin_no_color_logic():
    mp = MP_JS.read_text(encoding="utf-8")
    assert "FOOTPRINT_HOOK" in mp
    assert "savedCandleStyle" not in mp
    assert "upColor" not in mp
    assert "applyOptions" not in mp
    assert "mp-19" in mp
