#!/usr/bin/env python3
"""Estudio: escalones de máximos y mínimos de 20 sesiones en acciones (Yahoo, diario 2005-hoy y 1 h 2 años)."""
import importlib.util, json, os, time, datetime as dt
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
s = importlib.util.spec_from_file_location("sm", os.path.join(ROOT, "scripts", "daytrader", "scan.py"))
sm = importlib.util.module_from_spec(s); s.loader.exec_module(sm)
TICKERS = ["SPY","QQQ","IWM","DIA","AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","BRK-B","JPM","V","UNH","XOM","JNJ","PG","MA","HD",
           "AVGO","LLY","COST","PEP","KO","MRK","ABBV","CVX","WMT","BAC","ADBE","CRM","NFLX","AMD","INTC","CSCO","ORCL","DIS","NKE","MCD",
           "CAT","BA","GE","IBM","QCOM","TXN","PFE","T","VZ","GS"]
FEE = 0.0005   # por lado (comisión + deslizamiento aproximado, en acciones de EE. UU.)

def load(sym, rng, itv):
    if rng == "max":   # con range=max Yahoo devuelve velas mensuales: se pide por fechas
        path = "/v8/finance/chart/%s?period1=1104537600&period2=%d&interval=%s" % (sm.to_yahoo(sym), int(time.time()), itv)
    else:
        path = sm.chart_path(sym, rng, itv)
    se = sm.parse_chart(sm.yahoo_get(path), sym)
    df = pd.DataFrame({"open": se["o"], "high": se["h"], "low": se["l"], "close": se["c"]},
                      index=pd.to_datetime(se["t"], unit="s", utc=True)).dropna()
    return df

def sim(d, N=20, X=None, mode="ls", M=None):
    o, h, l, c = [d[k].values for k in ("open", "high", "low", "close")]
    top = d.high.rolling(N).max().shift().values; bot = d.low.rolling(N).min().shift().values
    xt = d.high.rolling(X).max().shift().values if X else top
    xb = d.low.rolling(X).min().shift().values if X else bot
    ma = d.close.rolling(M).mean().shift().values if M else None
    pc = d.close.shift().values
    pos, ent, eq, tr, cur = 0, 0.0, 1.0, [], []
    start = max(N, X or 0, M or 0) + 1
    for i in range(start, len(d)):
        if pos == 1 and l[i] <= xb[i]:
            px = min(o[i], xb[i]); r = px / ent - 1 - 2 * FEE; tr.append(r); eq *= 1 + r; pos = 0
            if not X and mode == "ls" and (M is None or pc[i] < ma[i]): pos, ent = -1, px
        elif pos == -1 and h[i] >= xt[i]:
            px = max(o[i], xt[i]); r = 1 - px / ent - 2 * FEE; tr.append(r); eq *= 1 + r; pos = 0
            if not X and mode == "ls" and (M is None or pc[i] > ma[i]): pos, ent = 1, px
        if pos == 0:
            okL = M is None or pc[i] > ma[i]; okS = M is None or pc[i] < ma[i]
            if mode != "short" and okL and h[i] > top[i]: pos, ent = 1, max(o[i], top[i])
            elif mode != "long" and okS and l[i] < bot[i]: pos, ent = -1, min(o[i], bot[i])
        cur.append(eq * ((c[i] / ent) if pos == 1 else (2 - c[i] / ent) if pos == -1 else 1))
    cu = pd.Series(cur) if cur else pd.Series([1.0])
    t = np.array(tr)
    return {"n": int(len(t)), "win": float((t > 0).mean()) if len(t) else None, "avg": float(t.mean()) if len(t) else None,
            "mult": float(cu.iloc[-1]), "dd": float((cu / cu.cummax() - 1).min())}

NS = (5, 10, 20, 30, 50, 100)
RULES = {}
for N in NS:
    X = max(2, N // 2)
    RULES["ls_%d" % N] = dict(N=N, mode="ls")                       # gira en cada escalón
    RULES["long_%d" % N] = dict(N=N, X=X, mode="long")              # largo en máximo nuevo, sale con mínimo de N/2
    RULES["short_%d" % N] = dict(N=N, X=X, mode="short")            # corto en mínimo nuevo, sale con máximo de N/2
    RULES["lsf_%d" % N] = dict(N=N, X=X, mode="ls", M=100)          # ambos, filtrado por la media de 100
    RULES["shortf_%d" % N] = dict(N=N, X=X, mode="short", M=100)    # cortos solo bajo la media de 100
out = {"run_at": dt.datetime.utcnow().isoformat() + "Z", "fee_per_side": FEE, "daily": {}, "hourly": {}}
for sym in TICKERS:
    try:
        d = load(sym, "max", "1d"); d = d[d.index >= "2005-01-01"]; time.sleep(0.4)
    except Exception as e:
        print(sym, e); continue
    res = {}
    for per, dd in (("2005-2015", d[d.index < "2016-01-01"]), ("2016-2026", d[d.index >= "2016-01-01"])):
        if len(dd) < 300: continue
        res[per] = {"bh": float(dd.close.iloc[-1] / dd.close.iloc[101])}
        for k, kw in RULES.items(): res[per][k] = sim(dd, **kw)
    out["daily"][sym] = res
    try:
        hdf = load(sym, "730d", "60m"); time.sleep(0.4)
        out["hourly"][sym] = {"bh": float(hdf.close.iloc[-1] / hdf.close.iloc[101]), **{k: sim(hdf, **kw) for k, kw in RULES.items()}}
    except Exception as e:
        print(sym, "1h", e)
    print(sym, "ok", flush=True)
with open(os.path.join(ROOT, "data", "investigacion_escalones.json"), "w") as fh:
    json.dump(out, fh)
