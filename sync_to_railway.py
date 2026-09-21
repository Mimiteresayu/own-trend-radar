#!/usr/bin/env python3
"""Push out/*.json (and narrative watchlist) to Railway cockpit /api/sync."""
from __future__ import annotations
import json, os, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = (os.environ.get("OTR_RAILWAY_URL") or "https://cockpit-production-ec2c.up.railway.app").rstrip("/")
PASS = (os.environ.get("COCKPIT_PASSWORD") or "").strip()
if not PASS and (ROOT / ".railway_password").exists():
    PASS = (ROOT / ".railway_password").read_text().strip()

FILES = [
    "out/desk_daily.json",
    "out/desk_daily_prev.json",
    "out/gc_radar_1d.json",
    "out/gc_radar_4h.json",
    "out/narrative_watchlist.json",
    "out/bitunix_gc_1d.json",
    "out/monitor_hl_exit.json",
]

def main() -> int:
    if not PASS:
        print("missing COCKPIT_PASSWORD", file=sys.stderr)
        return 2
    files = {}
    for rel in FILES:
        p = ROOT / rel
        if p.is_file():
            files[rel] = p.read_text(encoding="utf-8")
    body = json.dumps({"files": files}).encode()
    req = urllib.request.Request(
        URL + "/api/sync",
        data=body,
        headers={
            "Authorization": f"Bearer {PASS}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        print(r.read().decode())
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
