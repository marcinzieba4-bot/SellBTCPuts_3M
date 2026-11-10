#!/usr/bin/env python3
"""Charts + stress test from results/all_trades.csv (run backtest.py first)."""
from __future__ import annotations

import math
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
BASE = 0.25

RULES = ["ATM", "5% OTM", "10% OTM", "15% OTM", "20% OTM"]
COLORS = {"ATM": "#2a78d6", "5% OTM": "#eb6834", "10% OTM": "#1baf7a",
          "15% OTM": "#eda100", "20% OTM": "#e87ba4"}
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

d = pd.read_csv(os.path.join(OUT, "all_trades.csv"), parse_dates=["entry", "expiry"])
mart = d[d["mode"] == "base+cover"]
fixed = d[d["mode"] == "fixed"]

# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #
plt.rcParams.update({
    "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.titlecolor": INK,
    "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.titlesize": 11,
    "figure.facecolor": SURF, "axes.facecolor": SURF,
})
fig, axes = plt.subplots(3, 1, figsize=(11, 11), sharex=True,
                         gridspec_kw={"height_ratios": [1, 1.5, 1.1], "hspace": 0.28})


def style(ax):
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(length=0)


# 1) BTC price at each monthly expiry, losing months (ATM rule) marked
ax = axes[0]
atm = mart[mart.strike_rule == "ATM"]
spot = pd.concat([atm[["entry", "spot_entry"]].rename(columns={"entry": "t", "spot_entry": "s"}).iloc[:1],
                  atm[["expiry", "spot_expiry"]].rename(columns={"expiry": "t", "spot_expiry": "s"})])
ax.plot(spot.t, spot.s / 1000, color=INK2, lw=2)
lost = atm[atm.settled & (atm.pnl_per_lot < 0)]
ax.scatter(lost.expiry, lost.spot_expiry / 1000, s=36, color="#e34948", zorder=3,
           edgecolor=SURF, linewidth=2, label="ATM put lost money that month")
ax.set_title("BTC price at each Deribit monthly expiry ($k)")
ax.legend(frameon=False, loc="upper left", labelcolor=INK2)
style(ax)

# 2) cumulative P&L, recovery sizing, every strike rule (+ fixed-size ATM for reference)
ax = axes[1]
for r in RULES:
    x = mart[mart.strike_rule == r]
    ax.plot(x.expiry, x.cum_pnl / 1000, color=COLORS[r], lw=2, label=f"{r} (recovery sizing)")
    ax.annotate(f"{r}  ${x.cum_pnl.iloc[-1] / 1000:,.1f}k", (x.expiry.iloc[-1], x.cum_pnl.iloc[-1] / 1000),
                xytext=(6, 0), textcoords="offset points", va="center", color=INK, fontsize=9)
fx = fixed[fixed.strike_rule == "ATM"]
ax.plot(fx.expiry, fx.cum_pnl / 1000, color=INK2, lw=1.5, ls=(0, (4, 3)),
        label="ATM, fixed 0.25 lots (no recovery)")
ax.axhline(0, color=INK2, lw=0.8)
ax.set_title("Cumulative P&L, USD thousands (after Deribit fees; last point = open Nov-26 put, marked to market)")
ax.legend(frameon=False, loc="upper left", fontsize=9, labelcolor=INK2)
style(ax)

# 3) position size
ax = axes[2]
for r in ["ATM", "10% OTM"]:
    x = mart[mart.strike_rule == r]
    ax.step(x.expiry, x.size_lots, where="mid", color=COLORS[r], lw=2, label=r)
ax.axhline(BASE, color=INK2, lw=0.8, ls=(0, (2, 2)))
ax.text(pd.Timestamp("2023-12-15"), BASE + 0.2, "base 0.25 lots", color=INK2, fontsize=9)
ax.set_title("Position size sold each month (lots = BTC), recovery sizing")
ax.legend(frameon=False, loc="upper left", labelcolor=INK2)
style(ax)
ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 7]))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
axes[1].set_xlim(right=mart.expiry.max() + pd.Timedelta(days=200))

fig.savefig(os.path.join(OUT, "backtest_chart.png"), dpi=150, bbox_inches="tight")
print("saved results/backtest_chart.png")

# --------------------------------------------------------------------------- #
# Stress test: what if the recovery month at peak size had lost as well?
# Use the worst 1-month BTC drop in the sample (applied to the peak month).
# --------------------------------------------------------------------------- #
moves = (atm.spot_expiry / atm.spot_entry - 1)[atm.settled]
worst = moves.min()
worst_when = atm.loc[moves.idxmin(), "expiry"].strftime("%b %Y")
rows = []
for r in RULES:
    x = mart[(mart.strike_rule == r) & mart.settled].reset_index(drop=True)
    i = x.size_lots.idxmax()
    p = x.loc[i]
    st = p.spot_entry * (1 + worst)
    loss = p.size_lots * (max(p.strike - st, 0) + p.fee_entry - p.prem_usd)
    deficit = p.deficit_before + loss
    prem_net = p.prem_usd - p.fee_entry
    nxt = math.ceil((BASE + deficit / prem_net) * 100) / 100
    rows.append({
        "strike rule": r, "peak month": p.expiry.strftime("%b %Y"), "peak size (lots)": p.size_lots,
        "deficit carried in $": round(p.deficit_before),
        f"loss if BTC {worst:+.0%} that month $": round(loss),
        "new deficit $": round(deficit), "next size (lots)": nxt,
        "next notional $": round(nxt * p.strike),
    })
stress = pd.DataFrame(rows)
stress.to_csv(os.path.join(OUT, "stress_test.csv"), index=False)
print(f"worst 1-month BTC move in sample: {worst:+.1%} ({worst_when})")
print(stress.to_string(index=False))
