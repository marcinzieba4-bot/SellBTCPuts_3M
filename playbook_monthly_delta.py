#!/usr/bin/env python3
"""
Detail for the monthly 'sell put only after a BIG candle + delta hedge' strategy,
ATM and 10% OTM, fixed 0.25 lots vs the recovery rule (0.25 + deficit/premium).
Writes results/playbook_monthly_delta.csv and _yearly.csv, prints the numbers.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

import backtest as bt
import hedged_backtest as hb
from candle_patterns import ACCOUNT, candle

OUT = bt.OUT
BIG = 0.10
STRIKES = {0.00: "ATM", 0.10: "10% OTM"}
VARIANTS = {  # name -> (hedge on big-RED trades, hedge on big-GREEN trades)
    "no hedge": ("none", "none"),
    "daily delta, all trades": ("delta-daily", "delta-daily"),
    "daily delta, RED-candle trades only": ("delta-daily", "none"),
    "-10% then daily delta, all trades": ("delta@-10%", "delta@-10%"),
    "-10% then daily delta, RED only": ("delta@-10%", "none"),
}

inst, px = bt.load_instruments(), bt.load_delivery_prices()
perp, dvol, fund = hb.load_perp_1h(), hb.load_dvol_1h(), hb.load_funding_1h()
m = hb.load_perp_1m()
pairs = hb.cycle_expiries(inst, "1M")
exps = [pd.Timestamp(e) for e in sorted(inst.expiry.unique())]
prev = {b: a for a, b in zip(exps[:-1], exps[1:])}
prev[exps[0]] = exps[0] - pd.Timedelta(days=28)
ret = {t0: candle(m, max(prev[t0], pd.Timestamp("2021-12-01")), t0)["ret"] for t0, _ in pairs}
start = pd.Timestamp("2021-12-31 08:00")

rows, yrows, trows = [], [], []
for otm, strike in STRIKES.items():
    legs = {}
    for t0, t1 in pairs:
        c, q, s0 = hb.pick_put(inst, px, t0, t1, otm)
        for h in ("none", "delta-daily", "delta@-10%"):
            legs[(h, t0)] = hb.run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, h)
    for vname, (h_red, h_green) in VARIANTS.items():
        sel = [legs[(h_red if ret[t0] < 0 else h_green, t0)] for t0, _ in pairs if abs(ret[t0]) > BIG]
        for mode, mlabel in (("fixed", "fixed 0.25 lots"), ("base+cover", "recovery rule")):
            tr, eq = hb.simulate([c for c, _ in sel], [p for _, p in sel], mode)
            daily = eq[eq.index.hour == 8]
            equity = ACCOUNT + pd.concat([pd.Series({start: 0.0}), daily]).sort_index()
            years = (equity.index[-1] - start) / pd.Timedelta(days=365.25)
            dd = equity / equity.cummax() - 1
            s = tr[tr.settled]
            hedged = tr[tr.hedge_turnover_btc > 0]
            rows.append({
                "strike": strike, "hedge": vname, "sizing": mlabel, "trades": len(tr),
                "total P&L $": round(eq.iloc[-1]),
                "CAGR": (equity.iloc[-1] / ACCOUNT) ** (1 / years) - 1,
                "max DD %": dd.min(), "max DD date": dd.idxmin().date(),
                "win rate": (s.pnl > 0).mean(), "worst trade $": round(s.pnl.min()),
                "best trade $": round(s.pnl.max()),
                "max size (lots)": tr.size_lots.max(), "max notional $": round(tr.notional_usd.max()),
                "hedge P&L $": round((tr.size_lots * tr.hedge_pnl).sum()),
                "hedge fees $": round((tr.size_lots * tr.hedge_fees).sum()),
                "hedged trades": len(hedged),
            })
            ye = equity.groupby(equity.index.year).last()
            prv = pd.concat([pd.Series({2020: ACCOUNT}), ye]).shift(1).loc[ye.index]
            for y in ye.index:
                if y >= 2022:
                    yrows.append({"strike": strike, "hedge": vname, "sizing": mlabel, "year": y,
                                  "return": ye[y] / (prv[y] if y > 2022 else ACCOUNT) - 1})
            if mode == "fixed" and vname in ("no hedge", "daily delta, all trades", "daily delta, RED-candle trades only"):
                for _, r in tr.iterrows():
                    trows.append({"strike": strike, "hedge": vname, "entry": r.entry.date(),
                                  "last candle": ret[r.entry], "strike $": r.strike, "spot": round(r.spot_entry),
                                  "premium/lot $": round(r.prem_usd), "option P&L/lot $": round(r.option_pnl),
                                  "hedge P&L/lot $": round(r.hedge_pnl), "total P&L @0.25 $": round(r.pnl)})

res = pd.DataFrame(rows)
res.to_csv(os.path.join(OUT, "playbook_monthly_delta.csv"), index=False, float_format="%.4f")
yr = pd.DataFrame(yrows)
yr.to_csv(os.path.join(OUT, "playbook_monthly_delta_yearly.csv"), index=False, float_format="%.4f")
pd.DataFrame(trows).to_csv(os.path.join(OUT, "playbook_monthly_delta_trades.csv"), index=False, float_format="%.4f")
pd.set_option("display.width", 260)
print(res.round(3).to_string(index=False))
print((yr.pivot_table(index=["strike", "hedge", "sizing"], columns="year", values="return") * 100).round(1).to_string())
# typical hedge size at entry for 0.25 lots (delta of the put at entry)
for otm, strike in STRIKES.items():
    ds = []
    for t0, t1 in pairs:
        if abs(ret[t0]) > BIG:
            c, q, s0 = hb.pick_put(inst, px, t0, t1, otm)
            T = (t1 - t0) / pd.Timedelta(hours=1) / hb.YEAR_H
            iv = hb.implied_vol(q["premium_usd"], q["index"], c.strike, T)
            ds.append(-float(hb.bs_put_delta(q["index"], c.strike, T, iv)))
    print(f"{strike}: put delta at entry median {np.median(ds):.2f} (range {min(ds):.2f}-{max(ds):.2f})"
          f" -> initial hedge for 0.25 lots = short {0.25 * np.median(ds):.3f} BTC")
# where are we now
last_exp = max(t1 for _, t1 in pairs if t1 <= hb.NOW)
print("last settled expiry", last_exp, "price", px[last_exp.normalize()])
