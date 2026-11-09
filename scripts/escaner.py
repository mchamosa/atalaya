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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "scripts", "daytrader")
OUT = os.path.join(ROOT, "data", "escaner.json")
PTR_CACHE = os.path.join(ROOT, "data", "escaner_congreso.json")

NEWS_TICKERS = 20        # noticias de las N primeras acciones detectadas
NEWS_ITEMS = 80          # titulares que se guardan como mucho
PTR_DAYS = 45            # declaraciones del Congreso de los últimos N días
PTR_FETCH_MAX = 40       # PDF nuevos que se leen como mucho en cada pasada

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
        n = news([h["symbol"] for h in hits[:NEWS_TICKERS]])
        if n:
            data["news"] = n
        c = congress()
        if c:
            data["congress"] = c
    else:
        res, reason = scan("orb")
        if res:
            data["orb"] = res
            data.get("status", {}).pop("orb", None)
            log("apertura: %d rupturas" % len(res.get("hits", [])))
            n = news([h["symbol"] for h in res.get("hits", [])[:NEWS_TICKERS]])
            if n:
                data["news"] = n
        else:
            data.setdefault("status", {})["orb"] = reason

    data["updated"] = now_utc()
    save(OUT, data)


if __name__ == "__main__":
    main()
