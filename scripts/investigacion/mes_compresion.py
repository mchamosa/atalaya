#!/usr/bin/env python3
"""Final y principio de mes, y compresión (NR7 / día interior) antes de romper, en acciones 2005-hoy."""
import json, os, time, datetime as dt
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "scripts", "investigacion", "regimen.py")).read()
ns = {"__file__": os.path.join(ROOT, "scripts", "investigacion", "regimen.py")}
exec(src[:src.index("H = (1, 5, 10, 20)")], ns)
load, TICKERS = ns["load"], ["SPY", "QQQ", "IWM"] + ns["TICKERS"]
FEE = 0.0005
PER = (("2005-2015", "2005-01-01", "2016-01-01"), ("2016-2026", "2016-01-01", "2100-01-01"))
out = {"run_at": dt.datetime.utcnow().isoformat() + "Z", "tickers": len(TICKERS), "tom": {}, "comp": {}}
tom_acc = {}; comp_acc = {}
def addv(acc, key, vals):
    acc.setdefault(key, []).extend([float(v) for v in vals])

for sym in TICKERS:
    try:
        d = load(sym, "max", "1d"); time.sleep(0.35)
    except Exception as e:
        print(sym, e); continue
    d = d[d.index >= "2004-06-01"]
    if len(d) < 400: continue
    o, h, l, c = [d[k].values for k in ("open", "high", "low", "close")]
    n = len(d); idx = d.index
    ym = idx.year * 12 + idx.month
    last_of_month = np.r_[ym[1:] != ym[:-1], False]          # último día de cada mes
    # ---- Final/principio de mes: compra al cierre del último día, vende al cierre del 3.er día del mes siguiente
    for name, back, fwd in (("tom_-1_+3", 0, 3), ("tom_-1_+1", 0, 1), ("tom_-3_+3", 2, 3)):
        r_tom, r_other = [], []
        ent_days = np.where(last_of_month)[0]
        for i in ent_days:
            a, b = i - back, i + fwd
            if a < 1 or b >= n: continue
            r_tom.append((idx[a], c[b] / c[a - 1 if back else a] - 1 - 2 * FEE))
        # ventanas de la misma longitud en el resto del mes (sin solapar con el cambio de mes)
        L = back + fwd + (1 if back else 0)
        mask_tom = np.zeros(n, bool)
        for i in ent_days:
            mask_tom[max(0, i - back - L): min(n, i + fwd + 1)] = True
        starts = [j for j in range(1, n - L, L) if not mask_tom[j:j + L + 1].any()]
        r_other = [(idx[j], c[j + L] / c[j] - 1 - 2 * FEE) for j in starts]
        for pe, a0, b0 in PER:
            addv(tom_acc, (name, "cambio de mes", pe), [r for t, r in r_tom if a0 <= str(t.date()) < b0])
            addv(tom_acc, (name, "resto del mes", pe), [r for t, r in r_other if a0 <= str(t.date()) < b0])
    # ---- Compresión: NR7 o día interior; al día siguiente compra si rompe el máximo, stop en el mínimo
    rng = h - l
    nr7 = pd.Series(rng).rolling(7).apply(lambda x: x[-1] <= x.min(), raw=True).fillna(0).values.astype(bool)
    inside = np.r_[False, (h[1:] <= h[:-1]) & (l[1:] >= l[:-1])]
    ma200 = pd.Series(c).rolling(200).mean().values
    for setup, mask in (("nr7", nr7), ("dia_interior", inside), ("cualquier_dia", np.ones(n, bool))):
        for trend in ("todos", "tendencia"):
            res = []
            for i in np.where(mask)[0]:
                if i < 200 or i + 6 >= n: continue
                if trend == "tendencia" and not c[i] > ma200[i]: continue
                j = i + 1
                if h[j] <= h[i]: continue                      # no rompe: no hay operación
                ent = max(o[j], h[i]); stp = l[i]
                if ent <= stp: continue
                risk = ent - stp; rr = None
                tgt = ent + 2 * risk
                for k in range(j, min(n, j + 5)):
                    lo_hit = l[k] <= stp if k > j else (o[j] <= stp)
                    if k == j and l[k] <= stp and c[k] < stp: lo_hit = True
                    if lo_hit:
                        rr = (min(o[k], stp) - ent) / ent; break
                    if h[k] >= tgt and k > j:
                        rr = (max(o[k], tgt) - ent) / ent; break
                if rr is None:
                    rr = c[min(n - 1, j + 4)] / ent - 1
                res.append((idx[j], rr - 2 * FEE, (rr - 2 * FEE) * ent / risk))
            for pe, a0, b0 in PER:
                sel = [(r, R) for t, r, R in res if a0 <= str(t.date()) < b0]
                addv(comp_acc, (setup, trend, pe, "ret"), [r for r, R in sel])
                addv(comp_acc, (setup, trend, pe, "R"), [R for r, R in sel])
    print(sym, "ok", flush=True)

def summ(v):
    a = np.array(v)
    return {"n": int(len(a)), "avg": float(a.mean() * 100) if len(a) else None, "win": float((a > 0).mean() * 100) if len(a) else None}
out["tom"] = [{"rule": k[0], "window": k[1], "period": k[2], **summ(v)} for k, v in tom_acc.items()]
out["comp"] = []
for k, v in comp_acc.items():
    if k[3] != "ret": continue
    s = summ(v); R = np.array(comp_acc[k[:3] + ("R",)])
    out["comp"].append({"setup": k[0], "filter": k[1], "period": k[2], **s, "avgR": float(R.mean()) if len(R) else None})
with open(os.path.join(ROOT, "data", "investigacion_mes_compresion.json"), "w") as fh:
    json.dump(out, fh)
