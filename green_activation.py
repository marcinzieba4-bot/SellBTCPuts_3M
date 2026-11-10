#!/usr/bin/env python3
"""
Monthly 'sell put after a BIG candle' with daily delta hedge on RED-candle
trades (Playbook). Question: after a big GREEN candle the trade is unhedged -
should a daily delta hedge switch on if BTC then falls X% below the entry price?

For trades after a big GREEN candle, compare:
    no hedge (playbook) | daily delta from day 1 | daily delta activated at -X%
for X in ACTIVATION, with RED-candle trades always delta-hedged from day 1.
Activation = first 1-minute bar trading X% below the entry price; from then on
the position is delta-hedged daily at 08:00 UTC until expiry (never switched off).

Strikes: 10% OTM (main) and ATM (robustness). Fixed 0.25 lots and recovery rule.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

import backtest as bt
import hedged_backtest as hb
from candle_patterns import ACCOUNT, SPLIT, candle

OUT = bt.OUT
BIG = 0.10
ACTIVATION = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15]
STRIKES = {0.10: "10% OTM", 0.00: "ATM"}
start = pd.Timestamp("2021-12-31 08:00")

inst, px = bt.load_instruments(), bt.load_delivery_prices()
perp, dvol, fund = hb.load_perp_1h(), hb.load_dvol_1h(), hb.load_funding_1h()
m = hb.load_perp_1m()
pairs = hb.cycle_expiries(inst, "1M")
exps = [pd.Timestamp(e) for e in sorted(inst.expiry.unique())]
prev = {b: a for a, b in zip(exps[:-1], exps[1:])}
prev[exps[0]] = exps[0] - pd.Timedelta(days=28)
ret = {t0: candle(m, max(prev[t0], pd.Timestamp("2021-12-01")), t0)["ret"] for t0, _ in pairs}
trade_t0 = [t0 for t0, _ in pairs if abs(ret[t0]) > BIG]
green = {t0 for t0 in trade_t0 if ret[t0] > 0}


def evaluate(sel, mode):
    tr, eq = hb.simulate([c for c, _ in sel], [p for _, p in sel], mode)
    daily = eq[eq.index.hour == 8]
    equity = ACCOUNT + pd.concat([pd.Series({start: 0.0}), daily]).sort_index()
    years = (equity.index[-1] - start) / pd.Timedelta(days=365.25)
    dd = equity / equity.cummax() - 1
    return tr, {"CAGR": (equity.iloc[-1] / ACCOUNT) ** (1 / years) - 1, "max DD %": dd.min(),
                "max DD date": dd.idxmin().date(), "total P&L $": round(eq.iloc[-1]),
                "max size": tr.size_lots.max()}


rows, detail = [], []
orig_trigger = hb.TRIGGER
for otm, strike in STRIKES.items():
    legs = {}
    for t0, t1 in pairs:
        if t0 not in trade_t0:
            continue
        c, q, s0 = hb.pick_put(inst, px, t0, t1, otm)
        legs[("none", t0)] = hb.run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, "none")
        legs[("delta-daily", t0)] = hb.run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, "delta-daily")
        for x in ACTIVATION:
            hb.TRIGGER = x
            legs[(f"act{x}", t0)] = hb.run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, "delta@-10%")
        hb.TRIGGER = orig_trigger
        # how far did BTC fall below entry during the life of a green-candle trade?
        if t0 in green:
            life = perp.loc[t0:min(t1, hb.NOW)]
            detail.append({"strike": strike, "entry": t0.date(), "last candle": ret[t0],
                           "min price vs entry": life.min() / life.iloc[0] - 1,
                           "price at expiry vs entry": life.iloc[-1] / life.iloc[0] - 1,
                           "unhedged P&L @0.25 $": 0.25 * legs[("none", t0)][0]["pnl_per_lot"],
                           **{f"act -{int(x*100)}% P&L @0.25 $": 0.25 * legs[(f"act{x}", t0)][0]["pnl_per_lot"]
                              for x in ACTIVATION}})
    variants = {"green: no hedge (playbook)": "none", "green: daily delta from day 1": "delta-daily",
                **{f"green: delta hedge activated at -{int(x*100)}%": f"act{x}" for x in ACTIVATION}}
    for vname, gkey in variants.items():
        sel = [legs[(gkey if t0 in green else "delta-daily", t0)] for t0 in trade_t0]
        g_sel = [legs[(gkey, t0)] for t0 in trade_t0 if t0 in green]
        for mode, mlabel in (("fixed", "fixed 0.25"), ("base+cover", "recovery rule")):
            tr, st = evaluate(sel, mode)
            h1 = tr[tr.entry < SPLIT].pnl.sum()
            h2 = tr[tr.entry >= SPLIT].pnl.sum()
            rows.append({"strike": strike, "variant": vname, "sizing": mlabel, **st,
                         "P&L 1st half $": round(h1), "P&L 2nd half $": round(h2),
                         "green trades": len(g_sel),
                         "green trades hedged": sum(c["hedge_turnover_btc"] > 0 for c, _ in g_sel),
                         "green trades P&L @0.25 $": round(sum(0.25 * c["pnl_per_lot"] for c, _ in g_sel)),
                         "green losing trades": sum(c["pnl_per_lot"] < 0 for c, _ in g_sel)})

res = pd.DataFrame(rows)
res.to_csv(os.path.join(OUT, "green_activation.csv"), index=False, float_format="%.4f")
det = pd.DataFrame(detail)
det.to_csv(os.path.join(OUT, "green_activation_trades.csv"), index=False, float_format="%.4f")
pd.set_option("display.width", 260)
pd.set_option("display.max_columns", 30)
print(res.round(3).to_string(index=False))
print(det.round(3).to_string(index=False))
