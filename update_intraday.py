#!/usr/bin/env python3
"""
Atalaya · velas de 5 minutos de acciones de EE. UU. para la pestaña "Apertura".

Guarda solo la sesión regular (9:30-16:00, hora de Nueva York), un fichero por mes:
    data/intraday/QQQ/2026-11.json   ->  {"symbol","tf","bars":[[t,o,h,l,c,v], ...]}
    data/intraday/index.json         ->  meses disponibles por símbolo
donde t = segundos UTC de la apertura de la vela.

Fuentes:
  Massive (antes Polygon.io), plan gratuito: hasta 2 años de velas de 5 min.
      Necesita el secreto MASSIVE_API_KEY (el mismo que usa el escáner de máximos).
  Yahoo Finance (yfinance): respaldo sin clave, solo los últimos 60 días.
Cada pasada solo descarga lo que falta y nunca borra meses ya guardados.

Uso:  python scripts/update_intraday.py            (actualiza)
      python scripts/update_intraday.py --full     (vuelve a descargar los 2 años)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SYMBOLS = ["QQQ", "SPY", "TSLA", "NVDA"]          # añade aquí otros tickers si quieres
YEARS_BACK = 2                                    # lo que permite el plan gratuito de Massive

_HERE = Path(__file__).resolve().parent
ROOT = _HERE if (_HERE / "index.html").exists() else _HERE.parent
OUT = ROOT / "data" / "intraday"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
MASSIVE_KEY = os.environ.get("MASSIVE_API_KEY", "").strip()
MASSIVE_HOSTS = ["https://api.massive.com", "https://api.polygon.io"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NOW = datetime.now(UTC)
_last_call = [0.0]


# ------------------------------------------------------------------ utilidades
def rth(ts: int) -> bool:
    """¿La vela que abre en ts (segundos UTC) es de la sesión regular de Nueva York?"""
    d = datetime.fromtimestamp(ts, UTC).astimezone(NY)
    if d.weekday() >= 5:
        return False
    m = d.hour * 60 + d.minute
    return 570 <= m < 960            # 9:30 a 15:55 (la última vela cierra a las 16:00)


def month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(NY).strftime("%Y-%m")


def r2(x) -> float:
    x = float(x)
    return round(x, 4) if abs(x) < 10 else round(x, 2)


def load_month(sym: str, mk: str) -> dict[int, list]:
    f = OUT / sym / f"{mk}.json"
    if not f.exists():
        return {}
    try:
        return {int(b[0]): b for b in json.loads(f.read_text(encoding="utf-8"))["bars"]}
    except Exception:
        return {}


def save_months(sym: str, bars: dict[int, list], source: str) -> list[str]:
    """Fusiona las velas nuevas con lo guardado, mes a mes. Devuelve los meses tocados."""
    by_month: dict[str, dict[int, list]] = {}
    for t, b in bars.items():
        by_month.setdefault(month_key(t), {})[t] = b
    touched = []
    for mk, new in sorted(by_month.items()):
        cur = load_month(sym, mk)
        merged = {**cur, **new}
        if merged == cur:
            continue
        (OUT / sym).mkdir(parents=True, exist_ok=True)
        rows = [merged[t] for t in sorted(merged)]
        payload = {"symbol": sym, "tf": "5m", "tz": "America/New_York", "source": source,
                   "fields": ["t", "o", "h", "l", "c", "v"], "bars": rows}
        (OUT / sym / f"{mk}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        touched.append(mk)
    return touched


def last_stored(sym: str) -> int | None:
    d = OUT / sym
    files = sorted(d.glob("*.json")) if d.exists() else []
    for f in reversed(files):
        m = load_month(sym, f.stem)
        if m:
            return max(m)
    return None


def write_index(sources: dict[str, str]) -> None:
    idx_file = OUT / "index.json"
    try:
        idx = json.loads(idx_file.read_text(encoding="utf-8"))
    except Exception:
        idx = {}
    syms = idx.get("symbols", {})
    for d in sorted(p for p in OUT.iterdir() if p.is_dir()):
        months = sorted(f.stem for f in d.glob("*.json"))
        if not months:
            continue
        first = load_month(d.name, months[0]); last = load_month(d.name, months[-1])
        syms[d.name] = {
            "months": months,
            "from": datetime.fromtimestamp(min(first), UTC).astimezone(NY).strftime("%Y-%m-%d") if first else None,
            "to": datetime.fromtimestamp(max(last), UTC).astimezone(NY).strftime("%Y-%m-%d") if last else None,
            "source": sources.get(d.name, syms.get(d.name, {}).get("source", "")),
        }
    idx = {"tf": "5m", "session": "09:30-16:00 America/New_York",
           "updated": NOW.isoformat(timespec="seconds"), "symbols": syms}
    idx_file.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  ok  data/intraday/index.json ({len(syms)} símbolos)")


# ------------------------------------------------------------------ Massive
def massive_get(url: str, params: dict | None = None) -> dict:
    """GET con la clave y respetando 5 llamadas/minuto del plan gratuito."""
    wait = 12.5 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    last = None
    for host in ([""] if url.startswith("http") else MASSIVE_HOSTS):
        for attempt in range(2):
            try:
                _last_call[0] = time.time()
                r = requests.get(host + url, params=params or {}, timeout=90,
                                 headers={**UA, "Authorization": f"Bearer {MASSIVE_KEY}"})
                if r.status_code == 429:
                    time.sleep(65)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                break
    raise RuntimeError(f"Massive: {last}")


def fetch_massive(sym: str, start: datetime, end: datetime) -> dict[int, list]:
    out: dict[int, list] = {}
    url = f"/v2/aggs/ticker/{sym}/range/5/minute/{start:%Y-%m-%d}/{end:%Y-%m-%d}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000}
    pages = 0
    while url and pages < 40:
        j = massive_get(url, params)
        pages += 1
        for a in j.get("results") or []:
            t = int(a["t"]) // 1000
            if rth(t):
                out[t] = [t, r2(a["o"]), r2(a["h"]), r2(a["l"]), r2(a["c"]), int(a.get("v") or 0)]
        url, params = j.get("next_url"), None       # la paginación ya trae los parámetros
    return out


# ------------------------------------------------------------------ Yahoo
def fetch_yahoo(sym: str) -> dict[int, list]:
    import yfinance as yf
    df = yf.Ticker(sym).history(period="60d", interval="5m", prepost=False, auto_adjust=False)
    out: dict[int, list] = {}
    for ts, row in df.iterrows():
        t = int(ts.timestamp())
        if rth(t) and row["Close"] == row["Close"]:
            out[t] = [t, r2(row["Open"]), r2(row["High"]), r2(row["Low"]), r2(row["Close"]), int(row["Volume"] or 0)]
    return out


# ------------------------------------------------------------------ principal
def main() -> int:
    full = "--full" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    sources: dict[str, str] = {}
    errors = 0
    print(f"Velas de 5 min · {', '.join(SYMBOLS)} · Massive {'con clave' if MASSIVE_KEY else 'SIN clave (se usa Yahoo, 60 días)'}")
    for sym in SYMBOLS:
        try:
            last = None if full else last_stored(sym)
            bars, src = {}, ""
            if MASSIVE_KEY:
                try:
                    start = (datetime.fromtimestamp(last, UTC) - timedelta(days=3)) if last \
                        else NOW - timedelta(days=365 * YEARS_BACK - 5)
                    bars = fetch_massive(sym, start, NOW)
                    src = "Massive"
                except Exception as e:
                    print(f"  aviso {sym}: {e}. Se intenta Yahoo.")
            if not bars:
                bars = fetch_yahoo(sym)
                src = "Yahoo Finance"
            touched = save_months(sym, bars, src)
            sources[sym] = src
            print(f"  {sym}: {len(bars)} velas de {src}; meses actualizados: {', '.join(touched) or 'ninguno'}")
        except Exception as e:
            errors += 1
            print(f"  ERROR {sym}: {e}")
    write_index(sources)
    return 1 if errors == len(SYMBOLS) else 0


if __name__ == "__main__":
    sys.exit(main())
