#!/usr/bin/env python3
"""
Extended backtest: 1-month vs 3-month BTC puts, with and without delta hedging.

Builds on backtest.py (same Deribit option data, strikes, fees and recovery
sizing) and adds:

* Tenor
    1M  - every monthly expiry, sell the put expiring at the next monthly expiry
    3M  - every quarterly expiry (Mar/Jun/Sep/Dec), sell the put expiring at the
          next quarterly expiry (~3 months), hold to expiry, repeat
* Hedging with the BTC-PERPETUAL (hourly prices, real funding, 0.05% taker fee)
    none        - naked short put
    stop-10%    - stop order: when BTC trades 10% below the entry price, short
                  1 BTC per lot sold (full hedge); buy it back with a stop when
                  BTC goes back above that level; can re-trigger any number of
                  times. Simulated on 1-minute bars: fills at the trigger level
                  (or worse if price gapped through) +/-0.1% slippage + fees
    delta-daily - delta-neutral from day 1: rebalance the perp short to the
                  put's Black-Scholes delta every day at 08:00 UTC
    delta@-10%  - no hedge until BTC falls 10% below entry, then delta hedge
                  daily (as above) until expiry
* Daily mark-to-market of every position (Black-Scholes with the option's own
  entry implied vol, moved day by day with Deribit's DVOL index), so the max
  drawdown includes losses during the month and not only at expiry.

Run:  python3 hedged_backtest.py      (after backtest.py has cached option data)
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import norm

import backtest as bt

DATA, OUT = bt.DATA, bt.OUT
OTM_LEVELS = [0.00, 0.05, 0.10, 0.15]
HEDGES = ["none", "stop-10%", "delta-daily", "delta@-10%"]
TRIGGER = 0.10           # selloff that activates the stop / delayed-delta hedge
PERP_FEE = 0.0005         # Deribit BTC-PERPETUAL taker fee
STOP_SLIP = 0.001         # stop orders fill 0.1% beyond the trigger level
NOW = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
YEAR_H = 365.0 * 24


# --------------------------------------------------------------------------- #
# Hourly market data (cached)
# --------------------------------------------------------------------------- #
def _chunks(start: pd.Timestamp, end: pd.Timestamp, days: int):
    t = start
    while t < end:
        yield t, min(t + pd.Timedelta(days=days), end)
        t += pd.Timedelta(days=days)


def load_perp_1h() -> pd.Series:
    """BTC-PERPETUAL hourly close, indexed by the bar's CLOSE time."""
    path = os.path.join(DATA, "btc_perp_1h.csv")
    if not os.path.exists(path):
        frames = []
        for a, b in _chunks(pd.Timestamp("2021-12-01"), NOW, 150):
            r = bt.get(bt.MAIN_API, "get_tradingview_chart_data", instrument_name="BTC-PERPETUAL",
                       start_timestamp=bt.ms(a), end_timestamp=bt.ms(b), resolution=60)
            frames.append(pd.DataFrame({"t": pd.to_datetime(r["ticks"], unit="ms") + pd.Timedelta(hours=1),
                                        "close": r["close"]}))
        df = pd.concat(frames).drop_duplicates("t").sort_values("t")
        df.to_csv(path, index=False)
    s = pd.read_csv(path, parse_dates=["t"]).set_index("t")["close"]
    return s[~s.index.duplicated()]


def load_dvol_1h() -> pd.Series:
    """Deribit BTC DVOL (30d implied vol index, %) hourly close, by close time."""
    path = os.path.join(DATA, "btc_dvol_1h.csv")
    if not os.path.exists(path):
        rows = []
        for a, b in _chunks(pd.Timestamp("2021-12-01"), NOW, 40):
            r = bt.get(bt.MAIN_API, "get_volatility_index_data", currency="BTC",
                       start_timestamp=bt.ms(a), end_timestamp=bt.ms(b), resolution=3600)
            rows += r["data"]
        df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c"])
        df["t"] = pd.to_datetime(df.ts, unit="ms") + pd.Timedelta(hours=1)
        df = df.drop_duplicates("t").sort_values("t")[["t", "c"]].rename(columns={"c": "dvol"})
        df.to_csv(path, index=False)
    s = pd.read_csv(path, parse_dates=["t"]).set_index("t")["dvol"]
    return s[~s.index.duplicated()]


def load_funding_1h() -> pd.Series:
    """BTC-PERPETUAL hourly funding rate (fraction of notional paid long->short)."""
    path = os.path.join(DATA, "btc_perp_funding_1h.csv")
    if not os.path.exists(path):
        rows = []
        for a, b in _chunks(pd.Timestamp("2021-12-01"), NOW, 30):
            rows += bt.get(bt.MAIN_API, "get_funding_rate_history", instrument_name="BTC-PERPETUAL",
                           start_timestamp=bt.ms(a), end_timestamp=bt.ms(b))
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df.timestamp, unit="ms")
        df = df.drop_duplicates("t").sort_values("t")[["t", "interest_1h"]]
        df.to_csv(path, index=False)
    s = pd.read_csv(path, parse_dates=["t"]).set_index("t")["interest_1h"]
    return s[~s.index.duplicated()]


_M1: dict = {}


def load_perp_1m() -> dict:
    """BTC-PERPETUAL 1-minute OHLC (bar open time, ms). Not committed: ~25 MB."""
    if not _M1:
        path = os.path.join(DATA, "btc_perp_1m.npz")
        if not os.path.exists(path):
            a, cols = pd.Timestamp("2021-12-31"), {x: [] for x in "tohlc"}
            while a < NOW:
                b = min(a + pd.Timedelta(minutes=4999), NOW)
                r = bt.get(bt.MAIN_API, "get_tradingview_chart_data", instrument_name="BTC-PERPETUAL",
                           start_timestamp=bt.ms(a), end_timestamp=bt.ms(b), resolution=1)
                for x, key in zip("tohlc", ("ticks", "open", "high", "low", "close")):
                    cols[x] += r[key]
                a = b + pd.Timedelta(minutes=1)
            df = pd.DataFrame(cols).drop_duplicates("t").sort_values("t")
            np.savez_compressed(path, **{x: df[x].to_numpy() for x in "tohlc"})
        z = np.load(path)
        _M1.update({x: z[x] for x in "tohlc"})
    return _M1


_stop_cache: dict = {}


def stop_events(t0, t1, trig, hours) -> list[tuple[int, float, float]]:
    """Stop-order hedge on 1-minute bars: sell 1 BTC when price trades <= trig,
    buy back when it trades back > trig. Fill at the trigger level (or the bar
    open if it gapped through) +/- slippage. Returns (hour bucket, dh, fill)."""
    key = (t0, t1, round(trig, 6))
    if key not in _stop_cache:
        m = load_perp_1m()
        i0, i1 = np.searchsorted(m["t"], [bt.ms(t0), bt.ms(min(t1, NOW))])
        o, h, l, t = (m[x][i0:i1].tolist() for x in "ohlt")
        ev, on = [], False
        for j in range(len(t)):
            if not on and l[j] <= trig:
                ev.append((t[j] + 60_000, -1.0, min(o[j], trig) * (1 - STOP_SLIP)))
                on = True
            elif on and h[j] > trig:
                ev.append((t[j] + 60_000, 1.0, max(o[j], trig) * (1 + STOP_SLIP)))
                on = False
        _stop_cache[key] = ev
    hrs_ms = hours.as_unit("ms").asi8
    return [(int(np.searchsorted(hrs_ms, tm, side="left")), dh, f) for tm, dh, f in _stop_cache[key]]


# --------------------------------------------------------------------------- #
# Black-Scholes helpers (r = 0, forward ~ spot)
# --------------------------------------------------------------------------- #
def bs_put(s, k, t, v):
    s, k, t, v = map(np.asarray, (s, k, t, v))
    t = np.maximum(t, 1e-9)
    sd = np.maximum(v, 1e-4) * np.sqrt(t)
    d1 = (np.log(s / k) + 0.5 * sd ** 2) / sd
    return k * norm.cdf(-(d1 - sd)) - s * norm.cdf(-d1)


def bs_put_delta(s, k, t, v):
    s, k, t, v = map(np.asarray, (s, k, t, v))
    t = np.maximum(t, 1e-9)
    sd = np.maximum(v, 1e-4) * np.sqrt(t)
    d1 = (np.log(s / k) + 0.5 * sd ** 2) / sd
    return norm.cdf(d1) - 1.0


def implied_vol(price, s, k, t):
    lo, hi = 0.05, 5.0
    if price <= bs_put(s, k, t, lo):
        return lo
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_put(s, k, t, mid) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Cycles (entry -> expiry), per 1 lot
# --------------------------------------------------------------------------- #
def cycle_expiries(inst: pd.DataFrame, tenor: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    exps = [pd.Timestamp(e) for e in sorted(inst.expiry.unique())]
    if tenor == "3M":
        exps = [e for e in exps if e.month in (3, 6, 9, 12)]
    pairs = [(a, b) for a, b in zip(exps[:-1], exps[1:]) if bt.START <= a <= NOW]
    return pairs


def pick_put(inst, px, t0, t1, otm):
    s0 = float(px[t0.normalize()])
    cands = inst[(inst.expiry == t1) & (inst.created <= t0)].copy()
    cands["dist"] = (cands.strike - s0 * (1 - otm)).abs()
    for _, c in cands.sort_values(["dist", "strike"]).head(6).iterrows():
        q = bt.entry_quote(c.instrument, t0)
        if q:
            return c, q, s0
    raise RuntimeError(f"no tradable put for {t1.date()} otm={otm}")


def run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, hedge):
    """Per-lot path of one short put (+ hedge). Returns dict + daily MTM series."""
    k = float(c.strike)
    hours = perp.loc[t0:min(t1, NOW)].index
    s = perp.loc[hours].to_numpy()
    tau = ((t1 - hours) / pd.Timedelta(hours=1)).to_numpy() / YEAR_H
    iv0 = implied_vol(q["premium_usd"], q["index"], k, (t1 - t0) / pd.Timedelta(hours=1) / YEAR_H)
    dv = dvol.reindex(hours).ffill().bfill().to_numpy()
    iv = iv0 * dv / dv[0]
    fee_entry = min(bt.OPT_FEE_RATE * s0, bt.FEE_CAP * q["premium_usd"])
    prem = q["premium_usd"]
    settled = t1 <= NOW
    st = float(px[t1.normalize()]) if settled else float("nan")

    # ---- hedge: list of perp trades (hour bucket, BTC change, fill price) ----
    n = len(hours)
    trig = s0 * (1 - TRIGGER)
    delta = -bs_put_delta(s, k, tau, iv)  # long delta of the short put (0..1) per lot
    is_0800 = hours.hour == 8
    trades: list[tuple[int, float, float]] = []
    pos = 0.0
    if hedge == "stop-10%":
        for b, dh, f in stop_events(t0, t1, trig, hours):
            trades.append((b, dh, f))
            pos += dh
    elif hedge == "delta-daily":
        for i in range(n - 1):
            if i == 0 or is_0800[i]:
                trades.append((i, -delta[i] - pos, s[i]))
                pos = -delta[i]
    elif hedge == "delta@-10%":
        ev = stop_events(t0, t1, trig, hours)
        if ev:
            b, _, f = ev[0]  # first time BTC trades through the -10% level
            d0 = float(-bs_put_delta(trig, k, tau[b], iv[b]))
            trades.append((b, -d0, f))
            pos = -d0
            for i in range(b, n - 1):
                if is_0800[i]:
                    trades.append((i, -delta[i] - pos, s[i]))
                    pos = -delta[i]
    if settled and abs(pos) > 1e-12:  # close the hedge at expiry
        trades.append((n - 1, -pos, s[n - 1]))
    dh_h, cash_h, fee_h = np.zeros(n), np.zeros(n), np.zeros(n)
    for b, dh, f in trades:
        b = min(b, n - 1)
        dh_h[b] += dh
        cash_h[b] -= dh * f
        fee_h[b] += PERP_FEE * abs(dh) * f
    position = np.cumsum(dh_h)  # BTC per lot held from hour i to i+1
    fr = fund.reindex(hours).fillna(0.0).to_numpy()
    funding_step = np.zeros(n)
    funding_step[:-1] = -position[:-1] * s[:-1] * fr[1:]  # shorts receive positive funding
    funding_cum = np.concatenate([[0.0], np.cumsum(funding_step)[:-1]])
    # hedge P&L marked at hour j = cash from trades - fees + position value + funding
    hedge_cum = np.cumsum(cash_h) - np.cumsum(fee_h) + position * s + funding_cum
    hedge_fees = float(fee_h.sum())
    turnover = float(sum(abs(t[1]) for t in trades))

    # ---- option mark-to-market -------------------------------------------
    opt_val = bs_put(s, k, tau, iv)
    if settled:
        payout = max(k - st, 0.0)
        fee_settle = min(bt.DELIVERY_FEE_RATE * st, bt.FEE_CAP * payout) if payout > 0 else 0.0
        opt_val[-1] = payout + fee_settle
    mtm = prem - fee_entry - opt_val + hedge_cum
    path = pd.Series(mtm, index=hours)
    return dict(
        entry=t0, expiry=t1, instrument=c.instrument, strike=k, spot_entry=s0, iv_entry=iv0,
        prem_usd=prem, fee_entry=fee_entry, spot_expiry=st, settled=settled,
        option_pnl=float(prem - fee_entry - opt_val[-1]), hedge_pnl=float(hedge_cum[-1]),
        hedge_fees=hedge_fees, hedge_turnover_btc=turnover,
        pnl_per_lot=float(mtm[-1]),
    ), path


# --------------------------------------------------------------------------- #
# Sizing + portfolio equity
# --------------------------------------------------------------------------- #
def simulate(cycles: list[dict], paths: list[pd.Series], mode: str):
    deficit, realised = 0.0, 0.0
    rows, eq_parts = [], []
    for cyc, path in zip(cycles, paths):
        prem_net = cyc["prem_usd"] - cyc["fee_entry"]
        size = bt.BASE_LOTS if mode == "fixed" else bt.BASE_LOTS + deficit / prem_net
        size = math.ceil(size * 100 - 1e-9) / 100
        pnl = size * cyc["pnl_per_lot"]
        eq_parts.append(realised + size * path.iloc[:-1] if cyc["settled"] else realised + size * path)
        rows.append({**cyc, "size_lots": size, "deficit_before": deficit, "pnl": pnl,
                     "notional_usd": size * cyc["strike"]})
        realised += pnl
        if mode != "fixed" and cyc["settled"]:
            deficit = deficit - pnl if pnl < 0 else max(0.0, deficit - pnl)
    eq = pd.concat(eq_parts)
    if cycles[-1]["settled"]:
        eq.loc[cycles[-1]["expiry"]] = realised
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    return pd.DataFrame(rows), eq


def stats(trades: pd.DataFrame, eq: pd.Series) -> dict:
    daily = eq[eq.index.hour == 8]
    dd = (daily - daily.cummax().clip(lower=0)).min()
    s = trades[trades.settled]
    return {
        "P&L to date $": round(eq.iloc[-1]),
        "max drawdown $ (daily MTM)": round(dd),
        "losing cycles": f"{int((s.pnl_per_lot < 0).sum())}/{len(s)}",
        "worst cycle $": round(s.pnl.min()),
        "max size (lots)": round(trades.size_lots.max(), 2),
        "max notional $": round(trades.notional_usd.max()),
        "premium collected $": round((trades.size_lots * trades.prem_usd).sum()),
        "hedge P&L $": round((trades.size_lots * trades.hedge_pnl).sum()),
    }


def main() -> None:
    inst = bt.load_instruments()
    px = bt.load_delivery_prices()
    print("Loading hourly perp / DVOL / funding ...")
    perp, dvol, fund = load_perp_1h(), load_dvol_1h(), load_funding_1h()
    print(f"  perp {perp.index.min()} -> {perp.index.max()}  ({len(perp)} h)")
    summary, all_trades, equity = [], [], {}
    for tenor in ("1M", "3M"):
        pairs = cycle_expiries(inst, tenor)
        for otm in OTM_LEVELS:
            label = "ATM" if otm == 0 else f"{int(otm * 100)}% OTM"
            picks = [pick_put(inst, px, t0, t1, otm) + (t0, t1) for t0, t1 in pairs]
            for hedge in HEDGES:
                cycles, paths = [], []
                for c, q, s0, t0, t1 in picks:
                    cyc, path = run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, hedge)
                    cycles.append(cyc)
                    paths.append(path)
                for mode in ("base+cover", "fixed"):
                    tr, eq = simulate(cycles, paths, mode)
                    key = (tenor, label, hedge, mode)
                    equity[key] = eq[eq.index.hour == 8]
                    tr.insert(0, "sizing", mode)
                    tr.insert(0, "hedge", hedge)
                    tr.insert(0, "strike_rule", label)
                    tr.insert(0, "tenor", tenor)
                    all_trades.append(tr)
                    summary.append({"tenor": tenor, "strike": label, "hedge": hedge, "sizing": mode,
                                    **stats(tr, eq)})
            print(f"  done {tenor} {label}")
    summ = pd.DataFrame(summary)
    summ.to_csv(os.path.join(OUT, "hedged_summary.csv"), index=False)
    pd.concat(all_trades).to_csv(os.path.join(OUT, "hedged_trades.csv"), index=False)
    eq_df = pd.DataFrame({"|".join(k): v for k, v in equity.items()})
    eq_df.index.name = "time"
    eq_df.round(2).to_csv(os.path.join(OUT, "hedged_equity_daily.csv"))
    pd.set_option("display.width", 250)
    print(summ.to_string(index=False))


if __name__ == "__main__":
    main()
