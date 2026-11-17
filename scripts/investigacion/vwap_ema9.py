#!/usr/bin/env python3
"""VWAP + EMA 9 (estrategia de un vídeo de Instagram que dice tener un 85 % de aprobados en prop firms).

Datos: velas de 5 min de la sesión regular ya guardadas en data/intraday (QQQ, SPY, NVDA, TSLA; unos 2 años).
VWAP desde las 9:30 de Nueva York. EMA 9 continua sobre las velas de 5 min.
Entradas entre 9:45 y 15:30 de NY, una operación abierta como mucho, todo cerrado a las 15:55.

Variantes:
  rebote   precio y EMA 9 por encima del VWAP (largos; al revés para cortos), la vela toca la EMA 9 y cierra
           a favor (cierre por encima de la EMA y vela verde). Entrada al cierre. Stop en el extremo de la
           vela de señal, objetivo 2R.
  rebote_s igual, pero la salida es cuando una vela cierra al otro lado de la EMA 9 (sin objetivo fijo).
  cruce    la EMA 9 cruza el VWAP. Entrada al cierre, salida en el cruce contrario.
           Stop de referencia para medir en R: extremo de las 6 velas previas.
Coste: 0,02 $ por acción y operación (ida y vuelta: comisión y deslizamiento).

Simulación de prueba de fondeo (prop firm), con 0,5 % de riesgo por operación:
objetivo +6 % (12R) antes de perder un 4 % desde el máximo (8R, pérdida máxima arrastrada).
Se remuestrean al azar las operaciones reales 20.000 veces y se cuenta qué parte aprueba.
"""
import json, os, glob, datetime as dt
import numpy as np, pandas as pd
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NY = ZoneInfo("America/New_York")
SYMBOLS = ["QQQ", "SPY", "NVDA", "TSLA"]
COST = 0.02
rng = np.random.default_rng(7)


def load(sym):
    bars = []
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "intraday", sym, "*.json"))):
        bars += json.load(open(f))["bars"]
    d = pd.DataFrame(bars, columns=["t", "open", "high", "low", "close", "vol"]).drop_duplicates("t").sort_values("t")
    d.index = pd.to_datetime(d.t, unit="s", utc=True).dt.tz_convert(NY)
    return d.drop(columns="t").dropna()


def prepare(d):
    day = d.index.date
    tp = (d.high + d.low + d.close) / 3
    d = d.assign(vwap=(tp * d.vol).groupby(day).cumsum() / d.vol.groupby(day).cumsum(),
                 ema=d.close.ewm(span=9, adjust=False).mean(),
                 mins=d.index.hour * 60 + d.index.minute, day=day)
    return d


def backtest(d, mode):
    o, h, l, c = (d[k].values for k in ("open", "high", "low", "close"))
    vw, em, mn, day = d.vwap.values, d.ema.values, d.mins.values, d.day.values
    n = len(d); i = 1; out = []
    lo6 = d.low.rolling(6).min().values; hi6 = d.high.rolling(6).max().values
    while i < n - 1:
        side = 0
        if 585 <= mn[i] <= 930:
            if mode in ("rebote", "rebote_s"):
                if c[i] > vw[i] and em[i] > vw[i] and l[i] <= em[i] < c[i] and c[i] > o[i]: side = 1
                elif c[i] < vw[i] and em[i] < vw[i] and h[i] >= em[i] > c[i] and c[i] < o[i]: side = -1
            else:
                if em[i - 1] <= vw[i - 1] and em[i] > vw[i]: side = 1
                elif em[i - 1] >= vw[i - 1] and em[i] < vw[i]: side = -1
        if not side:
            i += 1; continue
        ent = c[i]
        if mode == "cruce":
            stp = lo6[i] if side == 1 else hi6[i]
        else:
            stp = l[i] if side == 1 else h[i]
        risk = side * (ent - stp)
        if not risk > 0.01:
            i += 1; continue
        tgt = ent + side * 2 * risk
        k = i + 1; pnl = None
        while k < n and day[k] == day[i]:
            if side * (stp - (l[k] if side == 1 else h[k])) >= 0:          # toca el stop
                pnl = -risk; break
            if mode == "rebote" and side * ((h[k] if side == 1 else l[k]) - tgt) >= 0:
                pnl = 2 * risk; break
            if mode == "rebote_s" and side * (c[k] - em[k]) < 0:
                pnl = side * (c[k] - ent); break
            if mode == "cruce" and side * (em[k] - vw[k]) < 0:
                pnl = side * (c[k] - ent); break
            if mn[k] >= 955:
                pnl = side * (c[k] - ent); break
            k += 1
        if pnl is None:
            k = min(k, n - 1); pnl = side * (c[k - 1] - ent)
        out.append({"fecha": str(day[i]), "lado": side, "R": (pnl - COST) / risk,
                    "pct": (pnl - COST) / ent * 100})
        i = k + 1
    return pd.DataFrame(out)


def prop_sim(r, risk_pct=0.5, target=6.0, dd=4.0, trials=20000, max_trades=400):
    """Remuestrea las operaciones (en R) y simula la prueba con pérdida máxima arrastrada."""
    tgt_r, dd_r = target / risk_pct, dd / risk_pct
    passed = failed = 0
    for _ in range(trials):
        seq = rng.choice(r, size=max_trades, replace=True)
        eq = np.cumsum(seq); peak = np.maximum.accumulate(np.maximum(eq, 0))
        hit_t = np.argmax(eq >= tgt_r) if (eq >= tgt_r).any() else max_trades
        hit_d = np.argmax(eq <= peak - dd_r) if (eq <= peak - dd_r).any() else max_trades
        if hit_t < hit_d: passed += 1
        elif hit_d < hit_t: failed += 1
    return passed / trials * 100


def stats(t):
    if t.empty: return None
    r = t.R.values
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    eq = np.cumsum(r)
    return {"ops": int(len(r)), "aciertos": round(float((r > 0).mean() * 100), 1),
            "media_R": round(float(r.mean()), 3), "total_R": round(float(r.sum()), 1),
            "factor_beneficio": round(float(wins / losses), 2) if losses else None,
            "max_caida_R": round(float((np.maximum.accumulate(eq) - eq).max()), 1),
            "media_pct": round(float(t.pct.mean()), 3)}


out = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "coste_por_accion": COST, "simbolos": {}}
for sym in SYMBOLS:
    d = prepare(load(sym))
    out["simbolos"][sym] = {"desde": str(d.day.iloc[0]), "hasta": str(d.day.iloc[-1]), "dias": int(d.day.nunique())}
    for mode in ("rebote", "rebote_s", "cruce"):
        t = backtest(d, mode)
        s = stats(t)
        mid = t.fecha.iloc[len(t) // 2]
        s["primera_mitad"] = stats(t[t.fecha < mid]); s["segunda_mitad"] = stats(t[t.fecha >= mid])
        s["largos"] = stats(t[t.lado == 1]); s["cortos"] = stats(t[t.lado == -1])
        s["prop_aprobados_pct"] = round(prop_sim(t.R.values), 1)
        out["simbolos"][sym][mode] = s
        print(sym, mode, {k: s[k] for k in ("ops", "aciertos", "media_R", "total_R", "factor_beneficio",
                                             "max_caida_R", "prop_aprobados_pct")}, flush=True)

with open(os.path.join(ROOT, "data", "investigacion_vwap_ema9.json"), "w") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
