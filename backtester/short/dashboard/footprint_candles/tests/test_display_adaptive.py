"""Adaptive display buckets + history/forming store contracts (Node/vm)."""

from __future__ import annotations

import subprocess
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
FP_JS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.js"


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", "-e", script], capture_output=True, text=True)


def _load(body: str) -> str:
    return f"""
const fs=require('fs');const vm=require('vm');
const code=fs.readFileSync({FP_JS.as_posix()!r},'utf8');
const fetches=[];
const sandbox={{
  window:{{addEventListener(){{}}}},
  document:{{addEventListener(){{}},hidden:false,getElementById(){{return null;}}}},
  console,
  AbortController: class {{ constructor(){{this.signal={{aborted:false}};}} abort(){{this.signal.aborted=true;}} }},
  URLSearchParams,
  setInterval(){{return 1;}}, clearInterval(){{}},
  setTimeout(cb){{ if(typeof cb==='function') cb(); return 1; }},
  clearTimeout(){{}},
  Date,
  requestAnimationFrame(cb){{cb();}},
  fetch(url, opts){{
    const u=String(url);
    const fm=(u.match(/from=(\\d+)/)||[])[1];
    const tm=(u.match(/to=(\\d+)/)||[])[1];
    const forming = fm && tm && (Number(tm)-Number(fm)===300);
    fetches.push({{url:u, forming:!!forming}});
    return Promise.resolve({{ok:true, json(){{return Promise.resolve({{success:true, coverage:'COMPLETE', candles:[]}});}}}});
  }}
}};
vm.createContext(sandbox); vm.runInContext(code, sandbox);
const fp=sandbox.window.FootprintCandles;
function assert(c,m){{ if(!c){{ console.error('FAIL',m); process.exit(1); }} }}
{body}
console.log('ok');
"""


def test_node_syntax_ok():
    proc = subprocess.run(["node", "--check", str(FP_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_raw_step_stays_5_when_tall_enough():
    proc = _run(
        _load(
            """
const step=fp._chooseDisplayStep(12, 11);
assert(step===5, 'expect raw $5 got '+step);
const plan=fp._chooseRenderPlan({barSpacing:70, pxPerRaw5:12});
assert(plan.mode==='full' && plan.displayStep===5, JSON.stringify(plan));
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_auto_aggregate_5_to_10_or_20():
    proc = _run(
        _load(
            """
assert(fp._chooseDisplayStep(2, 11)===50, '2px→50');
assert(fp._chooseDisplayStep(3, 11)===20, '3px→20 got '+fp._chooseDisplayStep(3,11));
assert(fp._chooseDisplayStep(6, 11)===10, '6px→10');
assert(fp._chooseDisplayStep(1.5, 3)===10, '1.5px delta→10 got '+fp._chooseDisplayStep(1.5,3));
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_smallest_readable_step_chosen():
    proc = _run(
        _load(
            """
const steps=fp._DISPLAY_STEPS;
assert(JSON.stringify(steps)==='[5,10,15,20,25,50]');
const s=fp._chooseDisplayStep(2.2, 11);
assert(s===25, '2.2*5=11 → 25 got '+s);
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_bucket_align_deterministic():
    proc = _run(
        _load(
            """
assert(fp._alignDisplayLow(100, 10)===100);
assert(fp._alignDisplayLow(101, 10)===100);
assert(fp._alignDisplayLow(109.9, 10)===100);
assert(fp._alignDisplayLow(110, 10)===110);
assert(fp._alignDisplayLow(97.5, 25)===75);
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_aggregate_sums_and_delta_vpoc():
    proc = _run(
        _load(
            """
const raw=[
  {price_low:100, price_high:105, bid_size:1, ask_size:2, bid_notional:100, ask_notional:200, ask_imbalance:true, bid_imbalance:false, is_vpoc:false},
  {price_low:105, price_high:110, bid_size:3, ask_size:1, bid_notional:300, ask_notional:110, ask_imbalance:false, bid_imbalance:true, is_vpoc:true},
  {price_low:110, price_high:115, bid_size:1, ask_size:1, bid_notional:50, ask_notional:50, is_vpoc:false}
];
const a=fp._aggregateLevelsForDisplay(raw, 10);
assert(a.displayStep===10, 'step');
assert(a.levels.length===2, 'two display rows got '+a.levels.length);
const r0=a.levels[0];
assert(r0.price_low===100 && r0.price_high===110, 'bounds');
assert(r0.bid_size===4 && r0.ask_size===3, 'sizes');
assert(r0.bid_notional===400 && r0.ask_notional===310, 'notional');
assert(r0.delta_size===-1, 'delta size');
assert(Math.abs(r0.delta_pct - (-1/7*100))<1e-6, 'delta pct');
assert(r0.is_vpoc===true, 'vpoc lower larger');
assert(a.levels[1].is_vpoc===false);
assert(r0.imbalances_recomputed===true, 'recomputed flag');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_vpoc_tie_break_lower_price():
    proc = _run(
        _load(
            """
const raw=[
  {price_low:100, price_high:105, bid_size:1, ask_size:1, bid_notional:1, ask_notional:1},
  {price_low:105, price_high:110, bid_size:1, ask_size:1, bid_notional:1, ask_notional:1},
  {price_low:110, price_high:115, bid_size:2, ask_size:0, bid_notional:2, ask_notional:0},
  {price_low:115, price_high:120, bid_size:0, ask_size:2, bid_notional:0, ask_notional:2}
];
const a=fp._aggregateLevelsForDisplay(raw, 10);
assert(a.levels[0].is_vpoc===true && a.levels[1].is_vpoc===false, 'tie lower');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_modes_horizontal_and_vertical():
    proc = _run(
        _load(
            """
assert(fp._chooseRenderPlan({barSpacing:70, pxPerRaw5:12}).mode==='full');
assert(fp._chooseRenderPlan({barSpacing:45, pxPerRaw5:5}).mode==='delta');
assert(fp._chooseRenderPlan({barSpacing:50, pxPerRaw5:2}).mode==='delta');
assert(fp._chooseRenderPlan({barSpacing:30, pxPerRaw5:5}).mode==='compact');
assert(fp._chooseRenderPlan({barSpacing:20, pxPerRaw5:12}).mode==='fallback');
assert(fp._chooseRenderPlan({barSpacing:70, pxPerRaw5:2}).mode==='full');
assert(fp._chooseRenderPlan({barSpacing:70, pxPerRaw5:2}).displayStep===50);
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_status_labels():
    proc = _run(
        _load(
            """
assert(fp._statusForPlan({mode:'full', displayStep:10}, 'COMPLETE').text.indexOf('Full · Display $10')===0);
assert(fp._statusForPlan({mode:'delta', displayStep:20}, 'PARTIAL').text.indexOf('Delta · Display $20')===0);
assert(fp._statusForPlan({mode:'compact', displayStep:5}, 'UNKNOWN').text==='Compact · Raw $5');
assert(fp._statusForPlan({mode:'compact', displayStep:25}, 'UNKNOWN').text.indexOf('Display $25')>0);
assert(fp._statusForPlan({mode:'fallback'}, 'COMPLETE').text==='Mehr hineinzoomen');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_upsert_and_prune_forming_does_not_wipe_history():
    proc = _run(
        _load(
            """
const hist=[
  {time:1000, levels:[{price_low:1}]},
  {time:1300, levels:[{price_low:2}]}
];
const merged=fp._upsertCandleList(hist, [{time:1600, levels:[{price_low:3}], provisional:true}]);
assert(merged.length===3, 'len');
assert(merged[0].time===1000 && merged[2].time===1600, 'sorted');
const upd=fp._upsertCandleList(merged, [{time:1600, levels:[{price_low:9}]}]);
assert(upd.length===3 && upd[2].levels[0].price_low===9, 'upsert replace');
const pruned=fp._pruneCandles(upd, 1200, 2000);
assert(pruned.every(c=>c.time>=600), 'pad prune');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_history_forming_separate_controllers_in_source():
    js = FP_JS.read_text(encoding="utf-8")
    assert "historyInflight" in js
    assert "formingInflight" in js
    assert "abortHistory" in js
    assert "abortForming" in js
    assert "FORMING_MS = 2000" in js
    assert "applyFormingBody" in js
    assert "applyHistoryBody" in js
    assert "Mehr hineinzoomen" in js
    assert "Display $" in js
    assert "Raw $" in js


def test_enable_fetches_visible_not_only_forming():
    proc = _run(
        _load(
            """
const ORIG={upColor:'#1',downColor:'#2',borderUpColor:'#1',borderDownColor:'#2',wickUpColor:'#1',wickDownColor:'#2'};
let opts=Object.assign({},ORIG);
const series={options(){return Object.assign({},opts);}, applyOptions(p){opts=Object.assign({},opts,p);}, priceToCoordinate(){return 10;}, coordinateToPrice(){return 100;}};
fp.setContext({
  candleSeries: series,
  symbol:'BTCUSDT', timeframe:'5m',
  chart:{ timeScale(){ return {
    options(){return {barSpacing:70};},
    getVisibleRange(){return {from:1000,to:1000+3*3600};},
    timeToCoordinate(){return 50;}
  }; } }
});
fp.enable();
assert(fetches.length>=1, 'at least one fetch');
const hist=fetches.filter(f=>!f.forming);
assert(hist.length>=1, 'history fetch on enable');
assert(/from=\\d+/.test(hist[0].url) && /to=\\d+/.test(hist[0].url), 'range params');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_stale_history_and_forming_guards():
    js = FP_JS.read_text(encoding="utf-8")
    assert "if (gen !== state.historyGen || ctrl.signal.aborted) return;" in js
    assert "if (gen !== state.formingGen || ctrl.signal.aborted) return;" in js


def test_compact_makes_transparent_in_resolve():
    proc = _run(
        _load(
            """
fp._state.enabled=true;
fp._state.payload={coverage:'COMPLETE', candles:[{time:1, coverage:'COMPLETE', levels:[{price_low:1,price_high:2}]}]};
assert(fp._resolveVisibilityMode({barSpacing:30, levelHeight:5})==='compact');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_avr_independent_of_display_step_contract():
    js = FP_JS.read_text(encoding="utf-8")
    assert "aggregateLevelsForDisplay" in js
    assert "dominant_state" in js
    assert "avr.dominant_state =" not in js.replace("avr.dominant_state ===", "avr.dominant_state__EQ__")
    assert "Reason: " in js


def test_frontend_contract_mentions_compact():
    js = FP_JS.read_text(encoding="utf-8")
    assert "compact" in js
    assert "COMPACT_BAR" in js


def test_sync_compact_hides_bodies():
    proc = _run(
        _load(
            """
const ORIG={upColor:'#26a69a',downColor:'#ef5350',borderUpColor:'#26a69a',borderDownColor:'#ef5350',wickUpColor:'#26a69a',wickDownColor:'#ef5350'};
let opts=Object.assign({},ORIG);
const series={options(){return Object.assign({},opts);}, applyOptions(p){opts=Object.assign({},opts,p);}, priceToCoordinate(){return 10;}};
fp.setContext({candleSeries:series, symbol:'BTCUSDT', timeframe:'5m', chart:{timeScale(){return {options(){return {barSpacing:30};}, getVisibleRange(){return {from:1,to:2};}, timeToCoordinate(){return 1;}};}}});
fp._state.enabled=true;
fp._syncCandleBodiesForMode('compact', fp._state.gen);
assert(opts.upColor===fp._TRANSPARENT, 'compact transparent');
fp._syncCandleBodiesForMode('fallback', fp._state.gen);
assert(opts.upColor===ORIG.upColor, 'fallback restore');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
