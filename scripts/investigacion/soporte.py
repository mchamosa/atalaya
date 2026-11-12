#!/usr/bin/env python3
"""Rebote en un soporte anterior (antiguo máximo roto) con objetivo en el máximo de arriba, en acciones 2005-hoy.
Ruptura del máximo de 50 sesiones -> el precio sube al menos un 10 % -> vuelve a la zona del máximo roto
(hasta un 2 % por encima): compra ahí. Stop un 5 % por debajo del nivel. Objetivo: el máximo previo
(o la mitad del camino). Salida a las 120 sesiones si no toca nada."""
import json, os, time, datetime as dt
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "scripts", "investigacion", "mejora_rupturas.py")).read()
ns = {"__file__": os.path.join(ROOT, "scripts", "investigacion", "mejora_rupturas.py")}
exec(src[:src.index("FILTERS =")], ns)
load, TICKERS, spy_ok, FEE = ns["load"], ns["TICKERS"], ns["spy_ok"], ns["FEE"]

def events(d):
    o, h, l, c = [d[k].values for k in ("open", "high", "low", "close")]
    n = len(d); top = d.high.rolling(50).max().shift().values
    ma200 = d.close.rolling(200).mean().values; ma50 = d.close.rolling(50).mean().values
    mk = spy_ok.reindex(d.index).ffill().fillna(False).values
    out = []; i = 260; busy_until = 0
    while i < n - 2:
        if c[i] > top[i] and i >= busy_until:
            L = top[i]; peak = h[i]; pi = i; t = None
            for k in range(i + 1, min(n, i + 250)):
                if h[k] > peak: peak, pi = h[k], k
                if peak >= L * 1.10 and k > pi and l[k] <= L * 1.02:
                    t = k; break
                if l[k] < L * 0.95 and peak < L * 1.10: break      # se perdió antes de alejarse: no es soporte
            if t is not None:
                ent = min(o[t], L * 1.02); stop = L * 0.95
                for tgt_name, tgt in (("maximo", peak), ("mitad", (ent + peak) / 2)):
                    res = None
                    for k in range(t, min(n, t + 120)):
                        if l[k] <= stop and not (k == t and o[k] < stop and False):
                            res = (min(o[k], stop) if k > t else stop, k, "stop"); break
                        if h[k] >= tgt and k > t:
                            res = (max(o[k], tgt), k, "objetivo"); break
                    if res is None:
                        k = min(n - 1, t + 119); res = (c[k], k, "tiempo")
                    px, k, why = res
                    r = px / ent - 1 - 2 * FEE; R = (px - ent) / (ent - stop)
                    out.append({"t": d.index[t], "target": tgt_name, "ret": r, "R": R, "why": why, "days": k - t,
                                "tendencia": bool(c[t - 1] > ma200[t - 1]), "mercado": bool(mk[t - 1]),
                                "sobre50": bool(c[t - 1] > ma50[t - 1]), "rr": (tgt - ent) / (ent - stop)})
                busy_until = t + 1
            i = max(i + 1, (t or i) + 1)
        else:
            i += 1
    return out

rows = []
for sym in TICKERS:
    try:
        d = load(sym); time.sleep(0.35)
    except Exception as e:
        print(sym, e); continue
    if len(d) < 600: continue
    for e in events(d):
        e["sym"] = sym; rows.append(e)
    print(sym, "ok", flush=True)
df = pd.DataFrame(rows)
df["per"] = np.where(df.t < pd.Timestamp("2016-01-01", tz="UTC"), "2005-2015", "2016-2026")
summary = []
for tg in ("maximo", "mitad"):
    for flt in ("todas", "tendencia", "mercado", "tendencia+mercado", "sobre50"):
        m = (df.target == tg).values
        for f in flt.split("+"):
            if f != "todas": m &= df[f].values
        for pe in ("2005-2015", "2016-2026", "2021-2026"):
            x = df[m & ((df.per == pe).values if pe != "2021-2026" else (df.t >= pd.Timestamp("2021-01-01", tz="UTC")).values)]
            if not len(x): continue
            summary.append({"target": tg, "filter": flt, "period": pe, "n": int(len(x)), "win": float((x.ret > 0).mean() * 100),
                            "avg": float(x.ret.mean() * 100), "median": float(x.ret.median() * 100), "avgR": float(x.R.mean()),
                            "hit_target": float((x.why == "objetivo").mean() * 100), "hit_stop": float((x.why == "stop").mean() * 100),
                            "days": float(x.days.mean()), "rr": float(x.rr.median())})
ex = df[(df.target == "maximo") & (df.t >= pd.Timestamp("2025-01-01", tz="UTC"))].sort_values("t").tail(15)
with open(os.path.join(ROOT, "data", "investigacion_soporte.json"), "w") as fh:
    json.dump({"run_at": dt.datetime.utcnow().isoformat() + "Z", "summary": summary,
               "examples": [{"sym": a.sym, "date": str(a.t.date()), "ret": round(a.ret * 100, 1), "why": a.why, "days": int(a.days)} for a in ex.itertuples()]}, fh)
