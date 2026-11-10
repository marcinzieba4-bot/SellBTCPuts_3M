# Selling monthly BTC puts with loss-recovery sizing: backtest from 2022

**Strategy tested**

* Every month, at Deribit's monthly expiry (last Friday, 08:00 UTC), sell a BTC put that expires at the next monthly expiry, about 1 month away.
* Start at **0.25 lots**. On Deribit, 1 lot = 1 BTC.
* If the put expires in the money and the month ends with a net loss, **size up next month** so that the premium collected covers the loss. Once the loss is recovered, go back to 0.25 lots.
* Losses that are not yet recovered add up. If a recovery month also loses, its loss is added to the amount still to be recovered (the deficit).
* Period: 58 settled monthly cycles, from the put sold on 31 Dec 2021 (expiring 28 Jan 2022) to the put that expired 30 Oct 2026. The November 2026 put is still open and is shown marked to market as of 10 Nov 2026.

The rule doesn't say which strike to sell, so every variant was run at **ATM, 5%, 10%, 15% and 20% OTM**.

**Sizing variants compared**

| name | size each month |
|---|---|
| `base+cover` (**your rule**) | `0.25 + deficit / premium per lot` |
| `cover-only` | `max(0.25, deficit / premium per lot)` |
| `capped-2` | same as `base+cover`, but never more than 2 lots |
| `fixed` | always 0.25 lots (no recovery) |

## Data: real Deribit prices, not a model

* **Premium:** Deribit mark price of the actual listed put, from real trades in the first 2 hours after the previous expiry (`history.deribit.com`). The window widens to 6h or 24h only if there were no trades.
* **Spot at entry and settlement:** Deribit `btc_usd` delivery price, which is the index Deribit options settle against.
* **Strikes:** the listed strike nearest to `spot × (1 − OTM%)`.
* **Fees:** Deribit's 0.03% trade fee and 0.015% delivery fee, each capped at 12.5% of the option value.
* **Not included:** bid/ask slippage (mark ≈ mid) and Deribit's 0.1-lot size step (0.25 itself isn't a valid Deribit size). Everything is in USD.

## Results (settled months only, Jan 2022 – Oct 2026)

"Max collateral" means `size × strike` at the peak month: the cash needed to fully secure the put.

| Strike | Sizing | Total P&L | Losing months | Worst month | Max drawdown | Max size | Max collateral | P&L ÷ max collateral |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ATM | **base+cover** | **$25,141** | 19 / 58 | −$13,856 | −$17,147 | **6.91 lots** | **$344,760** | 7.3% |
| ATM | capped-2 | $19,183 | 19 / 58 | −$13,856 | −$17,147 | 2.00 | $138,000 | 13.9% |
| ATM | fixed 0.25 | $1,845 | 19 / 58 | −$4,930 | −$7,314 | 0.25 | $28,750 | 6.4% |
| 5% OTM | **base+cover** | **$18,758** | 15 / 58 | −$7,482 | −$9,548 | 6.35 | $128,250 | 14.6% |
| 5% OTM | capped-2 | $17,174 | 15 / 58 | −$7,482 | −$9,548 | 2.00 | $128,250 | 13.4% |
| 5% OTM | fixed 0.25 | $785 | 15 / 58 | −$4,364 | −$4,676 | 0.25 | $27,500 | 2.9% |
| 10% OTM | **base+cover** | **$12,924** | 8 / 58 | −$6,196 | −$7,666 | 6.52 | $163,100 | 7.9% |
| 10% OTM | capped-2 | $11,290 | 8 / 58 | −$6,196 | −$7,666 | 2.00 | $140,000 | 8.1% |
| 10% OTM | fixed 0.25 | $1,280 | 8 / 58 | −$3,302 | −$3,344 | 0.25 | $26,000 | 4.9% |
| 15% OTM | **base+cover** | **$7,683** | 8 / 58 | −$4,600 | −$5,677 | 6.64 | $167,700 | 4.6% |
| 15% OTM | fixed 0.25 | $1,625 | 8 / 58 | −$2,026 | −$2,026 | 0.25 | $24,500 | 6.6% |
| 20% OTM | **base+cover** | **$4,932** | 4 / 58 | −$2,740 | −$3,383 | 5.67 | $116,350 | 4.2% |
| 20% OTM | fixed 0.25 | $2,226 | 4 / 58 | −$1,336 | −$1,336 | 0.25 | $23,000 | 9.7% |

The full table, including `cover-only` and the remaining `capped-2` rows, is in [`results/summary.csv`](results/summary.csv). Every trade is in [`results/all_trades.csv`](results/all_trades.csv).

For reference, **holding 0.25 BTC** over the same period made about **+$7,800** (BTC went from $46,957 to $78,119).

![Backtest chart](results/backtest_chart.png)

### Stress test: one more bad month at the peak

The largest sizes all came in **July 2022**, after the May and June 2022 crash. The worst one-month BTC move in the sample was **−28%** (June 2022). This is what would have happened if the July 2022 recovery month had also dropped 28% ([`results/stress_test.csv`](results/stress_test.csv)):

| Strike | Size in Jul 2022 | Loss that month | New deficit | **Next month's size** | Next month's collateral |
|---|---:|---:|---:|---:|---:|
| ATM | 6.91 | −$26,824 | $40,585 | **19.9 lots** | $417k |
| 10% OTM | 6.52 | −$17,771 | $25,437 | **21.0 lots** | $400k |
| 20% OTM | 5.67 | −$7,515 | $10,898 | **17.7 lots** | $301k |

## Conclusions

1. **Your recovery rule worked in this sample.** Every loss was recovered within 2–4 months, and the deficit is back to about $0. It made **7–24× more** than a fixed 0.25-lot seller (ATM: $25.1k vs $1.8k).
2. **That extra profit comes from a martingale.** The rule increases exposure after losses. At the peak the position was **~27× the base size** (6.9 lots in Jul 2022), and the ATM version needed up to **$345k of collateral** (Mar 2026) to make $25k over 4.8 years. That is about 1.5% a year on peak capital.
3. **It survived only because losing streaks were short** (3 months at most). One more crash month at the peak would have meant selling about 20 lots, or $400k of collateral, the next month. A third loss would push past $1M. Recovery sizing doesn't remove the risk. It trades many small wins for a rare, very large loss.
4. **Without the recovery rule, selling BTC puts roughly broke even in 2022–2026.** It made +$0.8k to +$2.2k over 4.8 years, less than simply holding 0.25 BTC. The 2022 bear market, February 2025 and the Nov 2025 – Feb 2026 sell-off took back most of the premium.
5. **Capping the size gives a much better trade-off.** Capped at 2 lots, the ATM version kept **76% of the profit with 40% of the capital** ($19.2k on $138k vs $25.1k on $345k). Recovery then takes longer: up to 14 months in deficit instead of 4.
6. **5–10% OTM is the better zone.** Losing months fall from 19 (ATM) to 15 and 8, and the 5% OTM variants had the best return on collateral (13–15%).

### How to use these results

* If you run this strategy, **cap the size** (for example 2 lots, 8× the base). Let a large deficit be recovered over several months rather than all at once.
* Sell **about 5–10% OTM** rather than ATM.
* **Plan your capital for the peak, not the base.** With a 2-lot cap at BTC ≈ $70k, that is about $140k cash-secured. On Deribit, margin is posted in BTC, so it shrinks just when puts lose money.
* Decide a hard limit in advance: a deficit size or number of losing months after which you **reset to 0.25 lots and accept the loss**. Without one, a single long bear streak (like 2022 with one more leg down) can wipe out years of premium.

### Open position (Nov 2026 expiry, marked 10 Nov 2026, BTC ≈ $73.9k)

| Strike rule | Put sold | Size (your rule) | Unrealized P&L |
|---|---|---:|---:|
| ATM | 78000 P | 0.48 lots (carrying $663 from October) | −$886 |
| 5% OTM | 74000 P | 0.28 lots | −$217 |
| 10% OTM | 70000 P | 0.25 lots | −$62 |
| 15% / 20% OTM | 66000 P / 62000 P | 0.25 lots | about $0 |

## Reproduce

```bash
pip install pandas matplotlib
python3 backtest.py   # downloads + caches Deribit data in ./data, writes ./results
python3 report.py     # chart + stress test
```

Cached data is committed in `data/`, so reruns only hit the API for anything not cached yet. Delete `data/` to download everything again. Strike levels, base size, fees and the cap are constants at the top of `backtest.py`.
