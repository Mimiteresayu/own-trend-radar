#!/usr/bin/env python3
"""Back-compat wrapper: daily (1d) GC scan.

Prefer: python scan_gc_radar.py [--tf 1h|4h|1d|all]
This script runs the 1d timeframe only (same outputs as before + gc_radar_1d.*).
"""
from __future__ import annotations

import sys

from scan_gc_radar import main

if __name__ == "__main__":
    # Force 1d unless user already passed --tf
    argv = list(sys.argv[1:])
    if not any(a == "--tf" or a.startswith("--tf=") for a in argv):
        argv = ["--tf", "1d"] + argv
    sys.exit(main(argv))
