#!/usr/bin/env python3
"""Qué patrón funciona en cada tipo de mercado (alcista, lateral, bajista según el SPY), 2005-hoy."""
import json, os, time, datetime as dt
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "scripts", "investigacion", "escalones.py")).read()
ns = {"__file__": os.path.join(ROOT, "scripts", "investigacion", "escalones.py")}
exec(src[:src.index("def sim")], ns)
load, TICKERS = ns["load"], [t for t in ns["TICKERS"] if t not in ("SPY", "QQQ", "IWM", "DIA")]
TICKERS += ["AMAT","LRCX","MU","ADI","PANW","NOW","INTU","ISRG","BKNG","UBER","SBUX","LOW","TGT","UPS","HON","LMT","RTX","DE","MMM","AMGN",
            "GILD","BMY","CVS","CI","ELV","MS","C","WFC","AXP","SCHW","BLK","SPGI","PLD","AMT","NEE","DUK","SO","COP","SLB","EOG"]
H = (1, 5, 10, 20)

def wilder_rsi(c, n):
    d = c.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean(); ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + au / ad)

def patterns(d):
    c = d.close
    r14 = wilder_rsi(c, 14); r2 = wilder_rsi(c, 2)
    ma20 = c.rolling(20).mean(); ma50 = c.rolling(50).mean(); ma200 = c.rolling(200).mean()
    p = pd.DataFrame(index=d.index)
    p["breakout_20d"] = c > c.shift(1).rolling(20).max()
    p["bull_flag"] = ((c.shift(5) / c.shift(15) - 1) > 0.10) & ((c.rolling(5).max() - c.rolling(5).min()) / c < 0.05)
    p["reclaim_ma20"] = (c.shift(1) < ma20.shift(1)) & (c > ma20)
    p["oversold"] = r14 < 30
    p["oversold_bounce"] = (r14.shift(1) < 30) & (c > c.shift(1))
    p["high_52w"] = c > c.shift(1).rolling(252).max()
    p["pullback_trend"] = (c > ma200) & (r2 < 10)                    # retroceso corto dentro de tendencia alcista
    p["trend_ma"] = (c > ma50) & (ma50 > ma200) & (c.shift(1) <= ma50.shift(1))   # recupera la media 50 en tendencia
    return p

spy = load("SPY", "max", "1d"); time.sleep(0.4)
c = spy.close; m50 = c.rolling(50).mean(); m200 = c.rolling(200).mean()
reg = pd.Series("lateral", index=spy.index)
reg[(c > m200) & (m50 > m200) & (m50 > m50.shift(20))] = "alcista"
reg[(c < m200) & (m50 < m50.shift(20))] = "bajista"
reg.index = reg.index.normalize()
acc = {}   # (patrón, régimen, periodo) -> listas por horizonte
def add(key, h, v):
    acc.setdefault(key, {k: [] for k in H})[h].extend(v)
for sym in TICKERS:
    try:
        d = load(sym, "max", "1d"); time.sleep(0.35)
    except Exception as e:
        print(sym, e); continue
    d = d[d.index >= "2004-01-01"]
    if len(d) < 400: continue
    p = patterns(d)
    rg = reg.reindex(d.index.normalize()).values
    per = np.where(d.index < "2016-01-01", "2005-2015", "2016-2026")
    o = d.open.values; cl = d.close.values; n = len(d)
    fwd = {}
    for k in H:
        f = np.full(n, np.nan)
        f[:n - k] = cl[k:] / o[1:n - k + 1] - 1      # compra a la apertura siguiente, vende al cierre k sesiones después
        fwd[k] = f
    valid = np.arange(n) >= 252
    for col in list(p.columns) + ["todos"]:
        m = (p[col].values if col != "todos" else np.ones(n, bool)) & valid
        for r in ("alcista", "lateral", "bajista"):
            for pe in ("2005-2015", "2016-2026"):
                mm = m & (rg == r) & (per == pe)
                for k in H:
                    v = fwd[k][mm]; v = v[~np.isnan(v)]
                    add((col, r, pe), k, v.tolist())
    print(sym, "ok", flush=True)
out = {"run_at": dt.datetime.utcnow().isoformat() + "Z", "tickers": len(TICKERS), "results": []}
for (col, r, pe), byh in acc.items():
    row = {"pattern": col, "regime": r, "period": pe}
    for k, v in byh.items():
        a = np.array(v)
        row["n"] = int(len(a)) if k == 1 else row.get("n")
        row["avg_%d" % k] = float(a.mean() * 100) if len(a) else None
        row["win_%d" % k] = float((a > 0).mean() * 100) if len(a) else None
    out["results"].append(row)
out["regime_today"] = str(reg.iloc[-1])
with open(os.path.join(ROOT, "data", "investigacion_regimen.json"), "w") as fh:
    json.dump(out, fh)
