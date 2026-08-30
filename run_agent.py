import asyncio, json, os, sys
from datetime import date
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import candidates as C

SYM, WIDTH, TGT = "SPY", 2.0, 0.13
FLOOR = 0.0740 * 1.15
TRANSMIT = "--transmit" in sys.argv

key, sec = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
P = StdioServerParameters(command="uvx", args=["alpaca-mcp-server"],
    env={**os.environ, "ALPACA_API_KEY": key, "ALPACA_SECRET_KEY": sec})

def pick():
    spot = C.underlying_price(SYM)
    cons = C.list_contracts(SYM, 4, spot)
    snaps = C.snapshots(SYM, "indicative")
    by = {}
    for c in cons: by.setdefault(c["expiration_date"], {})[float(c["strike_price"])] = c
    exps = sorted(e for e in by if date.fromisoformat(e) > date.today())
    if not exps: return None, spot
    exp = exps[0]; ks = by[exp]
    dte = (date.fromisoformat(exp) - date.today()).days
    T = max(dte, 0.5)/365.0
    best = None
    for k in sorted(ks):
        if k - WIDTH not in ks: continue
        qs = C.leg_quote(snaps.get(ks[k]["symbol"])); ql = C.leg_quote(snaps.get(ks[k-WIDTH]["symbol"]))
        if not qs or not ql: continue
        d = ((snaps.get(ks[k]["symbol"]) or {}).get("greeks") or {}).get("delta")
        if d is None:
            iv = C.implied_vol_put(qs[2], spot, k, T)
            d = C.put_delta(spot, k, T, iv) if iv else None
        if d is None: continue
        d = abs(d); cr = qs[2]-ql[2]
        if cr <= 0: continue
        if best is None or abs(d-TGT) < abs(best["delta"]-TGT):
            best = {"exp":exp,"dte":dte,"k":k,"kl":k-WIDTH,"delta":d,"credit":round(cr,2),
                    "pct":cr/WIDTH,"short":ks[k]["symbol"],"long":ks[k-WIDTH]["symbol"],
                    "natural":round(qs[0]-ql[1],2)}
    return best, spot

async def main():
    b, spot = pick()
    if not b: print("no candidate"); return
    print(f"spot {spot:.2f} | {b['exp']} dte {b['dte']}")
    print(f"{b['k']:.0f}/{b['kl']:.0f} delta {b['delta']:.3f} credit {b['credit']:.2f} "
          f"({b['pct']*100:.1f}% of width) natural {b['natural']:.2f}")
    print(f"floor {FLOOR*100:.1f}% -> {'PASS' if b['pct']>=FLOOR else 'FAIL'}")
    if b["pct"] < FLOOR: print("below floor, standing down"); return
    px = -abs(b["natural"])
    args = {"order_class":"mleg","qty":1,"type":"limit","time_in_force":"day",
            "limit_price":px,
            "legs":[{"symbol":b["short"],"side":"sell","ratio_qty":1,"position_intent":"sell_to_open"},
                    {"symbol":b["long"],"side":"buy","ratio_qty":1,"position_intent":"buy_to_open"}]}
    print("\nargs:", json.dumps(args))
    if not TRANSMIT: print("\nDRY RUN - add --transmit"); return
    async with stdio_client(P) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("place_option_order", args)
            for blk in res.content:
                if getattr(blk, "type", None) == "text": print(blk.text[:900])
asyncio.run(main())
