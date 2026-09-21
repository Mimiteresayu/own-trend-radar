#!/usr/bin/env python3
"""Build out/curves_compare.json + equity_history.json (HL equity vs BTC vs Bitunix BTC)."""
from __future__ import annotations
import json, urllib.request, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from scan_gc_radar import fetch_candles  # noqa: E402

OUT = ROOT / "out" / "curves_compare.json"
HIST = ROOT / "out" / "equity_history.json"
DESK = ROOT / "out" / "desk_daily.json"
HKT = timezone(timedelta(hours=8))

def account_equity(d: dict):
    a = d.get("account") or {}
    for k in ("equity", "total_eq", "spot_usdc"):
        if a.get(k) is not None:
            try: return float(a[k])
            except (TypeError, ValueError): pass
    return None

def load_hist():
    if HIST.is_file():
        try: return list(json.loads(HIST.read_text()).get("points") or [])
        except Exception: return []
    return []

def save_hist(points):
    HIST.parent.mkdir(parents=True, exist_ok=True)
    HIST.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "points": points[-200:]}, indent=2))

def append_equity():
    pts = load_hist()
    for name in ("desk_daily_prev.json", "desk_daily.json"):
        p = ROOT / "out" / name
        if not p.is_file(): continue
        d = json.loads(p.read_text())
        eq = account_equity(d)
        ts = d.get("ts") or d.get("ts_hkt")
        if eq is None or not ts: continue
        key = str(ts)[:16]
        if not any(str(x.get("ts", ""))[:16] == key for x in pts):
            pts.append({"ts": d.get("ts") or datetime.now(timezone.utc).isoformat(), "ts_hkt": d.get("ts_hkt"), "equity": eq})
    if DESK.is_file():
        d = json.loads(DESK.read_text())
        eq = account_equity(d)
        if eq is not None:
            day = str(d.get("ts") or "")[:10]
            updated = False
            for x in pts:
                if str(x.get("ts", ""))[:10] == day:
                    x["equity"] = eq; x["ts"] = d.get("ts"); x["ts_hkt"] = d.get("ts_hkt"); updated = True
            if not updated:
                pts.append({"ts": d.get("ts") or datetime.now(timezone.utc).isoformat(), "ts_hkt": d.get("ts_hkt"), "equity": eq})
    pts.sort(key=lambda x: x.get("ts") or "")
    save_hist(pts)
    return pts

def bitunix_btc_daily(n=60):
    urls = [
        "https://fapi.bitunix.com/api/v1/futures/market/kline?symbol=BTCUSDT&interval=1d&limit=60",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HarborCockpit/1"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = json.loads(r.read().decode())
            data = raw.get("data") or raw
            if isinstance(data, dict):
                data = data.get("list") or data.get("klines") or []
            out = []
            for row in data:
                if isinstance(row, dict):
                    t = row.get("time") or row.get("t") or row.get("openTime")
                    c = row.get("close") or row.get("c")
                elif isinstance(row, (list, tuple)) and len(row) >= 5:
                    t, c = row[0], row[4]
                else:
                    continue
                t = int(t)
                if t < 1e12: t *= 1000
                out.append({"t": t, "close": float(c)})
            if out:
                out.sort(key=lambda x: x["t"])
                return out[-n:]
        except Exception as e:
            print(f"[warn] bitunix: {e}", flush=True)
    return []

def to_pct(series):
    base = None; out = []
    for v in series:
        if v is None:
            out.append(None); continue
        if base is None:
            base = v; out.append(0.0)
        else:
            out.append((v / base - 1.0) * 100.0)
    return out

def main():
    eq_pts = append_equity()
    btc = [{"t": int(b["t"]), "close": float(b["close"])} for b in fetch_candles("BTC", "1d")][-45:]
    bun = bitunix_btc_daily(45)
    btc_m = {datetime.fromtimestamp(b["t"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"): b["close"] for b in btc}
    bun_m = {datetime.fromtimestamp(b["t"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"): b["close"] for b in bun}
    eq_by_day = {}
    for p in eq_pts:
        ts = p.get("ts") or ""
        try:
            day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            day = str(p.get("ts_hkt") or "")[:10]
        if day: eq_by_day[day] = float(p["equity"])
    labels = [d for d in sorted(set(btc_m) | set(eq_by_day) | set(bun_m)) if d in btc_m][-30:]
    eq_series, btc_series, bun_series = [], [], []
    last_eq = None
    for d in labels:
        if d in eq_by_day: last_eq = eq_by_day[d]
        eq_series.append(last_eq)
        btc_series.append(btc_m.get(d))
        bun_series.append(bun_m.get(d))
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT"),
        "note": "Normalized % from first point. Equity=HL Unified USDC. BTC=HL 1D. Bitunix=Bitunix BTCUSDT 1D (price benchmark until Bitunix equity log exists).",
        "labels": labels,
        "equity_usd": eq_series, "btc_usd": btc_series, "bitunix_btc_usd": bun_series,
        "equity_pct": to_pct(eq_series), "btc_pct": to_pct(btc_series), "bitunix_pct": to_pct(bun_series),
        "equity_points_n": len(eq_pts),
        "legend": {"equity": "HL Equity", "btc": "BTC", "bitunix": "Bitunix BTC",
                   "colors": {"equity": "#38bdf8", "btc": "#f59e0b", "bitunix": "#a78bfa"}},
    }
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT} labels={len(labels)} eq_hist={len(eq_pts)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
