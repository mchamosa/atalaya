#!/usr/bin/env python3
"""Escáner de Atalaya.

Junta en data/escaner.json lo que sacan tres herramientas del paquete
day-trader-skill (Jake Schincariol, licencia MIT, en scripts/daytrader/):

  - scan.py : escáner de gráficos de unas 5.900 acciones de EE. UU.
  - news.py : titulares de Yahoo Finance y 8-K de la SEC de las acciones detectadas
  - ptr.py  : operaciones de bolsa declaradas por los congresistas (House Clerk)

Uso:
    python scripts/escaner.py premarket   # escáner diario + noticias + Congreso
    python scripts/escaner.py orb         # ruptura del rango de apertura + noticias

Cada pasada solo cambia su parte del fichero; el resto se queda como estaba.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "scripts", "daytrader")
OUT = os.path.join(ROOT, "data", "escaner.json")
PTR_CACHE = os.path.join(ROOT, "data", "escaner_congreso.json")

NEWS_ITEMS = 150         # titulares que se guardan como mucho
PTR_DAYS = 45            # declaraciones del Congreso de los últimos N días
PTR_FETCH_MAX = 40       # PDF nuevos que se leen como mucho en cada pasada
GRADE_DAILY = 40         # acciones de pre-apertura con histórico calculado
GRADE_ORB = 40           # rupturas de apertura con máquina del tiempo
HOLD_DAYS = 5            # sesiones que se mantiene una señal diaria en el histórico
PORTFOLIO = os.path.join(ROOT, "data", "cartera.json")
STAKE = 1000.0           # euros simulados por cada verde
HORIZONS = (1, 3, 5, 10, 20)   # sesiones a las que se mira el resultado
RULES = {"win": 55.0, "n": 5}  # reglas por defecto del semáforo (las mismas que la página)

WORK = tempfile.mkdtemp(prefix="escaner-")


def now_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print("[escaner] " + msg, flush=True)


def run_tool(script, args, timeout=900):
    """Lanza una herramienta y devuelve su JSON, o None si no dio nada."""
    cmd = [sys.executable, "-I", os.path.join(TOOLS, script)] + args
    try:
        p = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("%s: tiempo agotado" % script)
        return None, "tiempo agotado"
    err = (p.stderr or "").strip().splitlines()
    out = (p.stdout or "").strip()
    if out.startswith("{") or out.startswith("["):
        try:
            return json.loads(out), None
        except ValueError:
            pass
    # Sin JSON: la herramienta explica el motivo en una línea
    reason = (out.splitlines() or err or ["sin datos"])[-1]
    log("%s %s: %s" % (script, " ".join(args[:1]), reason))
    return None, reason


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


# ------------------------------------------------------------------ escáner

def build_universe():
    data, reason = run_tool("scan.py", ["universe", "--json"], timeout=300)
    uni = os.path.join(WORK, "trader", "universe.txt")
    if not os.path.isfile(uni):
        raise SystemExit("No se pudo descargar la lista de acciones: %s" % reason)
    with open(uni, encoding="utf-8") as fh:
        n = sum(1 for _ in fh)
    log("universo: %d acciones" % n)
    return n


def scan(mode):
    args = [mode, "--json", "--quiet", "--show", "0"]
    data, reason = run_tool("scan.py", args, timeout=1800)
    if data is None:
        return None, reason
    data.pop("note", None)
    data.pop("offline", None)
    data["run_at"] = now_utc()
    return data, None


# ------------------------------------------------------------------ noticias

def news(tickers):
    if not tickers:
        return None
    args = ["--tickers"] + tickers + ["--all", "--hours", "36", "--dry-run", "--json"]
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if ua:
        args += ["--user-agent", ua]
    else:
        args += ["--no-sec"]
    data, _ = run_tool("news.py", args, timeout=600)
    if not data:
        return None
    items = sorted(data.get("items", []), key=lambda i: i.get("time") or "", reverse=True)
    keep = ("source", "kind", "ticker", "title", "link", "time", "tags", "form", "important")
    return {
        "run_at": now_utc(),
        "tickers": tickers,
        "sec": bool(ua),
        "moves": data.get("moves", []),
        "items": [{k: i.get(k) for k in keep if i.get(k) is not None} for i in items[:NEWS_ITEMS]],
    }


# ------------------------------------------------------------------ Congreso

def congress():
    since = (dt.date.today() - dt.timedelta(days=PTR_DAYS)).isoformat()
    filings, reason = run_tool("ptr.py", ["index", "--since", since, "--json"], timeout=300)
    if filings is None:
        return None
    cache = load(PTR_CACHE, {})
    fetched = 0
    out = []
    for f in sorted(filings, key=lambda r: r.get("filed") or "", reverse=True):
        doc = str(f.get("doc_id"))
        if doc not in cache and fetched < PTR_FETCH_MAX:
            fetched += 1
            rep, why = run_tool("ptr.py", ["fetch", doc, "--year", str(f.get("year")),
                                           "--filed", f.get("filed") or "", "--json",
                                           "--pdf-dir", os.path.join(WORK, "pdf")], timeout=120)
            if rep and isinstance(rep, dict):
                cache[doc] = [
                    {k: r.get(k) for k in ("owner_name", "type_name", "date", "amount", "ticker",
                                            "asset", "asset_type", "description",
                                            "days_to_disclose", "late")}
                    for r in rep.get("rows", [])
                ]
            else:
                # PDF escaneado o sin filas legibles: se apunta para no repetir
                cache[doc] = []
        out.append({
            "doc_id": doc,
            "name": f.get("name"),
            "district": f.get("district"),
            "filed": f.get("filed"),
            "url": f.get("url"),
            "rows": cache.get(doc),   # None = pendiente de leer en la próxima pasada
        })
    # El caché solo guarda lo que sigue en la ventana
    keep_ids = {o["doc_id"] for o in out}
    save(PTR_CACHE, {k: v for k, v in cache.items() if k in keep_ids})
    log("Congreso: %d declaraciones, %d PDF leídos ahora" % (len(out), fetched))
    return {"run_at": now_utc(), "since": since, "filings": out}


# ------------------------------------------------------------------ valoración

_scan_mod = None


def scanmod():
    """El propio scan.py importado como módulo, para reutilizar sus funciones."""
    global _scan_mod
    if _scan_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dts_scan", os.path.join(TOOLS, "scan.py"))
        _scan_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_scan_mod)
    return _scan_mod


def stats_from(results):
    """results: lista de resultados (R o % de rentabilidad) -> estadística."""
    n = len(results)
    if not n:
        return {"n": 0}
    wins = [r for r in results if r > 0]
    losses = [-r for r in results if r <= 0]
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    return {
        "n": n,
        "win_pct": round(100.0 * len(wins) / n, 1),
        "avg": round(sum(results) / n, 3),
        "avg_win": round(avg_w, 3),
        "avg_loss": round(avg_l, 3),
        "payoff": round(avg_w / avg_l, 2) if avg_l else None,
    }


def grade_daily(hits):
    """Para cada acción de pre-apertura: cuántas veces sus mismos patrones dieron
    ganancia a HOLD_DAYS sesiones en los últimos 2 años, y niveles propuestos."""
    sm = scanmod()
    now = dt.datetime.now(dt.timezone.utc)
    pooled = {}
    done = 0
    for h in hits[:GRADE_DAILY]:
        sym = h["symbol"]
        try:
            series = sm.parse_chart(sm.yahoo_get(sm.chart_path(sym, "2y", "1d")), sym)
        except Exception as exc:          # sin datos: se deja sin valorar
            h["grade"] = {"error": str(exc)[:80]}
            continue
        time.sleep(0.3)
        closes = [c for _, c in sm.daily_closes(series, now)]
        if len(closes) < 60:
            h["grade"] = {"error": "historial corto"}
            continue
        want = set(h["setups"])
        results = []
        for i in range(30, len(closes) - HOLD_DAYS):
            tags, _ = sm.find_setups(closes[: i + 1])
            if want.intersection(tags):
                r = (closes[i + HOLD_DAYS] / closes[i] - 1) * 100
                results.append(r)
                for t in want.intersection(tags):
                    pooled.setdefault(t, []).append(r)
        entry = closes[-1]
        stop = min(closes[-10:])
        if entry - stop < entry * 0.01:      # soporte demasiado pegado: 2 % por debajo
            stop = entry * 0.98
        stop = round(stop * 0.995, 2)
        target = round(entry + 2 * (entry - stop), 2)
        h["grade"] = {"hist": stats_from(results), "unit": "%", "hold_days": HOLD_DAYS,
                      "entry": round(entry, 2), "stop": stop, "target": target, "rr": 2.0}
        done += 1
    log("histórico diario: %d acciones" % done)
    return {k: stats_from(v) for k, v in pooled.items()}


def grade_orb(orb):
    """Para cada ruptura: plan de 2R con stop en la mitad del rango y la máquina del
    tiempo del paquete (5.000 simulaciones y el histórico de 60 días)."""
    day = (orb.get("as_of") or "").split(" ")[0]
    pooled = []
    done = 0
    for h in orb.get("hits", [])[:GRADE_ORB]:
        entry = h["price"]
        stop = int((h["or_high"] + h["or_low"]) / 2 * 100) / 100.0
        if stop >= entry:
            continue
        target = round(entry + 2 * (entry - stop), 2)
        tm, why = run_tool("timemachine.py", ["run", "--symbol", h["symbol"], "--date", day,
                                              "--entry", str(entry), "--stop", str(stop),
                                              "--target", str(target), "--entry-time", h["at"],
                                              "--until", "15:55", "--replays", "5000", "--json"],
                           timeout=180)
        if not tm or "mc" not in tm:
            h["grade"] = {"error": (why or "sin datos")[:80], "entry": entry, "stop": stop,
                          "target": target, "rr": 2.0}
            continue
        mc, hist = tm["mc"], tm.get("history") or {}
        rs = [t.get("R") for t in hist.get("trades", []) if t.get("R") is not None]
        pooled += rs
        side = (mc.get("p_target") or 0) + (mc.get("p_stop") or 0)
        h["grade"] = {
            "entry": entry, "stop": stop, "target": target, "rr": 2.0, "unit": "R",
            "hist": stats_from(rs),
            "mc": {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"),
                   "p_neither": mc.get("p_neither"), "p_green": mc.get("p_green"),
                   "avg_R": mc.get("avg_R"), "target_share": round(mc["p_target"] / side, 3) if side else None},
            "verdict": tm.get("verdict"),
        }
        done += 1
    log("máquina del tiempo: %d rupturas" % done)
    return {"orb": stats_from(pooled)}


def earnings_flags(news_block):
    """Tickers con noticias de resultados en el bloque de noticias."""
    out = set()
    for i in (news_block or {}).get("items", []):
        if "earnings" in (i.get("tags") or []):
            out.add(i.get("ticker"))
    return sorted(t for t in out if t)


# ------------------------------------------------------------------ cartera simulada

def sem_color(h, kind, earn):
    """Semáforo con las reglas por defecto: 2 verde, 1 ámbar, 0 rojo, -1 sin valorar."""
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
    r3 = h["symbol"] not in earn
    ok = sum([r1, r2, r3])
    return 2 if ok == 3 else 1 if ok == 2 else 0


def is_green(h, kind, earn):
    return sem_color(h, kind, earn) == 2


def _orb_outcome(sm, p):
    """Resultado real del plan de apertura: stop, objetivo o cierre a las 15:55."""
    day = dt.date.fromisoformat(p["date"])
    series = sm.parse_chart(sm.yahoo_get(sm.chart_path(p["symbol"], "5d", "5m")), p["symbol"])
    cutoff = dt.datetime.combine(day, dt.time(16, 0))
    bars = sm.session_bars(series, day, cutoff)
    if not bars or bars[-1]["start"].time() < dt.time(15, 50):
        return None                       # la sesión aún no ha terminado o faltan datos
    entry, stop, target = p["entry"], p["stop"], p["target"]
    risk = entry - stop
    start = dt.datetime.combine(day, dt.time(*map(int, p["entry_bar"].split(":")))) + dt.timedelta(minutes=5)
    last = None
    for b in bars:
        if b["start"] < start:
            continue
        if b["start"].time() >= dt.time(15, 55):
            break
        last = b
        if b["l"] is not None and b["l"] <= stop:
            return {"outcome": "stop", "R": -1.0, "ret": round((stop / entry - 1) * 100, 2), "at": b["start"].strftime("%H:%M")}
        if b["h"] is not None and b["h"] >= target:
            return {"outcome": "objetivo", "R": round((target - entry) / risk, 2), "ret": round((target / entry - 1) * 100, 2), "at": b["start"].strftime("%H:%M")}
    if last is None:
        return None
    c = last["c"]
    return {"outcome": "cierre", "R": round((c - entry) / risk, 2), "ret": round((c / entry - 1) * 100, 2), "at": "15:55"}


def _daily_outcome(p, after):
    """Resultado del plan diario con cierres: stop, objetivo o cierre a las 10 sesiones."""
    entry, stop, target = p["entry"], p["stop"], p["target"]
    if not (entry and stop and target) or entry <= stop:
        return None
    risk = entry - stop
    for k, (d, c) in enumerate(after[:10], 1):
        if c <= stop:
            return {"outcome": "stop", "R": round((c - entry) / risk, 2), "ret": round((c / entry - 1) * 100, 2), "sessions": k}
        if c >= target:
            return {"outcome": "objetivo", "R": round((c - entry) / risk, 2), "ret": round((c / entry - 1) * 100, 2), "sessions": k}
    if len(after) >= 10:
        c = after[9][1]
        return {"outcome": "cierre", "R": round((c - entry) / risk, 2), "ret": round((c / entry - 1) * 100, 2), "sessions": 10}
    return None


def _daily_outcome_ohlc(p, bars):
    """Plan diario con máximos y mínimos, como una orden de stop y otra de objetivo reales.
    bars: [(fecha, o, h, l, c)] desde el día de la compra (se compra a la apertura del primero).
    Si en el mismo día toca stop y objetivo, se cuenta el stop (lo prudente). Si abre por
    debajo del stop o por encima del objetivo, se sale al precio de apertura."""
    entry, stop, target = p["entry"], p["stop"], p["target"]
    if not (entry and stop and target) or entry <= stop:
        return None
    risk = entry - stop

    def res(outcome, px, k):
        return {"outcome": outcome, "R": round((px - entry) / risk, 2), "ret": round((px / entry - 1) * 100, 2),
                "sessions": k, "ohlc": True}
    for k, (d, o, h, l, c) in enumerate(bars[:10], 1):
        if k > 1 and o <= stop:
            return res("stop", o, k)
        if l <= stop:
            return res("stop", stop, k)
        if k > 1 and o >= target:
            return res("objetivo", o, k)
        if h >= target:
            return res("objetivo", target, k)
    if len(bars) >= 10:
        return res("cierre", bars[9][4], 10)
    return None


def daily_ohlc(sm, sym, now):
    """Velas diarias completas (fecha, o, h, l, c) de Yahoo; la de hoy solo si la sesión ha cerrado."""
    series = sm.parse_chart(sm.yahoo_get(sm.chart_path(sym, "3mo", "1d")), sym)
    ny = sm.to_eastern(now)
    out = []
    for i, ts in enumerate(series["t"]):
        vals = [series.get(k) and series[k][i] for k in ("o", "h", "l", "c")]
        if any(v is None for v in vals):
            continue
        d = sm.ts_to_eastern(ts).date()
        if d > ny.date() or (d == ny.date() and (ny.hour, ny.minute) < (16, 5)):
            continue
        out.append((d.isoformat(),) + tuple(float(v) for v in vals))
    return out


def orb_detection_prices(block):
    """Precio de cada ruptura en el momento en que la detecta el escáner: cierre de la
    última vela de 5 minutos completa a la hora del escaneo. {SYM: (precio, "HH:MM")}."""
    sm = scanmod()
    as_of = (block.get("as_of") or "").replace(" ET", "")
    try:
        cutoff = dt.datetime.strptime(as_of, "%Y-%m-%d %H:%M")
    except ValueError:
        return {}
    syms = [h["symbol"] for h in block.get("hits", []) if (h.get("grade") or {}).get("entry")]
    data, _ = sm.fetch_spark(syms, "1d", "5m", 0.3) if syms else ({}, {})
    out = {}
    for sym, series in data.items():
        bars = sm.session_bars(series, cutoff.date(), cutoff)
        if bars:
            b = bars[-1]
            out[sym] = (round(b["c"], 2), b["start"].strftime("%H:%M"))
    return out


def _fix_old_orb(sm, p):
    series = sm.parse_chart(sm.yahoo_get(sm.chart_path(p["symbol"], "5d", "5m")), p["symbol"])
    reg = dt.datetime.strptime(p["opened_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    cutoff = sm.to_eastern(reg).replace(tzinfo=None)
    bars = sm.session_bars(series, dt.date.fromisoformat(p["date"]), cutoff)
    if not bars:
        return
    b = bars[-1]
    p["breakout_price"] = p["entry"]
    entry = round(b["c"], 2)
    p["entry"], p["entry_bar"] = entry, b["start"].strftime("%H:%M")
    if p.get("stop") and entry > p["stop"]:
        p["target"] = round(entry + 2 * (entry - p["stop"]), 2)
    else:
        p["status"] = "anulada"     # cuando se detectó ya estaba por debajo del stop
        p["plan"] = {"outcome": "anulada", "R": 0.0, "ret": 0.0}


def portfolio_update(data, mode):
    """Registro de TODAS las señales valoradas de cada día (1.000 € simulados cada una),
    con su color de semáforo de ese momento, y seguimiento a días vista."""
    pf = load(PORTFOLIO, {"positions": []})
    pos = pf["positions"]
    have = {(p["symbol"], p["kind"], p["date"]) for p in pos}
    added, greens = [], []
    block = data.get("daily") if mode == "premarket" else data.get("orb")
    kind = "daily" if mode == "premarket" else "orb"
    if block and block.get("run_at", "")[:10] == now_utc()[:10]:
        earn = set(block.get("earnings") or [])
        if kind == "daily":
            day = dt.date.today().isoformat()   # se compra a la apertura de la sesión de hoy
        else:
            day = (block.get("as_of") or "").split(" ")[0]
        now_px = {}
        if kind == "orb":
            now_px = orb_detection_prices(block)
        for h in block.get("hits", []):
            g = h.get("grade") or {}
            if not g.get("entry") or g.get("error"):
                continue
            key = (h["symbol"], kind, day)
            if key in have:
                continue
            sem = sem_color(h, kind, earn)
            hs = g.get("hist") or {}
            entry, target, entry_bar = g["entry"], g.get("target"), h.get("bar_start")
            if kind == "orb":
                # Solo se puede comprar cuando el escáner la detecta: precio de ese momento
                px = now_px.get(h["symbol"])
                if not px:
                    continue
                entry, entry_bar = px
                if not g.get("stop") or entry <= g["stop"]:
                    continue                      # ya volvió por debajo del stop: no hay operación
                target = round(entry + 2 * (entry - g["stop"]), 2)
            p = {"symbol": h["symbol"], "kind": kind, "date": day, "sem": sem,
                 "entry": entry, "stop": g.get("stop"), "target": target,
                 "breakout_price": g["entry"] if kind == "orb" else None,
                 "setups": h.get("setups"), "win": hs.get("win_pct"), "n": hs.get("n"), "avg": hs.get("avg"),
                 "ts": (g.get("mc") or {}).get("target_share"),
                 "entry_bar": entry_bar, "earn": h["symbol"] in earn,
                 "rsi": h.get("rsi"), "at": h.get("at"), "pct_over": h.get("pct_over"),
                 "or_high": h.get("or_high"), "or_low": h.get("or_low"),
                 "signal_date": h.get("close_date") or day, "signal_price": g["entry"],
                 "entry_type": "apertura pendiente" if kind == "daily" else "ruptura",
                 "opened_at": now_utc(), "closes": {}, "last": entry, "status": "abierta"}
            pos.append(p)
            have.add(key)
            added.append(h["symbol"])
            if sem == 2:
                greens.append(h["symbol"])
    sm = scanmod()
    now = dt.datetime.now(dt.timezone.utc)
    live = [p for p in pos if p.get("status") != "terminada"]
    # Las señales de pre-apertura se compran a la apertura del día, no al cierre de la señal
    for p in live:
        if p["kind"] != "daily" or p.get("entry_type") == "apertura":
            continue
        try:
            series = sm.parse_chart(sm.yahoo_get(sm.chart_path(p["symbol"], "1mo", "1d")), p["symbol"])
            time.sleep(0.3)
        except Exception:
            continue
        opens = series.get("o") or []
        for i, ts in enumerate(series["t"]):
            if sm.ts_to_eastern(ts).date().isoformat() >= p["date"] and i < len(opens) and opens[i]:
                p["date"] = sm.ts_to_eastern(ts).date().isoformat()   # si era festivo, la siguiente sesión
                if not p.get("signal_price"):
                    p["signal_price"] = p["entry"]
                op = round(float(opens[i]), 2)
                p["entry"] = op
                if p.get("stop") and op > p["stop"]:
                    p["target"] = round(op + 2 * (op - p["stop"]), 2)
                elif p.get("stop"):
                    p["gap_below_stop"] = True    # abrió por debajo del stop: se habría anulado
                p["entry_type"] = "apertura"
                p["shares"] = round(STAKE / op, 4)
                break
    # Seguimiento: cierres diarios por lotes de 20 acciones
    syms = sorted({p["symbol"] for p in live})
    series_by, _ = sm.fetch_spark(syms, "3mo", "1d", 0.3) if syms else ({}, {})
    for p in live:
        series = series_by.get(p["symbol"])
        if not series:
            continue
        bars = [(d.isoformat(), c) for d, c in sm.daily_closes(series, now)]
        after = [(d, c) for d, c in bars if d >= p["date"]]
        last_price = (series.get("meta") or {}).get("regularMarketPrice") or (bars[-1][1] if bars else None)
        if last_price:
            p["last"] = round(float(last_price), 4)
        for k in HORIZONS:
            if len(after) >= k:
                d, c = after[k - 1]
                p["closes"][str(k)] = {"date": d, "ret": round((c / p["entry"] - 1) * 100, 2)}
        p["sessions"] = len(after)
        p["ret_now"] = round((p["last"] / p["entry"] - 1) * 100, 2)
        if p["kind"] == "daily" and not (p.get("plan") or {}).get("ohlc") and not p.get("gap_below_stop"):
            # plan con máximos y mínimos (los registros antiguos usaban solo cierres: se recalculan)
            try:
                ob = [b for b in daily_ohlc(sm, p["symbol"], now) if b[0] >= p["date"]]
                time.sleep(0.3)
                o = _daily_outcome_ohlc(p, ob)
            except Exception:
                o = None
            if o:
                p["plan"] = o
            elif p.get("plan") and not p["plan"].get("ohlc"):
                p.pop("plan")
        if len(after) >= max(HORIZONS) and p.get("plan"):
            p["status"] = "terminada"
    # Resultado del plan de apertura (stop, objetivo o 15:55), una vez cerrada la sesión
    for p in live:
        if p["kind"] != "orb" or p.get("plan") or not p.get("entry_bar"):
            continue
        if "breakout_price" not in p:
            # Registros antiguos: se compraban al precio de la vela de ruptura; se pasa al
            # precio que había cuando se registraron (lo que se podía comprar de verdad)
            try:
                _fix_old_orb(sm, p)
                time.sleep(0.3)
            except Exception:
                pass
            if p.get("status") == "anulada":
                continue
        try:
            o = _orb_outcome(sm, p)
            time.sleep(0.3)
        except Exception:
            o = None
        if o:
            p["plan"] = o
    pf["updated"] = now_utc()
    pf["stake"], pf["horizons"], pf["rules"] = STAKE, list(HORIZONS), RULES
    save(PORTFOLIO, pf)
    log("registro: %d señales, %d nuevas hoy (%d verdes)" % (len(pos), len(added), len(greens)))
    return greens


def notify(data, mode, added):
    """Aviso al móvil con ntfy si hay tema configurado (secreto NTFY_TOPIC)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    block = data.get("daily") if mode == "premarket" else data.get("orb")
    if not block:
        return
    earn = set(block.get("earnings") or [])
    kind = "daily" if mode == "premarket" else "orb"
    greens = [h for h in block.get("hits", []) if is_green(h, kind, earn)]
    lines = []
    for h in greens[:15]:
        g = h["grade"]
        lines.append("%s  %.1f %% aciertos (%d)  entrada %.2f  stop %.2f  objetivo %.2f" % (
            h["symbol"], g["hist"]["win_pct"], g["hist"]["n"], g["entry"], g["stop"], g["target"]))
    scan_syms = {h["symbol"] for h in block.get("hits", [])}
    cong = set()
    for f in (data.get("congress") or {}).get("filings", []):
        for r in f.get("rows") or []:
            if r.get("ticker") in scan_syms:
                cong.add(r["ticker"])
    if cong:
        lines.append("Congreso también en el escáner: " + ", ".join(sorted(cong)[:12]))
    title = ("Pre-apertura" if mode == "premarket" else "Ruptura de apertura") + \
        ": %d en verde" % len(greens)
    if added:
        title += " (%d a la cartera simulada)" % len(added)
    msg = "\n".join(lines) or "Ninguna acción cumple las tres reglas hoy."
    body = json.dumps({"topic": topic, "title": title, "message": msg,
                       "tags": ["chart_with_upwards_trend"],
                       "click": os.environ.get("ATALAYA_URL", "https://mchamosa.github.io/atalaya/escaner.html")}).encode("utf-8")
    try:
        import urllib.request
        req = urllib.request.Request("https://ntfy.sh/", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        log("aviso enviado a ntfy")
    except Exception as exc:
        log("ntfy: %s" % exc)


def fundamentals(data, tickers):
    """Datos de la SEC (trader-analyst) de las acciones valoradas."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        data["fundamentals_note"] = ("Para ver ingresos, beneficios y márgenes de la SEC hay que añadir el "
                                     "secreto SEC_USER_AGENT (nombre y correo) en GitHub.")
        return
    tickers = [t for t in dict.fromkeys(tickers) if t][:80]
    if not tickers:
        return
    out = os.path.join(WORK, "metrics.json")
    run_tool("facts.py", tickers + ["--metrics", out, "--no-filings", "--quarters", "8",
                                    "--user-agent", ua], timeout=900)
    m = load(out, {})
    fund = data.get("fundamentals") or {}
    for c in m.get("companies", []):
        fund[c["ticker"]] = {k: c.get(k) for k in ("name", "period_end", "revenue_ttm", "net_income_ttm",
                                                    "eps_ttm", "revenue_growth_yoy", "eps_growth_yoy",
                                                    "gross_margin", "operating_margin", "net_margin")}
    data["fundamentals"] = fund
    data.pop("fundamentals_note", None)
    log("fundamentales: %d empresas" % len(m.get("companies", [])))


# ------------------------------------------------------------------ principal

def main():
    mode = (sys.argv[1:] or ["premarket"])[0]
    if mode not in ("premarket", "orb"):
        raise SystemExit("modo: premarket u orb")

    data = load(OUT, {})
    data.setdefault("source", "day-trader-skill (Jake Schincariol, MIT) con datos gratuitos de "
                              "Yahoo Finance, SEC EDGAR y House Clerk")
    build_universe()

    if mode == "premarket":
        res, reason = scan("daily")
        if res:
            data["daily"] = res
            data.get("status", {}).pop("daily", None)
            log("pre-apertura: %d coincidencias" % len(res.get("hits", [])))
        else:
            data.setdefault("status", {})["daily"] = reason
        hits = (data.get("daily") or {}).get("hits", [])
        if res:
            res["market"] = grade_daily(hits)
        n = news([h["symbol"] for h in hits[:GRADE_DAILY]])
        if n:
            data["news"] = n
            if data.get("daily"):
                data["daily"]["earnings"] = earnings_flags(n)
        c = congress()
        if c:
            data["congress"] = c
    else:
        res, reason = scan("orb")
        if res:
            data["orb"] = res
            data.get("status", {}).pop("orb", None)
            log("apertura: %d rupturas" % len(res.get("hits", [])))
            res["market"] = grade_orb(res)
            n = news([h["symbol"] for h in res.get("hits", [])[:GRADE_ORB]])
            if n:
                data["news"] = n
                res["earnings"] = earnings_flags(n)
        else:
            data.setdefault("status", {})["orb"] = reason

    blk = data.get("daily") if mode == "premarket" else data.get("orb")
    if blk and blk.get("run_at", "")[:10] == now_utc()[:10]:
        fundamentals(data, [h["symbol"] for h in blk.get("hits", [])[:GRADE_DAILY]])
    added = portfolio_update(data, mode)
    notify(data, mode, added)
    data["updated"] = now_utc()
    save(OUT, data)


if __name__ == "__main__":
    main()
