import argparse, json, os, sys, requests
T = "https://paper-api.alpaca.markets"

def H():
    k, s = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    if not k or not s: sys.exit("Set ALPACA_KEY and ALPACA_SECRET first.")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}

def occ(u, e, k):
    y, m, d = e.split("-")
    return f"{u}{y[2:]}{m}{d}P{int(round(k*1000)):08d}"

def verify(s):
    r = requests.get(f"{T}/v2/options/contracts/{s}", headers=H(), timeout=20)
    return r.json() if r.status_code == 200 else None

def pl(ss, ls, lim):
    return {"order_class":"mleg","qty":"1","type":"limit","time_in_force":"day",
        "limit_price":str(lim),
        "legs":[{"symbol":ss,"side":"sell","ratio_qty":"1","position_intent":"sell_to_open"},
                {"symbol":ls,"side":"buy","ratio_qty":"1","position_intent":"buy_to_open"}]}

def go(p, label):
    print(f"\n--- {label} (limit {p['limit_price']}) ---")
    r = requests.post(f"{T}/v2/orders", json=p, headers=H(), timeout=20)
    print("HTTP", r.status_code)
    try: b = r.json()
    except Exception: print(r.text[:500]); return None
    if r.status_code in (200, 201):
        print(f"ACCEPTED id={b.get('id')} status={b.get('status')} limit={b.get('limit_price')}")
        return b
    print("REJECTED:", json.dumps(b)[:400]); return None

a = argparse.ArgumentParser()
a.add_argument("--under", default="SPY")
a.add_argument("--short", type=float, required=True)
a.add_argument("--long", type=float, required=True)
a.add_argument("--expiry", required=True)
a.add_argument("--limit", type=float, default=0.90)
a.add_argument("--transmit", action="store_true")
a.add_argument("--keep", action="store_true")
a = a.parse_args()

ss, ls = occ(a.under, a.expiry, a.short), occ(a.under, a.expiry, a.long)
print("short:", ss, "\nlong: ", ls)
for s in (ss, ls):
    c = verify(s)
    if not c: sys.exit(f"{s} not found")
    print(f"  ok {c['name']} tradable={c['tradable']} OI={c.get('open_interest')}")

p = pl(ss, ls, a.limit)
print("\nPAYLOAD:", json.dumps(p, indent=1))
if not a.transmit:
    print("\nDry run. Add --transmit to send."); sys.exit()

o = go(p, "positive limit")
if o:
    if not a.keep:
        r = requests.delete(f"{T}/v2/orders/{o['id']}", headers=H(), timeout=20)
        print("cancel HTTP", r.status_code)
    print("\nRESULT: NET_CREDIT_SIGN = 1.0"); sys.exit()

o = go(pl(ss, ls, -a.limit), "negative limit")
if o:
    if not a.keep:
        r = requests.delete(f"{T}/v2/orders/{o['id']}", headers=H(), timeout=20)
        print("cancel HTTP", r.status_code)
    print("\nRESULT: NET_CREDIT_SIGN = -1.0"); sys.exit()
print("\nBoth rejected — paste the error text.")
