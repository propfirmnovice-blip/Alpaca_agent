import time, json, os, requests
from datetime import date
import candidates as C

T = "https://paper-api.alpaca.markets"
H = {"APCA-API-KEY-ID": os.environ["ALPACA_KEY"],
     "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET"]}
SYM, WIDTH, TARGET_DELTA, WAIT = "SPY", 2.0, 0.13, 240

spot = C.underlying_price(SYM)
contracts = C.list_contracts(SYM, 5, spot)
snaps = C.snapshots(SYM, "indicative")
by_exp = {}
for c in contracts:
    by_exp.setdefault(c["expiration_date"], {})[float(c["strike_price"])] = c

exp = sorted(e for e in by_exp if date.fromisoformat(e) > date.today())[0]
ks = by_exp[exp]
dte = (date.fromisoformat(exp) - date.today()).days
Tm = max(dte, 0.5)/365.0
print(f"spot {spot:.2f} | expiry {exp} (dte {dte})")

best = None
for k in sorted(ks, reverse=True):
    if k - WIDTH not in ks: continue
    qs, ql = C.leg_quote(snaps.get(ks[k]["symbol"])), C.leg_quote(snaps.get(ks[k-WIDTH]["symbol"]))
    if not qs or not ql: continue
    d = ((snaps.get(ks[k]["symbol"]) or {}).get("greeks") or {}).get("delta")
    if d is None:
        iv = C.implied_vol_put(qs[2], spot, k, Tm)
        d = C.put_delta(spot, k, Tm, iv) if iv else None
    if d is None: continue
    d = abs(d)
    if best is None or abs(d-TARGET_DELTA) < abs(best[1]-TARGET_DELTA):
        best = (k, d, qs, ql)

k, d, qs, ql = best
mid = round(qs[2]-ql[2], 2)
natural = round(qs[0]-ql[1], 2)
print(f"\nstrike {k:.0f}/{k-WIDTH:.0f}  delta {d:.3f}  {(spot-k)/spot*100:.2f}% OTM")
print(f"mid credit {mid:.2f} ({mid/WIDTH*100:.1f}% of width) | natural {natural:.2f}")
print(f"crossing would cost {(mid-natural)*100:.0f}c")

p = {"order_class":"mleg","qty":"1","type":"limit","time_in_force":"day",
     "limit_price": str(-abs(mid)),
     "legs":[{"symbol":ks[k]["symbol"],"side":"sell","ratio_qty":"1","position_intent":"sell_to_open"},
             {"symbol":ks[k-WIDTH]["symbol"],"side":"buy","ratio_qty":"1","position_intent":"buy_to_open"}]}
r = requests.post(f"{T}/v2/orders", json=p, headers=H, timeout=20)
print(f"\nsubmitted at {-abs(mid)} -> HTTP {r.status_code}")
if r.status_code not in (200,201):
    print(r.text[:400]); raise SystemExit
oid = r.json()["id"]

for i in range(WAIT//15):
    time.sleep(15)
    s = requests.get(f"{T}/v2/orders/{oid}", headers=H, timeout=20).json()
    print(f"  {(i+1)*15:3d}s  {s['status']}  filled_avg={s.get('filled_avg_price')}")
    if s["status"] == "filled":
        print(f"\nFILLED AT MID. credit {abs(float(s['filled_avg_price'])):.2f}")
        raise SystemExit
requests.delete(f"{T}/v2/orders/{oid}", headers=H, timeout=20)
print("\nNO FILL at mid after 4 min - cancelled.")
