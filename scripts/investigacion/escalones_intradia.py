#!/usr/bin/env python3
"""Escalones de máximos y mínimos en velas de 15 y 5 minutos (Yahoo: últimos 60 días)."""
import importlib.util, json, os, time, datetime as dt
import pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
s = importlib.util.spec_from_file_location("esc", os.path.join(ROOT, "scripts", "investigacion", "escalones.py"))
src = open(s.origin).read()
ns = {}
exec(src[:src.index("out = {")], ns)          # carga, sim y lista de valores sin ejecutar el estudio diario
load, sim, TICKERS = ns["load"], ns["sim"], ns["TICKERS"]
out = {"run_at": dt.datetime.utcnow().isoformat() + "Z", "fee_per_side": ns["FEE"], "tf": {}}
for itv in ("15m", "5m"):
    out["tf"][itv] = {}
    for sym in TICKERS:
        try:
            d = load(sym, "60d", itv); time.sleep(0.4)
        except Exception as e:
            print(sym, itv, e); continue
        if len(d) < 300:
            continue
        r = {"bh": float(d.close.iloc[-1] / d.close.iloc[101])}
        for N in (5, 10, 20, 30, 50, 100):
            X = max(2, N // 2)
            r["ls_%d" % N] = sim(d, N=N, mode="ls")
            r["long_%d" % N] = sim(d, N=N, X=X, mode="long")
            r["short_%d" % N] = sim(d, N=N, X=X, mode="short")
        out["tf"][itv][sym] = r
        print(itv, sym, "ok", flush=True)
with open(os.path.join(ROOT, "data", "investigacion_escalones_intradia.json"), "w") as fh:
    json.dump(out, fh)
