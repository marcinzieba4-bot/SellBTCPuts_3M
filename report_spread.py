#!/usr/bin/env python3
"""Bid/ask sensitivity: how much P&L is lost if every put is sold 1 tick below
the Deribit mark price (premiums in the backtest are mark ~ mid).

Deribit BTC option tick: 0.0001 BTC for prices below 0.005 BTC, 0.0005 BTC above.
Cheap weekly options are hit hardest, because a tick is a large share of the premium.
"""
from __future__ import annotations

import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")

tr = pd.read_csv(os.path.join(OUT, "hedged_trades.csv"))
tr = tr[tr.hedge.isin(["none", "delta-daily"])].copy()
prem_btc = tr.prem_usd / tr.spot_entry
tick = prem_btc.where(prem_btc < 0.005, other=0).astype(bool).map({True: 0.0001, False: 0.0005})
tr["tick_cost"] = tr.size_lots * tick * tr.spot_entry
tr["tick_pct_of_premium"] = tick / prem_btc
g = tr.groupby(["tenor", "strike_rule", "hedge", "sizing"]).agg(
    total_pnl=("pnl", "sum"), cost_1_tick=("tick_cost", "sum"),
    tick_as_pct_of_premium=("tick_pct_of_premium", "median"), avg_otm=("otm_actual", "mean"),
    cycles=("pnl", "size"))
g["pnl_after_1_tick"] = g.total_pnl - g.cost_1_tick
g.round(3).to_csv(os.path.join(OUT, "spread_sensitivity.csv"))
pd.set_option("display.width", 200)
print(g.round(3).to_string())
