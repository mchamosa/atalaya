import importlib.util, json, os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
s = importlib.util.spec_from_file_location("e", os.path.join(ROOT, "scripts", "escaner.py")); e = importlib.util.module_from_spec(s); s.loader.exec_module(e)
cal = e.earnings_calendar(days_ahead=7)
out = {"ok": cal is not None, "n": len(cal or {}), "riesgo_hoy": e.earnings_risk(cal)[:40] if cal else [],
       "muestra": dict(list((cal or {}).items())[:10])}
print(out)
json.dump(out, open(os.path.join(ROOT, "data", "investigacion_calendario.json"), "w"), ensure_ascii=False)
