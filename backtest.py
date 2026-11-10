#!/usr/bin/env python3
"""
Backtest: sell 1-month BTC puts every month on Deribit, starting from 2022.

Strategy
--------
* Every Deribit monthly expiry (last Friday of the month, 08:00 UTC) we sell a
  put that expires at the NEXT monthly expiry (~1 month to expiry).
* Base size is 0.25 lots (1 lot = 1 BTC on Deribit).
* If a put expires in the money and the month ends with a net loss, the loss is
  added to a "deficit". Next month the size is increased so that the premium
  collected covers the deficit:

      size = base + deficit / premium_per_lot          (mode "base+cover", default)
      size = max(base, deficit / premium_per_lot)      (mode "cover-only")
      size = min(base + deficit / premium_per_lot, 2)  (mode "capped-2")

  Once the deficit is paid back the size goes back to 0.25 lots. If the
  recovery month loses too, its loss is added to the deficit (cumulative).
* For comparison the same trades are also run with a fixed 0.25 lot size.

Data (all real Deribit market data, no model prices)
----------------------------------------------------
* Entry premium: Deribit mark price of the chosen put from actual trades in the
  first hours after the previous monthly expiry (history.deribit.com).
* Spot at entry / settlement: Deribit BTC delivery price (08:00 UTC index TWAP),
  which is exactly what Deribit options settle against.
* Strikes: only strikes Deribit actually listed (and that traded) are used.
* Fees: Deribit option fee 0.03% of underlying (capped at 12.5% of premium) at
  entry, 0.015% delivery fee (capped at 12.5% of payout) when expiring ITM.

Run:  python3 backtest.py            (downloads + caches data in ./data)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "results")
os.makedirs(DATA, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

MAIN_API = "https://www.deribit.com/api/v2/public/"
HIST_API = "https://history.deribit.com/api/v2/public/"

START = pd.Timestamp("2021-12-31 08:00")  # Dec-21 expiry -> first put expires 28 Jan 2022
BASE_LOTS = 0.25
OTM_LEVELS = [0.00, 0.05, 0.10, 0.15, 0.20]  # strike = spot * (1 - otm)
OPT_FEE_RATE = 0.0003       # 0.03% of underlying per contract
DELIVERY_FEE_RATE = 0.00015  # 0.015% of underlying per contract
FEE_CAP = 0.125              # fee capped at 12.5% of option value
MAX_LOTS_CAPPED = 2.0        # size cap for the "capped-2" variant


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def get(base: str, method: str, **params) -> dict:
    url = base + method + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read())["result"]
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
            print(f"  retry {method}: {exc}", file=sys.stderr)
    raise RuntimeError("unreachable")


def ms(ts: pd.Timestamp) -> int:
    return int(ts.tz_localize("UTC").timestamp() * 1000) if ts.tzinfo is None else int(ts.timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Data loading (cached)
# --------------------------------------------------------------------------- #
def load_instruments() -> pd.DataFrame:
    """All monthly BTC puts expiring from Dec 2021 onwards (expired + live)."""
    path = os.path.join(DATA, "monthly_btc_puts.csv")
    if os.path.exists(path):
        return pd.read_csv(path, parse_dates=["expiry", "created"])
    rows = []
    for base, expired in ((HIST_API, "true"), (MAIN_API, "false")):
        res = get(base, "get_instruments", currency="BTC", kind="option", expired=expired)
        for x in res:
            if x["option_type"] != "put" or x["settlement_period"] != "month":
                continue
            rows.append(dict(
                instrument=x["instrument_name"], strike=x["strike"],
                expiry=pd.to_datetime(x["expiration_timestamp"], unit="ms"),
                created=pd.to_datetime(x["creation_timestamp"], unit="ms"),
            ))
    df = pd.DataFrame(rows).drop_duplicates("instrument")
    df = df[(df.expiry >= START) & (df.expiry.dt.hour == 8) & (df.expiry.dt.minute == 0)]
    df = df.sort_values(["expiry", "strike"]).reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


def load_delivery_prices() -> pd.Series:
    """Daily Deribit btc_usd delivery price (08:00 UTC settlement index)."""
    path = os.path.join(DATA, "btc_delivery_prices.csv")
    if os.path.exists(path):
        s = pd.read_csv(path, parse_dates=["date"]).set_index("date")["delivery_price"]
        return s
    rows, offset = [], 0
    while True:
        res = get(MAIN_API, "get_delivery_prices", index_name="btc_usd", count=100, offset=offset)
        rows += res["data"]
        offset += len(res["data"])  # API returns at most 100 rows per page
        if not res["data"] or offset >= res["records_total"] or rows[-1]["date"] < "2021-12-01":
            break
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df.date >= "2021-12-01"].sort_values("date")
    df.to_csv(path, index=False)
    return df.set_index("date")["delivery_price"]


_trade_cache_path = os.path.join(DATA, "entry_trades_cache.json")
_trade_cache: dict = json.load(open(_trade_cache_path)) if os.path.exists(_trade_cache_path) else {}


def entry_quote(instrument: str, t0: pd.Timestamp) -> dict | None:
    """Premium of `instrument` right after t0 from real Deribit trades.

    Uses the median mark price (BTC) and median USD value (mark * index) of all
    trades in the first 2 hours after t0 (falls back to 6h, then 24h).
    """
    key = f"{instrument}@{t0.isoformat()}"
    if key in _trade_cache:
        return _trade_cache[key]
    quote = None
    for hours in (2, 6, 24):
        res = get(HIST_API, "get_last_trades_by_instrument_and_time",
                  instrument_name=instrument, start_timestamp=ms(t0),
                  end_timestamp=ms(t0 + pd.Timedelta(hours=hours)),
                  count=1000, sorting="asc", include_old="true")
        trades = res["trades"]
        if trades:
            t = pd.DataFrame(trades)
            t["usd"] = t.mark_price * t.index_price
            quote = dict(
                mark_btc=float(t.mark_price.median()),
                premium_usd=float(t.usd.median()),
                index=float(t.index_price.median()),
                iv=float(t.loc[t.iv > 0, "iv"].median()) if (t.iv > 0).any() else None,
                n_trades=int(len(t)), window_h=hours,
            )
            break
    _trade_cache[key] = quote
    json.dump(_trade_cache, open(_trade_cache_path, "w"), indent=0)
    time.sleep(0.05)
    return quote


def live_mark(instrument: str) -> dict | None:
    try:
        r = get(MAIN_API, "ticker", instrument_name=instrument)
        return dict(mark_usd=r["mark_price"] * r["underlying_price"], index=r["index_price"],
                    ts=pd.to_datetime(r["timestamp"], unit="ms"))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Build the monthly trade list for one strike rule (independent of sizing)
# --------------------------------------------------------------------------- #
def build_trades(inst: pd.DataFrame, px: pd.Series, otm: float) -> pd.DataFrame:
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    expiries = sorted(inst.expiry.unique())
    rows = []
    for t0, t1 in zip(expiries[:-1], expiries[1:]):
        t0, t1 = pd.Timestamp(t0), pd.Timestamp(t1)
        if t0 < START or t0 > now:  # only past entries (the last one may still be open)
            continue
        s0 = float(px.get(t0.normalize(), float("nan")))
        if math.isnan(s0):
            raise RuntimeError(f"missing delivery price for {t0.date()}")
        target = s0 * (1 - otm)
        cands = inst[(inst.expiry == t1) & (inst.created <= t0)].copy()
        cands["dist"] = (cands.strike - target).abs()
        quote = None
        for _, c in cands.sort_values(["dist", "strike"]).head(6).iterrows():
            quote = entry_quote(c.instrument, t0)
            if quote:
                break
        if quote is None:
            print(f"  !! no tradable put for {t1.date()} otm={otm}", file=sys.stderr)
            continue
        k = float(c.strike)
        settled = t1 <= now
        st = float(px.get(t1.normalize(), float("nan"))) if settled else float("nan")
        # lowest daily settlement-index print during the life of the option
        life = px[(px.index > t0.normalize()) & (px.index <= t1.normalize())]
        rows.append(dict(
            entry=t0, expiry=t1, instrument=c.instrument, spot_entry=s0, strike=k,
            otm_actual=1 - k / s0, iv=quote["iv"], prem_btc=quote["mark_btc"],
            prem_usd=quote["premium_usd"], spot_expiry=st,
            min_spot_life=float(life.min()) if len(life) else float("nan"),
            settled=settled,
        ))
    df = pd.DataFrame(rows)
    # per-lot economics
    df["fee_entry"] = [min(OPT_FEE_RATE * s, FEE_CAP * p) for s, p in zip(df.spot_entry, df.prem_usd)]
    df["payout"] = (df.strike - df.spot_expiry).clip(lower=0)
    df["fee_settle"] = [min(DELIVERY_FEE_RATE * s, FEE_CAP * p) if p > 0 else 0.0
                        for s, p in zip(df.spot_expiry, df.payout)]
    df["itm"] = df.payout > 0
    # open position: mark it to the current Deribit mark price
    for i in df.index[~df.settled]:
        m = live_mark(df.at[i, "instrument"])
        if m:
            df.at[i, "payout"] = m["mark_usd"]           # cost to buy back now
            df.at[i, "spot_expiry"] = m["index"]
            df.at[i, "fee_settle"] = 0.0
            df.at[i, "itm"] = df.at[i, "strike"] > m["index"]
    df["pnl_per_lot"] = df.prem_usd - df.fee_entry - df.payout - df.fee_settle
    return df


# --------------------------------------------------------------------------- #
# Sizing simulation
# --------------------------------------------------------------------------- #
def simulate(trades: pd.DataFrame, mode: str) -> pd.DataFrame:
    df = trades.copy()
    deficit = 0.0
    sizes, deficits_before, pnls, deficits_after = [], [], [], []
    for _, r in df.iterrows():
        prem_net = r.prem_usd - r.fee_entry  # premium actually kept per lot
        if mode == "fixed":
            size = BASE_LOTS
        elif mode == "base+cover":
            size = BASE_LOTS + deficit / prem_net
        elif mode == "cover-only":
            size = max(BASE_LOTS, deficit / prem_net)
        elif mode == "capped-2":
            size = min(BASE_LOTS + deficit / prem_net, MAX_LOTS_CAPPED)
        else:
            raise ValueError(mode)
        size = math.ceil(size * 100 - 1e-9) / 100  # round up to 0.01 lot
        pnl = size * r.pnl_per_lot
        deficits_before.append(deficit)
        if mode != "fixed":
            deficit = deficit - pnl if pnl < 0 else max(0.0, deficit - pnl)
        sizes.append(size)
        pnls.append(pnl)
        deficits_after.append(deficit)
    df["mode"] = mode
    df["size_lots"] = sizes
    df["deficit_before"] = deficits_before
    df["pnl"] = pnls
    df["deficit_after"] = deficits_after
    df["cum_pnl"] = df.pnl.cumsum()
    df["notional_usd"] = df.size_lots * df.strike  # cash needed to fully secure the put
    df["premium_collected"] = df.size_lots * df.prem_usd
    df["worst_intramonth_intrinsic"] = df.size_lots * (df.strike - df.min_spot_life).clip(lower=0)
    return df


def summarize(sim: pd.DataFrame) -> dict:
    s = sim[sim.settled]
    cum = s.pnl.cumsum()
    dd = (cum - cum.cummax().clip(lower=0)).min()
    streak = best = 0
    for lost in s.pnl_per_lot < 0:
        streak = streak + 1 if lost else 0
        best = max(best, streak)
    return {
        "months": len(s),
        "ITM months": int(s.itm.sum()),
        "losing months": int((s.pnl_per_lot < 0).sum()),
        "win rate": f"{(s.pnl_per_lot >= 0).mean():.0%}",
        "total P&L $": round(s.pnl.sum()),
        "premium collected $": round(s.premium_collected.sum()),
        "worst month $": round(s.pnl.min()),
        "max drawdown $": round(dd),
        "max size (lots)": round(s.size_lots.max(), 2),
        "max notional $": round(s.notional_usd.max()),
        "max deficit $": round(s.deficit_after.max()),
        "longest losing streak": best,
        "end deficit $": round(s.deficit_after.iloc[-1]) if len(s) else 0,
        "P&L / max notional": f"{s.pnl.sum() / s.notional_usd.max():.1%}",
    }


def main() -> None:
    print("Loading instruments ...")
    inst = load_instruments()
    print("Loading BTC delivery prices ...")
    px = load_delivery_prices()
    all_sims, summary = [], []
    for otm in OTM_LEVELS:
        label = "ATM" if otm == 0 else f"{int(otm * 100)}% OTM"
        print(f"Building trades for {label} ...")
        trades = build_trades(inst, px, otm)
        for mode in ("base+cover", "cover-only", "capped-2", "fixed"):
            sim = simulate(trades, mode)
            sim.insert(0, "strike_rule", label)
            all_sims.append(sim)
            summary.append({"strike rule": label, "sizing": mode, **summarize(sim)})
    sims = pd.concat(all_sims, ignore_index=True)
    sims.to_csv(os.path.join(OUT, "all_trades.csv"), index=False)
    summ = pd.DataFrame(summary)
    summ.to_csv(os.path.join(OUT, "summary.csv"), index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(summ.to_string(index=False))


if __name__ == "__main__":
    main()
