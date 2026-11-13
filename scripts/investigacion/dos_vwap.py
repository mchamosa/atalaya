#!/usr/bin/env python3
"""Dos VWAP en el Nasdaq (futuro NQ=F): VWAP diario (desde las 18:00 de NY, inicio de la sesión del futuro)
y VWAP de Nueva York (desde las 9:30). Señal entre 9:45 y 15:30 de NY cuando el precio cruza el VWAP de NY:
largo si cruza hacia arriba y está también por encima del VWAP diario; corto al revés.
Se compara con cruzar solo el VWAP de NY (sin la confirmación del diario).
Salidas: A) cuando vuelve a cruzar el VWAP de NY o a las 15:55; B) stop en el extremo de las 10 velas previas
y objetivo 1,5 veces el riesgo. Coste: 0,5 puntos por operación (comisión y deslizamiento)."""
import json, os, time, datetime as dt, importlib.util
import numpy as np, pandas as pd
from zoneinfo import ZoneInfo
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
s = importlib.util.spec_from_file_location("sm", os.path.join(ROOT, "scripts", "daytrader", "scan.py"))
sm = importlib.util.module_from_spec(s); s.loader.exec_module(sm)
NY = ZoneInfo("America/New_York"); COST = 0.5
def get(sym, itv, days):
    now = int(time.time()); out = []; step = 7 * 86400 if itv == "1m" else 55 * 86400; t0 = now - days * 86400
    while t0 < now:
        t1 = min(now, t0 + step)
        try:
            se = sm.parse_chart(sm.yahoo_get("/v8/finance/chart/%s?period1=%d&period2=%d&interval=%s&includePrePost=true" % (sym, t0, t1, itv)), sym)
            out.append(pd.DataFrame({"open": se["o"], "high": se["h"], "low": se["l"], "close": se["c"], "vol": se["v"]},
                                    index=pd.to_datetime(se["t"], unit="s", utc=True)))
        except Exception as e: print(sym, itv, e)
        t0 = t1; time.sleep(0.5)
    d = pd.concat(out).dropna(); d = d[~d.index.duplicated()].sort_index(); d.index = d.index.tz_convert(NY); return d
def run(d):
    tp = (d.high + d.low + d.close) / 3; pv = tp * d.vol
    sess = (d.index - pd.Timedelta(hours=18)).date            # sesión del futuro: de 18:00 a 17:00
    vw_d = pv.groupby(sess).cumsum() / d.vol.groupby(sess).cumsum()
    t = d.index.hour * 60 + d.index.minute
    rth = (t >= 570) & (t < 960); day = d.index.date
    m = pd.Series(rth, index=d.index)
    pv_ny = pv.where(m, 0).groupby(day).cumsum(); v_ny = d.vol.where(m, 0).groupby(day).cumsum()
    vw_ny = (pv_ny / v_ny).where(m)
    o, h, l, c = [d[k].values for k in ("open", "high", "low", "close")]
    vd, vn = vw_d.values, vw_ny.values; n = len(d); tv = np.asarray(t)
    hh = d.high.rolling(10).max().shift().values; ll = d.low.rolling(10).min().shift().values
    res = {k: [] for k in ("conf_A", "solo_A", "conf_B", "solo_B")}
    for i in range(1, n - 1):
        if not (585 <= tv[i] <= 930) or np.isnan(vn[i]) or np.isnan(vn[i - 1]): continue
        up = c[i - 1] <= vn[i - 1] and c[i] > vn[i]; dn = c[i - 1] >= vn[i - 1] and c[i] < vn[i]
        if not (up or dn): continue
        side = 1 if up else -1
        conf = (c[i] > vd[i]) if up else (c[i] < vd[i])
        ent = c[i]
        # A: sale al volver a cruzar el VWAP de NY o a las 15:55
        k = i + 1
        while k < n and tv[k] < 955 and d.index[k].date() == d.index[i].date() and side * (c[k] - vn[k]) > 0: k += 1
        k = min(k, n - 1); rA = side * (c[k] - ent) - COST
        # B: stop en el extremo de 10 velas, objetivo 1,5R, cierre a las 15:55
        stp = ll[i] if side == 1 else hh[i]; risk = side * (ent - stp)
        rB = None
        if risk > 0:
            tgt = ent + side * 1.5 * risk; k = i + 1
            while k < n and tv[k] < 955 and d.index[k].date() == d.index[i].date():
                if (side == 1 and l[k] <= stp) or (side == -1 and h[k] >= stp): rB = -risk; break
                if (side == 1 and h[k] >= tgt) or (side == -1 and l[k] <= tgt): rB = 1.5 * risk; break
                k += 1
            if rB is None: rB = side * (c[min(k, n - 1)] - ent)
            rB = (rB - COST) / risk
        for key, ok in (("conf", conf), ("solo", True)):
            if ok:
                res[key + "_A"].append(rA)
                if rB is not None: res[key + "_B"].append(rB)
    return res
out = {"run_at": dt.datetime.utcnow().isoformat() + "Z"}
for itv, days in (("1m", 29), ("5m", 58)):
    d = get("NQ=F", itv, days)
    r = run(d)
    out[itv] = {"desde": str(d.index[0].date()), "hasta": str(d.index[-1].date()), "dias": int(len(set(d.index.date)))}
    for k, v in r.items():
        a = np.array(v)
        out[itv][k] = {"ops": int(len(a)), "aciertos": float((a > 0).mean() * 100) if len(a) else None,
                       "media": float(a.mean()) if len(a) else None, "total": float(a.sum()) if len(a) else None}
    print(itv, out[itv], flush=True)
with open(os.path.join(ROOT, "data", "investigacion_dos_vwap.json"), "w") as fh: json.dump(out, fh)
