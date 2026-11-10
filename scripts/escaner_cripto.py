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
    python scripts/escaner_cripto.py session  # ruptura del rango de apertura de Londres o Nueva York

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
from zoneinfo import ZoneInfo

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

SESSIONS = {   # ruptura del rango de apertura en las sesiones de las bolsas
    "london": {"name": "Londres", "tz": "Europe/London", "open": (8, 0), "close": (16, 30)},
    "ny": {"name": "Nueva York", "tz": "America/New_York", "open": (9, 30), "close": (16, 0)},
}
SESSION_COINS = 40       # monedas con más volumen que se miran en cada sesión
OR_MIN = 15              # minutos del rango de apertura
DETECT_MIN = 40          # el escáner mira la ruptura a los 40 minutos de la apertura
HIST_DAYS = 45           # días de historia intradía para la estadística
MIN_RISK = 0.005         # riesgo mínimo (entrada menos stop) del 0,5 %: con rangos más estrechos no compensa
FEES = 0.002             # comisiones de compra y venta en Binance (0,1 % + 0,1 %), descontadas del resultado
NY = ZoneInfo("America/New_York")

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


def sem_color(h, kind="daily"):
    g = h.get("grade") or {}
    hs = g.get("hist") or {}
    if g.get("error") or not hs.get("n"):
        return -1
    r1 = hs["n"] >= RULES["n"] and hs.get("win_pct", 0) >= RULES["win"]
    if kind == "orb":
        ts = (g.get("mc") or {}).get("target_share")
        r2 = ts is not None and ts >= 1 / 3
    else:
        r2 = (hs.get("avg") or 0) > 0
    r3 = (h.get("vol_ratio") or 0) >= RULES["vol"]
    ok = sum([r1, r2, r3])
    return 2 if ok == 3 else 1 if ok == 2 else 0


def scan(data):
    uni = universe()
    log("universo: %d pares con más de %.0f M USDT en 24 h" % (len(uni), MIN_QUOTE_VOL / 1e6))
    hits, pooled, failed, eligible = [], {}, 0, []
    signal_day = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    for u in uni:
        try:
            ks = [k for k in klines(u["symbol"]) if k["closed"]]
        except Exception:
            failed += 1
            continue
        if len(ks) < MIN_DAYS or ks[-1]["date"] != signal_day:
            continue                     # poca historia o sin la vela de ayer (par parado)
        eligible.append(u["base"])
        closes = [k["c"] for k in ks]
        if u["base"] == "BTC" and len(closes) >= 230:
            m50, m50p, m200 = sum(closes[-50:]) / 50, sum(closes[-70:-20]) / 50, sum(closes[-200:]) / 200
            name = "alcista" if closes[-1] > m200 and m50 > m200 and m50 > m50p else "bajista" if closes[-1] < m200 and m50 < m50p else "lateral"
            data["regime"] = {"name": name, "ref": "BTC", "spy": closes[-1], "ma50": round(m50, 2), "ma200": round(m200, 2),
                              "ma50_slope_pct": round((m50 / m50p - 1) * 100, 2), "at": now_utc()}
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
    data["eligible"] = eligible           # monedas con historia suficiente (también para las sesiones)
    data["daily"] = {"run_at": now_utc(), "as_of": "%s cierre UTC" % (signal_day or ""), "close_date": signal_day,
                     "universe": len(uni), "scanned": len(uni) - failed, "failed": failed,
                     "filters": {"min_quote_volume": MIN_QUOTE_VOL}, "hits": hits,
                     "market": {k: stats_from(v) for k, v in pooled.items()}, "earnings": []}
    log("señales: %d (valoradas %d)" % (len(hits), min(len(hits), GRADE_MAX)))

# ------------------------------------------------------------------ sesiones (rango de apertura)

def klines_5m(symbol, start_ms, end_ms):
    """Velas de 5 minutos entre dos instantes (paginando de 1.000 en 1.000)."""
    out, cur = [], start_ms
    while cur < end_ms:
        rows = get("/api/v3/klines", {"symbol": symbol, "interval": "5m", "startTime": cur,
                                       "endTime": end_ms, "limit": 1000})
        time.sleep(0.1)
        if not rows:
            break
        out += [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                 "qv": float(r[7]), "end": r[6]} for r in rows]
        cur = rows[-1][0] + 300000
        if len(rows) < 1000:
            break
    return out


def session_times(key, day):
    """Apertura, final del rango, detección y cierre de la sesión de ese día (en ms UTC)."""
    ss = SESSIONS[key]
    tz = ZoneInfo(ss["tz"])
    op = dt.datetime.combine(day, dt.time(*ss["open"]), tz)
    cl = dt.datetime.combine(day, dt.time(*ss["close"]), tz)
    ms = lambda x: int(x.timestamp() * 1000)
    return ms(op), ms(op + dt.timedelta(minutes=OR_MIN)), ms(op + dt.timedelta(minutes=DETECT_MIN)), ms(cl)


def orb_day(bars, t_open, t_or, t_det, t_close):
    """La regla en un día: rango de los primeros 15 min, ruptura (cierre de vela por encima)
    antes de la detección, entrada al precio de la detección, stop en la mitad del rango,
    objetivo a 2R y salida al cierre de la sesión."""
    rng = [b for b in bars if t_open <= b["t"] < t_or]
    if len(rng) < OR_MIN // 5:
        return None
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    after = [b for b in bars if t_or <= b["t"] and b["end"] < t_det]
    brk = next((b for b in after if b["c"] > hi), None)
    if not brk or not after:
        return {"range": (hi, lo), "breakout": None}
    det = after[-1]
    entry, stop = det["c"], int((hi + lo) / 2 * 1e8) / 1e8
    res = {"range": (hi, lo), "breakout": brk, "detect": det, "entry": entry, "stop": stop}
    if entry <= stop:
        res["void"] = True
        return res
    if (entry - stop) / entry < MIN_RISK:
        res["void"] = True          # rango demasiado estrecho: las comisiones se comerían el resultado
        res["tight"] = True
        return res
    target = entry + 2 * (entry - stop)
    fee_r = FEES * entry / (entry - stop)   # comisiones expresadas en R
    res["target"] = target
    rest = [b for b in bars if b["t"] > det["t"] and b["end"] <= t_close]
    res["outcome"] = None
    for b in rest:
        if b["l"] <= stop:
            res.update(outcome="stop", R=round(-1.0 - fee_r, 2), at=b["t"])
            break
        if b["h"] >= target:
            res.update(outcome="objetivo", R=round(2.0 - fee_r, 2), at=b["t"])
            break
    if res["outcome"] is None and rest and rest[-1]["end"] >= t_close - 1000:
        c = rest[-1]["c"]
        res.update(outcome="cierre", R=round((c - entry) / (entry - stop) - fee_r, 2), at=rest[-1]["t"])
    return res


def ny_label(ms):
    t = dt.datetime.fromtimestamp(ms / 1000, NY)
    return t.strftime("%I:%M %p").lstrip("0"), t.strftime("%H:%M")


def active_session(now):
    """Qué sesión toca escanear ahora (entre 25 y 120 minutos tras la apertura)."""
    for key in SESSIONS:
        day = now.astimezone(ZoneInfo(SESSIONS[key]["tz"])).date()
        if day.weekday() >= 5:
            continue
        t_open = session_times(key, day)[0]
        mins = (now.timestamp() * 1000 - t_open) / 60000
        if 25 <= mins <= 120:
            return key, day
    return None, None


def scan_session(data, key, day):
    ss = SESSIONS[key]
    elig = set(data.get("eligible") or [])
    uni = [u for u in universe() if not elig or u["base"] in elig][:SESSION_COINS]
    t_open, t_or, t_det, t_close = session_times(key, day)
    now_ms = int(time.time() * 1000)
    t_det = min(max(t_det, t_or + 5 * 60000), now_ms)      # detección: ahora, como mínimo tras el rango
    start = t_open - HIST_DAYS * 86400000
    hits, pooled, scanned = [], [], 0
    for u in uni:
        try:
            bars = klines_5m(u["symbol"], start, now_ms)
        except Exception:
            continue
        scanned += 1
        today = orb_day(bars, t_open, t_or, t_det, t_close)
        if not today or not today.get("breakout") or today.get("void"):
            continue
        # Histórico: la misma regla en las sesiones anteriores
        trades, vols = [], []
        for k in range(1, HIST_DAYS + 1):
            d = day - dt.timedelta(days=k)
            if d.weekday() >= 5:
                continue
            o, r, de, c = session_times(key, d)
            win = [b for b in bars if o <= b["t"] < de]
            if win:
                vols.append(sum(b["qv"] for b in win))
            res = orb_day(bars, o, r, de, c)
            if res and res.get("outcome"):
                trades.append(res)
        rs = [t["R"] for t in trades]
        pooled += rs
        n = len(trades)
        cnt = lambda x: sum(1 for t in trades if t["outcome"] == x)
        side = cnt("objetivo") + cnt("stop")
        vol_today = sum(b["qv"] for b in bars if t_open <= b["t"] < t_det)
        vol_avg = sum(vols[:20]) / len(vols[:20]) if vols else None
        hi, lo = today["range"]
        at12, at24 = ny_label(today["breakout"]["end"] + 1)
        h = {"symbol": u["base"], "pair": u["symbol"], "session": key, "price": today["breakout"]["c"],
             "at": at12, "bar_start": ny_label(today["breakout"]["t"])[1], "or_high": hi, "or_low": lo,
             "pct_over": round((today["breakout"]["c"] / hi - 1) * 100, 2),
             "detect_price": today["entry"], "detect_bar": ny_label(today["detect"]["t"])[1],
             "detect_ts": dt.datetime.fromtimestamp((today["detect"]["end"] + 1) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "signal_ts": dt.datetime.fromtimestamp((today["breakout"]["end"] + 1) / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "vol_ratio": round(vol_today / vol_avg, 2) if vol_avg else None, "qv24": u["qv24"],
             "grade": {"entry": today["entry"], "stop": today["stop"], "target": today["target"], "rr": 2.0,
                       "unit": "R", "hist": stats_from(rs),
                       "mc": {"p_target": cnt("objetivo") / n if n else None, "p_stop": cnt("stop") / n if n else None,
                              "p_neither": cnt("cierre") / n if n else None,
                              "p_green": sum(1 for x in rs if x > 0) / n if n else None,
                              "avg_R": round(sum(rs) / n, 3) if n else None,
                              "target_share": round(cnt("objetivo") / side, 3) if side else None, "historic": True}}}
        hits.append(h)
    hits.sort(key=lambda h: -h["qv24"])
    det_label = dt.datetime.fromtimestamp(t_det / 1000, NY).strftime("%Y-%m-%d %H:%M")
    data["orb"] = {"run_at": now_utc(), "as_of": det_label + " ET", "session": key, "session_name": ss["name"],
                   "or_minutes": OR_MIN, "scanned": scanned, "universe": len(uni), "hits": hits,
                   "market": {"orb": stats_from(pooled)}, "earnings": []}
    data.setdefault("orb_sessions", {})[key] = data["orb"]
    log("sesión %s: %d rupturas de %d monedas" % (ss["name"], len(hits), scanned))


def session_register(data, pf):
    blk = data.get("orb") or {}
    if blk.get("run_at", "")[:10] != now_utc()[:10]:
        return 0
    pos = pf["positions"]
    have = {(p["symbol"], p.get("session"), p["date"]) for p in pos}
    added = 0
    for h in blk.get("hits", []):
        g = h["grade"]
        key = (h["symbol"], h["session"], blk["as_of"][:10])
        if key in have:
            continue
        hs = g.get("hist") or {}
        pos.append({"symbol": h["symbol"], "pair": h["pair"], "kind": "orb", "session": h["session"],
                    "session_name": SESSIONS[h["session"]]["name"], "sem": sem_color(h, "orb"),
                    "date": blk["as_of"][:10], "signal_date": blk["as_of"][:10], "signal_ts": h["signal_ts"],
                    "buy_ts": h["detect_ts"], "entry_type": "deteccion", "breakout_price": h["price"],
                    "at": h["at"], "entry_bar": h["detect_bar"], "pct_over": h["pct_over"],
                    "or_high": h["or_high"], "or_low": h["or_low"], "entry": g["entry"], "stop": g["stop"],
                    "target": g["target"], "win": hs.get("win_pct"), "n": hs.get("n"),
                    "ts": g["mc"].get("target_share"), "vol_ratio": h.get("vol_ratio"),
                    "closes": {}, "last": g["entry"], "status": "abierta"})
        have.add(key)
        added += 1
    return added


def session_outcomes(pf):
    """Cierra el plan de las rupturas de sesión una vez terminada la sesión."""
    for p in pf["positions"]:
        if p.get("kind") != "orb" or p.get("plan") or not p.get("session"):
            continue
        day = dt.date.fromisoformat(p["date"])
        o, r, de, c = session_times(p["session"], day)
        if time.time() * 1000 < c + 5 * 60000:
            continue
        buy = int(dt.datetime.strptime(p["buy_ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        try:
            bars = klines_5m(p["pair"], buy, c + 300000)
        except Exception:
            continue
        risk = p["entry"] - p["stop"]
        fee_r = FEES * p["entry"] / risk
        res = None
        last = None
        for b in bars:
            if b["t"] < buy or b["end"] > c:
                continue
            last = b
            if b["l"] <= p["stop"]:
                res = {"outcome": "stop", "R": round(-1.0 - fee_r, 2)}
                break
            if b["h"] >= p["target"]:
                res = {"outcome": "objetivo", "R": round(2.0 - fee_r, 2)}
                break
        if not res and last:
            res = {"outcome": "cierre", "R": round((last["c"] - p["entry"]) / risk - fee_r, 2)}
        if res:
            res["ret"] = round(res["R"] * risk / p["entry"] * 100, 2)
            res["net"] = True
            p["plan"] = res



def portfolio_update(data, mode):
    pf = load(PORTFOLIO, {"positions": []})
    pos = pf["positions"]
    have = {(p["symbol"], p["signal_date"]) for p in pos if p.get("kind") != "orb"}
    added = 0
    blk = data.get("daily") or {}
    if mode == "scan" and blk.get("run_at", "")[:10] == now_utc()[:10]:
        # Si se repite el escaneo del mismo cierre, quedan solo las señales que siguen saliendo
        today_syms = {h["symbol"] for h in blk.get("hits", []) if (h.get("grade") or {}).get("entry")}
        pos[:] = [p for p in pos if p.get("kind") == "orb" or p["date"] != now_utc()[:10]
                  or (p["signal_date"] == blk.get("close_date") and p["symbol"] in today_syms)]
        have = {(p["symbol"], p["signal_date"]) for p in pos if p.get("kind") != "orb"}
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
                 "win": hs.get("win_pct"), "n": hs.get("n"), "avg": hs.get("avg"), "rsi": h.get("rsi"), "vol_ratio": h.get("vol_ratio"),
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
        if not p.get("plan") and p.get("kind") != "orb":
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
    elig = set(data.get("eligible") or [])
    if elig:   # fuera acciones tokenizadas y monedas sin historia
        pos[:] = [p for p in pos if p.get("kind") != "orb" or p["symbol"] in elig]
    # fuera las rupturas con riesgo menor del mínimo (rango demasiado estrecho)
    pos[:] = [p for p in pos if p.get("kind") != "orb" or (p["entry"] - p["stop"]) / p["entry"] >= MIN_RISK]
    for p in pos:   # rupturas ya cerradas sin comisiones: se recalculan
        if p.get("kind") == "orb" and p.get("plan") and not p["plan"].get("net"):
            p["plan"]["R"] = round(p["plan"]["R"] - FEES * p["entry"] / (p["entry"] - p["stop"]), 2)
            p["plan"]["net"] = True
    if mode == "session":
        added += session_register(data, pf)
    session_outcomes(pf)
    pf.update({"updated": now_utc(), "stake": STAKE, "horizons": list(HORIZONS), "rules": RULES, "positions": pos})
    save(PORTFOLIO, pf)
    log("registro: %d señales, %d nuevas" % (len(pos), added))


def notify_session(data):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    blk = data.get("orb") or {}
    if not topic or not blk:
        return
    greens = [h for h in blk.get("hits", []) if sem_color(h, "orb") == 2]
    lines = ["%s  rompe %s  entrada %s  stop %s  objetivo %s  (%.0f %% aciertos en %d)" % (
        h["symbol"], h["at"], fmt(h["grade"]["entry"]), fmt(h["grade"]["stop"]), fmt(h["grade"]["target"]),
        h["grade"]["hist"].get("win_pct") or 0, h["grade"]["hist"].get("n") or 0) for h in greens[:15]]
    body = json.dumps({"topic": topic, "title": "Cripto %s: %d rupturas, %d en verde" % (blk["session_name"], len(blk.get("hits", [])), len(greens)),
                       "message": "\n".join(lines) or "Ninguna ruptura cumple las tres reglas.",
                       "tags": ["coin"], "click": "https://mchamosa.github.io/atalaya/escaner.html?m=cripto"}).encode("utf-8")
    try:
        req = urllib.request.Request("https://ntfy.sh/", data=body, method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as exc:
        log("ntfy: %s" % exc)


def fmt(x):
    return ("%.2f" % x) if x >= 1 else ("%.4g" % x)


def notify(data):
    """Aviso al móvil con ntfy (secreto NTFY_TOPIC, el mismo que el escáner de acciones)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    blk = data.get("daily") or {}
    if not topic or not blk:
        return
    greens = [h for h in blk.get("hits", []) if sem_color(h) == 2]
    labels = {"breakout_20d": "ruptura 20 d", "bull_flag": "bandera", "reclaim_ma20": "recupera media 20",
              "oversold": "sobreventa", "oversold_bounce": "rebote", "high_52w": "máx. 52 sem", "ath": "máx. histórico",
              "pullback_trend": "retroceso en tendencia"}
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
    if mode == "session":
        key, day = active_session(dt.datetime.now(dt.timezone.utc))
        if len(sys.argv) > 2:
            key, day = sys.argv[2], dt.datetime.now(ZoneInfo(SESSIONS[sys.argv[2]]["tz"])).date()
        if not key:
            log("ahora no toca ninguna sesión; solo se actualiza el registro")
            mode = "update"
        else:
            scan_session(data, key, day)
    if mode == "scan":
        scan(data)
    portfolio_update(data, mode)
    if mode == "scan":
        notify(data)
    if mode == "session":
        notify_session(data)
    data["updated"] = now_utc()
    save(OUT, data)


if __name__ == "__main__":
    main()
