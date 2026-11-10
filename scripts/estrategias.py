#!/usr/bin/env python3
"""Las tres estrategias que dieron dinero en el estudio de BTC (2018-2026, con comisiones).

1. Tendencia con la media de 50 días: dentro si la vela diaria cierra por encima de su media de 50,
   fuera si cierra por debajo. Se compra o vende a la apertura del día siguiente (00:00 UTC).
2. Rupturas 20/55/100 por tercios: cada tercio entra cuando el cierre supera el máximo de los
   N cierres anteriores (N = 20, 55, 100) y sale cuando baja del mínimo de los N/2 anteriores.
3. Zona verde de 4 horas: hueco alcista (máximo de la vela de hace 3 por debajo del mínimo de la
   de hace 1) de al menos un 1 %. Cuando el precio vuelve a tocarla, si la moneda está por encima
   de su media de 50 días, orden de compra a mitad de zona; stop si una vela de 4 h cierra por
   debajo de la zona; objetivo a dos veces el ancho de la zona; salida a las 100 velas si no toca.

Todo se recalcula cada vez con las velas cerradas, así que el resultado no depende de a qué hora
corra el script. El registro guarda las operaciones que empiezan desde la puesta en marcha.
"""
import datetime as dt
import importlib.util
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("ec", os.path.join(ROOT, "scripts", "escaner_cripto.py"))
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

OUT = os.path.join(ROOT, "data", "estrategias.json")
COINS = 25               # monedas con más volumen (BTC siempre incluido)
STAKE = 1000.0           # euros por operación (en la de rupturas, un tercio por tramo)
FEE = 0.002              # compra + venta en Binance
ZONE_MIN = 0.01          # ancho mínimo de la zona verde
ZONE_TARGET = 2.0        # objetivo: veces el ancho de la zona desde la entrada
ZONE_HOLD = 100          # velas de 4 h como máximo en la operación
ZONE_TOUCH_MAX = 300     # velas como máximo entre la zona y el primer toque
ZONE_FILL_MAX = 100      # velas como máximo entre el toque y la compra a mitad de zona
BREAKS = (20, 55, 100)
NAMES = {"media50": "Tendencia media 50", "rupturas": "Rupturas 20/55/100", "zona4h": "Zona verde 4 h"}


def log(msg):
    print("[estrategias] " + msg, flush=True)


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kl(symbol, interval, limit=1000):
    rows = ec.get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    time.sleep(0.12)
    now = time.time() * 1000
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
            for k in rows if k[6] < now]


def sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


# ------------------------------------------------------------------ 1 y 2: velas diarias

def trades_from_state(d, state, kind, weight=1.0, extra=None):
    """state[i] = dentro tras el cierre i. Se opera a la apertura de i+1."""
    trades, cur = [], None
    for i in range(len(d) - 1):
        prev = state[i - 1] if i > 0 else False
        if state[i] and not prev:
            cur = {"kind": kind, "signal_ts": iso(d[i]["t"] + 86400000), "entry_ts": iso(d[i + 1]["t"]),
                   "entry": d[i + 1]["o"], "weight": weight}
            if extra:
                cur.update(extra)
        elif prev and not state[i] and cur:
            cur.update({"exit_ts": iso(d[i + 1]["t"]), "exit": d[i + 1]["o"], "reason": "señal de salida"})
            trades.append(cur)
            cur = None
    n = len(d) - 1   # último cierre: si la señal cambia hoy, la compra o venta es a la apertura de hoy
    prev = state[n - 1]
    if state[n] and not prev:
        cur = {"kind": kind, "signal_ts": iso(d[n]["t"] + 86400000), "entry_ts": iso(d[n]["t"] + 86400000),
               "entry": None, "weight": weight}
        if extra:
            cur.update(extra)
    elif prev and not state[n] and cur:
        cur.update({"exit_ts": iso(d[n]["t"] + 86400000), "exit": None, "reason": "señal de salida"})
        trades.append(cur)
        cur = None
    if cur:
        cur["open"] = True
        trades.append(cur)
    return trades


def state_media50(d):
    c = [x["c"] for x in d]
    m = sma(c, 50)
    return [m[i] is not None and c[i] > m[i] for i in range(len(c))]


def state_break(d, n):
    c = [x["c"] for x in d]
    st, on = [], False
    for i in range(len(c)):
        if i >= n and c[i] > max(c[i - n:i]):
            on = True
        elif i >= n // 2 and c[i] < min(c[i - n // 2:i]):
            on = False
        st.append(on)
    return st


def equity(d, states_w):
    """Rentabilidad de apertura a apertura con comisiones. states_w: lista de (estado, peso)."""
    eq, peak, dd, bh = 1.0, 1.0, 0.0, None
    start = 100
    for i in range(start, len(d) - 1):
        r = d[i + 1]["o"] / d[i]["o"] - 1
        expo, chg = 0.0, 0.0
        for st, w in states_w:
            a = st[i - 1] if i > 0 else False
            b = st[i - 2] if i > 1 else False
            expo += w * a
            chg += w * (a != b)
        eq *= 1 + expo * r - chg * FEE / 2
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    bh_mult = d[-1]["o"] / d[start]["o"]
    return eq, dd, bh_mult


# ------------------------------------------------------------------ 3: zona verde de 4 h

def zones_4h(b, trend_by_day):
    """Simula la regla sobre las velas de 4 h. Devuelve operaciones, órdenes pendientes y zonas a vigilar."""
    h = [x["h"] for x in b]; l = [x["l"] for x in b]; c = [x["c"] for x in b]
    n = len(b)
    trades, pending, watch = [], [], []
    for i in range(3, n):
        if not (h[i - 3] < l[i - 1]):
            continue
        top, bot = l[i - 1], h[i - 3]
        w = (top - bot) / bot
        if w < ZONE_MIN:
            continue
        mid = (top + bot) / 2
        tgt = mid + ZONE_TARGET * (top - bot)
        z = {"zone_ts": iso(b[i - 1]["t"]), "top": top, "bot": bot, "mid": mid, "target": tgt,
             "width": round(w * 100, 2)}
        j = i + 1
        while j < n and l[j] > top:
            j += 1
        if j >= n:                      # aún sin tocar
            if n - i <= ZONE_TOUCH_MAX:
                watch.append(z)
            continue
        if j - i > ZONE_TOUCH_MAX:
            continue
        day = dt.datetime.fromtimestamp(b[j]["t"] / 1000, dt.timezone.utc).date().isoformat()
        if not trend_by_day.get(day):   # al tocar la zona la moneda no estaba por encima de su media 50
            continue
        k0 = None
        for k in range(j, min(n, j + ZONE_FILL_MAX)):
            if l[k] <= mid:
                k0 = k
                break
        if k0 is None:
            if n - j < ZONE_FILL_MAX:
                z["touch_ts"] = iso(b[j]["t"])
                z["expires_ts"] = iso(b[j]["t"] + ZONE_FILL_MAX * 4 * 3600000)
                pending.append(z)
            continue
        t = dict(z, kind="zona4h", signal_ts=iso(b[j]["t"]), entry_ts=iso(b[k0]["t"]), entry=mid,
                 stop=bot, weight=1.0)
        for k in range(k0, min(n, k0 + ZONE_HOLD)):
            if c[k] < bot:
                t.update(exit_ts=iso(b[k]["t"] + 4 * 3600000), exit=c[k], reason="stop (cierre bajo la zona)")
                break
            if k > k0 and h[k] >= tgt:
                t.update(exit_ts=iso(b[k]["t"]), exit=tgt, reason="objetivo")
                break
        else:
            if k0 + ZONE_HOLD <= n:
                k = k0 + ZONE_HOLD - 1
                t.update(exit_ts=iso(b[k]["t"] + 4 * 3600000), exit=c[k], reason="100 velas")
            else:
                t["open"] = True
        trades.append(t)
    return trades, pending, watch[-6:]


def result(t, last):
    px = t.get("exit") if not t.get("open") else last
    if not px or not t.get("entry"):
        return None
    return (px / t["entry"] - 1) - (FEE if not t.get("open") else FEE / 2)


# ------------------------------------------------------------------ principal

def main():
    prev = ec.load(OUT, {})
    go_live = prev.get("go_live") or ec.now_utc()
    uni = ec.universe()
    syms = [u for u in uni][:COINS * 2]
    if not any(u["base"] == "BTC" for u in syms):
        syms.insert(0, {"symbol": "BTCUSDT", "base": "BTC", "price": None})
    coins, all_trades = [], []
    for u in syms:
        if len(coins) >= COINS:
            break
        try:
            d = kl(u["symbol"], "1d")
        except Exception as exc:
            log("%s: %s" % (u["symbol"], exc))
            continue
        if len(d) < 200 or iso(d[-1]["t"])[:10] != (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat():
            continue
        try:
            b4 = kl(u["symbol"], "4h")
        except Exception:
            b4 = []
        last = u.get("price") or d[-1]["c"]
        s50 = state_media50(d)
        sb = {n: state_break(d, n) for n in BREAKS}
        tr = trades_from_state(d, s50, "media50")
        for n in BREAKS:
            tr += trades_from_state(d, sb[n], "rupturas", 1 / 3, {"tramo": n})
        # la tendencia de cada día es la del cierre anterior
        trend_by_day = {}
        for i in range(len(d) - 1):
            trend_by_day[iso(d[i + 1]["t"])[:10]] = s50[i]
        trend_by_day[(dt.datetime.fromtimestamp(d[-1]["t"] / 1000, dt.timezone.utc) + dt.timedelta(days=1)).date().isoformat()] = s50[-1]
        z_tr, pend, watch = zones_4h(b4, trend_by_day) if len(b4) > 50 else ([], [], [])
        tr += z_tr
        for t in tr:
            if t.get("entry") is None:
                t["entry"] = last            # señal de hoy: precio al detectarla
                t["entry_now"] = True
            if t.get("exit") is None and not t.get("open"):
                t["exit"] = last
            t["symbol"] = u["base"]
            t["pair"] = u["symbol"]
            t["id"] = "%s|%s|%s|%s" % (t["kind"], u["base"], t.get("tramo") or t.get("zone_ts") or "", t["entry_ts"])
            r = result(t, last)
            t["ret"] = round(r * 100, 2) if r is not None else None
            t["eur"] = round(r * STAKE * t["weight"], 2) if r is not None else None
            if t["kind"] == "zona4h" and r is not None:
                risk = (t["entry"] - t["stop"]) / t["entry"]
                t["R"] = round(r / risk, 2)
        eq50, dd50, bh = equity(d, [(s50, 1.0)])
        eqb, ddb, _ = equity(d, [(sb[n], 1 / 3) for n in BREAKS])
        z_closed = [t for t in z_tr if not t.get("open")]
        zr = [t["R"] for t in z_closed if t.get("R") is not None]
        open_tr = [t for t in tr if t.get("open")]
        coin = {
            "symbol": u["base"], "pair": u["symbol"], "price": last, "close": d[-1]["c"],
            "close_date": iso(d[-1]["t"])[:10], "sma50": sma([x["c"] for x in d], 50)[-1],
            "media50": {"on": s50[-1], "since": next((t["entry_ts"] for t in open_tr if t["kind"] == "media50"), None)},
            "rupturas": {"tramos": [{"n": n, "on": sb[n][-1], "level_in": max(x["c"] for x in d[-n:]),
                                      "level_out": min(x["c"] for x in d[-(n // 2):])} for n in BREAKS]},
            "zona4h": {"pending": pend, "watch": watch if s50[-1] else [],
                       "open": [t for t in open_tr if t["kind"] == "zona4h"]},
            "bt": {"from": iso(d[100]["t"])[:10], "bh": round(bh, 2),
                   "media50": {"mult": round(eq50, 2), "dd": round(dd50 * 100, 1)},
                   "rupturas": {"mult": round(eqb, 2), "dd": round(ddb * 100, 1)},
                   "zona4h": {"n": len(zr), "win": round(100 * sum(r > 0 for r in zr) / len(zr)) if zr else None,
                              "avgR": round(sum(zr) / len(zr), 2) if zr else None,
                              "from": iso(b4[0]["t"])[:10] if b4 else None}},
        }
        coins.append(coin)
        all_trades += tr
    reg = [t for t in all_trades if t["entry_ts"] >= go_live]
    reg.sort(key=lambda t: t["entry_ts"], reverse=True)
    old = {t["id"]: t for t in prev.get("log", [])}
    for t in reg:   # la entrada al precio de detección se conserva
        o = old.get(t["id"])
        if o and o.get("entry_now"):
            t["entry"] = o["entry"]
            t["entry_now"] = True
            r = result(t, next(c["price"] for c in coins if c["symbol"] == t["symbol"]))
            if r is not None:
                t["ret"] = round(r * 100, 2)
                t["eur"] = round(r * STAKE * t["weight"], 2)
    news = [t for t in reg if t["id"] not in old]
    closed_now = [t for t in reg if not t.get("open") and old.get(t["id"], {}).get("open")]
    out = {"updated": ec.now_utc(), "go_live": go_live, "stake": STAKE, "fee": FEE, "names": NAMES,
           "params": {"coins": COINS, "zone_min": ZONE_MIN, "zone_target": ZONE_TARGET, "zone_hold": ZONE_HOLD},
           "coins": coins, "log": reg,
           "history": sorted([t for t in all_trades if not t.get("open") and t["entry_ts"] < go_live and t["kind"] == "zona4h"],
                             key=lambda t: t["entry_ts"], reverse=True)[:60]}
    ec.save(OUT, out)
    log("monedas %d, operaciones en el registro %d (nuevas %d, cerradas ahora %d)" % (len(coins), len(reg), len(news), len(closed_now)))
    if prev.get("go_live"):
        notify(news, closed_now, coins)


def notify(news, closed, coins):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or not (news or closed):
        return
    f = ec.fmt
    lines = []
    for t in news:
        if t.get("open"):
            extra = "  stop cierre 4h < %s  objetivo %s" % (f(t["stop"]), f(t["target"])) if t["kind"] == "zona4h" else ""
            lines.append("COMPRA %s · %s%s a %s%s" % (t["symbol"], NAMES[t["kind"]], " (%d)" % t["tramo"] if t.get("tramo") else "", f(t["entry"]), extra))
    for t in closed:
        lines.append("VENTA %s · %s%s: %+.1f %% (%s)" % (t["symbol"], NAMES[t["kind"]], " (%d)" % t["tramo"] if t.get("tramo") else "", t["ret"] or 0, t.get("reason", "")))
    btc = next((c for c in coins if c["symbol"] == "BTC"), None)
    if btc:
        lines.append("BTC: %s de la media 50 · %d/3 tramos de rupturas" % ("por encima" if btc["media50"]["on"] else "por debajo",
                                                                          sum(t["on"] for t in btc["rupturas"]["tramos"])))
    body = json.dumps({"topic": topic, "title": "Estrategias: %d compras, %d ventas" % (len([t for t in news if t.get("open")]), len(closed)),
                       "message": "\n".join(lines)[:3800], "tags": ["chart_with_upwards_trend"],
                       "click": "https://mchamosa.github.io/atalaya/estrategias.html"}).encode("utf-8")
    try:
        req = urllib.request.Request("https://ntfy.sh/", data=body, method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        log("aviso enviado")
    except Exception as exc:
        log("ntfy: %s" % exc)


if __name__ == "__main__":
    main()
