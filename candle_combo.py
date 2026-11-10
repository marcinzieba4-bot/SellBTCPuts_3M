#!/usr/bin/env python3
"""
Part 6: combine the big-candle filter (Part 5) with hedges, and test BUYING
calls after a small candle.

A) Sell puts only after a BIG last candle (1W |ret|>5%, 1M |ret|>10%) with each
   hedge from Parts 2-4:
     none | stop-10% hold | stop-10%/-5% | delta-daily | delta@-10% (= -10% then daily delta hedge)
   plus "hedge only the trades after a big RED candle" (crash continuation is the
   main risk there), trades after a big GREEN candle stay unhedged.
B) BUY calls (ATM / 5% / 10% OTM, 0.25 lots) only after a SMALL last candle
   (quiet market -> cheap premium -> breakout?). Real Deribit call premiums
   (mark price in the first hours after expiry), same fees, daily mark-to-market.
   Also: buy ATM puts / ATM straddles after a small candle, for comparison.
C) Always in the market: sell puts after a big candle + buy calls after a small one.

Run:  python3 candle_combo.py prefetch   (download call quotes once, ~15 min)
      python3 candle_combo.py            (all results)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

import backtest as bt
import hedged_backtest as hb
from candle_patterns import ACCOUNT, BIG, SPLIT, STRIKES, candle, equity_stats, perm_p

OUT, DATA = bt.OUT, bt.DATA
CALL_OTM = {0.00: "ATM", 0.05: "5% OTM", 0.10: "10% OTM"}
HEDGES = ["none", "stop-10% hold", "stop-10%/-5%", "delta-daily", "delta@-10%"]
YEAR_H = hb.YEAR_H


def load_calls() -> pd.DataFrame:
    """All weekly + monthly BTC calls (Friday 08:00 expiries) from Dec 2021."""
    path = os.path.join(DATA, "btc_calls.csv")
    if not os.path.exists(path):
        rows = []
        for base, expired in ((bt.HIST_API, "true"), (bt.MAIN_API, "false")):
            for x in bt.get(base, "get_instruments", currency="BTC", kind="option", expired=expired):
                if x["option_type"] == "call" and x["settlement_period"] in ("week", "month"):
                    rows.append(dict(instrument=x["instrument_name"], strike=x["strike"],
                                     expiry=pd.to_datetime(x["expiration_timestamp"], unit="ms"),
                                     created=pd.to_datetime(x["creation_timestamp"], unit="ms")))
        df = pd.DataFrame(rows).drop_duplicates("instrument")
        df = df[(df.expiry >= bt.START) & (df.expiry.dt.hour == 8) & (df.expiry.dt.minute == 0)
                & (df.expiry.dt.dayofweek == 4)]
        df.sort_values(["expiry", "strike"]).to_csv(path, index=False)
    return pd.read_csv(path, parse_dates=["expiry", "created"])


def calls_for(inst_calls: pd.DataFrame, tenor: str) -> pd.DataFrame:
    if tenor == "1W":
        return inst_calls
    monthly = set(bt.load_instruments().expiry)  # monthly expiries only
    return inst_calls[inst_calls.expiry.isin(monthly)]


def pick_call(inst, px, t0, t1, otm):
    s0 = float(px[t0.normalize()])
    cands = inst[(inst.expiry == t1) & (inst.created <= t0)].copy()
    cands["dist"] = (cands.strike - s0 * (1 + otm)).abs()
    for _, c in cands.sort_values(["dist", "strike"]).head(6).iterrows():
        q = bt.entry_quote(c.instrument, t0)
        if q:
            return c, q, s0
    raise RuntimeError(f"no tradable call for {t1.date()} otm={otm}")


def bs_call(s, k, t, v):
    return hb.bs_put(s, k, t, v) + np.asarray(s) - k


def implied_vol_call(price, s, k, t):
    lo, hi = 0.05, 5.0
    if price <= bs_call(s, k, t, lo):
        return lo
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_call(s, k, t, mid) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def call_cycle(c, q, s0, t0, t1, px, perp, dvol):
    """Per-lot path of one LONG call (bought at the real Deribit premium)."""
    k = float(c.strike)
    hours = perp.loc[t0:min(t1, hb.NOW)].index
    s = perp.loc[hours].to_numpy()
    tau = ((t1 - hours) / pd.Timedelta(hours=1)).to_numpy() / YEAR_H
    iv0 = implied_vol_call(q["premium_usd"], q["index"], k, (t1 - t0) / pd.Timedelta(hours=1) / YEAR_H)
    dv = dvol.reindex(hours).ffill().bfill().to_numpy()
    val = bs_call(s, k, tau, iv0 * dv / dv[0])
    prem = q["premium_usd"]
    fee_entry = min(bt.OPT_FEE_RATE * s0, bt.FEE_CAP * prem)
    settled = t1 <= hb.NOW
    st = float(px[t1.normalize()]) if settled else float("nan")
    if settled:
        payout = max(st - k, 0.0)
        val[-1] = payout - (min(bt.DELIVERY_FEE_RATE * st, bt.FEE_CAP * payout) if payout > 0 else 0.0)
    path = pd.Series(val - prem - fee_entry, index=hours)
    return dict(entry=t0, expiry=t1, instrument=c.instrument, strike=k, spot_entry=s0, prem_usd=prem,
                fee_entry=fee_entry, settled=settled, spot_expiry=st, pnl_per_lot=float(path.iloc[-1])), path


def long_put_from_short(cyc: dict, path: pd.Series):
    """Long put = mirror of the (unhedged) short put, paying the fees on both legs."""
    fee_settle = 0.0
    if cyc["settled"]:
        payout = max(cyc["strike"] - cyc["spot_expiry"], 0.0)
        fee_settle = min(bt.DELIVERY_FEE_RATE * cyc["spot_expiry"], bt.FEE_CAP * payout) if payout > 0 else 0.0
    p = -path - 2 * cyc["fee_entry"]
    if cyc["settled"]:
        p.iloc[-1] -= 2 * fee_settle
    return {**cyc, "pnl_per_lot": float(p.iloc[-1])}, p


def combine(parts: list[tuple[dict, pd.Series]]):
    """Several legs in the same cycle -> one 'cycle' with summed per-lot paths."""
    cyc = dict(parts[0][0])
    path = sum(p for _, p in parts)
    cyc.update(pnl_per_lot=float(path.iloc[-1]), prem_usd=sum(c["prem_usd"] for c, _ in parts),
               fee_entry=sum(c["fee_entry"] for c, _ in parts))
    return cyc, path


def run(cycles_paths: list, mode: str) -> dict:
    if not cycles_paths:
        return {"total P&L $": 0, "CAGR": 0.0, "max DD %": 0.0, "max size": 0.0}
    return equity_stats([c for c, _ in cycles_paths], [p for _, p in cycles_paths], mode)


def main() -> None:
    prefetch = len(sys.argv) > 1 and sys.argv[1] == "prefetch"
    inst_m, inst_w, px = bt.load_instruments(), hb.load_friday_puts(), bt.load_delivery_prices()
    perp, dvol, fund = hb.load_perp_1h(), hb.load_dvol_1h(), hb.load_funding_1h()
    calls = load_calls()
    m = hb.load_perp_1m()
    rows_a, rows_b, rows_c = [], [], []
    for tenor, inst in (("1W", inst_w), ("1M", inst_m)):
        pairs = hb.cycle_expiries(inst, tenor)
        exps = [pd.Timestamp(e) for e in sorted(inst.expiry.unique())]
        prev = {b: a for a, b in zip(exps[:-1], exps[1:])}
        prev[exps[0]] = exps[0] - pd.Timedelta(days=7 if tenor == "1W" else 28)
        ret = {t0: candle(m, max(prev[t0], pd.Timestamp("2021-12-01")), t0)["ret"] for t0, _ in pairs}
        big = {t0: abs(r) > BIG[tenor] for t0, r in ret.items()}
        red = {t0: r < 0 for t0, r in ret.items()}
        ci = calls_for(calls, tenor)
        # ---- B) long calls ---------------------------------------------------
        call_legs = {}
        for otm, label in CALL_OTM.items():
            for t0, t1 in pairs:
                c, q, s0 = pick_call(ci, px, t0, t1, otm)
                if not prefetch:
                    call_legs[(label, t0)] = call_cycle(c, q, s0, t0, t1, px, perp, dvol)
            print(f"  calls {tenor} {label} ok", flush=True)
        if prefetch:
            continue
        # ---- A) short puts after big candle, with hedges ---------------------
        put_legs = {}
        for otm, label in STRIKES.items():
            picks = [(hb.pick_put(inst, px, t0, t1, otm), t0, t1) for t0, t1 in pairs]
            for hedge in HEDGES:
                for (c, q, s0), t0, t1 in picks:
                    put_legs[(label, hedge, t0)] = hb.run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, hedge)
            for hedge in HEDGES:
                always = [put_legs[(label, hedge, t0)] for t0, _ in pairs]
                filt = [put_legs[(label, hedge, t0)] for t0, _ in pairs if big[t0]]
                for name, sel in ((f"always sell, {hedge}", always), (f"BIG candle, {hedge}", filt)):
                    rows_a.append({"tenor": tenor, "strike": label, "strategy": name, "trades": len(sel),
                                   **{f"fixed {k}": v for k, v in run(sel, "fixed").items()},
                                   **{f"rule {k}": v for k, v in run(sel, "base+cover").items()}})
            for hedge in ("stop-10% hold", "delta-daily", "delta@-10%"):
                sel = [put_legs[(label, hedge if red[t0] else "none", t0)] for t0, _ in pairs if big[t0]]
                rows_a.append({"tenor": tenor, "strike": label, "strategy": f"BIG candle, {hedge} only after big RED",
                               "trades": len(sel),
                               **{f"fixed {k}": v for k, v in run(sel, "fixed").items()},
                               **{f"rule {k}": v for k, v in run(sel, "base+cover").items()}})
            print(f"  puts {tenor} {label} ok", flush=True)
        # ---- B) evaluate long options after small / big / always -------------
        t0s = [t0 for t0, _ in pairs]
        small_mask = np.array([not big[t] for t in t0s])
        legs_by = {}
        for label in CALL_OTM.values():
            legs_by[f"buy {label} call"] = [call_legs[(label, t)] for t in t0s]
        legs_by["buy ATM put"] = [long_put_from_short(*put_legs[("ATM", "none", t)]) for t in t0s]
        legs_by["buy ATM straddle"] = [combine([call_legs[("ATM", t)], legs_by["buy ATM put"][i]])
                                       for i, t in enumerate(t0s)]
        for name, legs in legs_by.items():
            pnl_pct = np.array([c["pnl_per_lot"] / c["spot_entry"] for c, _ in legs])
            half = np.array([t < SPLIT for t in t0s])
            for when, mask in (("always", np.ones(len(t0s), bool)), ("after SMALL candle", small_mask),
                               ("after BIG candle", ~small_mask)):
                sel = [legs[i] for i in np.where(mask)[0]]
                st = run(sel, "fixed")
                rows_b.append({"tenor": tenor, "trade": name, "when": when, "trades": int(mask.sum()),
                               "avg P&L/trade % spot": pnl_pct[mask].mean() * 100,
                               "avg H1": pnl_pct[mask & half].mean() * 100,
                               "avg H2": pnl_pct[mask & ~half].mean() * 100,
                               "win rate": (pnl_pct[mask] > 0).mean(),
                               "perm p-value": perm_p(pnl_pct, mask) if when != "always" else float("nan"),
                               **{f"fixed {k}": v for k, v in st.items()}})
        # ---- C) always in the market -----------------------------------------
        for put_label, put_hedge, call_label in (("ATM", "none", "ATM"), ("5% OTM", "none", "ATM"),
                                                 ("5% OTM", "none", "5% OTM"), ("ATM", "delta-daily", "ATM"),
                                                 ("5% OTM", "stop-10% hold", "5% OTM")):
            sel, sel_put = [], []
            for t in t0s:
                if big[t]:
                    sel.append(put_legs[(put_label, put_hedge, t)])
                    sel_put.append(put_legs[(put_label, put_hedge, t)])
                else:
                    sel.append(call_legs[(call_label, t)])
            rows_c.append({"tenor": tenor, "strategy": f"sell {put_label} put ({put_hedge}) after BIG + buy {call_label} call after SMALL",
                           "trades": len(sel), **{f"fixed {k}": v for k, v in run(sel, "fixed").items()},
                           "puts only: fixed CAGR": run(sel_put, "fixed")["CAGR"]})
    if prefetch:
        print("prefetch done")
        return
    pd.set_option("display.width", 260)
    pd.set_option("display.max_rows", 400)
    for name, rows in (("candle_combo_hedges.csv", rows_a), ("candle_combo_long_options.csv", rows_b),
                       ("candle_combo_always_in.csv", rows_c)):
        df = pd.DataFrame(rows)
        df.to_csv(os.path.join(OUT, name), index=False, float_format="%.4f")
        print(f"\n### {name}\n" + df.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
