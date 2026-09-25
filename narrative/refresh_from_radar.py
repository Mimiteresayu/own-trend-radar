#!/usr/bin/env python3
"""Intersect Narrative Watchlist (gc_scan=yes) with Own Radar 1D JSON. Quiet if no signals."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WL = Path(__file__).resolve().parent / "watchlist.json"
RADAR = ROOT / "out" / "gc_radar_1d.json"
OUT = Path(__file__).resolve().parent / "daily_gc_overlay.json"

def main() -> None:
    wl = json.loads(WL.read_text(encoding="utf-8")) if WL.is_file() else {"items": []}
    radar_rows = []
    if RADAR.is_file():
        d = json.loads(RADAR.read_text(encoding="utf-8"))
        radar_rows = d.get("rows") or d.get("results") or (d if isinstance(d, list) else [])
    by = {}
    for r in radar_rows:
        if not isinstance(r, dict):
            continue
        s = (r.get("symbol") or r.get("coin") or "").upper()
        if s:
            by[s] = r
    overlay = []
    entries = []
    for it in wl.get("items") or []:
        if not it.get("gc_scan"):
            continue
        t = str(it.get("ticker") or "").upper()
        r = by.get(t)
        row = {
            "ticker": t,
            "sector": it.get("sector"),
            "narrative": it.get("narrative"),
            "in_radar": bool(r),
            "trend": (r or {}).get("trend") or (r or {}).get("gc_trend"),
            "signal": (r or {}).get("signal") or (r or {}).get("dual_cross"),
            "close": (r or {}).get("close"),
        }
        overlay.append(row)
        sig = str(row.get("signal") or "").lower()
        if r and ("cross" in sig or sig in ("dual_cross_up", "long_entry", "upper_cross")):
            entries.append(row)
        # also flag Green + any cross-like fields
        if r and row.get("trend") == "Green" and (r.get("cross_up") or r.get("dual_cross_up")):
            if row not in entries:
                entries.append(row)
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "watch_gc_count": len(overlay),
        "entry_candidates": entries,
        "overlay": overlay,
    }
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"overlay={len(overlay)} entries={len(entries)} -> {OUT}")
    for e in entries:
        print(f"  ENTRY? {e['ticker']} trend={e.get('trend')} signal={e.get('signal')}")

if __name__ == "__main__":
    main()
