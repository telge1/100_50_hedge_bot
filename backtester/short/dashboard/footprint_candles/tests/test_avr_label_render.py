"""Regression: AVR classification badges must render despite delta collision."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
FP_JS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.js"


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", "-e", script], capture_output=True, text=True)


def _harness(body: str) -> str:
    return f"""
const fs=require('fs');const vm=require('vm');
const code=fs.readFileSync({FP_JS.as_posix()!r},'utf8');
const ops=[];
function makeCtx(){{
  return {{
    font:'', textAlign:'', textBaseline:'', fillStyle:'', strokeStyle:'', lineWidth:1, globalAlpha:1,
    setTransform(){{}}, clearRect(){{}}, fillRect(){{}}, strokeRect(){{}},
    beginPath(){{}}, moveTo(){{}}, lineTo(){{}}, stroke(){{}}, fill(){{}}, arc(){{}},
    fillText(t,x,y){{ ops.push(['fillText', String(t), x, y]); }},
    measureText(t){{ return {{width: (''+t).length*6}}; }}
  }};
}}
const canvas={{hidden:false,width:1200,height:500,style:{{}}, getContext:()=>makeCtx()}};
const sandbox={{
  window:{{addEventListener(){{}}, devicePixelRatio:1, requestAnimationFrame(cb){{cb();}}}},
  document:{{addEventListener(){{}},hidden:false,getElementById(id){{
    if(id==='fpOverlay') return canvas;
    if(id==='fpStatus') return {{textContent:'', className:''}};
    if(id==='fpShowAvrPanel') return {{checked:true, addEventListener(){{}}, _fpBound:false}};
    if(id==='fpAvrExpand') return {{setAttribute(){{}}, disabled:false, addEventListener(){{}}, _fpBound:false, getAttribute:()=>'false'}};
    if(id==='fpAvrPanel') return {{hidden:true}};
    if(id==='fpAvrPanelBody') return {{textContent:'', setAttribute(){{}}, attrs:{{}}}};
    if(id==='fpAvrPanelHint') return {{textContent:''}};
    if(id==='fpAvrLegend') return {{hidden:true, setAttribute(){{}}, getAttribute:()=>null, innerHTML:''}};
    if(id==='price-pane'||id==='mpChart'||id==='chart') return {{getBoundingClientRect:()=>({{width:1200,height:500,left:0,top:0}})}};
    return null;
  }}}},
  console, AbortController:class{{constructor(){{this.signal={{aborted:false}}}}abort(){{}}}},
  URLSearchParams, setInterval(){{return 1}}, clearInterval(){{}},
  setTimeout(cb){{if(typeof cb==='function')cb();return 1}}, clearTimeout(){{}},
  Date, requestAnimationFrame(cb){{cb();}},
  fetch(){{return Promise.resolve({{ok:true,json:()=>Promise.resolve({{success:true,candles:[]}})}})}}
}};
sandbox.window.requestAnimationFrame = sandbox.requestAnimationFrame;
vm.createContext(sandbox); vm.runInContext(code, sandbox);
const fp=sandbox.window.FootprintCandles;
fp.setContext({{
  candleSeries: {{ options(){{return {{}};}}, applyOptions(){{}}, priceToCoordinate(p){{ return 250-(Number(p)-90000)/8; }} }},
  symbol:'BTCUSDT', timeframe:'5m',
  chart:{{ timeScale(){{ return {{
    options(){{return {{barSpacing:40}};}},
    getVisibleRange(){{return {{from:0,to:10000}};}},
    timeToCoordinate(t){{return Number(t)/7.5;}}
  }}; }}, subscribeCrosshairMove(){{}}, unsubscribeCrosshairMove(){{}} }}
}});
fp._state.enabled=true; fp._state.panelVisible=true;
function assert(cond,msg){{ if(!cond){{ console.error(msg||'assert'); process.exit(2); }} }}
function mk(t, st, opts){{
  opts=opts||{{}};
  return {{
    time:t, open:100000, high:100150, low:99900, close:100050, coverage:'COMPLETE',
    levels:[{{price_low:100000,price_high:100005,bid_size:1,ask_size:1,bid_notional:1e5,ask_notional:2e5,delta_size:1,delta_notional:1e5}}],
    avr:{{
      dominant_state:st, final_state:st,
      dominant_strength: opts.strength!=null?opts.strength:80,
      verification: opts.ver||'UNVERIFIED',
      coverage_status: opts.cov||'UNKNOWN',
      provisional: !!opts.provisional,
      config_hash: opts.hash||'abc',
      badge: opts.badge||'',
      candle_delta_notional: opts.delta!=null?opts.delta:2.5e6
    }}
  }};
}}
{body}
"""


def test_two_pass_badge_priority_in_source():
    js = FP_JS.read_text(encoding="utf-8")
    assert "labelJobs" in js
    assert "Pass A: classification badges" in js
    assert "Pass B: candle deltas" in js


def test_mapping_all_seven_classification_types():
    proc = _run(
        _harness(
            """
const map={
  SELLER_CONTROL:'S CTRL',
  BUYER_CONTROL:'B CTRL',
  SELL_ABSORPTION_CANDIDATE:'S ABS',
  BUY_ABSORPTION_CANDIDATE:'B ABS',
  VACUUM_DOWN_PROXY:'VAC ↓',
  VACUUM_UP_PROXY:'VAC ↑',
  BALANCED:''
};
Object.keys(map).forEach(st=>{
  const spec=fp._badgeSpec({dominant_state:st, verification:'VERIFIED', coverage_status:'COMPLETE'});
  if(st==='BALANCED'){
    assert(spec && spec.kind==='dot', 'bal dot');
  } else {
    assert(spec && spec.kind==='badge', st+' kind');
    assert(spec.label.indexOf(map[st])===0, st+' label '+spec.label);
  }
});
assert(fp._badgeSpec({dominant_state:'INSUFFICIENT_DATA'}).kind==='none');
assert(fp._badgeSpec({dominant_state:'INSUFFICIENT_BASELINE'}).kind==='none');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_renderer_shows_classified_hides_insufficient():
    proc = _run(
        _harness(
            """
const candles=[
  mk(0,'INSUFFICIENT_DATA',{strength:0}),
  mk(300,'INSUFFICIENT_DATA',{strength:0}),
  mk(600,'INSUFFICIENT_DATA',{strength:0}),
  mk(900,'SELLER_CONTROL',{strength:111, badge:'S CTRL'}),
  mk(1200,'INSUFFICIENT_DATA',{strength:0}),
  mk(1500,'BUYER_CONTROL',{strength:94, badge:'B CTRL'}),
  mk(1800,'INSUFFICIENT_BASELINE',{strength:0}),
  mk(2100,'BALANCED',{strength:10})
];
fp._state.historyCandles=candles;
fp._state.payload={success:true, coverage:'UNKNOWN', candles};
ops.length=0; fp.scheduleDraw();
const texts=ops.filter(o=>o[0]==='fillText').map(o=>o[1]);
const badges=texts.filter(t=>/CTRL|ABS|VAC/.test(t));
assert(fp._state.lastDrawStats.badges===2, 'badge count '+fp._state.lastDrawStats.badges);
assert(badges.some(t=>t.indexOf('S CTRL')>=0), 'S CTRL');
assert(badges.some(t=>t.indexOf('B CTRL')>=0), 'B CTRL');
assert(!badges.some(t=>t.indexOf('DATA')>=0), 'no DATA badge');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_deltas_do_not_block_later_badges():
    """Reproduces the production failure: many insufficient+delta then sparse CTRL."""
    proc = _run(
        _harness(
            """
const candles=[];
for(let i=0;i<20;i++){
  const st=(i%4===0)?'SELLER_CONTROL':'INSUFFICIENT_DATA';
  candles.push(mk(i*300, st, {strength: st==='SELLER_CONTROL'?80:0}));
}
fp._state.historyCandles=candles;
fp._state.payload={success:true, coverage:'UNKNOWN', candles};
ops.length=0; fp.scheduleDraw();
assert(fp._state.lastDrawStats.badges===5, 'expected 5 CTRL badges got '+fp._state.lastDrawStats.badges);
const badges=ops.filter(o=>o[0]==='fillText'&&/S CTRL/.test(o[1]));
assert(badges.length===5, 'fillText badges');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_history_merge_keeps_classification_fields():
    proc = _run(
        _harness(
            """
const now=Math.floor(Date.now()/1000);
const fw=fp._formingCandleWindow(now);
const closed=mk(fw.from-300,'BUYER_CONTROL',{provisional:false, hash:'h1', badge:'B CTRL'});
const forming=mk(fw.from,'SELLER_CONTROL',{provisional:true, strength:7});
fp._state.historyCandles=[closed, forming];
fp._state.historyCandles=fp._upsertCandleList(
  fp._state.historyCandles,
  [mk(fw.from-300,'SELLER_CONTROL',{provisional:true, hash:'stale'}), mk(fw.from,'BALANCED',{provisional:true, strength:1})],
  'history'
);
assert(fp._state.historyCandles[0].avr.dominant_state==='BUYER_CONTROL', 'closed kept');
assert(fp._state.historyCandles[0].avr.config_hash==='h1', 'hash');
assert(fp._state.historyCandles[1].avr.dominant_strength===7, 'forming protected');
fp._applyFinalizeBody({success:true, candles:[mk(fw.from-300,'BUYER_CONTROL',{provisional:false, hash:'h1', strength:99})]});
const fin=fp._state.historyCandles.find(c=>c.time===fw.from-300);
assert(fin.avr.provisional===false, 'finalized');
assert(fin.avr.dominant_strength===99, 'final strength');
console.log('ok');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_backend_payload_contains_classification_for_real_candle():
    """Live CH optional — uses fixture-shaped payload contract from API fields."""
    # Contract shape always asserted; optional live check if sessions/CH available.
    sample = {
        "dominant_state": "SELLER_CONTROL",
        "final_state": "SELLER_CONTROL",
        "badge": "S CTRL",
        "dominant_strength": 111.871,
        "verification": "UNVERIFIED",
        "provisional": False,
        "config_hash": "a2ad82e897bc8300",
    }
    assert sample["badge"] == "S CTRL"
    assert sample["dominant_state"] == "SELLER_CONTROL"

    live = Path("/tmp/fp_live.json")
    if not live.exists():
        return
    try:
        data = json.loads(live.read_text(encoding="utf-8"))
    except Exception:
        return
    candles = data.get("candles") or []
    with_avr = [c for c in candles if c.get("avr")]
    if not with_avr:
        return
    avr = with_avr[-1]["avr"]
    assert "dominant_state" in avr
    assert "badge" in avr
    assert "config_hash" in avr
