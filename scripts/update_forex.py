#!/usr/bin/env python3
"""
Atalaya · velas de 1 hora de los pares de divisas principales, para la pestaña "Soldados".

Guarda un fichero por par y mes (mes en UTC):
    data/forex/EURUSD/2026-11.json  ->  {"pair","tf":"1h","pip","bars":[[t,o,h,l,c], ...]}
    data/forex/index.json           ->  meses disponibles por par
t = segundos UTC de la apertura de la vela.

Fuente: Yahoo Finance (yfinance), sin clave: hasta 730 días de velas de 1 hora.
Cada pasada fusiona lo nuevo con lo guardado, así que el histórico crece con el tiempo.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
         "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "AUDJPY", "CADJPY", "EURAUD"]

_HERE = Path(__file__).resolve().parent
ROOT = _HERE if (_HERE / "index.html").exists() else _HERE.parent
OUT = ROOT / "data" / "forex"
UTC = timezone.utc
NOW = datetime.now(UTC)


def pip(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


def month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def load_month(pair: str, mk: str) -> dict[int, list]:
    f = OUT / pair / f"{mk}.json"
    if not f.exists():
        return {}
    try:
        return {int(b[0]): b for b in json.loads(f.read_text(encoding="utf-8"))["bars"]}
    except Exception:
        return {}


def fetch(pair: str) -> dict[int, list]:
    import yfinance as yf
    df = yf.Ticker(f"{pair}=X").history(period="730d", interval="1h", auto_adjust=False)
    nd = 3 if pair.endswith("JPY") else 5
    out: dict[int, list] = {}
    for ts, row in df.iterrows():
        o, h, l, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
        if not all(x == x and x > 0 for x in (o, h, l, c)):
            continue
        t = int(ts.timestamp())
        out[t] = [t, round(o, nd), round(max(o, h, l, c), nd), round(min(o, h, l, c), nd), round(c, nd)]
    return out


def save(pair: str, bars: dict[int, list]) -> list[str]:
    by_month: dict[str, dict[int, list]] = {}
    for t, b in bars.items():
        by_month.setdefault(month_key(t), {})[t] = b
    touched = []
    for mk, new in sorted(by_month.items()):
        cur = load_month(pair, mk)
        merged = {**cur, **new}
        if merged == cur:
            continue
        (OUT / pair).mkdir(parents=True, exist_ok=True)
        payload = {"pair": pair, "tf": "1h", "pip": pip(pair), "source": "Yahoo Finance",
                   "fields": ["t", "o", "h", "l", "c"], "bars": [merged[t] for t in sorted(merged)]}
        (OUT / pair / f"{mk}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        touched.append(mk)
    return touched


def write_index() -> None:
    pairs = {}
    for d in sorted(p for p in OUT.iterdir() if p.is_dir()):
        months = sorted(f.stem for f in d.glob("*.json"))
        if not months:
            continue
        first, last = load_month(d.name, months[0]), load_month(d.name, months[-1])
        pairs[d.name] = {"months": months, "pip": pip(d.name),
                         "from": datetime.fromtimestamp(min(first), UTC).strftime("%Y-%m-%d"),
                         "to": datetime.fromtimestamp(max(last), UTC).strftime("%Y-%m-%d")}
    idx = {"tf": "1h", "updated": NOW.isoformat(timespec="seconds"), "source": "Yahoo Finance", "pairs": pairs}
    (OUT / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  ok  data/forex/index.json ({len(pairs)} pares)")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    errors = 0
    print(f"Velas de 1 hora · {len(PAIRS)} pares · Yahoo Finance")
    for pair in PAIRS:
        try:
            bars = fetch(pair)
            touched = save(pair, bars)
            print(f"  {pair}: {len(bars)} velas; meses actualizados: {', '.join(touched) or 'ninguno'}")
        except Exception as e:
            errors += 1
            print(f"  ERROR {pair}: {e}")
    write_index()
    return 1 if errors == len(PAIRS) else 0


if __name__ == "__main__":
    sys.exit(main())
