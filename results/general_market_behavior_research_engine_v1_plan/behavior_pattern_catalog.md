# Behavior Pattern Catalog V1 — candidates (not trading signals)

| ID | Pattern | Measurability with our data | Label honesty |
|----|---------|-----------------------------|---------------|
| P01 | Buyer control | High taker buy + bid refill / ask consume + mid up | PROXY “apparent control” |
| P02 | Seller control | Mirror | PROXY |
| P03 | Buy absorption | High taker buy + small up-move + ask persist/refill | PROXY |
| P04 | Sell absorption | Mirror | PROXY |
| P05 | Ask liquidity vacuum | Ask near-touch depth collapse without matching sells alone | PARTIAL exact (depth) |
| P06 | Bid liquidity vacuum | Mirror | PARTIAL exact |
| P07 | Defended wall / refill | OB Fight refill sequences | HEURISTIC reusable |
| P08 | Wall consumption | Trade∩wall size drop | EXECUTED_LIKELY |
| P09 | Wall pull | Size drop w/o trades | CANCEL_LIKELY |
| P10 | Wall migration | Level shift tracking | HEURISTIC |
| P11 | Cancel wave | Broad CANCEL_LIKELY surge both sides | PROXY |
| P12 | Liquidation cascade | Clustered forced flows + mid move | Measurable events; cause PROXY |
| P13 | OI-driven breakout | ΔOI + range break | PROXY (OI ambiguity) |
| P14 | Covering move | Price vs OI quadrant + liq | PROXY |
| P15 | Compression→expansion | Vol regime transition | Measurable |
| P16 | Aggression w/o progress | High |delta| + tiny |return| | Measurable |
| P17 | Side exhaustion | Declining aggression + adverse refill | PROXY |
| P18 | Balanced / unclear | Default when gates fail | REQUIRED class |

Episode start = detector on past/present only. Outcomes attached later offline.
