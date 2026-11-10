#!/usr/bin/env python3
"""
Playbook + protective put (put spread).

Playbook: monthly, sell the 10% OTM put only after a BIG candle (|move| > 10%),
daily delta hedge only on trades after a RED candle.
Here a further-OTM put (WING: 15/20/25/30% OTM, same expiry, real Deribit
premium) is BOUGHT against the sold put, which caps the loss per trade at
(K_sold - K_bought) - net premium.

Variants
  A  wing only on GREEN-candle trades; RED trades unchanged (naked + daily delta)
  B  wing on ALL trades; RED trades delta-hedge the spread's net delta daily
  C  wing on ALL trades; no delta hedging at all (the spread is the only protection)

Fees: option fee on both legs (no combo discount assumed), delivery fee on both
legs, perp fees/funding on the delta hedge. Daily mark-to-market as elsewhere.

Crash stress: the backtest had no crash after a green month, so the insurance
never paid. For every green-candle trade we also compute the loss if BTC had
instead fallen 28% by expiry (the worst monthly move in the sample, Jun 2022).
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
SOLD_OTM = 0.10
WINGS = [0.15, 0.20, 0.25, 0.30]
CRASH = -0.28
start = pd.Timestamp("2021-12-31 08:00")


def hedge_path(hours, s, net_delta, settled, fund):
    """Daily (08:00 UTC) delta hedge of a position with long delta `net_delta` per lot."""
    n = len(hours)
    is_0800 = hours.hour == 8
    trades, pos = [], 0.0
    for i in range(n - 1):
        if i == 0 or is_0800[i]:
            trades.append((i, -net_delta[i] - pos, s[i]))
            pos = -net_delta[i]
    if settled and abs(pos) > 1e-12:
        trades.append((n - 1, -pos, s[n - 1]))
    dh, cash, fee = np.zeros(n), np.zeros(n), np.zeros(n)
    for b, d, f in trades:
        dh[b] += d
        cash[b] -= d * f
        fee[b] += hb.PERP_FEE * abs(d) * f
    position = np.cumsum(dh)
    fr = fund.reindex(hours).fillna(0.0).to_numpy()
    fstep = np.zeros(n)
    fstep[:-1] = -position[:-1] * s[:-1] * fr[1:]
    fcum = np.concatenate([[0.0], np.cumsum(fstep)[:-1]])
    return np.cumsum(cash) - np.cumsum(fee) + position * s + fcum, float(fee.sum())


def spread_cycle(legs, s0, t0, t1, px, perp, dvol, fund, hedge):
    """legs: list of (sign, strike, quote); sign -1 = sold, +1 = bought. Per-lot path."""
    hours = perp.loc[t0:min(t1, hb.NOW)].index
    s = perp.loc[hours].to_numpy()
    tau = ((t1 - hours) / pd.Timedelta(hours=1)).to_numpy() / hb.YEAR_H
    T = (t1 - t0) / pd.Timedelta(hours=1) / hb.YEAR_H
    dv = dvol.reindex(hours).ffill().bfill().to_numpy()
    settled = t1 <= hb.NOW
    st = float(px[t1.normalize()]) if settled else float("nan")
    mtm = np.zeros(len(hours))
    net_delta = np.zeros(len(hours))
    credit = fees = 0.0
    for sign, k, q in legs:
        prem = q["premium_usd"]
        iv = hb.implied_vol(prem, q["index"], k, T) * dv / dv[0]
        val = hb.bs_put(s, k, tau, iv)
        fee_e = min(bt.OPT_FEE_RATE * s0, bt.FEE_CAP * prem)
        if settled:
            payout = max(k - st, 0.0)
            fee_s = min(bt.DELIVERY_FEE_RATE * st, bt.FEE_CAP * payout) if payout > 0 else 0.0
            val[-1] = payout + (fee_s if sign < 0 else -fee_s)
        # sold: +prem - value ; bought: -prem + value ; fees always paid
        mtm += -sign * prem + sign * val - fee_e
        net_delta += sign * hb.bs_put_delta(s, k, tau, iv)  # long put delta < 0; sold -> +|delta|
        credit += -sign * prem
        fees += fee_e
    hedge_fees = 0.0
    if hedge == "delta-daily":
        hcum, hedge_fees = hedge_path(hours, s, net_delta, settled, fund)
        mtm = mtm + hcum
    path = pd.Series(mtm, index=hours)
    strikes = sorted(k for _, k, _ in legs)
    return dict(entry=t0, expiry=t1, strike=max(strikes), wing=min(strikes) if len(legs) > 1 else np.nan,
                spot_entry=s0, spot_expiry=st, settled=settled, prem_usd=credit, fee_entry=fees,
                hedge_fees=hedge_fees, hedge_turnover_btc=1.0 if hedge != "none" else 0.0,
                pnl_per_lot=float(path.iloc[-1])), path


def pick_put_otm(inst, px, t0, t1, otm):
    c, q, s0 = hb.pick_put(inst, px, t0, t1, otm)
    return float(c.strike), q, s0


def evaluate(sel, mode):
    tr, eq = hb.simulate([c for c, _ in sel], [p for _, p in sel], mode)
    daily = eq[eq.index.hour == 8]
    equity = ACCOUNT + pd.concat([pd.Series({start: 0.0}), daily]).sort_index()
    years = (equity.index[-1] - start) / pd.Timedelta(days=365.25)
    dd = equity / equity.cummax() - 1
    s = tr[tr.settled]
    return {"CAGR": (equity.iloc[-1] / ACCOUNT) ** (1 / years) - 1, "max DD %": dd.min(),
            "max DD date": dd.idxmin().date(), "total P&L $": round(eq.iloc[-1]),
            "worst trade $": round(s.pnl.min()), "win rate": (s.pnl > 0).mean(),
            "P&L 1st half $": round(tr[tr.entry < SPLIT].pnl.sum()),
            "P&L 2nd half $": round(tr[tr.entry >= SPLIT].pnl.sum()), "max size": tr.size_lots.max()}


def main():
    inst, px = bt.load_instruments(), bt.load_delivery_prices()
    perp, dvol, fund = hb.load_perp_1h(), hb.load_dvol_1h(), hb.load_funding_1h()
    m = hb.load_perp_1m()
    pairs = hb.cycle_expiries(inst, "1M")
    exps = [pd.Timestamp(e) for e in sorted(inst.expiry.unique())]
    prev = {b: a for a, b in zip(exps[:-1], exps[1:])}
    prev[exps[0]] = exps[0] - pd.Timedelta(days=28)
    ret = {t0: candle(m, max(prev[t0], pd.Timestamp("2021-12-01")), t0)["ret"] for t0, _ in pairs}
    trades = [(t0, t1) for t0, t1 in pairs if abs(ret[t0]) > BIG]

    legs = {}  # (variant key, t0) -> (cyc, path)
    stress = []
    for t0, t1 in trades:
        k1, q1, s0 = pick_put_otm(inst, px, t0, t1, SOLD_OTM)
        for h in ("none", "delta-daily"):
            legs[(f"naked|{h}", t0)] = spread_cycle([(-1, k1, q1)], s0, t0, t1, px, perp, dvol, fund, h)
        row = {"entry": t0.date(), "candle": "green" if ret[t0] > 0 else "red", "spot": round(s0),
               "sold strike": k1, "sold premium/lot $": round(q1["premium_usd"])}
        st = s0 * (1 + CRASH)
        fee1 = min(bt.OPT_FEE_RATE * s0, bt.FEE_CAP * q1["premium_usd"])
        row["naked loss @0.25 if -28% $"] = round(0.25 * (q1["premium_usd"] - fee1 - max(k1 - st, 0)))
        for w in WINGS:
            k2, q2, _ = pick_put_otm(inst, px, t0, t1, w)
            for h in ("none", "delta-daily"):
                legs[(f"wing{w}|{h}", t0)] = spread_cycle([(-1, k1, q1), (1, k2, q2)], s0, t0, t1,
                                                          px, perp, dvol, fund, h)
            fee2 = min(bt.OPT_FEE_RATE * s0, bt.FEE_CAP * q2["premium_usd"])
            row[f"wing {int(w*100)}% strike"] = k2
            row[f"wing {int(w*100)}% cost/lot $"] = round(q2["premium_usd"])
            row[f"spread {int(w*100)}% loss @0.25 if -28% $"] = round(
                0.25 * (q1["premium_usd"] - q2["premium_usd"] - fee1 - fee2 - max(k1 - st, 0) + max(k2 - st, 0)))
        stress.append(row)

    green = {t0 for t0, _ in trades if ret[t0] > 0}
    variants = {"playbook (no wing)": lambda t0: ("naked|none" if t0 in green else "naked|delta-daily")}
    for w in WINGS:
        tag = f"{int(w*100)}% OTM wing"
        variants[f"A: {tag} on GREEN trades only (+delta on RED)"] = (
            lambda t0, w=w: f"wing{w}|none" if t0 in green else "naked|delta-daily")
        variants[f"B: {tag} on ALL trades (+delta on RED)"] = (
            lambda t0, w=w: f"wing{w}|none" if t0 in green else f"wing{w}|delta-daily")
        variants[f"C: {tag} on ALL trades, no delta hedge"] = lambda t0, w=w: f"wing{w}|none"
    variants["naked, no hedge at all (Part 5)"] = lambda t0: "naked|none"

    rows = []
    for name, choose in variants.items():
        sel = [legs[(choose(t0), t0)] for t0, _ in trades]
        g_sel = [legs[(choose(t0), t0)] for t0, _ in trades if t0 in green]
        for mode, ml in (("fixed", "fixed 0.25"), ("base+cover", "recovery rule")):
            rows.append({"variant": name, "sizing": ml, **evaluate(sel, mode),
                         "green-trade P&L @0.25 $": round(sum(0.25 * c["pnl_per_lot"] for c, _ in g_sel))})
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(OUT, "put_spread.csv"), index=False, float_format="%.4f")
    st = pd.DataFrame(stress)
    st.to_csv(os.path.join(OUT, "put_spread_stress.csv"), index=False)
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 40)
    print(res.round(3).to_string(index=False))
    g = st[st.candle == "green"]
    cols = ["naked loss @0.25 if -28% $"] + [f"spread {int(w*100)}% loss @0.25 if -28% $" for w in WINGS]
    print("\nGREEN trades, loss at 0.25 lots if BTC had fallen 28% by expiry:")
    print(g[cols].agg(["mean", "min"]).round(0).to_string())
    print("wing cost per lot (avg $):", {w: round(g[f"wing {int(w*100)}% cost/lot $"].mean()) for w in WINGS},
          " sold premium avg $", round(g["sold premium/lot $"].mean()))
    print(st.to_string(index=False))


if __name__ == "__main__":
    main()
