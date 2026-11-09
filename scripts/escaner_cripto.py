#!/usr/bin/env python3
"""Escáner cripto de Atalaya.

Lo mismo que el escáner de acciones, pero con las criptomonedas de Binance (pares
contra USDT) y velas diarias, que en cripto cierran a las 00:00 UTC:

  - Patrones con la vela diaria cerrada: los cinco del escáner de acciones
    (scripts/daytrader/scan.py) más ruptura del máximo de 52 semanas y máximo histórico.
  - Histórico de cada patrón en esa moneda (hasta 1.000 días) a 5 días vista.
  - Niveles: entrada, stop (mínimo de 10 días) y objetivo a 2R.
  - Semáforo con tres reglas: aciertos, que compense y volumen que confirme.
  - Registro de todas las señales: se compran de forma simulada con 1.000 € al
    precio que hay cuando el escáner las detecta, y se siguen a 1, 3, 5, 10 y 20 días.

Uso:
    python scripts/escaner_cripto.py scan     # tras el cierre diario: señales + registro
    python scripts/escaner_cripto.py update   # solo actualiza el registro

Datos: espejo público de Binance (data-api.binance.vision), sin claves.
"""

import datetime as dt
import importlib.util
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "cripto_escaner.json")
PORTFOLIO = os.path.join(ROOT, "data", "cripto_registro.json")
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; atalaya-escaner/1.0)"}

MIN_QUOTE_VOL = 2e6      # volumen mínimo en 24 h, en USDT
MIN_DAYS = 200           # días de historia mínimos (fuera monedas nuevas y acciones tokenizadas)
GRADE_MAX = 60           # monedas con histórico calculado
HOLD_DAYS = 5
STAKE = 1000.0
HORIZONS = (1, 3, 5, 10, 20)
RULES = {"win": 55.0, "n": 5, "vol": 1.5}
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "EURI", "AEUR", "USDE", "PYUSD",
          "USD1", "XUSD", "BFUSD", "USDS", "RLUSD", "GBP", "TRY", "BRL", "PAXG", "XAUT",
          "WBTC", "WBETH", "BETH", "STETH", "WSTETH", "BNSOL", "WETH"}

_spec = importlib.util.spec_from_file_location("dts_scan", os.path.join(ROOT, "scripts", "daytrader", "scan.py"))
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def now_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print("[cripto] " + msg, flush=True)


def get(path, params=None, tries=3):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    last = None
    for i in range(tries):
        for host in HOSTS:
            try:
                req = urllib.request.Request(host + path + q, headers=UA)
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as exc:
                last = exc
        time.sleep(2 * (i + 1))
    raise RuntimeError("%s: %s" % (path, last))


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def klines(symbol, limit=1000):
    """Velas diarias cerradas: lista de dicts (fecha, o, h, l, c, qv). Quita la vela en curso."""
    rows = get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
    time.sleep(0.12)
    now_ms = time.time() * 1000
    out = []
    for k in rows:
        out.append({"date": dt.datetime.fromtimestamp(k[0] / 1000, dt.timezone.utc).date().isoformat(),
                    "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                    "qv": float(k[7]), "closed": k[6] < now_ms})
    return out


def universe():
    """Pares contra USDT con volumen suficiente, sin stablecoins ni tokens apalancados."""
    tick = get("/api/v3/ticker/24hr")
    out = []
    for t in tick:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLE or re.search(r"(UP|DOWN|BULL|BEAR)$", base):
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < MIN_QUOTE_VOL or float(t.get("lastPrice") or 0) <= 0:
            continue
        out.append({"symbol": s, "base": base, "price": float(t["lastPrice"]), "qv24": qv,
                    "chg24": float(t.get("priceChangePercent") or 0)})
    out.sort(key=lambda x: -x["qv24"])
    return out


def setups_at(closes, qvs, i):
    """Patrones con la vela i cerrada. closes/qvs: listas completas."""
    win = closes[max(0, i - 299): i + 1]
    if len(win) < 30:
        return [], {}
    tags, nums = sm.find_setups(win)
    prev = closes[:i]
    last = closes[i]
    if i >= 365 and last > max(closes[i - 365: i]):
        tags.append("high_52w")
    if i >= 200 and prev and last > max(prev):
        tags.append("ath")
        if "high_52w" in tags:
            tags.remove("high_52w")
    v20 = qvs[max(0, i - 20): i]
    nums["vol_ratio"] = (qvs[i] / (sum(v20) / len(v20))) if v20 and sum(v20) else None
    return tags, nums


def stats_from(results):
    n = len(results)
    if not n:
        return {"n": 0}
    wins = [r for r in results if r > 0]
    losses = [-r for r in results if r <= 0]
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    return {"n": n, "win_pct": round(100.0 * len(wins) / n, 1), "avg": round(sum(results) / n, 3),
            "avg_win": round(avg_w, 3), "avg_loss": round(avg_l, 3),
            "payoff": round(avg_w / avg_l, 2) if avg_l else None}


def sem_color(h):
    g = h.get("grade") or {}
    hs = g.get("hist") or {}
    if g.get("error") or not hs.get("n"):
        return -1
    r1 = hs["n"] >= RULES["n"] and hs.get("win_pct", 0) >= RULES["win"]
    r2 = (hs.get("avg") or 0) > 0
    r3 = (h.get("vol_ratio") or 0) >= RULES["vol"]
    ok = sum([r1, r2, r3])
    return 2 if ok == 3 else 1 if ok == 2 else 0


def scan(data):
    uni = universe()
    log("universo: %d pares con más de %.0f M USDT en 24 h" % (len(uni), MIN_QUOTE_VOL / 1e6))
    hits, pooled, failed = [], {}, 0
    signal_day = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    for u in uni:
        try:
            ks = [k for k in klines(u["symbol"]) if k["closed"]]
        except Exception:
            failed += 1
            continue
        if len(ks) < MIN_DAYS or ks[-1]["date"] != signal_day:
            continue                     # poca historia o sin la vela de ayer (par parado)
        closes = [k["c"] for k in ks]
        qvs = [k["qv"] for k in ks]
        i = len(closes) - 1
        tags, nums = setups_at(closes, qvs, i)
        if not tags:
            continue
        h = {"symbol": u["base"], "pair": u["symbol"], "setups": tags, "price": u["price"],
             "close": closes[i], "close_date": ks[i]["date"], "rsi": round(nums["rsi"], 1) if nums.get("rsi") else None,
             "volume": qvs[i], "qv24": u["qv24"], "chg24": u["chg24"],
             "vol_ratio": round(nums["vol_ratio"], 2) if nums.get("vol_ratio") else None,
             "_closes": closes, "_qvs": qvs}
        hits.append(h)
    hits.sort(key=lambda h: -h["qv24"])
    # Histórico de los mismos patrones en cada moneda y niveles del plan
    for h in hits[:GRADE_MAX]:
        closes, qvs = h["_closes"], h["_qvs"]
        want = set(h["setups"])
        res = []
        for i in range(30, len(closes) - HOLD_DAYS):
            tags, _ = setups_at(closes, qvs, i)
            common = want.intersection(tags)
            if common:
                r = (closes[i + HOLD_DAYS] / closes[i] - 1) * 100
                res.append(r)
                for t in common:
                    pooled.setdefault(t, []).append(r)
        entry = closes[-1]
        stop = min(closes[-10:])
        if entry - stop < entry * 0.02:
            stop = entry * 0.97
        stop = stop * 0.995
        target = entry + 2 * (entry - stop)
        h["grade"] = {"hist": stats_from(res), "unit": "%", "hold_days": HOLD_DAYS,
                      "entry": entry, "stop": stop, "target": target, "rr": 2.0}
    for h in hits:
        h.pop("_closes", None)
        h.pop("_qvs", None)
    data["daily"] = {"run_at": now_utc(), "as_of": "%s cierre UTC" % (signal_day or ""), "close_date": signal_day,
                     "universe": len(uni), "scanned": len(uni) - failed, "failed": failed,
                     "filters": {"min_quote_volume": MIN_QUOTE_VOL}, "hits": hits,
                     "market": {k: stats_from(v) for k, v in pooled.items()}, "earnings": []}
    log("señales: %d (valoradas %d)" % (len(hits), min(len(hits), GRADE_MAX)))


def portfolio_update(data, mode):
    pf = load(PORTFOLIO, {"positions": []})
    pos = pf["positions"]
    have = {(p["symbol"], p["signal_date"]) for p in pos}
    added = 0
    blk = data.get("daily") or {}
    if mode == "scan" and blk.get("run_at", "")[:10] == now_utc()[:10]:
        # Si se repite el escaneo del mismo cierre, quedan solo las señales que siguen saliendo
        today_syms = {h["symbol"] for h in blk.get("hits", []) if (h.get("grade") or {}).get("entry")}
        pos[:] = [p for p in pos if p["date"] != now_utc()[:10]
                  or (p["signal_date"] == blk.get("close_date") and p["symbol"] in today_syms)]
        have = {(p["symbol"], p["signal_date"]) for p in pos}
        prices = {u["symbol"]: u["price"] for u in universe()}
        for h in blk.get("hits", []):
            g = h.get("grade") or {}
            if not g.get("entry"):
                continue
            key = (h["symbol"], h["close_date"])
            if key in have:
                continue
            px = prices.get(h["pair"])          # precio en el momento de la detección
            if not px or px <= g["stop"]:
                continue
            hs = g.get("hist") or {}
            p = {"symbol": h["symbol"], "pair": h["pair"], "kind": "daily", "sem": sem_color(h),
                 "setups": h["setups"], "signal_date": h["close_date"], "signal_price": h["close"],
                 "signal_ts": (dt.date.fromisoformat(h["close_date"]) + dt.timedelta(days=1)).isoformat() + "T00:00:00Z",
                 "date": now_utc()[:10], "buy_ts": now_utc(), "entry_type": "deteccion",
                 "entry": px, "stop": g["stop"], "target": px + 2 * (px - g["stop"]),
                 "win": hs.get("win_pct"), "n": hs.get("n"), "rsi": h.get("rsi"), "vol_ratio": h.get("vol_ratio"),
                 "closes": {}, "last": px, "status": "abierta"}
            pos.append(p)
            have.add(key)
            added += 1
    # Seguimiento con velas diarias (máximos y mínimos para el plan)
    for p in pos:
        if p.get("status") == "terminada":
            continue
        try:
            ks = klines(p["pair"], 60)
        except Exception:
            continue
        after = [k for k in ks if k["date"] >= p["date"]]
        if not after:
            continue
        p["last"] = after[-1]["c"]
        closed = [k for k in after if k["closed"]]
        for k in HORIZONS:
            if len(closed) >= k:
                p["closes"][str(k)] = {"date": closed[k - 1]["date"], "ret": round((closed[k - 1]["c"] / p["entry"] - 1) * 100, 2)}
        p["sessions"] = len(closed)
        p["ret_now"] = round((p["last"] / p["entry"] - 1) * 100, 2)
        if not p.get("plan"):
            risk = p["entry"] - p["stop"]
            for n, k in enumerate(closed[:10], 1):
                if k["l"] <= p["stop"]:
                    p["plan"] = {"outcome": "stop", "R": -1.0, "ret": round((p["stop"] / p["entry"] - 1) * 100, 2), "sessions": n}
                    break
                if k["h"] >= p["target"]:
                    p["plan"] = {"outcome": "objetivo", "R": 2.0, "ret": round((p["target"] / p["entry"] - 1) * 100, 2), "sessions": n}
                    break
            if not p.get("plan") and len(closed) >= 10:
                c = closed[9]["c"]
                p["plan"] = {"outcome": "cierre", "R": round((c - p["entry"]) / risk, 2), "ret": round((c / p["entry"] - 1) * 100, 2), "sessions": 10}
        if len(closed) >= max(HORIZONS) and p.get("plan"):
            p["status"] = "terminada"
    pf.update({"updated": now_utc(), "stake": STAKE, "horizons": list(HORIZONS), "rules": RULES, "positions": pos})
    save(PORTFOLIO, pf)
    log("registro: %d señales, %d nuevas" % (len(pos), added))


def notify(data):
    """Aviso al móvil con ntfy (secreto NTFY_TOPIC, el mismo que el escáner de acciones)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    blk = data.get("daily") or {}
    if not topic or not blk:
        return
    greens = [h for h in blk.get("hits", []) if sem_color(h) == 2]
    labels = {"breakout_20d": "ruptura 20 d", "bull_flag": "bandera", "reclaim_ma20": "recupera media 20",
              "oversold": "sobreventa", "oversold_bounce": "rebote", "high_52w": "máx. 52 sem", "ath": "máx. histórico"}
    lines = ["%s  %s  %.0f %% aciertos (%d)  vol %.1fx" % (
        h["symbol"], ", ".join(labels.get(t, t) for t in h["setups"]), h["grade"]["hist"]["win_pct"],
        h["grade"]["hist"]["n"], h.get("vol_ratio") or 0) for h in greens[:15]]
    aths = [h["symbol"] for h in blk.get("hits", []) if "ath" in h["setups"]]
    if aths:
        lines.append("Máximo histórico: " + ", ".join(aths[:15]))
    body = json.dumps({"topic": topic, "title": "Cripto: %d en verde" % len(greens),
                       "message": "\n".join(lines) or "Ninguna cumple las tres reglas hoy.",
                       "tags": ["coin"], "click": "https://mchamosa.github.io/atalaya/escaner.html?m=cripto"}).encode("utf-8")
    try:
        req = urllib.request.Request("https://ntfy.sh/", data=body, method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        log("aviso enviado a ntfy")
    except Exception as exc:
        log("ntfy: %s" % exc)


def main():
    mode = (sys.argv[1:] or ["scan"])[0]
    data = load(OUT, {})
    data["mode"] = "cripto"
    data["source"] = "Binance (data-api.binance.vision), velas diarias UTC"
    if mode == "scan":
        scan(data)
    portfolio_update(data, mode)
    if mode == "scan":
        notify(data)
    data["updated"] = now_utc()
    save(OUT, data)


if __name__ == "__main__":
    main()
