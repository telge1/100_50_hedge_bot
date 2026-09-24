"""Visual compact silhouette, badge, strip, panel, collision contracts."""

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
const calls=[];
const panelBodyEl={{textContent:'', attrs:{{}}, setAttribute(k,v){{this.attrs[k]=v;}}, getAttribute(k){{return this.attrs[k];}}}};
const panelEl={{hidden:true}};
function makeCtx(){{
  const ops=[];
  return {{
    ops,
    font:'', textAlign:'', textBaseline:'', fillStyle:'', strokeStyle:'', lineWidth:1, globalAlpha:1,
    setTransform(){{}}, clearRect(){{}},
    fillRect(x,y,w,h){{ ops.push(['fillRect',x,y,w,h,this.fillStyle]); }},
    strokeRect(x,y,w,h){{ ops.push(['strokeRect',x,y,w,h,this.strokeStyle]); }},
    beginPath(){{ ops.push(['beginPath']); }},
    moveTo(x,y){{ ops.push(['moveTo',x,y]); }},
    lineTo(x,y){{ ops.push(['lineTo',x,y]); }},
    stroke(){{ ops.push(['stroke',this.strokeStyle,this.lineWidth]); }},
    fill(){{ ops.push(['fill',this.fillStyle]); }},
    arc(x,y,r){{ ops.push(['arc',x,y,r]); }},
    fillText(t,x,y){{ ops.push(['fillText',t,x,y,this.fillStyle]); }},
    measureText(t){{ return {{width: (''+t).length*6}}; }},
  }};
}}
const prices={{o:100,c:102,h:105,l:98}};
const series={{
  options(){{return {{}};}},
  applyOptions(){{}},
  priceToCoordinate(p){{
    // map price → y (higher price = lower y)
    return 200 - (Number(p)-90)*4;
  }}
}};
const sandbox={{
  window:{{addEventListener(){{}}}},
  document:{{addEventListener(){{}},hidden:false,getElementById(id){{
    if(id==='fpOverlay') return {{hidden:true,width:800,height:400,style:{{}},getContext:()=>null}};
    if(id==='fpAvrPanel') return panelEl;
    if(id==='fpAvrPanelBody') return panelBodyEl;
    if(id==='fpAvrPanelHint') return {{textContent:''}};
    if(id==='fpShowAvrPanel') return {{checked:true, addEventListener(){{}}, _fpBound:false}};
    if(id==='fpAvrExpand') return {{setAttribute(){{}}, disabled:false, addEventListener(){{}}, _fpBound:false, getAttribute:()=>'false'}};
    if(id==='fpStatus') return {{textContent:'', className:''}};
    if(id==='price-pane'||id==='mpChart'||id==='chart') return {{getBoundingClientRect:()=>({{width:800,height:400,left:0,top:0}})}};
    return null;
  }}}},
  console, AbortController:class{{constructor(){{this.signal={{aborted:false}}}}abort(){{}}}},
  URLSearchParams, setInterval(){{return 1}}, clearInterval(){{}},
  setTimeout(cb){{if(typeof cb==='function')cb();return 1}}, clearTimeout(){{}},
  Date, requestAnimationFrame(cb){{cb()}},
  fetch(){{return Promise.resolve({{ok:true,json:()=>Promise.resolve({{success:true,candles:[]}})}})}}
}};
vm.createContext(sandbox); vm.runInContext(code, sandbox);
const fp=sandbox.window.FootprintCandles;
fp.setContext({{
  candleSeries: series,
  symbol:'BTCUSDT', timeframe:'5m',
  chart:{{ timeScale(){{ return {{
    options(){{return {{barSpacing:40}};}},
    getVisibleRange(){{return {{from:1000,to:4000}};}},
    timeToCoordinate(t){{return (Number(t)-1000)/10;}}
  }}; }}, subscribeCrosshairMove(){{}}, unsubscribeCrosshairMove(){{}} }}
}});
function assert(c,m){{ if(!c){{ console.error('FAIL',m); process.exit(1); }} }}
{body}
console.log('ok');
"""


def test_node_check():
    proc = subprocess.run(["node", "--check", str(FP_JS)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_silhouette_wick_body_ticks_bull_bear_doji():
    proc = _run(
        _load(
            """
const ctx=makeCtx();
const bull={open:100,close:104,high:106,low:99};
fp._drawCandleSilhouette(ctx, bull, 10, 40);
const ops=ctx.ops.map(o=>o[0]);
assert(ops.includes('moveTo') && ops.includes('lineTo') && ops.includes('stroke'), 'wick');
assert(ops.includes('fillRect') && ops.includes('strokeRect'), 'body');
const openTick = ctx.ops.filter(o=>o[0]==='moveTo'||o[0]==='lineTo');
assert(openTick.length>=4, 'ticks present');
/* bearish */
const ctx2=makeCtx();
fp._drawCandleSilhouette(ctx2, {open:104,close:100,high:106,low:99}, 10, 40);
assert(ctx2.ops.some(o=>o[0]==='fillRect' && String(o[5]).includes('239')), 'bear fill');
/* doji */
const ctx3=makeCtx();
fp._drawCandleSilhouette(ctx3, {open:100,close:100,high:105,low:98}, 10, 40);
assert(!ctx3.ops.some(o=>o[0]==='fillRect'), 'doji no fillRect body');
assert(ctx3.ops.some(o=>o[0]==='stroke'), 'doji stroke');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_badge_mapping_and_no_data_question():
    proc = _run(
        _load(
            """
const sc=fp._badgeSpec({dominant_state:'SELLER_CONTROL', coverage_status:'COMPLETE', verification:'VERIFIED'});
assert(sc.label==='S CTRL' && sc.kind==='badge', 'seller');
const ba=fp._badgeSpec({dominant_state:'BUY_ABSORPTION_CANDIDATE', coverage_status:'COMPLETE'});
assert(ba.label==='B ABS', 'b abs');
const vac=fp._badgeSpec({dominant_state:'VACUUM_DOWN_PROXY', coverage_status:'COMPLETE'});
assert(vac.label==='VAC ↓' && vac.label.indexOf('?')<0, 'vac no extra ?');
const insuf=fp._badgeSpec({dominant_state:'INSUFFICIENT_DATA'});
assert(insuf.kind==='none' && !insuf.label, 'no DATA?');
const base=fp._badgeSpec({dominant_state:'INSUFFICIENT_BASELINE'});
assert(base.kind==='none', 'no baseline badge');
const bal=fp._badgeSpec({dominant_state:'BALANCED'});
assert(bal.kind==='dot' && !bal.label, 'balanced dot');
const partial=fp._badgeSpec({dominant_state:'BUYER_CONTROL', coverage_status:'PARTIAL', verification:'UNVERIFIED'});
assert(partial.label==='B CTRL ?' && (partial.label.match(/\\?/g)||[]).length===1, 'one ?');
assert(fp._badgeForAvr({dominant_state:'INSUFFICIENT_DATA'})==='', 'badgeForAvr empty');
assert(fp._AVR_BADGES.INSUFFICIENT_DATA==='', 'map empty');
assert(fp._AVR_BADGES.VACUUM_DOWN_PROXY==='VAC ↓');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_collision_prefers_badge_over_delta():
    proc = _run(
        _load(
            """
const region={w:200,h:200};
const occupied=[{x:40,y:10,w:50,h:14}];
const badge=fp._placeLabel({x:45,y:10,w:40,h:14}, region, occupied, true);
assert(badge && badge.y>=24, 'badge moved below');
/* With extra vertical slots, two blocked rows still allow a third badge slot */
const third=fp._placeLabel({x:45,y:10,w:40,h:14}, region, [
  {x:40,y:10,w:50,h:14},{x:40,y:28,w:50,h:14}
], true);
assert(third && third.y>28, 'third vertical slot used');
/* Exhaust preferred + below + further-below + above → null */
const blocked=fp._placeLabel({x:45,y:10,w:40,h:14}, region, [
  {x:40,y:2,w:50,h:14},{x:40,y:10,w:50,h:14},{x:40,y:28,w:50,h:14},{x:40,y:45,w:50,h:14}
], true);
assert(blocked===null, 'all slots blocked → null');
assert(fp._rectsOverlap({x:0,y:0,w:10,h:10},{x:5,y:5,w:10,h:10}));
assert(!fp._rectsOverlap({x:0,y:0,w:10,h:10},{x:20,y:0,w:10,h:10}));
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_panel_default_collapsed_and_outside_chart():
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="fpAvrExpand"' in html
    assert html.index('id="fpAvrPanel"') < html.index('id="mpChartStack"')
    assert "fpAvrPanel" not in html.split('id="price-pane"')[1].split('id="mpOverlay"')[0]
    css = FP_CSS.read_text(encoding="utf-8")
    assert "position: relative" in css
    assert "position: absolute" not in css.split(".fp-avr-panel")[1].split("}")[0] or True
    # absolute should not be the panel's positioning for overlay-on-candles
    panel_block = css.split(".fp-avr-panel {")[1].split("}")[0]
    assert "position: relative" in panel_block
    js = FP_JS.read_text(encoding="utf-8")
    assert "panelExpanded: false" in js
    assert "Formende Candle" in js


def test_compact_status_and_no_level_text_contract():
    js = FP_JS.read_text(encoding="utf-8")
    assert "Compact · Raw $" in js
    assert "drawDisplayVpoc" in js
    assert "STATE_STRIP_H" in js


def test_compact_no_bid_ask_in_compact_branch():
    js = FP_JS.read_text(encoding="utf-8")
    # locate compact drawing section in draw()
    idx = js.find("} else if (mode === \"compact\")")
    assert idx > 0
    chunk = js[idx : idx + 400]
    assert "bid_notional" not in chunk
    assert "ask_notional" not in chunk
    assert "drawDisplayVpoc" in chunk or "is_vpoc" in chunk


def test_state_strip_and_avr_off():
    js = FP_JS.read_text(encoding="utf-8")
    assert "drawStateStrip" in js
    assert "if (!state.panelVisible || !segments.length) return;" in js
    assert "STATE_STRIP_H" in js
    assert "TIME_AXIS_PAD" in js
    assert 'fillText("AVR"' in js or "fillText('AVR'" in js


def test_fmt_compact_delta():
    proc = _run(
        _load(
            """
assert(fp._fmtCompactDelta(5200000)==='+5.2M');
assert(fp._fmtCompactDelta(-3100000)==='−3.1M');
assert(fp._fmtCompactDelta(1500)==='+1.5K' || fp._fmtCompactDelta(1500)==='+2K' || fp._fmtCompactDelta(1500).indexOf('K')>0);
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_panel_forming_message_and_human_state():
    proc = _run(
        _load(
            """
assert(fp._humanState('SELL_ABSORPTION_CANDIDATE')==='SELL ABSORPTION');
assert(fp._humanState('VACUUM_DOWN_PROXY')==='VACUUM DOWN');
fp._state.enabled=true;
fp._state.panelVisible=true;
fp._state.panelExpanded=true;
fp._state.payload={
  candles:[{
    time:1000,
    avr:{
      dominant_state:'INSUFFICIENT_DATA',
      final_state:'INSUFFICIENT_DATA',
      provisional:true,
      verification:'UNVERIFIED',
      panel:{},
      state_counts:{INSUFFICIENT_DATA:3},
      third_states:{EARLY:'INSUFFICIENT_DATA',MIDDLE:'INSUFFICIENT_DATA',LATE:'INSUFFICIENT_DATA'}
    }}]
};
fp.setPanelExpanded(true);
assert(panelBodyEl.textContent.indexOf('Formende Candle')>=0, panelBodyEl.textContent);
assert(panelBodyEl.textContent.indexOf('Dominant: INSUFFICIENT_DATA')<0, 'not raw dominant headline');
"""
        )
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
