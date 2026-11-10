#!/usr/bin/env python3
"""CAGR, max drawdown % and calendar-year returns from hedged_backtest.py output.

All P&L in the backtests is in USD with no capital base. Here every variant is
put on the same account:

* ACCOUNT = $30,000 -- enough to fully cash-secure 0.25 BTC puts at the highest
  strike in the sample ($115k x 0.25 = $28.75k). Equity = ACCOUNT + cumulative
  daily mark-to-market P&L. Max DD % is measured from the running equity peak.
* "CAGR fully funded" instead uses capital = the largest cash-secured notional
  the variant ever needed (size x strike), i.e. no leverage at any point.
* "max leverage" = largest (size x strike) / account equity at entry. Above 1x
  the puts are no longer cash-secured on the $30k account (margin is needed).
"""
from __future__ import annotations

import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
ACCOUNT = 30_000.0
STRIKES = ["ATM", "5% OTM", "10% OTM", "15% OTM"]
HEDGES = ["none", "stop-10% hold", "stop-10%/-5%", "delta-daily", "delta@-10%"]

eq = pd.read_csv(os.path.join(OUT, "hedged_equity_daily.csv"), parse_dates=["time"]).set_index("time")
tr = pd.read_csv(os.path.join(OUT, "hedged_trades.csv"), parse_dates=["entry", "expiry"])
start, end = pd.Timestamp("2021-12-31 08:00"), eq.index.max()
years = (end - start) / pd.Timedelta(days=365.25)

rows, yearly = [], []
for col in eq.columns:
    tenor, strike, hedge, sizing = col.split("|")
    pnl = eq[col].dropna()
    pnl.loc[start] = 0.0
    pnl = pnl.sort_index()
    equity = ACCOUNT + pnl
    dd_pct = (equity / equity.cummax() - 1).min()
    dd_usd = (equity - equity.cummax()).min()
    t = tr[(tr.tenor == tenor) & (tr.strike_rule == strike) & (tr.hedge == hedge) & (tr.sizing == sizing)]
    eq_at_entry = ACCOUNT + pnl.reindex(t.entry, method="ffill").to_numpy()
    lev = (t.notional_usd.to_numpy() / eq_at_entry).max()
    peak_notional = t.notional_usd.max()
    final = equity.iloc[-1]
    rows.append({
        "tenor": tenor, "strike": strike, "hedge": hedge, "sizing": sizing,
        "total P&L $": round(pnl.iloc[-1]),
        "CAGR on $30k": (final / ACCOUNT) ** (1 / years) - 1 if equity.min() > 0 else -1.0,
        "max DD %": dd_pct if equity.min() > 0 else -1.0, "max DD $": round(dd_usd),
        "min equity $": round(equity.min()),
        "account wiped out": bool(equity.min() <= 0),
        "max leverage (notional/equity)": round(lev, 1),
        "peak notional $": round(peak_notional),
        "CAGR fully funded": (max(1 + pnl.iloc[-1] / peak_notional, 0.0)) ** (1 / years) - 1,
    })
    ye = pnl.groupby(pnl.index.year).last()
    prev = pd.concat([pd.Series({2021: 0.0}), ye]).shift(1).loc[ye.index]
    for y in ye.index:
        if y == 2021:
            continue
        yearly.append({"tenor": tenor, "strike": strike, "hedge": hedge, "sizing": sizing, "year": y,
                       "P&L $": round(ye[y] - prev[y]),
                       "return on $30k + prior P&L": (ye[y] - prev[y]) / (ACCOUNT + prev[y])})

summ = pd.DataFrame(rows)
summ.to_csv(os.path.join(OUT, "cagr_summary.csv"), index=False, float_format="%.4f")
yr = pd.DataFrame(yearly)
yr.to_csv(os.path.join(OUT, "yearly_returns.csv"), index=False, float_format="%.4f")

print(f"period {start.date()} -> {end.date()} = {years:.2f} years, account ${ACCOUNT:,.0f}\n")


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def main_table(sizing: str, hedges=("none", "delta-daily")) -> str:
    out = ["| Tenor | Strike | " + " | ".join(f"{h}: total P&L | CAGR | max DD" for h in hedges) + " |",
           "|---|---|" + "---:|---:|---:|" * len(hedges)]
    for tenor in ("1W", "1M", "3M"):
        for st in STRIKES:
            cells = []
            for h in hedges:
                r = summ[(summ.tenor == tenor) & (summ.strike == st) & (summ.hedge == h) & (summ.sizing == sizing)].iloc[0]
                cells += [f"${r['total P&L $']:,}", pct(r["CAGR on $30k"]), pct(r["max DD %"])]
            out.append(f"| {tenor} | {st} | " + " | ".join(cells) + " |")
    return "\n".join(out)


for sizing in ("base+cover", "fixed"):
    print(f"## {sizing}\n" + main_table(sizing) + "\n")

sub = summ[summ.hedge.isin(["none", "delta-daily"])]
print(sub[["tenor", "strike", "hedge", "sizing", "max DD $", "min equity $", "max leverage (notional/equity)",
           "peak notional $", "CAGR fully funded"]].to_string(index=False))
piv = yr[yr.hedge.isin(["none", "delta-daily"])].pivot_table(
    index=["tenor", "strike", "hedge", "sizing"], columns="year", values="P&L $")
print(piv.to_string())
pivp = yr[yr.hedge.isin(["none", "delta-daily"])].pivot_table(
    index=["tenor", "strike", "hedge", "sizing"], columns="year", values="return on $30k + prior P&L")
print((pivp * 100).round(1).to_string())
print(summ[summ["account wiped out"]][["tenor", "strike", "hedge", "sizing", "min equity $"]].to_string(index=False))
print(summ[summ.hedge.isin(["stop-10% hold", "stop-10%/-5%", "delta@-10%"]) & ~summ["account wiped out"]]
      [["tenor", "strike", "hedge", "sizing", "total P&L $", "CAGR on $30k", "max DD %"]].round(3).to_string(index=False))
