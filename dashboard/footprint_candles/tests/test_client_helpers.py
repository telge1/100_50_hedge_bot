"""Client-side range helpers (Node) + default-off / forming contracts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
FP_JS = DASHBOARD_DIR / "footprint_candles" / "static" / "footprint_candles.js"
PAGE = DASHBOARD_DIR / "templates" / "market_profile_v1.html"
MP_JS = DASHBOARD_DIR / "static" / "market_profile_v1" / "app.js"


def test_node_syntax_check():
    proc = subprocess.run(
        ["node", "--check", str(FP_JS)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_forming_window_is_exactly_one_5m_bucket():
    proc = subprocess.run(
        [
            "node",
            "-e",
            f"""
const fs=require('fs');const vm=require('vm');
const code=fs.readFileSync({FP_JS.as_posix()!r},'utf8');
const sandbox={{window:{{addEventListener(){{}}}},document:{{addEventListener(){{}},hidden:false}},console}};
vm.createContext(sandbox);vm.runInContext(code,sandbox);
const fp=sandbox.window.FootprintCandles;
const now=1757156400+123;
const w=fp._formingCandleWindow(now);
if(w.to-w.from!==300) process.exit(2);
if(w.from!==Math.floor(now/300)*300) process.exit(3);
const c=fp._clampQueryWindow(0,7*3600);
if(c.to-c.from!==6*3600) process.exit(4);
if(fp.isEnabled()) process.exit(5);
if(fp._state.enabled) process.exit(6);
console.log('ok');
""",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "ok" in proc.stdout


def test_default_off_html_and_no_eager_fetch_strings():
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="mpTimeframe"' in html
    assert "15m" in html
    m = re.search(r'<input[^>]*id="mpShowFootprint"[^>]*>', html)
    assert m and "checked" not in m.group(0)
    js = FP_JS.read_text(encoding="utf-8")
    assert "formingCandleWindow" in js
    assert "now - 600" not in js
    assert "document.hidden" in js
    assert "pagehide" in js
    mp = MP_JS.read_text(encoding="utf-8")
    assert "/api/footprint-candles" not in mp
