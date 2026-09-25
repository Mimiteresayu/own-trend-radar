# Own Trend Radar — finished for now (token pause)

## Done
- HL volume Top ~150 universe
- TF: **1H / 4H / 1D**
- GC: hlc3 / 4 / 144 / 1.414 + Lag + Fast (GiiQ-aligned)
- UI: `ui.html` + `serve.py` → http://127.0.0.1:8787/
- Offline: `ui_snapshot.html`
- Desktop path: `C:\Users\yumim\Documents\own-trend-radar\`
- Nightly routine: **08:40 HKT daily** → 1D scan only (saves tokens)

## How to open
1. `cd C:\Users\yumim\Documents\own-trend-radar`
2. `python serve.py`
3. Browser → http://127.0.0.1:8787/ → switch TF tabs

## Not in this finish (later)
- Signum smoothed `gc.trend` SoT
- Bitunix MATCH / mcap Top 150
- Auto 1H/4H scans
- Live trading
