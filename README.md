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

---

## P&L and max drawdown at a glance (1-month puts, no hedge)

* **P&L:** settled months, Jan 2022 – Oct 2026.
* **"At expiries":** the drawdown counted only on expiry days.
* **"Daily MTM":** every open put is marked to market every day, so it also includes losses during the month.

| Strike | **Your rule** P&L | max DD at expiries | max DD daily MTM | **Fixed 0.25** P&L | max DD at expiries | max DD daily MTM |
|---|---:|---:|---:|---:|---:|---:|
| ATM | **+$25,141** | −$17,147 | **−$31,360** | +$1,845 | −$7,314 | −$7,606 |
| 5% OTM | **+$18,758** | −$9,548 | **−$13,814** | +$785 | −$4,676 | −$5,440 |
| 10% OTM | **+$12,924** | −$7,666 | **−$11,828** | +$1,280 | −$3,344 | −$4,488 |
| 15% OTM | **+$7,683** | −$5,677 | **−$9,516** | +$1,625 | −$2,026 | −$3,247 |

The deepest point for your rule at ATM came at the end of February 2026. A 5-lot recovery position for March had just been opened, and BTC kept falling.

---

## Part 2: 1-month vs 3-month expiry, with and without hedging (`hedged_backtest.py`)

**Variants**

* **Tenor**
  * **1M:** the same monthly cycle as above (58 settled months).
  * **3M:** sell a ~3-month put at each quarterly expiry (Mar/Jun/Sep/Dec), hold it to expiry, then sell the next one (19 settled quarters). The 0.25-lot base and the recovery rule are the same, applied once per quarter.
* **Hedges** use the BTC-PERPETUAL with real hourly prices and real funding (shorts receive funding when it is positive), at a 0.05% taker fee per trade.

| hedge | rule |
|---|---|
| no hedge | naked short put |
| full hedge at −10%, hold | stop order: when BTC trades 10% below the entry price, short **1 BTC per lot** and keep the short until expiry |
| full hedge at −10%, off at −5% | same, but buy the hedge back if BTC recovers to −5%. It can re-trigger any number of times |
| delta hedge daily | delta-neutral from day 1: re-hedge to the put's delta every day at 08:00 UTC |
| delta hedge after −10% | no hedge until BTC trades 10% below entry, then delta-hedge daily until expiry |

* **Stop hedges** are simulated on **1-minute BTC bars**. They fill at the trigger level (or worse if price gapped through it), with 0.1% slippage plus fees.
* **Daily mark-to-market.** Every open put is marked every day with Black-Scholes. The option's own entry implied vol (backed out of the real Deribit premium) is moved day by day with Deribit's DVOL index. Deltas come from the same model.
* **"P&L to date"** runs to 10 Nov 2026 and includes the open Nov-26 (1M) and Dec-26 (3M) puts, marked to market.

### Results: P&L to date / max drawdown (daily mark-to-market)

**Your recovery rule** (0.25 lots, sized up to cover losses):

| 1M | no hedge | full hedge at −10%, hold | full hedge at −10%, off at −5% | **delta hedge daily** | delta hedge after −10% |
|---|---:|---:|---:|---:|---:|
| ATM | +$24.1k / −$31.4k | +$19.0k / −$699.8k | +$19.0k / −$82.7k | **+$2.3k / −$2.9k** | +$18.9k / −$126.8k |
| 5% OTM | +$18.4k / −$13.8k | +$14.5k / −$1,437.6k | +$14.5k / −$308.5k | **+$3.4k / −$2.2k** | +$14.5k / −$114.6k |
| 10% OTM | +$12.8k / −$11.8k | +$15.6k / −$48.6k | +$10.4k / −$514.2k | **+$3.3k / −$1.8k** | +$9.4k / −$41.9k |
| 15% OTM | +$7.7k / −$9.5k | +$16.3k / −$51.2k | +$12.2k / −$182.0k | **+$2.3k / −$2.5k** | +$5.3k / −$11.6k |

| 3M | no hedge | full hedge at −10%, hold | full hedge at −10%, off at −5% | **delta hedge daily** | delta hedge after −10% |
|---|---:|---:|---:|---:|---:|
| ATM | +$10.5k / −$18.2k | +$5.5k / −$55.0k | +$4.4k / −$27.8k | **+$2.7k / −$1.4k** | +$4.0k / −$8.2k |
| 5% OTM | +$9.3k / −$16.3k | +$6.6k / −$64.9k | +$3.5k / −$46.7k | **+$2.3k / −$1.9k** | +$3.4k / −$7.7k |
| 10% OTM | +$8.6k / −$13.1k | +$9.8k / −$36.6k | +$4.1k / −$95.7k | **+$2.1k / −$2.2k** | +$2.5k / −$6.2k |
| 15% OTM | +$8.1k / −$8.5k | +$14.0k / −$73.2k | +$8.8k / −$277.2k | **+$2.0k / −$2.4k** | +$2.3k / −$5.6k |

**Fixed 0.25 lots** (shows the effect of each hedge without any size changes):

| 1M | no hedge | full hedge at −10%, hold | full hedge at −10%, off at −5% | **delta hedge daily** | delta hedge after −10% |
|---|---:|---:|---:|---:|---:|
| ATM | +$1.3k / −$7.6k | −$3.5k / −$10.2k | +$0.3k / −$7.9k | **+$1.3k / −$1.6k** | +$1.8k / −$6.5k |
| 5% OTM | +$0.5k / −$5.4k | −$4.3k / −$10.7k | −$0.5k / −$8.7k | **+$1.7k / −$1.3k** | +$1.8k / −$4.7k |
| 10% OTM | +$1.2k / −$4.5k | −$3.6k / −$10.7k | +$0.1k / −$8.0k | **+$1.5k / −$1.2k** | +$1.6k / −$2.9k |
| 15% OTM | +$1.6k / −$3.2k | −$3.2k / −$11.6k | +$0.6k / −$8.2k | **+$0.7k / −$1.3k** | +$0.5k / −$2.1k |

| 3M | no hedge | full hedge at −10%, hold | full hedge at −10%, off at −5% | **delta hedge daily** | delta hedge after −10% |
|---|---:|---:|---:|---:|---:|
| ATM | −$1.2k / −$9.5k | −$10.1k / −$14.4k | −$8.8k / −$10.8k | **+$2.4k / −$1.4k** | −$3.5k / −$4.7k |
| 5% OTM | +$0.7k / −$7.8k | −$8.2k / −$15.1k | −$6.9k / −$9.7k | **+$1.9k / −$1.6k** | −$2.0k / −$4.1k |
| 10% OTM | +$1.6k / −$6.0k | −$7.3k / −$15.5k | −$6.0k / −$10.5k | **+$1.6k / −$1.7k** | −$1.2k / −$3.4k |
| 15% OTM | +$3.2k / −$4.2k | −$5.7k / −$16.8k | −$4.3k / −$10.3k | **+$1.6k / −$1.7k** | +$0.0k / −$2.7k |

**Profit per $1 of max drawdown, fixed size.** This ratio doesn't depend on position size, so it is the fair way to compare the hedges:

| | no hedge | hedge at −10%, hold | hedge at −10%, off at −5% | **delta daily** | delta after −10% |
|---|---:|---:|---:|---:|---:|
| 1M ATM / 5% / 10% / 15% | 0.17 / 0.09 / 0.26 / 0.49 | < 0 | ≈ 0 | **0.85 / 1.30 / 1.25 / 0.55** | 0.28 / 0.38 / 0.55 / 0.25 |
| 3M ATM / 5% / 10% / 15% | −0.13 / 0.08 / 0.26 / 0.75 | < 0 | < 0 | **1.78 / 1.18 / 0.96 / 0.99** | < 0 or ≈ 0 |

![Fixed size](results/hedged_equity_fixed.png)
![Recovery sizing](results/hedged_equity_recovery.png)

Full results: [`results/hedged_summary.csv`](results/hedged_summary.csv), every cycle in [`results/hedged_trades.csv`](results/hedged_trades.csv), daily equity in [`results/hedged_equity_daily.csv`](results/hedged_equity_daily.csv).

### What Part 2 shows

1. **A stop that sells and buys back at the same −10% level destroys the strategy.** On 1-minute data it whipsawed **1,606 times** in 1M (−$49.9k on just 0.25 lots), and lost −$76.8k in 3M. It is kept in the CSV as `stop-10% naive` only as a warning.
2. **"Full hedge after −10%" loses money in both practical versions,** whether held to expiry or removed at −5%. In 2022–2026 BTC's −10% drops were usually followed by sharp rebounds. A 1-BTC short opened at −10% then lost on the rebound, and those losses were larger than the put losses it prevented. The worst case was the 3M put sold in Sep 2024: it was hedged at about $59k in Oct 2024, then BTC rallied to $95k by the December expiry (−$33k per lot on the hedge).
3. **Daily delta hedging is the only hedge that clearly works.**
   * Drawdown falls by **2.5–7×**, to about $1.2–1.7k at 0.25 lots.
   * P&L stays similar or improves (**3M ATM goes from −$1.2k to +$2.4k**). The exception is 15% OTM, where P&L roughly halves.
   * It turns the trade into collecting the gap between implied and realised volatility, instead of betting on direction.
   * It costs about $0.3–1.0k in perp fees over the 5 years at 0.25 lots. These fees are included in the results.
4. **Delta hedging only after −10% is a weak compromise.** It locks in the loss at the moment it starts. It helps a little in 1M and loses money in 3M.
5. **Your recovery rule must not be combined with hedges that realise losses.** The stop hedges and the "after −10%" delta hedge create extra losing months that the rule then tries to recover with bigger and bigger sizes. In 1M the size reached **12–517 lots**, with drawdowns of −$12k to −$1.4M. In 3M it reached up to 101 lots, with drawdowns down to −$277k. The naive stop went much further. **With daily delta hedging the rule stays tame:** at most 0.4–1.7 lots and drawdowns of −$1.4k to −$2.9k.
6. **3M vs 1M, unhedged:** 3M has fewer losing cycles (3–6 of 19 vs 8–19 of 58) but less total profit. It also needs your recovery rule to make money at ATM: fixed-size 3M ATM lost −$1.2k. **3M with a daily delta hedge** had the best profit per $ of drawdown in the whole test: 3M ATM made +$2.4k with a −$1.4k max drawdown.

### How to use these results

* **Don't use a fixed −10% stop as a hedge for short BTC puts.** Either it whipsaws (same-level buy-back) or it gets caught in rebounds (hold, or off at −5%).
* **If you hedge, delta-hedge daily from day 1 with the perp.** Because the drawdown is 2.5–7× smaller, you can sell a **larger** size for the same risk budget.
  * Example: 1 lot of **3M ATM, delta-hedged** (4× the base) would have made about **+$9.8k with about −$5.5k max drawdown**.
  * Unhedged 1M ATM at 0.25 lots made **+$1.3k with −$7.6k**.
  * Your unhedged recovery rule made +$24.1k but needed a −$31.4k drawdown and up to 6.9 lots.
* **If you keep your recovery rule,** use it only with daily delta hedging, or with no hedge plus a size cap (Part 1). Never use it with stop-type hedges.
* **Limits of the delta-hedged results:**
  * Deltas are model-based (Black-Scholes plus DVOL), and re-hedging is once a day.
  * A one-day crash still hits the position, and it also loses when implied vol spikes.
  * The edge is small in dollars (about $150–500 a year per 0.25 lot), so execution costs matter.

## Reproduce

```bash
pip install pandas matplotlib scipy
python3 backtest.py          # Part 1: downloads + caches Deribit data in ./data, writes ./results
python3 report.py            # Part 1 chart + stress test
python3 hedged_backtest.py   # Part 2: 1M/3M x hedges (downloads ~25 MB of 1-minute BTC bars once)
python3 report_hedged.py     # Part 2 tables + charts
```

Cached data is committed in `data/`, except the 1-minute bars, which are re-downloaded. Reruns only hit the API for anything not cached yet. Strike levels, base size, fees, triggers and slippage are constants at the top of `backtest.py` and `hedged_backtest.py`.
