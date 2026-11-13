#!/usr/bin/env python3
"""Vela bajista sin mecha en oro (futuro GC=F de Yahoo), velas de 1, 5 y 15 minutos: corto al cierre,
stop sobre el máximo, objetivo 1,5 veces el riesgo. Coste: 0,30 $ por onza (diferencial típico de XAUUSD)."""
import json, os, time, datetime as dt
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
s = importlib.util.spec_from_file_location("sm", os.path.join(ROOT, "scripts", "daytrader", "scan.py"))
sm = importlib.util.module_from_spec(s); s.loader.exec_module(sm)
COST = 0.30
def get(itv, days):
    now = int(time.time()); out = []
    step = 7 * 86400 if itv == "1m" else 50 * 86400
    t0 = now - days * 86400
    while t0 < now:
        t1 = min(now, t0 + step)
        try:
            se = sm.parse_chart(sm.yahoo_get("/v8/finance/chart/GC=F?period1=%d&period2=%d&interval=%s" % (t0, t1, itv)), "GC=F")
            out.append(pd.DataFrame({"open": se["o"], "high": se["h"], "low": se["l"], "close": se["c"]},
                                    index=pd.to_datetime(se["t"], unit="s", utc=True)))
        except Exception as e:
            print(itv, e)
        t0 = t1; time.sleep(0.5)
    d = pd.concat(out).dropna(); return d[~d.index.duplicated()].sort_index()
res = {}
for itv, days in (("1m", 29), ("5m", 59), ("15m", 59)):
    d = get(itv, days)
    o, h, l, c = [d[k].values for k in ("open", "high", "low", "close")]; n = len(d); rng = h - l
    ma = pd.Series(c).rolling(50).mean().values
    red = (c < o) & (rng > 0)
    for name, sig in (("sin_mecha_arriba", red & ((h - o) <= 0.05 * rng)), ("sin_mecha_abajo", red & ((c - l) <= 0.05 * rng)),
                      ("sin_mecha_arriba_tendencia", red & ((h - o) <= 0.05 * rng) & (c < ma))):
        Rg, Rn, rk = [], [], []
        for i in np.where(sig)[0]:
            if i + 1 >= n: break
            ent = c[i]; stp = h[i]; risk = stp - ent
            if risk <= 0: continue
            tgt = ent - 1.5 * risk; out = None
            for k in range(i + 1, min(n, i + 61)):
                if h[k] >= stp: out = -1.0; break
                if l[k] <= tgt: out = 1.5; break
            if out is None: out = (ent - c[min(n - 1, i + 60)]) / risk
            Rg.append(out); Rn.append(out - COST / risk); rk.append(risk)
        Rg, Rn, rk = map(np.array, (Rg, Rn, rk))
        res["%s_%s" % (itv, name)] = {"velas": int(n), "desde": str(d.index[0].date()), "ops": int(len(Rg)),
            "win_bruto": float((Rg > 0).mean() * 100), "R_bruto": float(Rg.mean()),
            "win_neto": float((Rn > 0).mean() * 100), "R_neto_mediana": float(np.median(Rn)), "R_neto_media": float(Rn.mean()),
            "riesgo_mediano_usd": float(np.median(rk))}
        print(itv, name, res["%s_%s" % (itv, name)], flush=True)
with open(os.path.join(ROOT, "data", "investigacion_oro_sinmecha.json"), "w") as fh:
    json.dump({"run_at": dt.datetime.utcnow().isoformat() + "Z", "results": res}, fh)
