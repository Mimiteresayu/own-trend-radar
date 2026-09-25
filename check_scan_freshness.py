#!/usr/bin/env python3
"""Fail if gc_radar_1d.json is stale. Used by daily/4H routines before any report."""
from __future__ import annotations
import json, sys
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out" / "gc_radar_1d.json"
MAX_AGE_H = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0

def main() -> int:
    if not OUT.is_file():
        print(f"MISSING {OUT}")
        return 2
    d = json.loads(OUT.read_text(encoding="utf-8"))
    ts = d.get("ts")
    now = datetime.now(timezone.utc)
    if ts:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age_h = (now - t.astimezone(timezone.utc)).total_seconds() / 3600
    else:
        age_h = (now.timestamp() - OUT.stat().st_mtime) / 3600
    ok = age_h <= MAX_AGE_H
    print(json.dumps({
        "ok": ok,
        "age_h": round(age_h, 3),
        "max_age_h": MAX_AGE_H,
        "ts": ts,
        "n": d.get("n_scanned"),
        "breadth": d.get("breadth"),
        "path": str(OUT),
    }))
    return 0 if ok else 2

if __name__ == "__main__":
    raise SystemExit(main())
