"""v2 bet decision + safety stack.

Modules land here in P4: sizing (fractional Kelly), filters (NaN/min/max guards),
circuit_breaker (daily/weekly stop-loss), kill_switch, audit (counterfactual
sweep), clv (internal closing-line value tracking).
"""
