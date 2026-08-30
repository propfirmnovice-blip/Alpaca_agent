"""
validate_spread.py — empirical payoff test for short-dated put credit spreads.

Idea
----
A put credit spread's payoff at expiry depends only on (a) the underlying's
forward return over the holding period and (b) the credit you collected.
So we can reconstruct the exact payoff over years of free daily bars without
any historical options data.

Payoff per $1 of spread width, with credit c (as a fraction of width),
short strike s% below spot, long strike (s+w)% below spot:

    R = forward return over h days
    loss_fraction L = clip( ((-R) - s) / w , 0, 1 )
    payoff = c - L

Therefore  E[payoff] = c - E[L], and the BREAKEVEN CREDIT is simply

    c* = E[L]

If the market pays more than c*, the rule has positive expectancy.
If it pays less, it does not. That is the number to take into Friday.

Usage
-----
  python validate_spread.py --symbol SPY --start 2018-01-01
  python validate_spread.py --symbol SPY --csv spy_daily.csv
  python validate_spread.py --synthetic        (offline self-test)

Env: ALPACA_KEY / ALPACA_SECRET (free IEX feed is sufficient for daily bars).
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd

BARS_URL = "https://data.alpaca.markets/v2/stocks/{sym}/bars"


# ---------------------------------------------------------------- data

def fetch_bars(symbol, start, end, feed="iex"):
    import requests
    key, sec = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    if not key or not sec:
        sys.exit("Set ALPACA_KEY and ALPACA_SECRET first.")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    params = {"timeframe": "1Day", "start": start, "end": end,
              "adjustment": "all", "feed": feed, "limit": 10000}
    rows, page = [], None
    while True:
        if page:
            params["page_token"] = page
        r = requests.get(BARS_URL.format(sym=symbol), headers=headers,
                         params=params, timeout=30)
        r.raise_for_status()
        js = r.json()
        rows.extend(js.get("bars") or [])
        page = js.get("next_page_token")
        if not page:
            break
    if not rows:
        sys.exit(f"No bars returned for {symbol}.")
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"])
    return df[["t", "c"]].rename(columns={"c": "close"}).sort_values("t").reset_index(drop=True)


def synthetic_bars(n=2200, seed=7):
    """Fat-tailed random walk with vol clustering — for offline smoke-testing."""
    rng = np.random.default_rng(seed)
    vol = 0.008
    out, px = [], 300.0
    for _ in range(n):
        vol = 0.90 * vol + 0.10 * 0.008 + 0.02 * abs(rng.standard_normal()) * 0.008
        px *= 1 + rng.standard_t(4) * vol * 0.5 + 0.0003
        out.append(px)
    return pd.DataFrame({"t": pd.bdate_range("2017-01-02", periods=n), "close": out})


# ---------------------------------------------------------------- core

def loss_fractions(closes, horizon, short_otm, width_otm):
    """Loss fraction L in [0,1] for every entry day, per $1 of width."""
    px = np.asarray(closes, dtype=float)
    fwd = px[horizon:] / px[:-horizon] - 1.0
    excess = (-fwd) - short_otm            # how far past the short strike
    return np.clip(excess / width_otm, 0.0, 1.0), fwd


def summarise(df, horizon, short_otm, width_otm, by_year=True):
    L, fwd = loss_fractions(df["close"].values, horizon, short_otm, width_otm)
    years = df["t"].dt.year.values[:-horizon]

    out = {
        "n": len(L),
        "breakeven_credit": L.mean(),
        "p_untouched": float((L == 0).mean()),
        "p_max_loss": float((L >= 1.0).mean()),
        "worst_1pct_L": float(np.quantile(L, 0.99)),
    }
    if by_year:
        yr = pd.DataFrame({"year": years, "L": L}).groupby("year")["L"].agg(["mean", "count"])
        out["by_year"] = yr
    return out


def report(df, horizons, short_range, width_otm, credit_offered=None):
    print(f"\nSample: {df['t'].min().date()} to {df['t'].max().date()}  "
          f"({len(df)} sessions)")
    print(f"Long strike sits {width_otm*100:.2f}% below the short strike.\n")

    for h in horizons:
        print(f"--- {h}-day hold " + "-" * 52)
        print(f"{'shortOTM':>9} {'breakeven c*':>13} {'P(clean)':>9} "
              f"{'P(maxloss)':>11} {'worst yr c*':>12}")
        for s in short_range:
            r = summarise(df, h, s, width_otm)
            worst_year = r["by_year"]["mean"].max()
            print(f"{s*100:8.2f}% {r['breakeven_credit']*100:12.2f}% "
                  f"{r['p_untouched']*100:8.1f}% {r['p_max_loss']*100:10.2f}% "
                  f"{worst_year*100:11.2f}%")
        print()

    if credit_offered is not None:
        print(f"=== Edge check at a credit of {credit_offered*100:.0f}% of width ===")
        for h in horizons:
            for s in short_range:
                r = summarise(df, h, s, width_otm)
                ev = credit_offered - r["breakeven_credit"]
                worst = credit_offered - r["by_year"]["mean"].max()
                flag = "EDGE" if ev > 0 else "no edge"
                print(f"  {h}d, short {s*100:.2f}% OTM: EV {ev*100:+.2f}% of width "
                      f"({flag}); worst year {worst*100:+.2f}%")
        print("\nEV is per $1 of width. On a $5-wide spread, +2.00% = +$10 per contract.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--csv", default=None, help="CSV with columns t,close")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--horizons", default="1,2,3")
    ap.add_argument("--short-otm", default="0.5,0.75,1.0,1.5,2.0,3.0",
                    help="percent OTM for the short strike")
    ap.add_argument("--width-otm", type=float, default=1.0,
                    help="percent of spot between short and long strike")
    ap.add_argument("--credit", type=float, default=20.0,
                    help="credit offered, as percent of width")
    a = ap.parse_args()

    if a.synthetic:
        df = synthetic_bars()
        print("[synthetic data — self-test only]")
    elif a.csv:
        df = pd.read_csv(a.csv, parse_dates=["t"]).sort_values("t").reset_index(drop=True)
    else:
        end = a.end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")
        df = fetch_bars(a.symbol, a.start, end)

    horizons = [int(x) for x in a.horizons.split(",")]
    shorts = [float(x) / 100 for x in a.short_otm.split(",")]
    report(df, horizons, shorts, a.width_otm / 100, a.credit / 100)


if __name__ == "__main__":
    main()
