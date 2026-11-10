#!/usr/bin/env python3
"""
Does the LAST CANDLE before a put sale predict how that put does?

For every cycle (1W and 1M puts, no hedge) the "last candle" is the BTC price
candle of the previous cycle, i.e. from the previous expiry to this entry:
    weekly puts  -> last weekly candle  (Fri 08:00 -> Fri 08:00 UTC)
    monthly puts -> last monthly candle (previous monthly expiry -> this one)
Weekly puts are also checked against the last DAILY candle (Thu 08:00 -> Fri 08:00).
High/low come from 1-minute BTC-PERPETUAL bars.

Candle features: return (close/open-1), colour, range, lower/upper wick, close
position in the range. Outcome: the put's P&L per lot as % of BTC price at entry
(so 2022 and 2025 weeks are comparable).

Overfitting checks (small samples: 253 weeks, 58 months):
* every bucket is shown for the first half (2022-01..2024-06) and second half
  (2024-07..2026-11) separately -> a real pattern should have the same sign in both
* permutation test: a filter that trades N of M cycles is compared with 20,000
  random selections of N cycles -> p-value
* the "BIG candle either way" and "WIDE candle" rules were added AFTER seeing the
  bucket stats (post-hoc), so their p-values are optimistic; they use only past
  data (fixed thresholds / trailing median) so they are tradable without look-ahead

Filter strategies are then run through the same sizing (fixed 0.25 lots and the
recovery rule) and $30k-account CAGR / max drawdown as report_cagr.py.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

import backtest as bt
import hedged_backtest as hb

OUT = bt.OUT
ACCOUNT = 30_000.0
SPLIT = pd.Timestamp("2024-07-01")
STRIKES = {0.00: "ATM", 0.05: "5% OTM", 0.10: "10% OTM", 0.15: "15% OTM"}
BIG = {"1W": 0.05, "1M": 0.10}  # "big" candle threshold per timeframe
RNG = np.random.default_rng(7)


def candle(m: dict, a: pd.Timestamp, b: pd.Timestamp) -> dict:
    i0, i1 = np.searchsorted(m["t"], [bt.ms(a), bt.ms(b)])
    o, c = m["o"][i0], m["c"][i1 - 1]
    hi, lo = m["h"][i0:i1].max(), m["l"][i0:i1].min()
    body_lo, body_hi = min(o, c), max(o, c)
    return dict(ret=c / o - 1, range=(hi - lo) / o, lower_wick=(body_lo - lo) / o,
                upper_wick=(hi - body_hi) / o, close_pos=(c - lo) / (hi - lo) if hi > lo else 0.5)


def build() -> pd.DataFrame:
    inst_m, inst_w, px = bt.load_instruments(), hb.load_friday_puts(), bt.load_delivery_prices()
    perp, dvol, fund = hb.load_perp_1h(), hb.load_dvol_1h(), hb.load_funding_1h()
    m = hb.load_perp_1m()
    rows, paths = [], {}
    for tenor, inst in (("1W", inst_w), ("1M", inst_m)):
        exps = [pd.Timestamp(e) for e in sorted(inst.expiry.unique())]
        prev = {b: a for a, b in zip(exps[:-1], exps[1:])}
        prev[exps[0]] = exps[0] - pd.Timedelta(days=7 if tenor == "1W" else 28)
        for t0, t1 in hb.cycle_expiries(inst, tenor):
            c_main = candle(m, max(prev[t0], pd.Timestamp("2021-12-01")), t0)
            c_day = candle(m, t0 - pd.Timedelta(days=1), t0)
            for otm, label in STRIKES.items():
                c, q, s0 = hb.pick_put(inst, px, t0, t1, otm)
                cyc, path = hb.run_cycle(c, q, s0, t0, t1, px, perp, dvol, fund, "none")
                key = (tenor, label, t0)
                paths[key] = (cyc, path)
                rows.append({"tenor": tenor, "strike": label, "entry": t0, "expiry": t1,
                             "settled": cyc["settled"], "spot": s0, "iv": cyc["iv_entry"],
                             "pnl_lot": cyc["pnl_per_lot"], "pnl_pct": cyc["pnl_per_lot"] / s0,
                             **{f"c_{k}": v for k, v in c_main.items()},
                             **{f"d_{k}": v for k, v in c_day.items()}})
    return pd.DataFrame(rows), paths


def bucketize(df: pd.DataFrame, tenor: str) -> pd.DataFrame:
    big = BIG[tenor]
    r = df.c_ret
    df = df.copy()
    df["colour"] = np.where(r > 0, "green", "red")
    df["size"] = pd.cut(r, [-1, -big, 0, big, 10], labels=[f"big red (<-{big:.0%})", "small red",
                                                           "small green", f"big green (>+{big:.0%})"])
    df["hammer"] = np.where((df.c_lower_wick > 2 * r.abs()) & (df.c_close_pos > 0.6), "hammer-like", "other")
    df["range_tercile"] = pd.qcut(df.c_range, 3, labels=["calm", "normal", "wild"])
    df["day_colour"] = np.where(df.d_ret > 0, "green day", "red day")
    df["big_move"] = np.where(r.abs() > big, f"big candle (|ret|>{big:.0%})", "small candle")
    df["half"] = np.where(df.entry < SPLIT, "H1 2022-24", "H2 2024-26")
    return df


def stats(df: pd.DataFrame, by: str) -> pd.DataFrame:
    g = df.groupby(by, observed=True).pnl_pct
    out = pd.DataFrame({"n": g.size(), "avg P&L % of spot": g.mean() * 100,
                        "avg entry IV %": df.groupby(by, observed=True).iv.mean() * 100,
                        "win rate": df.assign(w=df.pnl_pct >= 0).groupby(by, observed=True).w.mean(),
                        "worst %": g.min() * 100})
    for h in ("H1 2022-24", "H2 2024-26"):
        out[f"avg {h}"] = df[df.half == h].groupby(by, observed=True).pnl_pct.mean() * 100
    out["t-stat"] = g.mean() / (g.std(ddof=1) / np.sqrt(g.size()))
    return out


def perm_p(all_pnl: np.ndarray, mask: np.ndarray, n: int = 20_000) -> float:
    k = int(mask.sum())
    if k == 0 or k == len(mask):
        return float("nan")
    obs = all_pnl[mask].mean()
    sims = np.array([all_pnl[RNG.choice(len(all_pnl), k, replace=False)].mean() for _ in range(n)])
    return float((sims >= obs).mean())


def equity_stats(cycles, paths_, mode):
    tr, eq = hb.simulate(cycles, paths_, mode)
    daily = eq[eq.index.hour == 8]
    start = pd.Timestamp("2021-12-31 08:00")
    years = (daily.index[-1] - start) / pd.Timedelta(days=365.25)
    equity = ACCOUNT + pd.concat([pd.Series({start: 0.0}), daily]).sort_index()
    final = equity.iloc[-1]
    dd = (equity / equity.cummax() - 1).min()
    return {"total P&L $": round(eq.iloc[-1]),
            "CAGR": (final / ACCOUNT) ** (1 / years) - 1 if equity.min() > 0 else -1.0,
            "max DD %": dd if equity.min() > 0 else -1.0,
            "max size": tr.size_lots.max()}


def run_filters(df: pd.DataFrame, paths: dict, tenor: str) -> pd.DataFrame:
    big = BIG[tenor]
    rules = {
        "always sell (baseline)": lambda d: np.ones(len(d), bool),
        "only after GREEN candle": lambda d: d.c_ret > 0,
        "only after RED candle": lambda d: d.c_ret <= 0,
        f"skip after BIG RED (<-{big:.0%})": lambda d: d.c_ret >= -big,
        f"only after BIG RED (<-{big:.0%})": lambda d: d.c_ret < -big,
        f"skip after BIG GREEN (>+{big:.0%})": lambda d: d.c_ret <= big,
        "skip after WILD candle (top-third range)": lambda d: d.c_range <= d.c_range.quantile(2 / 3),
        # found in the bucket stats (post-hoc), defined without look-ahead:
        f"only after BIG candle either way (|ret|>{big:.0%})": lambda d: d.c_ret.abs() > big,
        "only after WIDE candle (range > trailing median)": lambda d: d.c_range > d.c_range.shift(1).rolling(
            52 if tenor == "1W" else 12, min_periods=4).median().fillna(np.inf),
    }
    if tenor == "1W":
        rules["only after GREEN last DAY"] = lambda d: d.d_ret > 0
        rules["only after RED last DAY"] = lambda d: d.d_ret <= 0
    rows = []
    for strike in STRIKES.values():
        d = df[df.strike == strike].sort_values("entry").reset_index(drop=True)
        all_pnl = d.pnl_pct.to_numpy()
        for name, f in rules.items():
            mask = np.asarray(f(d), bool)
            cyc = [paths[(tenor, strike, t)] for t in d.entry[mask]]
            if not cyc:
                continue
            cycles, ps = [c for c, _ in cyc], [p for _, p in cyc]
            sel = d[mask]
            row = {"tenor": tenor, "strike": strike, "rule": name, "trades": int(mask.sum()),
                   "avg P&L/trade % spot": sel.pnl_pct.mean() * 100,
                   "avg H1": sel[sel.half == "H1 2022-24"].pnl_pct.mean() * 100,
                   "avg H2": sel[sel.half == "H2 2024-26"].pnl_pct.mean() * 100,
                   "perm p-value": perm_p(all_pnl, mask) if name != "always sell (baseline)" else float("nan")}
            for mode, tag in (("fixed", "fixed"), ("base+cover", "rule")):
                st = equity_stats(cycles, ps, mode)
                row.update({f"{tag} {k}": v for k, v in st.items()})
            rows.append(row)
    return pd.DataFrame(rows)


def threshold_sensitivity(df: pd.DataFrame, paths: dict) -> pd.DataFrame:
    """'Only after a big candle' for several thresholds -> is 5%/10% a lucky pick?"""
    rows = []
    grid = {"1W": [0.03, 0.04, 0.05, 0.06, 0.08], "1M": [0.05, 0.075, 0.10, 0.125, 0.15]}
    for tenor, ths in grid.items():
        for strike in STRIKES.values():
            d = df[(df.tenor == tenor) & (df.strike == strike)].sort_values("entry").reset_index(drop=True)
            for th in ths:
                mask = (d.c_ret.abs() > th).to_numpy()
                cyc = [paths[(tenor, strike, t)] for t in d.entry[mask]]
                st = equity_stats([c for c, _ in cyc], [p for _, p in cyc], "fixed")
                sel = d[mask]
                rows.append({"tenor": tenor, "strike": strike, "threshold": th, "trades": int(mask.sum()),
                             "avg P&L/trade % spot": sel.pnl_pct.mean() * 100,
                             "avg skipped % spot": d[~mask].pnl_pct.mean() * 100,
                             "fixed CAGR": st["CAGR"], "fixed max DD %": st["max DD %"]})
    return pd.DataFrame(rows)


def chart(df: pd.DataFrame, paths: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
    BLUE, ORANGE = "#2a78d6", "#eb6834"
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "xtick.color": INK2, "ytick.color": INK2,
                         "axes.titlecolor": INK, "axes.titleweight": "bold", "axes.titlelocation": "left",
                         "axes.titlesize": 10, "figure.facecolor": SURF, "axes.facecolor": SURF})
    fig, axes = plt.subplots(1, 3, figsize=(17, 4.8), gridspec_kw={"width_ratios": [1.3, 1, 1]})
    ax = axes[0]
    labels, big_v, small_v = [], [], []
    for tenor in ("1W", "1M"):
        for strike in STRIKES.values():
            d = df[(df.tenor == tenor) & (df.strike == strike)]
            m = d.c_ret.abs() > BIG[tenor]
            labels.append(f"{tenor} {strike.replace(' OTM', '')}")
            big_v.append(d[m].pnl_pct.mean() * 100)
            small_v.append(d[~m].pnl_pct.mean() * 100)
    x = np.arange(len(labels))
    ax.bar(x - 0.2, big_v, 0.38, color=BLUE, label="after BIG candle (1W >5%, 1M >10%, either way)")
    ax.bar(x + 0.2, small_v, 0.38, color=ORANGE, label="after SMALL candle")
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_xticks(x, labels)
    ax.set_title("Average P&L per put sold, % of BTC price")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="upper left")
    for i, (tenor, strike) in enumerate((("1M", "ATM"), ("1W", "ATM"))):
        ax = axes[i + 1]
        d = df[(df.tenor == tenor) & (df.strike == strike)].sort_values("entry")
        for name, mask, col in (("always sell", np.ones(len(d), bool), INK2),
                                ("only after BIG candle", (d.c_ret.abs() > BIG[tenor]).to_numpy(), BLUE)):
            cyc = [paths[(tenor, strike, t)] for t in d.entry[mask]]
            _, eq = hb.simulate([c for c, _ in cyc], [p for _, p in cyc], "fixed")
            eq = eq[eq.index.hour == 8]
            ax.plot(eq.index, (ACCOUNT + eq) / 1000, color=col, lw=1.8, label=name)
        ax.axhline(ACCOUNT / 1000, color=INK2, lw=0.7)
        ax.set_title(f"{tenor} {strike} puts, fixed 0.25 lots: \\$30k account equity (\\$k)")
        ax.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="upper left")
    for ax in axes:
        ax.grid(axis="y", color=GRID, lw=0.7)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.tick_params(length=0)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "candle_patterns.png"), dpi=140, bbox_inches="tight")


def main():
    df, paths = build()
    df.to_csv(os.path.join(OUT, "candle_cycles.csv"), index=False)
    all_stats, all_filters = [], []
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    for tenor in ("1W", "1M"):
        d = bucketize(df[df.tenor == tenor], tenor)
        for by in ("colour", "size", "big_move", "hammer", "range_tercile") + (("day_colour",) if tenor == "1W" else ()):
            for strike in STRIKES.values():
                s = stats(d[d.strike == strike], by).reset_index().rename(columns={by: "bucket"})
                s.insert(0, "feature", by)
                s.insert(0, "strike", strike)
                s.insert(0, "tenor", tenor)
                all_stats.append(s)
        # correlation of candle return with next put P&L
        for strike in STRIKES.values():
            x = d[d.strike == strike]
            print(f"{tenor} {strike:8s} Spearman(candle ret, put P&L) = {x.c_ret.corr(x.pnl_pct, method='spearman'):+.3f}"
                  f"   H1 {x[x.half.str.startswith('H1')].c_ret.corr(x[x.half.str.startswith('H1')].pnl_pct, method='spearman'):+.3f}"
                  f"   H2 {x[x.half.str.startswith('H2')].c_ret.corr(x[x.half.str.startswith('H2')].pnl_pct, method='spearman'):+.3f}")
        all_filters.append(run_filters(d, paths, tenor))
    st = pd.concat(all_stats)
    st.to_csv(os.path.join(OUT, "candle_bucket_stats.csv"), index=False, float_format="%.4f")
    fl = pd.concat(all_filters)
    fl.to_csv(os.path.join(OUT, "candle_filter_strategies.csv"), index=False, float_format="%.4f")
    chart(df, paths)
    sens = threshold_sensitivity(df, paths)
    sens.to_csv(os.path.join(OUT, "candle_threshold_sensitivity.csv"), index=False, float_format="%.4f")
    print(st.round(3).to_string(index=False))
    print(fl.round(3).to_string(index=False))
    print(sens.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
