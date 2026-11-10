#!/usr/bin/env python3
"""Tables + charts for hedged_backtest.py results."""
from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
STRIKES = ["ATM", "5% OTM", "10% OTM", "15% OTM"]
HEDGES = ["none", "stop-10% hold", "stop-10%/-5%", "delta-daily", "delta@-10%"]
HEDGE_LABEL = {"none": "no hedge", "stop-10% hold": "full hedge at -10%, hold",
               "stop-10%/-5%": "full hedge at -10%, off at -5%",
               "delta-daily": "delta hedge daily", "delta@-10%": "delta hedge after -10%"}
COLORS = {"none": "#2a78d6", "stop-10% hold": "#eb6834", "stop-10%/-5%": "#1baf7a",
          "delta-daily": "#eda100", "delta@-10%": "#e87ba4"}
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

summ = pd.read_csv(os.path.join(OUT, "hedged_summary.csv"))
eq = pd.read_csv(os.path.join(OUT, "hedged_equity_daily.csv"), parse_dates=["time"]).set_index("time")


def k(x: float) -> str:
    return f"{'−' if x < 0 else '+'}${abs(x) / 1000:,.1f}k"


def table(sizing: str) -> str:
    lines = []
    for tenor in ("1M", "3M"):
        hdr = f"| {tenor} | " + " | ".join(HEDGE_LABEL[h] for h in HEDGES) + " |"
        lines += [hdr, "|---|" + "---:|" * len(HEDGES)]
        for st in STRIKES:
            cells = []
            for h in HEDGES:
                r = summ[(summ.tenor == tenor) & (summ.strike == st) & (summ.hedge == h) & (summ.sizing == sizing)].iloc[0]
                dd = r["max drawdown $ (daily MTM)"]
                cells.append(f"{k(r['P&L to date $'])} / {k(dd)}")
            lines.append(f"| {st} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


md = ["# P&L to date / max drawdown (daily mark-to-market), 31 Dec 2021 – 10 Nov 2026", "",
      "## Your recovery rule (0.25 lots + size up to cover losses)", "", table("base+cover"),
      "## Fixed 0.25 lots", "", table("fixed")]
open(os.path.join(OUT, "hedged_tables.md"), "w").write("\n".join(md))
print("\n".join(md))

plt.rcParams.update({
    "font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2,
    "ytick.color": INK2, "axes.titlecolor": INK, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.titlesize": 10, "figure.facecolor": SURF, "axes.facecolor": SURF,
})


def chart(sizing: str, fname: str, title: str, floor_k: float | None = None) -> None:
    fig, axes = plt.subplots(2, 4, figsize=(16, 7.5), sharex=True)
    for row, tenor in enumerate(("1M", "3M")):
        for col, st in enumerate(STRIKES):
            ax = axes[row, col]
            for h in HEDGES:
                s = eq[f"{tenor}|{st}|{h}|{sizing}"].dropna()
                ax.plot(s.index, s / 1000, color=COLORS[h], lw=1.6, label=HEDGE_LABEL[h])
            ax.axhline(0, color=INK2, lw=0.7)
            if floor_k is not None:  # clip blow-ups and say how deep they went
                off = [(h, eq[f"{tenor}|{st}|{h}|{sizing}"].min() / 1000) for h in HEDGES]
                off = [(h, m) for h, m in off if m < floor_k]
                ax.set_ylim(bottom=floor_k)
                if off:
                    txt = "off chart (min equity):\n" + "\n".join(f"{HEDGE_LABEL[h]}: {k(m * 1000)}" for h, m in off)
                    ax.text(0.02, 0.03, txt, transform=ax.transAxes, fontsize=7.5, color=INK2, va="bottom",
                            bbox=dict(facecolor=SURF, edgecolor=GRID, boxstyle="round,pad=0.3"))
            ax.set_title(f"{tenor} expiry · {st}")
            ax.grid(axis="y", color=GRID, lw=0.7)
            ax.spines[["top", "right", "left"]].set_visible(False)
            ax.tick_params(length=0)
            ax.xaxis.set_major_locator(mdates.YearLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            if col == 0:
                ax.set_ylabel("equity, $k")
    h, lab = axes[0, 0].get_legend_handles_labels()
    fig.legend(h, lab, loc="upper center", ncol=5, frameon=False, labelcolor=INK2, bbox_to_anchor=(0.5, 0.995))
    fig.suptitle(title, x=0.01, y=1.04, ha="left", fontweight="bold", color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, fname), dpi=140, bbox_inches="tight")
    print("saved", fname)


chart("fixed", "hedged_equity_fixed.png",
      "Equity (daily mark-to-market), fixed 0.25 lots: 1-month vs 3-month puts, with and without hedging")
chart("base+cover", "hedged_equity_recovery.png",
      "Equity (daily mark-to-market), your recovery sizing: 1-month vs 3-month puts, with and without hedging",
      floor_k=-40)
