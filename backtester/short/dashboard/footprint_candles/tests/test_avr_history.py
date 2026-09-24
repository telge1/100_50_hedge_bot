"""AVR candle-history store, finalize, strip, crosshair, legend regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
FP_JS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.js"
FP_CSS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.css"
PAGE = DASHBOARD_DIR / "templates" / "market_profile_v1.html"


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", "-e", script], capture_output=True, text=True)


def _load(body: str) -> str:
    return f"""
const fs=require('fs');const vm=require('vm');
const code=fs.readFileSync({FP_JS.as_posix()!r},'utf8');
const panelBodyEl={{textContent:'', attrs:{{}}, setAttribute(k,v){{this.attrs[k]=v;}}, getAttribute(k){{return this.attrs[k];}}}};
const panelEl={{hidden:true}};
const legendEl={{hidden:true, innerHTML:'', attrs:{{}}, setAttribute(k,v){{this.attrs[k]=v;}}, getAttribute(k){{return this.attrs[k]||null;}}}};
function makeCtx(){{
  return {{
    font:'', textAlign:'', textBaseline:'', fillStyle:'', strokeStyle:'', lineWidth:1, globalAlpha:1,
    setTransform(){{}}, clearRect(){{}}, fillRect(){{}}, strokeRect(){{}},
    beginPath(){{}}, moveTo(){{}}, lineTo(){{}}, stroke(){{}}, fill(){{}}, arc(){{}},
    fillText(){{}}, measureText(t){{ return {{width: (''+t).length*6}}; }}
  }};
}}
const fpCanvas = {{
  hidden:true, width:800, height:400, style:{{}},
  getContext: function(){{ return makeCtx(); }}
}};
const sandbox={{
  window:{{addEventListener(){{}}, devicePixelRatio:1, requestAnimationFrame(cb){{ if(typeof cb==='function') cb(); }}}},
  document:{{addEventListener(){{}},hidden:false,getElementById(id){{
    if(id==='fpOverlay') return fpCanvas;
    if(id==='fpAvrPanel') return panelEl;
    if(id==='fpAvrPanelBody') return panelBodyEl;
    if(id==='fpAvrPanelHint') return {{textContent:''}};
    if(id==='fpAvrLegend') return legendEl;
    if(id==='fpShowAvrPanel') return {{checked:true, addEventListener(){{}}, _fpBound:false}};
    if(id==='fpAvrExpand') return {{setAttribute(){{}}, disabled:false, addEventListener(){{}}, _fpBound:false, getAttribute:()=>'false'}};
    if(id==='fpStatus') return {{textContent:'', className:''}};
    if(id==='price-pane'||id==='mpChart'||id==='chart') return {{getBoundingClientRect:()=>({{width:800,height:400,left:0,top:0}})}};
    return null;
  }}}},
  console, AbortController:class{{constructor(){{this.signal={{aborted:false}}}}abort(){{}}}},
  URLSearchParams, setInterval(){{return 1}}, clearInterval(){{}},
  setTimeout(cb){{if(typeof cb==='function')cb();return 1}}, clearTimeout(){{}},
  Date, requestAnimationFrame(cb){{if(typeof cb==='function')cb();}},
  fetch(){{return Promise.resolve({{ok:true,json:()=>Promise.resolve({{success:true,candles:[]}})}})}}
}};
sandbox.window.requestAnimationFrame = sandbox.requestAnimationFrame;
vm.createContext(sandbox); vm.runInContext(code, sandbox);
const fp=sandbox.window.FootprintCandles;
fp.setContext({{
  candleSeries: {{
    options(){{return {{}};}},
    applyOptions(){{}},
    priceToCoordinate(p){{ return 200 - (Number(p)-90)*4; }}
  }},
  symbol:'BTCUSDT', timeframe:'5m',
  chart:{{ timeScale(){{ return {{
    options(){{return {{barSpacing:40}};}},
    getVisibleRange(){{return {{from:1000,to:4000}};}},
    timeToCoordinate(t){{return (Number(t)-1000)/10;}}
  }}; }}, subscribeCrosshairMove(){{}}, unsubscribeCrosshairMove(){{}} }}
}});
function assert(cond, msg){{ if(!cond){{ console.error(msg||'assert'); process.exit(2); }} }}
function mkAvr(st, opts){{
  opts = opts || {{}};
  return {{
    dominant_state: st,
    final_state: st,
    dominant_strength: opts.strength != null ? opts.strength : 2.5,
    state_share: {{[st]: 0.6}},
    third_states: {{EARLY: st, MIDDLE: st, LATE: st}},
    verification: opts.verification || 'COMPLETE',
    coverage_status: opts.coverage || 'COMPLETE',
    provisional: !!opts.provisional,
    config_hash: opts.hash || 'abc123deadbeef00',
    candle_delta_notional: opts.delta != null ? opts.delta : 1000,
    panel: {{ window_seconds: 15, available_at: 1200 }}
  }};
}}
function mkCandle(t, st, opts){{
  return {{
    time: t,
    open:100, high:105, low:98, close:102,
    coverage: 'COMPLETE',
    levels: [{{price_low:100, price_high:105, bid_size:1, ask_size:1, bid_notional:1, ask_notional:1, delta_size:0}}],
    avr: mkAvr(st, opts)
  }};
}}
{body}
"""


def test_node_syntax_ok():
    proc = subprocess.run(["node", "--check", str(FP_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_one_store_entry_and_strip_per_historical_candle():
    proc = _run(
        _load(
            """
fp._state.historyCandles = [
  mkCandle(1000,'SELLER_CONTROL',{provisional:false}),
  mkCandle(1300,'BUYER_CONTROL',{provisional:false}),
  mkCandle(1600,'BALANCED',{provisional:false}),
  mkCandle(1900,'SELLER_CONTROL',{provisional:true})
];
fp._syncAvrMarksFromCandles();
const keys = Object.keys(fp._state.avrMarks);
assert(keys.length===4, 'marks');
assert(keys.every(k=>k.indexOf('BTCUSDT|5m|')===0), 'key shape');
const segs=[];
fp._state.historyCandles.forEach(c=>{
  fp._pushStripSegment(segs, c.avr, 10, 20);
});
assert(segs.length===4, 'strip segs');
assert(segs.every(s=>s.color && s.alpha!=null), 'strip color');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_forming_poll_preserves_history_and_does_not_replace():
    proc = _run(
        _load(
            """
const now = Math.floor(Date.now()/1000);
const fw = fp._formingCandleWindow(now);
fp._state.historyCandles = [
  mkCandle(fw.from - 600, 'SELLER_CONTROL', {provisional:false}),
  mkCandle(fw.from - 300, 'BUYER_CONTROL', {provisional:false}),
  mkCandle(fw.from, 'BALANCED', {provisional:true, strength:1})
];
fp._applyFormingBody({
  success:true,
  coverage:'PARTIAL',
  candles:[mkCandle(fw.from, 'SELLER_CONTROL', {provisional:true, strength:9})]
});
assert(fp._state.historyCandles.length===3, 'hist kept');
assert(fp._state.historyCandles[0].avr.dominant_state==='SELLER_CONTROL', 'old0');
assert(fp._state.historyCandles[1].avr.dominant_state==='BUYER_CONTROL', 'old1');
assert(fp._state.historyCandles[2].avr.dominant_strength===9, 'forming upsert');
assert(fp._state.historyCandles[2].avr.provisional===true, 'still provisional');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_history_merge_protects_forming_and_finalized():
    proc = _run(
        _load(
            """
const FIXED = Math.floor(Date.now()/1000);
const fw = fp._formingCandleWindow(FIXED);
const closed = mkCandle(fw.from - 300, 'BUYER_CONTROL', {provisional:false, hash:'h1'});
const forming = mkCandle(fw.from, 'SELLER_CONTROL', {provisional:true, strength:7});
fp._state.historyCandles = [closed, forming];
fp._state.historyCandles = fp._upsertCandleList(
  fp._state.historyCandles,
  [
    mkCandle(fw.from - 300, 'SELLER_CONTROL', {provisional:true, hash:'stale'}),
    mkCandle(fw.from, 'BALANCED', {provisional:true, strength:1})
  ],
  'history'
);
assert(fp._state.historyCandles.length===2, 'len');
assert(fp._state.historyCandles[0].avr.provisional===false, 'closed kept');
assert(fp._state.historyCandles[0].avr.config_hash==='h1', 'hash kept');
assert(fp._state.historyCandles[1].avr.dominant_strength===7, 'forming protected');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_bucket_finalize_sets_provisional_false_and_new_forming_true():
    proc = _run(
        _load(
            """
const now = Math.floor(Date.now()/1000);
const fw = fp._formingCandleWindow(now);
const prev = fw.from - 300;
const next = fw.from;
fp._state.historyCandles = [
  mkCandle(prev, 'SELLER_CONTROL', {provisional:true, strength:3}),
];
fp._applyFinalizeBody({
  success:true,
  candles:[mkCandle(prev, 'SELLER_CONTROL', {provisional:false, strength:8})]
});
assert(fp._state.historyCandles.length===1, 'still one');
assert(fp._state.historyCandles[0].avr.provisional===false, 'finalized');
assert(fp._state.historyCandles[0].avr.dominant_strength===8, 'final strength');
fp._applyFormingBody({
  success:true,
  candles:[mkCandle(next, 'BUYER_CONTROL', {provisional:true})]
});
assert(fp._state.historyCandles.length===2, 'both candles');
const by={}; fp._state.historyCandles.forEach(c=>by[c.time]=c);
assert(by[prev].avr.provisional===false, 'prev closed');
assert(by[next].avr.provisional===true, 'new forming');
assert(Object.keys(fp._state.avrMarks).length===2, 'marks');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_no_duplicate_keys_after_repeated_upserts():
    proc = _run(
        _load(
            """
let list=[];
for(let i=0;i<5;i++){
  list = fp._upsertCandleList(list, [mkCandle(1000,'SELLER_CONTROL',{provisional:false})], 'replace');
  list = fp._upsertCandleList(list, [mkCandle(1300,'BUYER_CONTROL',{provisional:false})], 'history');
}
assert(list.length===2, 'dedupe');
fp._state.historyCandles = list;
fp._syncAvrMarksFromCandles();
assert(Object.keys(fp._state.avrMarks).length===2, 'mark dedupe');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_prune_removes_candle_and_mark_together():
    proc = _run(
        _load(
            """
fp._state.historyCandles = [
  mkCandle(1000,'SELLER_CONTROL',{provisional:false}),
  mkCandle(2000,'BUYER_CONTROL',{provisional:false}),
  mkCandle(3000,'BALANCED',{provisional:false})
];
fp._syncAvrMarksFromCandles();
assert(Object.keys(fp._state.avrMarks).length===3);
fp._state.historyCandles = fp._pruneCandles(fp._state.historyCandles, 2500, 3500);
fp._syncAvrMarksFromCandles();
assert(fp._state.historyCandles.every(c=>c.time>=1900), 'pad');
assert(fp._state.historyCandles.some(c=>c.time===3000), 'kept near');
assert(!fp._state.historyCandles.some(c=>c.time===1000), 'pruned far');
assert(!fp._state.avrMarks[fp._avrMarkKey(1000)], 'mark gone');
assert(fp._state.avrMarks[fp._avrMarkKey(3000)], 'mark kept');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_scroll_reload_reuses_store_and_merges_older():
    proc = _run(
        _load(
            """
fp._state.historyCandles = [
  mkCandle(2000,'BUYER_CONTROL',{provisional:false}),
  mkCandle(2300,'SELLER_CONTROL',{provisional:true})
];
fp._state.historyCandles = fp._upsertCandleList(
  fp._state.historyCandles,
  [
    mkCandle(1400,'VACUUM_DOWN_PROXY',{provisional:false}),
    mkCandle(1700,'SELL_ABSORPTION_CANDIDATE',{provisional:false}),
    mkCandle(2000,'BUYER_CONTROL',{provisional:false})
  ],
  'history'
);
assert(fp._state.historyCandles.length===4, 'merged older');
assert(fp._state.historyCandles.map(c=>c.time).join(',')==='1400,1700,2000,2300');
/* forward again — store still has them */
const again = fp._upsertCandleList(fp._state.historyCandles, [
  mkCandle(2000,'BUYER_CONTROL',{provisional:false})
], 'history');
assert(again.length===4, 'reuse');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_crosshair_shows_historical_not_current():
    proc = _run(
        _load(
            """
fp._state.enabled = true;
fp._state.panelVisible = true;
fp._state.payload = {
  candles: [
    mkCandle(1000,'SELLER_CONTROL',{provisional:false, hash:'hist'}),
    mkCandle(1300,'BUYER_CONTROL',{provisional:true, hash:'live'})
  ]
};
fp.setSelectedTime(1000);
fp.setPanelExpanded(true);
const pick = fp._pickPanelCandle(fp._state.payload);
assert(pick.candle.time===1000, 'hist candle');
assert(pick.candle.avr.config_hash==='hist', 'hist hash');
assert(pick.label==='Ausgewählte Candle');
/* panel attributes filled when expanded */
assert(panelEl.hidden===false, 'panel shown '+panelEl.hidden);
assert(panelBodyEl.attrs['data-candle_time']==='1000', 'panel time '+JSON.stringify(panelBodyEl.attrs));
assert(panelBodyEl.attrs['data-config_hash']==='hist', 'panel hash');
assert(panelBodyEl.attrs['data-provisional']==='false', 'panel closed');
/* leave → current */
fp.setSelectedTime(null);
const live = fp._pickPanelCandle(fp._state.payload);
assert(live.candle.time===1300, 'live');
assert(live.label==='Aktuelle Candle');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_avr_toggle_hides_and_restores_from_store():
    proc = _run(
        _load(
            """
fp._state.enabled = true;
fp._state.historyCandles = [
  mkCandle(1000,'SELLER_CONTROL',{provisional:false}),
  mkCandle(1300,'BUYER_CONTROL',{provisional:false})
];
fp._syncAvrMarksFromCandles();
fp.setPanelVisible(false);
assert(legendEl.hidden===true, 'legend off');
assert(Object.keys(fp._state.avrMarks).length===2, 'store kept');
fp.setPanelVisible(true);
assert(Object.keys(fp._state.avrMarks).length===2, 'store still');
fp._syncAvrLegend();
assert(legendEl.hidden===false, 'legend on');
assert(String(legendEl.innerHTML).indexOf('S CTRL')>=0, 'legend items');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_symbol_change_clears_only_current_context():
    proc = _run(
        _load(
            """
fp._state.enabled = true;
fp._state.historyCandles = [mkCandle(1000,'SELLER_CONTROL',{provisional:false})];
fp._syncAvrMarksFromCandles();
assert(Object.keys(fp._state.avrMarks).length===1);
fp._clearContextStore();
assert(fp._state.historyCandles.length===0);
assert(Object.keys(fp._state.avrMarks).length===0);
assert(fp._state.lastFormingBucket===null);
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_source_has_finalize_and_legend_contract():
    js = FP_JS.read_text(encoding="utf-8")
    assert "fetchFinalize" in js
    assert "lastFormingBucket" in js
    assert "applyFinalizeBody" in js
    assert "avrMarks" in js
    assert "syncAvrLegend" in js
    assert "fpAvrLegend" in js
    assert 'policy || "replace"' in js or 'pol === "history"' in js
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="fpAvrLegend"' in html
    # Legend must appear before Market Profile legend
    assert html.index("fpAvrLegend") < html.index('id="mpLegend"')
    css = FP_CSS.read_text(encoding="utf-8")
    assert ".fp-avr-legend" in css
    assert "Verkäuferkontrolle" in js or "Verkäuferkontrolle" in html or True
    assert "Verkäuferkontrolle" in js


def test_closed_candle_no_lookahead_engine():
    """Server closed summary uses cutoff=c_end (no post-candle trades)."""
    import sys

    if str(DASHBOARD_DIR) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_DIR))

    from footprint_candles.response_engine import SecondBucket, SecondSeries, summarize_candle_response

    def _bucket(second_ts, buy=1e3, sell=1e3, first=100.0, last=100.0):
        return SecondBucket(
            second_ts=int(second_ts),
            buy_notional=float(buy),
            sell_notional=float(sell),
            buy_size=float(buy) / first if first else 0.0,
            sell_size=float(sell) / first if first else 0.0,
            buy_trade_count=1 if buy > 0 else 0,
            sell_trade_count=1 if sell > 0 else 0,
            first_price=float(first),
            last_price=float(last),
            high_price=max(float(first), float(last)),
            low_price=min(float(first), float(last)),
        )

    t0 = 2_100_000_000
    hist = [_bucket(t0 - 2000 + i, buy=2e3, sell=2e3) for i in range(1800)]
    inside = [_bucket(t0 + i, buy=1e3, sell=5e7, first=100.0, last=99.5) for i in range(300)]
    after = [_bucket(t0 + 300 + i, buy=9e8, sell=1e3, first=99.5, last=110.0) for i in range(200)]
    series = SecondSeries(hist + inside + after)
    ohlc = {"open": 100.0, "high": 100.5, "low": 99.0, "close": 99.5}
    closed = summarize_candle_response(
        candle_time=t0,
        ohlc=ohlc,
        coverage="COMPLETE",
        series=series,
        now_unix=t0 + 500,
    )
    assert closed["provisional"] is False
    closed2 = summarize_candle_response(
        candle_time=t0,
        ohlc=ohlc,
        coverage="COMPLETE",
        series=series,
        now_unix=t0 + 900,
    )
    assert closed2["provisional"] is False
    assert closed["config_hash"] == closed2["config_hash"]
    assert closed["dominant_state"] == closed2["dominant_state"]
    assert closed["final_state"] == closed2["final_state"]
    forming = summarize_candle_response(
        candle_time=t0,
        ohlc=ohlc,
        coverage="COMPLETE",
        series=series,
        now_unix=t0 + 100,
    )
    assert forming["provisional"] is True


def test_fallback_still_draws_strip_in_source():
    js = FP_JS.read_text(encoding="utf-8")
    assert "drawAvrStripAndBadgesOnly" in js
    assert "Mehr hineinzoomen" in js
    # Extract fallback block and ensure strip helper is invoked there
    start = js.index('if (plan.mode === "fallback")')
    end = js.index("var displayStep = plan.displayStep", start)
    block = js[start:end]
    assert "drawAvrStripAndBadgesOnly" in block
