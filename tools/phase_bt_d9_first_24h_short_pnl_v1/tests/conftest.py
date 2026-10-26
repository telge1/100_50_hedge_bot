import sys
from pathlib import Path

BT_D9 = Path(__file__).resolve().parents[1]
if str(BT_D9) not in sys.path:
    sys.path.insert(0, str(BT_D9))
