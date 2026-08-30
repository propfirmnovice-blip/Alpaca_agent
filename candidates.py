"""
candidates.py — scan an Alpaca options chain and build ranked put credit spreads.

Deterministic. No LLM here: this produces the shortlist that the gatekeeper
later approves, shrinks, or vetoes.

Usage:
  set ALPACA_KEY=...
  set ALPACA_SECRET=...
  python candidates.py --symbol SPY --max-dte 3 --width 5
"""

import argparse
import math
import os
import sys
from dataclasses import dataclass, asdict
from datetime import date, timedelta

import requests

TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"


def headers():
    k, s = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    if not k or not s:
        sys.exit("Set ALPACA_KEY and ALPACA_SECRET first.")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


# ------------------------------------------------------- Black-Scholes fallback
# Alpaca returns greeks on the OPRA feed. On the indicative feed they may be
# missing, so we solve for IV from the mid and compute delta ourselves.

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S, K, T, sigma, r=0.045):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def put_delta(S, K, T, sigma, r=0.045):
    if T <= 0 or sigma <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


def implied_vol_put(price, S, K, T, lo=0.01, hi=5.0, tol=1e-4):
    if T <= 0 or price <= 0:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_put(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


# ------------------------------------------------------------------- fetching

def underlying_price(symbol):
    r = requests.get(f"{DATA}/v2/stocks/{symbol}/trades/latest",
                     headers=headers(), params={"feed": "iex"}, timeout=20)
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


def list_contracts(symbol, max_dte, spot, band=0.12):
    """Puts expiring within max_dte, strikes within `band` below spot."""
    params = {
        "underlying_symbols": symbol,
        "status": "active",
        "type": "put",
        "expiration_date_gte": date.today().isoformat(),
        "expiration_date_lte": (date.today() + timedelta(days=max_dte)).isoformat(),
        "strike_price_gte": str(round(spot * (1 - band), 2)),
        "strike_price_lte": str(round(spot, 2)),
        "limit": 1000,
    }
    out, page = [], None
    while True:
        if page:
            params["page_token"] = page
        r = requests.get(f"{TRADING}/v2/options/contracts", headers=headers(),
                         params=params, timeout=30)
        r.raise_for_status()
        js = r.json()
        out.extend(js.get("option_contracts") or [])
        page = js.get("next_page_token")
        if not page:
            break
    return out


def snapshots(symbol, feed):
    """Quotes (and greeks, on OPRA) keyed by contract symbol."""
    out, page = {}, None
    while True:
        params = {"feed": feed, "limit": 1000}
        if page:
            params["page_token"] = page
        r = requests.get(f"{DATA}/v1beta1/options/snapshots/{symbol}",
                         headers=headers(), params=params, timeout=30)
        if r.status_code == 403:
            sys.exit("403 on options snapshots — your data plan may not cover "
                     "this feed. Try --feed indicative.")
        r.raise_for_status()
        js = r.json()
        out.update(js.get("snapshots") or {})
        page = js.get("next_page_token")
        if not page:
            break
    return out


# ------------------------------------------------------------------ building

@dataclass
class Spread:
    underlying: str
    expiry: str
    dte: int
    short_symbol: str
    long_symbol: str
    short_strike: float
    long_strike: float
    width: float
    credit: float
    credit_pct_width: float
    short_delta: float
    max_loss: float
    short_spread_pct: float   # bid-ask width on the short leg, as % of mid
    open_interest: int


def leg_quote(snap):
    q = (snap or {}).get("latestQuote") or {}
    bid, ask = q.get("bp"), q.get("ap")
    if not bid or not ask or ask <= 0:
        return None
    return float(bid), float(ask), 0.5 * (float(bid) + float(ask))


def build(symbol, contracts, snaps, spot, width, delta_lo, delta_hi,
          min_credit_pct, max_leg_spread_pct, min_oi):
    by_expiry = {}
    for c in contracts:
        by_expiry.setdefault(c["expiration_date"], {})[float(c["strike_price"])] = c

    out = []
    for expiry, strikes in by_expiry.items():
        dte = (date.fromisoformat(expiry) - date.today()).days
        T = max(dte, 0.5) / 365.0
        for k_short in sorted(strikes):
            k_long = k_short - width
            if k_long not in strikes:
                continue
            cs, cl = strikes[k_short], strikes[k_long]
            qs = leg_quote(snaps.get(cs["symbol"]))
            ql = leg_quote(snaps.get(cl["symbol"]))
            if not qs or not ql:
                continue
            s_bid, s_ask, s_mid = qs
            _, l_ask, l_mid = ql

            greeks = (snaps.get(cs["symbol"]) or {}).get("greeks") or {}
            delta = greeks.get("delta")
            if delta is None:
                iv = implied_vol_put(s_mid, spot, k_short, T)
                delta = put_delta(spot, k_short, T, iv) if iv else None
            if delta is None:
                continue
            delta = abs(float(delta))
            if not (delta_lo <= delta <= delta_hi):
                continue

            credit = s_mid - l_mid
            if credit <= 0:
                continue
            pct = credit / width
            if pct < min_credit_pct:
                continue

            leg_spread_pct = (s_ask - s_bid) / s_mid if s_mid > 0 else 9.9
            if leg_spread_pct > max_leg_spread_pct:
                continue

            oi = int(cs.get("open_interest") or 0)
            if oi < min_oi:
                continue

            out.append(Spread(
                underlying=symbol, expiry=expiry, dte=dte,
                short_symbol=cs["symbol"], long_symbol=cl["symbol"],
                short_strike=k_short, long_strike=k_long, width=width,
                credit=round(credit, 3), credit_pct_width=round(pct, 4),
                short_delta=round(delta, 4),
                max_loss=round((width - credit) * 100, 2),
                short_spread_pct=round(leg_spread_pct, 4),
                open_interest=oi,
            ))

    out.sort(key=lambda s: s.credit_pct_width / max(s.short_delta, 1e-6), reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--max-dte", type=int, default=3)
    ap.add_argument("--width", type=float, default=5.0)
    ap.add_argument("--delta-lo", type=float, default=0.08)
    ap.add_argument("--delta-hi", type=float, default=0.16)
    ap.add_argument("--min-credit-pct", type=float, default=0.18)
    ap.add_argument("--max-leg-spread-pct", type=float, default=0.15)
    ap.add_argument("--min-oi", type=int, default=100)
    ap.add_argument("--feed", default="indicative", choices=["indicative", "opra"])
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()

    spot = underlying_price(a.symbol)
    contracts = list_contracts(a.symbol, a.max_dte, spot)
    snaps = snapshots(a.symbol, a.feed)
    print(f"{a.symbol} spot {spot:.2f} | {len(contracts)} put contracts "
          f"<= {a.max_dte} DTE | {len(snaps)} snapshots ({a.feed} feed)")

    rows = build(a.symbol, contracts, snaps, spot, a.width, a.delta_lo,
                 a.delta_hi, a.min_credit_pct, a.max_leg_spread_pct, a.min_oi)
    if not rows:
        print("No candidates passed the filters.")
        return
    print(f"\n{'expiry':>11} {'short':>8} {'long':>8} {'credit':>7} "
          f"{'%width':>7} {'delta':>6} {'maxloss':>8} {'OI':>7}")
    for s in rows[:a.top]:
        print(f"{s.expiry:>11} {s.short_strike:8.1f} {s.long_strike:8.1f} "
              f"{s.credit:7.2f} {s.credit_pct_width*100:6.1f}% "
              f"{s.short_delta:6.3f} {s.max_loss:8.2f} {s.open_interest:7d}")
    print("\nRanked by credit-per-unit-of-delta. Feed candidates[0] to the gatekeeper.")


if __name__ == "__main__":
    main()
