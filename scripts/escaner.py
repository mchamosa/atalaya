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

    data["updated"] = now_utc()
    save(OUT, data)


if __name__ == "__main__":
    main()
