import json
from collections import Counter
from datetime import date
import candidates as C

sym, width, feed = "SPY", 5.0, "indicative"
spot = C.underlying_price(sym)
contracts = C.list_contracts(sym, 3, spot)
snaps = C.snapshots(sym, feed)
print(f"spot {spot:.2f} | {len(contracts)} contracts | {len(snaps)} snapshots\n")
print("CONTRACT:", json.dumps(contracts[0], indent=1)[:600])

syms = [c["symbol"] for c in contracts]
present = [s for s in syms if s in snaps]
print(f"\nin snapshots: {len(present)}/{len(syms)}")
if not present:
    print("NO OVERLAP\n snap keys:", list(snaps)[:3], "\n contracts:", syms[:3])
    raise SystemExit
print("\nSNAPSHOT:", json.dumps(snaps[present[0]], indent=1)[:900])

hq = sum(1 for s in present if (snaps[s] or {}).get("latestQuote"))
zb = sum(1 for s in present if not ((snaps[s] or {}).get("latestQuote") or {}).get("bp"))
gk = sum(1 for s in present if ((snaps[s] or {}).get("greeks") or {}).get("delta") is not None)
print(f"\nwith quote: {hq} | zero/no bid: {zb} | with delta: {gk}")
print("open_interest non-empty:", sum(1 for c in contracts if c.get("open_interest")))

by_exp = {}
for c in contracts:
    by_exp.setdefault(c["expiration_date"], {})[float(c["strike_price"])] = c
for e, ks in sorted(by_exp.items()):
    k = sorted(ks)
    g = Counter(round(k[i+1]-k[i], 2) for i in range(len(k)-1))
    print(f"{e}: {len(k)} strikes {k[0]:.0f}-{k[-1]:.0f} gaps {dict(g)}")

st, ex = Counter(), []
for e, ks in by_exp.items():
    dte = (date.fromisoformat(e) - date.today()).days
    T = max(dte, 0.5)/365.0
    for k in sorted(ks):
        st["1 pairs"] += 1
        if k - width not in ks:
            st["2 no long strike"] += 1; continue
        qs, ql = C.leg_quote(snaps.get(ks[k]["symbol"])), C.leg_quote(snaps.get(ks[k-width]["symbol"]))
        if not qs or not ql:
            st["3 missing quote"] += 1; continue
        d = ((snaps.get(ks[k]["symbol"]) or {}).get("greeks") or {}).get("delta")
        if d is None:
            iv = C.implied_vol_put(qs[2], spot, k, T)
            d = C.put_delta(spot, k, T, iv) if iv else None
        if d is None:
            st["4 no delta"] += 1; continue
        cr = qs[2] - ql[2]
        if cr <= 0:
            st["5 credit<=0"] += 1; continue
        st["6 SURVIVED"] += 1
        if len(ex) < 10: ex.append((e, k, abs(d), cr, cr/width, qs[0], qs[1]))
print("\nFUNNEL"); [print(" ", x, st[x]) for x in sorted(st)]
if ex:
    print("\n expiry     short  delta credit %width   bid   ask")
    for x in ex: print(f"{x[0]} {x[1]:7.0f} {x[2]:6.3f} {x[3]:6.2f} {x[4]*100:6.1f}% {x[5]:5.2f} {x[6]:5.2f}")
