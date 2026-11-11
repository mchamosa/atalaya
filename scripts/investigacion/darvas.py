#!/usr/bin/env python3
"""Cajas de Darvas en acciones, 2005-hoy.
Caja: tras un máximo nuevo de 52 semanas, el techo es un máximo que no se supera en 3 sesiones;
el suelo, el mínimo posterior que no se pierde en 3 sesiones. Compra al romper el techo con la
caja formada; stop bajo el suelo de la caja; el stop sube con cada caja nueva (siguiendo la tendencia)."""
import json, os, time, datetime as dt
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "scripts", "investigacion", "mejora_rupturas.py")).read()
ns = {"__file__": os.path.join(ROOT, "scripts", "investigacion", "mejora_rupturas.py")}
exec(src[:src.index("FILTERS =")], ns)
load, TICKERS, spy_ok, FEE = ns["load"], ns["TICKERS"], ns["spy_ok"], ns["FEE"]

def boxes(d, need_52w=True, market=False):
    o, h, l, c = [d[k].values for k in ("open", "high", "low", "close")]
    n = len(d); hi252 = d.high.rolling(252).max().values
    mk = spy_ok.reindex(d.index).ffill().fillna(False).values
    trades, pos = [], 0
    state, top, bot, ti, bi = 0, None, None, 0, 0   # 0 buscando techo, 1 techo fijado buscando suelo, 2 caja hecha
    stop = None
    for i in range(253, n):
        # gestión de la posición
        if pos and l[i] <= stop:
            px = min(o[i], stop); trades.append({"t": d.index[ei], "ret": px / ent - 1 - 2 * FEE, "days": i - ei}); pos = 0
        # construcción de cajas
        if state == 0:
            if (not need_52w or h[i] >= hi252[i - 1]) or pos:
                top, ti, state = h[i], i, 1
        elif state == 1:
            if h[i] > top:
                top, ti = h[i], i
            elif i - ti >= 3:
                bot, bi, state = min(l[ti + 1:i + 1]), i, 2
        elif state == 2:
            if h[i] > top:                       # rompe el techo: compra o sube el stop
                if pos:
                    stop = max(stop, bot * 0.995)
                elif not market or mk[i - 1]:
                    ent = max(o[i], top); ei = i; pos = 1; stop = bot * 0.995
                    if ent <= stop: pos = 0
                state, top, ti = 1, h[i], i
            elif l[i] < bot:
                if l[i] < bot and i - bi < 3:
                    bot, bi = l[i], i            # el suelo aún no está confirmado
                else:
                    state = 0                    # la caja se rompe por abajo: se empieza de nuevo
    return trades

res = {}
for sym in TICKERS:
    try:
        d = load(sym); time.sleep(0.35)
    except Exception as e:
        print(sym, e); continue
    if len(d) < 600: continue
    for name, kw in (("darvas", {}), ("darvas_mercado", {"market": True}), ("cajas_sin_52s", {"need_52w": False})):
        for t in boxes(d, **kw):
            res.setdefault(name, []).append(t)
    print(sym, "ok", flush=True)
summary = []
for name, T in res.items():
    df = pd.DataFrame(T); df["per"] = np.where(df.t < pd.Timestamp("2016-01-01", tz="UTC"), "2005-2015", "2016-2026")
    for pe in ("2005-2015", "2016-2026"):
        x = df[df.per == pe]
        summary.append({"rule": name, "period": pe, "n": int(len(x)), "avg": float(x.ret.mean() * 100), "median": float(x.ret.median() * 100),
                        "win": float((x.ret > 0).mean() * 100), "days": float(x.days.mean()), "sum_per_year": float(x.ret.sum() / 11 * 1000)})
with open(os.path.join(ROOT, "data", "investigacion_darvas.json"), "w") as fh:
    json.dump({"run_at": dt.datetime.utcnow().isoformat() + "Z", "summary": summary}, fh)
