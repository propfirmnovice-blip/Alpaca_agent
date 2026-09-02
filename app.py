"""
app.py — live dashboard for the SPY put-credit-spread agent.

Deployed on Streamlit Community Cloud. Read-only: it queries the competition
paper account and re-runs the agent's entry test against the live chain, so a
visitor sees the same decision the agent would make right now.

Secrets (Streamlit Cloud -> App settings -> Secrets):
    ALPACA_KEY = "..."
    ALPACA_SECRET = "..."
"""

import math
import os
from datetime import date, datetime, timezone

import pandas as pd
import requests
import streamlit as st

TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"

SYMBOL = "SPY"
WIDTH = 2.0
TARGET_DELTA = 0.13
FLOOR = 0.0740          # measured breakeven, SPY 1-day hold, ~1% OTM, 1993-2026

st.set_page_config(page_title="Credit spread agent", layout="wide")


# ------------------------------------------------------------------ auth

def creds():
    k = st.secrets.get("ALPACA_KEY", os.environ.get("ALPACA_KEY"))
    s = st.secrets.get("ALPACA_SECRET", os.environ.get("ALPACA_SECRET"))
    return k, s


def headers():
    k, s = creds()
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


# ------------------------------------------------------------------ data

@st.cache_data(ttl=60)
def account():
    r = requests.get(f"{TRADING}/v2/account", headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=60)
def positions():
    r = requests.get(f"{TRADING}/v2/positions", headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=60)
def orders():
    r = requests.get(f"{TRADING}/v2/orders", headers=headers(),
                     params={"status": "all", "limit": 100, "nested": "true"},
                     timeout=20)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=120)
def spot():
    r = requests.get(f"{DATA}/v2/stocks/{SYMBOL}/trades/latest",
                     headers=headers(), params={"feed": "iex"}, timeout=20)
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


@st.cache_data(ttl=120)
def chain(px):
    params = {"underlying_symbols": SYMBOL, "status": "active", "type": "put",
              "expiration_date_gte": date.today().isoformat(),
              "expiration_date_lte": (date.today() + pd.Timedelta(days=5)).isoformat(),
              "strike_price_gte": str(round(px * 0.90, 2)),
              "strike_price_lte": str(round(px, 2)), "limit": 1000}
    out, page = [], None
    while True:
        if page:
            params["page_token"] = page
        r = requests.get(f"{TRADING}/v2/options/contracts", headers=headers(),
                         params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        out.extend(j.get("option_contracts") or [])
        page = j.get("next_page_token")
        if not page:
            break
    snaps, page = {}, None
    while True:
        p = {"feed": "indicative", "limit": 1000}
        if page:
            p["page_token"] = page
        r = requests.get(f"{DATA}/v1beta1/options/snapshots/{SYMBOL}",
                         headers=headers(), params=p, timeout=30)
        r.raise_for_status()
        j = r.json()
        snaps.update(j.get("snapshots") or {})
        page = j.get("next_page_token")
        if not page:
            break
    return out, snaps


# ------------------------------------------------------- pricing helpers

def _cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S, K, T, sig, r=0.045):
    if T <= 0 or sig <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig ** 2) * T) / (sig * math.sqrt(T))
    return K * math.exp(-r * T) * _cdf(-(d1 - sig * math.sqrt(T))) - S * _cdf(-d1)


def put_delta(S, K, T, sig, r=0.045):
    if T <= 0 or sig <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sig ** 2) * T) / (sig * math.sqrt(T))
    return _cdf(d1) - 1.0


def iv_put(price, S, K, T):
    if T <= 0 or price <= 0:
        return None
    lo, hi = 0.01, 5.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if bs_put(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def quote(snap):
    q = (snap or {}).get("latestQuote") or {}
    b, a = q.get("bp"), q.get("ap")
    if not b or not a or a <= 0:
        return None
    return float(b), float(a), 0.5 * (float(b) + float(a))


def current_decision(px, contracts, snaps):
    by = {}
    for c in contracts:
        by.setdefault(c["expiration_date"], {})[float(c["strike_price"])] = c
    exps = sorted(e for e in by if date.fromisoformat(e) > date.today())
    if not exps:
        return None
    exp = exps[0]
    ks = by[exp]
    dte = (date.fromisoformat(exp) - date.today()).days
    T = max(dte, 0.5) / 365.0
    best = None
    for k in sorted(ks):
        if k - WIDTH not in ks:
            continue
        qs = quote(snaps.get(ks[k]["symbol"]))
        ql = quote(snaps.get(ks[k - WIDTH]["symbol"]))
        if not qs or not ql:
            continue
        g = (snaps.get(ks[k]["symbol"]) or {}).get("greeks") or {}
        d = g.get("delta")
        if d is None:
            v = iv_put(qs[2], px, k, T)
            d = put_delta(px, k, T, v) if v else None
        if d is None:
            continue
        d, cr = abs(float(d)), qs[2] - ql[2]
        if cr <= 0:
            continue
        row = {"expiry": exp, "dte": dte, "short": k, "long": k - WIDTH,
               "delta": d, "credit": round(cr, 2), "pct": cr / WIDTH,
               "natural": round(qs[0] - ql[1], 2)}
        if best is None or abs(d - TARGET_DELTA) < abs(best["delta"] - TARGET_DELTA):
            best = row
    return best


# ------------------------------------------------------------------- UI

st.title("SPY credit spread agent")
st.caption("Autonomous options agent · Alpaca MCP server · paper trading")

k, s = creds()
if not k or not s:
    st.error("No Alpaca credentials configured. Add ALPACA_KEY and "
             "ALPACA_SECRET in the app's Secrets settings.")
    st.stop()

try:
    acct = account()
except Exception as e:
    st.error(f"Could not reach Alpaca: {e}")
    st.stop()

eq = float(acct.get("equity", 0))
last_eq = float(acct.get("last_equity", eq))
start = 100_000.0

c1, c2, c3, c4 = st.columns(4)
c1.metric("Equity", f"${eq:,.2f}", f"{eq - start:+,.2f} since start")
c2.metric("Today", f"{eq - last_eq:+,.2f}")
c3.metric("Return", f"{(eq / start - 1) * 100:+.3f}%")
c4.metric("Options level", acct.get("options_trading_level", "—"))

st.divider()

# ---- what the agent would do right now
st.subheader("Live entry test")
st.write(
    "The agent scans the nearest expiry for a $2-wide put spread near 0.13 "
    f"delta, then compares the credit against a floor of {FLOOR*100:.2f}% of "
    "width. That floor is the breakeven credit measured across 33 years of "
    "SPY history — below it, the trade has negative expectancy, so the agent "
    "declines."
)

try:
    px = spot()
    contracts, snaps = chain(px)
    d = current_decision(px, contracts, snaps)
except Exception as e:
    d, px = None, None
    st.warning(f"Chain unavailable: {e}")

if d:
    a, b, c = st.columns([2, 1, 1])
    a.write(f"**{d['short']:.0f} / {d['long']:.0f} put spread**  ·  expiry "
            f"{d['expiry']} ({d['dte']}d)  ·  delta {d['delta']:.3f}")
    b.metric("Credit", f"{d['pct']*100:.1f}% of width")
    if d["pct"] >= FLOOR:
        c.success(f"PASS — above {FLOOR*100:.2f}%")
    else:
        c.error(f"DECLINE — below {FLOOR*100:.2f}%")
    st.caption(f"SPY {px:.2f} · mid credit ${d['credit']:.2f} · "
               f"natural ${d['natural']:.2f} · crossing costs "
               f"${d['credit'] - d['natural']:.2f}")
elif px:
    st.info("No qualifying spread on the current board.")

st.divider()

# ---- positions
st.subheader("Open positions")
try:
    ps = positions()
except Exception:
    ps = []
if ps:
    df = pd.DataFrame([{
        "Contract": p["symbol"], "Qty": p["qty"],
        "Entry": p["avg_entry_price"], "Current": p.get("current_price"),
        "Unrealised P&L": p["unrealized_pl"],
    } for p in ps])
    st.dataframe(df, use_container_width=True, hide_index=True)
else:
    st.write("Flat. No open positions.")

# ---- orders
st.subheader("Order history")
try:
    os_ = orders()
except Exception:
    os_ = []
rows = []
for o in os_:
    if o.get("order_class") != "mleg":
        continue
    rows.append({
        "Submitted": o["submitted_at"][:16].replace("T", " "),
        "Status": o["status"],
        "Limit": o.get("limit_price"),
        "Filled at": o.get("filled_avg_price") or "—",
        "Legs": " / ".join(l["symbol"][-8:] for l in (o.get("legs") or [])),
    })
if rows:
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.write("No multi-leg orders yet.")

st.divider()

# ---- the evidence
st.subheader("Where the floor comes from")
st.write(
    "A put credit spread's payoff at expiry is fully determined by the "
    "underlying's forward return and the credit collected. That means the "
    "whole payoff distribution can be reconstructed from daily bars alone, "
    "with no historical options data. Running that over every SPY session "
    "since 1993 gives the breakeven credit — the premium at which expectancy "
    "is exactly zero."
)
eviden = pd.DataFrame({
    "Short strike (% OTM)": ["0.50%", "0.75%", "1.00%", "1.50%", "2.00%"],
    "1-day breakeven": ["13.42%", "9.92%", "7.40%", "4.17%", "2.33%"],
    "3-day breakeven": ["21.38%", "17.94%", "15.01%", "10.50%", "7.25%"],
    "P(finishes clean), 1d": ["76.7%", "82.9%", "87.5%", "93.0%", "96.0%"],
})
st.dataframe(eviden, use_container_width=True, hide_index=True)
st.caption("SPY, 8,451 sessions, 1993–2026, dividend-adjusted. Holding one "
           "day rather than three roughly halves the required credit, which "
           "is why the agent only trades the nearest expiry.")

st.divider()

# ---- architecture
st.subheader("How it decides")
st.markdown(
    "- **Scan** — deterministic chain filter picks the $2-wide spread nearest "
    "0.13 delta on the front expiry\n"
    "- **Gate** — credit must clear the measured breakeven; below it the "
    "agent stands down rather than forcing a trade\n"
    "- **Size** — risk budget caps contracts; the review step may shrink a "
    "position but is clamped so it can never enlarge one\n"
    "- **Place** — submitted as an atomic multi-leg order through Alpaca's "
    "MCP server, so a partial fill can never leave a naked short put\n"
    "- **Run** — fires unattended on a schedule; no human in the loop"
)

st.caption(f"Last refreshed {datetime.now(timezone.utc):%H:%M UTC}. "
           "Account data cached for 60 seconds.")
