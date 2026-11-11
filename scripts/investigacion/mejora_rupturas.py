#!/usr/bin/env python3
"""Rupturas de máximos de 50 y 100 sesiones en acciones: ¿qué filtro las mejora? 2005-hoy."""
import json, os, time, datetime as dt, itertools
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "scripts", "investigacion", "regimen.py")).read()
ns = {"__file__": os.path.join(ROOT, "scripts", "investigacion", "regimen.py")}
exec(src[:src.index("H = (1, 5, 10, 20)")], ns)
sm, TICKERS = ns["ns"]["sm"], ns["TICKERS"]
FEE = 0.0005

def load(sym):
    path = "/v8/finance/chart/%s?period1=1104537600&period2=%d&interval=1d" % (sm.to_yahoo(sym), int(time.time()))
    se = sm.parse_chart(sm.yahoo_get(path), sym)
    return pd.DataFrame({"open": se["o"], "high": se["h"], "low": se["l"], "close": se["c"], "vol": se["v"]},
                        index=pd.to_datetime(se["t"], unit="s", utc=True).normalize()).dropna()

spy = load("SPY"); time.sleep(0.4)
sc = spy.close
spy_ok = (sc > sc.rolling(200).mean())                         # mercado por encima de su media de 200
spy_ret126 = sc / sc.shift(126) - 1

FILTERS = ["mercado", "tendencia", "fuerza", "volumen", "compresion", "no_extendida"]
def trades(d, N, X, exit_mode="canal"):
    o, h, l, c, v = [d[k].values for k in ("open", "high", "low", "close", "vol")]
    n = len(d)
    top = d.high.rolling(N).max().shift().values
    bot = d.low.rolling(X).min().shift().values
    ma200 = d.close.rolling(200).mean().values; ma50 = d.close.rolling(50).mean().values
    v20 = d.vol.rolling(20).mean().shift().values
    atr = (pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(), (d.low - d.close.shift()).abs()], axis=1).max(axis=1)).rolling(20).mean().values
    atr_rel = atr / c
    atr_med = pd.Series(atr_rel).rolling(250).median().values
    r126 = (d.close / d.close.shift(126) - 1).values
    mk = spy_ok.reindex(d.index).ffill().fillna(False).values
    sr = spy_ret126.reindex(d.index).ffill().values
    out, pos = [], 0
    for i in range(max(N, 260), n):
        if pos:
            stop = bot[i] if exit_mode == "canal" else max(trail, c[i - 1] - 3 * atr[i - 1]) if exit_mode == "atr" else bot[i]
            if exit_mode == "atr":
                trail = stop
            if l[i] <= stop:
                px = min(o[i], stop)
                f["ret"] = px / ent - 1 - 2 * FEE; f["days"] = i - ei; out.append(f); pos = 0
            continue
        if h[i] > top[i] and not np.isnan(top[i]):
            j = i - 1    # filtros con datos del día anterior
            f = {"t": d.index[i], "mercado": bool(mk[j]), "tendencia": bool(c[j] > ma200[j] and ma50[j] > ma200[j]),
                 "fuerza": bool(r126[j] > sr[j]) if not np.isnan(sr[j]) else False,
                 "volumen": bool(v[i] > 1.5 * v20[i]) if v20[i] else False,     # volumen del día de la ruptura
                 "compresion": bool(atr_rel[j] < atr_med[j]) if not np.isnan(atr_med[j]) else False,
                 "no_extendida": bool(c[j] < ma50[j] * 1.15)}
            ent = max(o[i], top[i]); ei = i; pos = 1; trail = ent - 3 * atr[j]
    return out

res = {}
for sym in TICKERS:
    try:
        d = load(sym); time.sleep(0.35)
    except Exception as e:
        print(sym, e); continue
    if len(d) < 600: continue
    for (N, X) in ((50, 25), (100, 50)):
        for ex in ("canal", "atr"):
            for t in trades(d, N, X, ex):
                t["sym"] = sym
                res.setdefault((N, ex), []).append(t)
    print(sym, "ok", flush=True)

summary = []
for (N, ex), T in res.items():
    df = pd.DataFrame(T)
    df["per"] = np.where(df.t < pd.Timestamp("2016-01-01", tz="UTC"), "2005-2015", "2016-2026")
    combos = [()] + [(f,) for f in FILTERS] + list(itertools.combinations(FILTERS, 2)) + list(itertools.combinations(FILTERS, 3))
    for cb in combos:
        m = np.ones(len(df), bool)
        for f in cb: m &= df[f].values
        for pe in ("2005-2015", "2016-2026"):
            x = df[m & (df.per == pe).values]
            if len(x) < 30: continue
            summary.append({"N": N, "exit": ex, "filters": "+".join(cb) or "ninguno", "period": pe, "n": int(len(x)),
                            "avg": float(x.ret.mean() * 100), "median": float(x.ret.median() * 100),
                            "win": float((x.ret > 0).mean() * 100), "days": float(x.days.mean()),
                            "sum_per_year": float(x.ret.sum() / 11 * 1000)})
with open(os.path.join(ROOT, "data", "investigacion_mejora_rupturas.json"), "w") as fh:
    json.dump({"run_at": dt.datetime.utcnow().isoformat() + "Z", "tickers": len(TICKERS), "summary": summary}, fh)
