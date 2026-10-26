import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
D3 = TOOLS.parent / "phase_bt_d3_canonical_depth2_provider_v1"
for p in (str(TOOLS), str(D3)):
    if p not in sys.path:
        sys.path.insert(0, p)
