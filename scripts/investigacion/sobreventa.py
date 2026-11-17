#!/usr/bin/env python3
"""Sobreventa RSI<30 + rebote de sobreventa en acciones, 2005-hoy, con el plan del escáner:
compra a la apertura siguiente, stop en el mínimo de 10 cierres (como máximo un 10 % por debajo),
objetivo a 2 veces el riesgo, salida a las 10 sesiones. Se compara con cada patrón por separado."""
import json, os, time, datetime as dt
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "scripts", "investigacion", "mejora_rupturas.py")).read()
ns = {"__file__": os.path.join(ROOT, "scripts", "investigacion", "mejora_rupturas.py")}
exec(src[:src.index("FILTERS =")], ns)
load, TICKERS, spy_ok, FEE, sc = ns["load"], ns["TICKERS"], ns["spy_ok"], ns["FEE"], ns["sc"]
reg = pd.Series("lateral", index=sc.index); m50 = sc.rolling(50).mean(); m200 = sc.rolling(200).mean()
reg[(sc > m200) & (m50 > m200) & (m50 > m50.shift(20))] = "alcista"; reg[(sc < m200) & (m50 < m50.shift(20))] = "bajista"

def rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)
rows = []
for sym in TICKERS:
    try:
        d = load(sym); time.sleep(0.35)
    except Exception as e:
        print(sym, e); continue
    if len(d) < 400: continue
    c = d.close; r = rsi(c)
    over = (r < 30).values; bounce = ((r.shift(1) < 30) & (c > c.shift(1))).values
    ma200 = c.rolling(200).mean().values
    o, h, l, cl = [d[k].values for k in ("open", "high", "low", "close")]; n = len(d)
    rg = reg.reindex(d.index).ffill().values
    for i in range(210, n - 11):
        tags = []
        if over[i]: tags.append("oversold")
        if bounce[i]: tags.append("oversold_bounce")
        if not tags: continue
        combo = "+".join(tags)
        ent = o[i + 1]
        stop = min(cl[i - 9:i + 1]); stop = ent * 0.98 if ent - stop < ent * 0.01 else stop
        stop = max(stop * 0.995, ent * 0.90)
        if ent <= stop: continue
        risk = ent - stop; tgt = ent + 2 * risk; res = None
        for k in range(i + 1, min(n, i + 11)):
            if k > i + 1 and o[k] <= stop: res = ("stop", o[k]); break
            if l[k] <= stop: res = ("stop", stop); break
            if k > i + 1 and o[k] >= tgt: res = ("objetivo", o[k]); break
            if h[k] >= tgt: res = ("objetivo", tgt); break
        if res is None: res = ("cierre", cl[i + 10])
        ret = res[1] / ent - 1 - 2 * FEE
        rows.append({"t": d.index[i], "sym": sym, "combo": combo, "ret": ret, "R": (res[1] - ent) / risk, "out": res[0],
                     "f1": cl[i + 1] / ent - 1, "f5": cl[i + 5] / ent - 1, "f10": cl[i + 10] / ent - 1,
                     "trend": bool(cl[i] > ma200[i]), "reg": rg[i]})
    print(sym, "ok", flush=True)
df = pd.DataFrame(rows)
def summ(x):
    return {"n": int(len(x)), "win": float((x.ret > 0).mean() * 100), "avg": float(x.ret.mean() * 100), "R": float(x.R.mean()),
            "obj": float((x.out == "objetivo").mean() * 100), "stop": float((x.out == "stop").mean() * 100),
            "f1": float(x.f1.mean() * 100), "f5": float(x.f5.mean() * 100), "f10": float(x.f10.mean() * 100)} if len(x) else None
out = {"run_at": dt.datetime.utcnow().isoformat() + "Z", "tickers": len(TICKERS), "res": {}}
periods = {"2005-2015": (df.t < pd.Timestamp("2016-01-01", tz="UTC")), "2016-2026": (df.t >= pd.Timestamp("2016-01-01", tz="UTC")),
           "2021-2026": (df.t >= pd.Timestamp("2021-01-01", tz="UTC"))}
for combo in ("oversold+oversold_bounce", "oversold", "oversold_bounce"):
    for pe, m in periods.items():
        x = df[m & (df.combo == combo)]
        out["res"]["%s|%s|todas" % (combo, pe)] = summ(x)
        for rg in ("alcista", "lateral", "bajista"):
            out["res"]["%s|%s|%s" % (combo, pe, rg)] = summ(x[x.reg == rg])
        out["res"]["%s|%s|tendencia" % (combo, pe)] = summ(x[x.trend])
        out["res"]["%s|%s|sin_tendencia" % (combo, pe)] = summ(x[~x.trend])
yr = df[df.combo == "oversold+oversold_bounce"].copy(); yr["y"] = yr.t.dt.year
out["per_year"] = {int(y): {"n": int(len(g)), "eur": float((g.ret * 1000).sum()), "win": float((g.ret > 0).mean() * 100)} for y, g in yr.groupby("y")}
with open(os.path.join(ROOT, "data", "investigacion_sobreventa.json"), "w") as fh: json.dump(out, fh)
