#!/usr/bin/env python3
"""Regenerate ui_snapshot.html from out/gc_radar_*.json + ui.html.

Prefers inlining all available TFs into INLINE_BY_TF.
Falls back to 1d-only INLINE_DATA if others missing.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TFS = ("1h", "4h", "1d")


def main() -> None:
    by_tf = {}
    for tf in TFS:
        path = ROOT / "out" / f"gc_radar_{tf}.json"
        if path.is_file():
            by_tf[tf] = json.loads(path.read_text(encoding="utf-8"))

    # Prefer 1d; fall back to daily alias
    if "1d" not in by_tf:
        alias = ROOT / "out" / "daily_gc_radar.json"
        if alias.is_file():
            data = json.loads(alias.read_text(encoding="utf-8"))
            if "tf" not in data:
                data["tf"] = "1d"
            by_tf["1d"] = data

    if not by_tf:
        raise SystemExit("No gc_radar_*.json found in out/ — run scan first")

    ui = (ROOT / "ui.html").read_text(encoding="utf-8")
    primary = by_tf.get("1d") or next(iter(by_tf.values()))
    inline_primary = json.dumps(primary, separators=(",", ":"))
    inline_by_tf = json.dumps(by_tf, separators=(",", ":"))

    # Inject after script IIFE start — look for REFRESH_MS constant
    marker = "  const REFRESH_MS = 60000;"
    if marker not in ui:
        raise SystemExit("REFRESH_MS marker missing in ui.html")

    note = ""
    missing = [t for t in TFS if t not in by_tf]
    if missing:
        note = (
            f"\n  /* SNAPSHOT note: missing TFs {missing} — switch tabs need server "
            f"or re-run scan_gc_radar.py */"
        )

    patched = ui.replace(
        marker,
        marker
        + "\n  /* SNAPSHOT: data inlined for file:// viewing */\n"
        + f"  const INLINE_DATA = {inline_primary};\n"
        + f"  const INLINE_BY_TF = {inline_by_tf};"
        + note,
        1,
    )

    patched = patched.replace(
        "<title>Own Trend Radar · GC Multi-TF</title>",
        "<title>Own Trend Radar · GC Multi-TF · Snapshot</title>",
        1,
    )
    foot_old = "Auto-refresh every 60s · fetch <code>out/gc_radar_{tf}.json</code> · no trading"
    foot_new = (
        "Self-contained snapshot (JSON inlined"
        + (f"; TFs: {', '.join(by_tf.keys())}" if by_tf else "")
        + ") · open via file:// · no trading"
        + (f" · missing: {', '.join(missing)} need server" if missing else "")
    )
    patched = patched.replace(foot_old, foot_new, 1)

    out = ROOT / "ui_snapshot.html"
    out.write_text(patched, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    print(f"  inlined TFs: {list(by_tf.keys())}")
    for tf, data in by_tf.items():
        flags = data.get("flags") or {}
        print(f"  {tf}: ts={data.get('ts')} breadth={data.get('breadth')} "
              f"dual_up={flags.get('dual_cross_up')}")


if __name__ == "__main__":
    main()
