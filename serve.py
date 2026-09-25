#!/usr/bin/env python3
"""Own Trend Radar UI — local :8787 or Railway (PORT, COCKPIT_PASSWORD).

Local:  python serve.py  → http://127.0.0.1:8787/  (no password unless set)
Railway: password gate + POST /api/sync; background scanner writes out/gc_radar_*.json
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request as _url_req
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT") or "8787")
HOST = os.environ.get("HOST") or (
    "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY") else "0.0.0.0"
)
SCAN_SCRIPT = os.path.join(ROOT, "scan_gc_radar.py")
FAILSAFE_SCRIPT = os.path.join(ROOT, "failsafe_exit_worker.py")
UI_PATH = os.path.join(ROOT, "ui.html")
OUT_DIR = os.path.join(ROOT, "out")
RESCAN_TIMEOUT_S = 1800
PASSWORD = (os.environ.get("COCKPIT_PASSWORD") or "").strip()
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY"))
COOKIE_NAME = "otr_session"
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or PASSWORD or "dev-local").encode()
# Scheduler (Railway): UTC :05 after bar close. HKT = UTC+8.
SCHEDULER_ENABLE = (os.environ.get("OTR_SCHEDULER") or ("1" if ON_RAILWAY else "0")).strip() in (
    "1",
    "true",
    "yes",
)
SCAN_MAX = int(os.environ.get("OTR_SCAN_MAX") or "280")
SCAN_MAX_1H = int(os.environ.get("OTR_SCAN_MAX_1H") or "200")
SCAN_CONCURRENCY = int(os.environ.get("OTR_SCAN_CONCURRENCY") or "2")
# Radar refresh interval for 1h+4h (minutes). Default 15 for 1H exits.
SCAN_INTERVAL_MIN = max(5, int(os.environ.get("OTR_SCAN_INTERVAL_MIN") or "15"))
_scan_lock = threading.Lock()
_last_scan: dict[str, str] = {}  # tf -> slot key
_last_failsafe: str = ""  # last failsafe run slot key (hour)

# Desk-data endpoint: HL wallet (public read-only)
HL_WALLET = os.environ.get("HL_WALLET") or "0xcFCda0F8576a268BaA17935368081F4e687dB122"
HL_API_URL = "https://api.hyperliquid.xyz/info"

LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">
<title>Own Trend Radar</title>
<style>body{font-family:system-ui;background:#0b0f14;color:#e6edf3;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#161b22;padding:24px;border-radius:12px;width:min(360px,92vw);border:1px solid #30363d}
input{width:100%;padding:10px;margin:8px 0 16px;border-radius:8px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;box-sizing:border-box}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#238636;color:#fff;font-weight:600}
.err{color:#f85149;font-size:13px;margin:0 0 8px}</style></head>
<body><form method=POST action=/login>
<h2 style=margin:0 0 8px>Own Trend Radar</h2>
<p style=opacity:.7;font-size:13px;margin:0 0 12px>read-only · auto-refresh</p>
__ERR__
<label>Password</label>
<input type=password name=password autofocus required>
<button type=submit>Enter</button>
</form></body></html>
"""


def _token_for(password: str) -> str:
    return hmac.new(SESSION_SECRET, password.encode(), hashlib.sha256).hexdigest()


def _run_failsafe() -> tuple[bool, str]:
    """Run fail-safe exit worker. Returns (ok, note/error)."""
    if not os.path.isfile(FAILSAFE_SCRIPT):
        return False, "failsafe_exit_worker.py missing"
    cmd = [sys.executable, FAILSAFE_SCRIPT]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min timeout
        )
    except subprocess.TimeoutExpired:
        return False, "failsafe timed out (>300s)"
    except OSError as e:
        return False, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "failsafe failed")[-1000:]
        return False, err
    # Success; extract status from stdout (JSON)
    try:
        result = json.loads(proc.stdout)
        status = result.get("status", "unknown")
        mode = result.get("mode", "?")
        pos_count = len(result.get("positions", []))
        action_count = len(result.get("actions", []))
        return True, f"failsafe {mode} status={status} pos={pos_count} actions={action_count}"
    except Exception:
        return True, "failsafe ran (status unknown)"
    """Run scan_gc_radar.py for given TFs. Returns (ok, note/error)."""
    if not os.path.isfile(SCAN_SCRIPT):
        return False, "scan_gc_radar.py missing"
    if not tfs:
        return False, "no tfs"
    mx = max_symbols if max_symbols is not None else SCAN_MAX
    cmd = [
        sys.executable,
        SCAN_SCRIPT,
        "--tf",
        ",".join(tfs),
        "--max",
        str(mx),
        "--concurrency",
        str(max(1, min(8, SCAN_CONCURRENCY))),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=RESCAN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"scan timed out (>{RESCAN_TIMEOUT_S}s)"
    except OSError as e:
        return False, str(e)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "scan failed")[-2000:]
        return False, err
    return True, f"scanned {','.join(tfs)}"


def _run_scan(tfs: list[str], max_symbols: int | None = None) -> tuple[bool, str]:
    """Dedup key so each schedule window runs once."""
    if kind == "1d":
        return now.strftime("%Y-%m-%d") + ":1d"
    # 1h / 4h share the same 15-minute radar slot (floor minute)
    slot_min = (now.minute // SCAN_INTERVAL_MIN) * SCAN_INTERVAL_MIN
    return now.strftime("%Y-%m-%dT%H") + f":{slot_min:02d}:{kind}"


def _due_tfs(now: datetime) -> list[tuple[str, int]]:
    """Return (tf, max_symbols) due now.

    - 1h + 4h every SCAN_INTERVAL_MIN minutes (default 15 → :00/:15/:30/:45)
    - 1d daily at 00:05 UTC only (daily bars; not on 15m tick)
    """
    due: list[tuple[str, int]] = []
    # Intraday radar (exits): every N minutes on the clock
    if now.minute % SCAN_INTERVAL_MIN == 0:
        if _last_scan.get("1h") != _slot_key(now, "1h"):
            due.append(("1h", SCAN_MAX_1H))
        if _last_scan.get("4h") != _slot_key(now, "4h"):
            due.append(("4h", SCAN_MAX))
    # 1D daily at 00:05 UTC (= 08:05 HKT)
    if now.hour == 0 and now.minute == 5 and _last_scan.get("1d") != _slot_key(now, "1d"):
        due.append(("1d", SCAN_MAX))
    return due


def _log_desk_data() -> None:
    """One-line summary of scan completion (reduced from full JSON dump)."""
    try:
        radar_ts = {}
        pos_count = {}
        for tf in ("1h", "4h", "1d"):
            fp = os.path.join(OUT_DIR, f"gc_radar_{tf}.json")
            try:
                with open(fp) as f:
                    data = json.load(f)
                    radar_ts[tf] = data.get("ts", "")[:19]
                    rows = data.get("rows") or []
                    pos_count[tf] = len(rows)
            except Exception:
                radar_ts[tf] = "error"
                pos_count[tf] = 0
        sys.stderr.write(
            f"[DESK_DATA] {datetime.now(timezone.utc).isoformat()[:19]} "
            f"radar_1h={pos_count['1h']}@{radar_ts['1h']} "
            f"radar_4h={pos_count['4h']}@{radar_ts['4h']} "
            f"radar_1d={pos_count['1d']}@{radar_ts['1d']}\n"
        )
    except Exception as e:
        sys.stderr.write(f"[DESK_DATA] error: {e}\n")


def _scheduler_loop() -> None:
    sys.stderr.write(
        f"[scheduler] started UTC: 1h+4h every {SCAN_INTERVAL_MIN}m; 1d@00:05 "
        f"(concurrency={SCAN_CONCURRENCY})\n"
    )
    # Soft boot: if radar files missing, scan after short delay (volume may be empty)
    time.sleep(15)
    missing = [
        tf
        for tf in ("1d", "4h", "1h")
        if not os.path.isfile(os.path.join(OUT_DIR, f"gc_radar_{tf}.json"))
    ]
    if missing and _scan_lock.acquire(blocking=False):
        try:
            sys.stderr.write(f"[scheduler] boot scan missing={missing}\n")
            for tf in missing:
                mx = SCAN_MAX_1H if tf == "1h" else SCAN_MAX
                ok, note = _run_scan([tf], max_symbols=mx)
                sys.stderr.write(f"[scheduler] boot tf={tf} ok={ok} {note[:200]}\n")
                if ok:
                    _last_scan[tf] = "boot"
        finally:
            _scan_lock.release()
        _log_desk_data()  # emit after boot scans for MCP relay

    while True:
        try:
            now = datetime.now(timezone.utc)
            due = _due_tfs(now)
            if due and _scan_lock.acquire(blocking=False):
                try:
                    # Mark slots first so we don't re-fire if scan spans the next minute tick
                    for tf, _mx in due:
                        _last_scan[tf] = _slot_key(now, tf)
                    # 1d alone when due; 1h+4h together on the 15m tick (4h uses SCAN_MAX)
                    daily = [tf for tf, _ in due if tf == "1d"]
                    intraday = [tf for tf, _ in due if tf in ("1h", "4h")]
                    if daily:
                        ok, note = _run_scan(daily, max_symbols=SCAN_MAX)
                        sys.stderr.write(f"[scheduler] {now.isoformat()} 1d ok={ok} {note[:300]}\n")
                    if intraday:
                        # Prefer scanning 4h then 1h sequentially via one or two calls
                        if "4h" in intraday:
                            ok, note = _run_scan(["4h"], max_symbols=SCAN_MAX)
                            sys.stderr.write(f"[scheduler] {now.isoformat()} 4h ok={ok} {note[:300]}\n")
                        if "1h" in intraday:
                            ok, note = _run_scan(["1h"], max_symbols=SCAN_MAX_1H)
                            sys.stderr.write(f"[scheduler] {now.isoformat()} 1h ok={ok} {note[:300]}\n")
                            _log_desk_data()  # emit after each scan slot for MCP relay
                finally:
                    _scan_lock.release()

            # Fail-safe worker: hourly at :05 UTC (after 1H close; 4H logic only on closed 4H bars)
            if now.minute == 5:
                slot = now.strftime("%Y-%m-%dT%H")
                if _last_failsafe != slot:
                    _last_failsafe = slot
                    try:
                        ok, note = _run_failsafe()
                        sys.stderr.write(f"[scheduler] {now.isoformat()} failsafe ok={ok} {note[:300]}\n")
                    except Exception as e:
                        sys.stderr.write(f"[scheduler] failsafe exception (non-fatal): {e}\n")

        except Exception as e:
            sys.stderr.write(f"[scheduler] error: {e}\n")
        time.sleep(15)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _authed(self) -> bool:
        if not PASSWORD:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:].strip(), PASSWORD):
            return True
        if self.headers.get("X-Cockpit-Password") == PASSWORD:
            return True
        if auth.lower().startswith("basic "):
            try:
                raw = base64.b64decode(auth[6:].strip()).decode()
                _u, _, pw = raw.partition(":")
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        cookie = SimpleCookie()
        if self.headers.get("Cookie"):
            cookie.load(self.headers.get("Cookie"))
        if COOKIE_NAME in cookie:
            return hmac.compare_digest(cookie[COOKIE_NAME].value, _token_for(PASSWORD))
        return False

    def _need_auth(self) -> bool:
        if self._authed():
            return False
        path = urlparse(self.path).path
        if path == "/login" and self.command == "POST":
            return False
        if path in ("/health", "/healthz"):
            return False
        self._send_login()
        return True

    def _send_login(self, err: str = "") -> None:
        html = LOGIN_HTML.replace("__ERR__", f'<p class=err>{err}</p>' if err else "")
        body = html.encode()
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("WWW-Authenticate", 'Bearer realm="otr"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/health", "/healthz"):
            self._send_json(
                200,
                {
                    "ok": True,
                    "scheduler": SCHEDULER_ENABLE,
                    "last_scan": dict(_last_scan),
                    "scanner": os.path.isfile(SCAN_SCRIPT),
                },
            )
            return
        if self._need_auth():
            return
        if path == "/api/desk-data":
            self._desk_data()
            return
        if path in ("/", "/index.html"):
            self._send_file(UI_PATH, "text/html; charset=utf-8")
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            self._login()
            return
        if path == "/api/sync":
            if not self._authed():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self._sync()
            return
        if path == "/api/rescan":
            # Password-gated (same as UI); allowed on Railway so Harbor is optional
            if self._need_auth():
                return
            self._rescan()
            return
        self.send_error(404, "Not Found")

    def _login(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        password = ""
        if "application/json" in ctype:
            try:
                password = str(json.loads(raw.decode()).get("password") or "")
            except Exception:
                password = ""
        else:
            qs = parse_qs(raw.decode(errors="ignore"))
            password = (qs.get("password") or [""])[0]
        if not PASSWORD or not hmac.compare_digest(password, PASSWORD):
            self._send_login("Wrong password")
            return
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={_token_for(PASSWORD)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000",
        )
        self.end_headers()

    def _safe_rel(self, rel: str) -> str | None:
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            return None
        if not (rel.startswith("out/") or rel.startswith("narrative/")):
            return None
        return rel

    def _sync(self) -> None:
        """Merge-write: only replaces keys present in payload; never wipes omitted radar files.
        
        Staleness guard: reject gc_radar_*.json if incoming scan timestamp is older than existing.
        """
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode())
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            self._send_json(400, {"ok": False, "error": "need {files:{path: content}}"})
            return
        written = []
        skipped_stale = []
        for rel, content in files.items():
            safe = self._safe_rel(str(rel))
            if not safe:
                self._send_json(400, {"ok": False, "error": f"bad path: {rel}"})
                return
            dest = os.path.join(ROOT, safe)
            
            # Staleness check for radar JSONs
            if safe.startswith("out/gc_radar_") and safe.endswith(".json"):
                if isinstance(content, str):
                    try:
                        incoming = json.loads(content)
                        incoming_ts = incoming.get("ts", "")
                        if os.path.isfile(dest):
                            with open(dest) as f:
                                existing = json.load(f)
                                existing_ts = existing.get("ts", "")
                            if existing_ts and incoming_ts and incoming_ts < existing_ts:
                                skipped_stale.append(f"{safe} (incoming={incoming_ts[:19]} < existing={existing_ts[:19]})")
                                continue
                    except Exception as e:
                        sys.stderr.write(f"[sync] staleness check failed for {safe}: {e}\n")
            
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if isinstance(content, str):
                data = content.encode("utf-8")
            else:
                self._send_json(400, {"ok": False, "error": f"content must be string: {rel}"})
                return
            tmp = dest + ".tmp." + secrets.token_hex(4)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            written.append(safe)
        result = {"ok": True, "written": written}
        if skipped_stale:
            result["skipped_stale"] = skipped_stale
        self._send_json(200, result)

    def _desk_data(self) -> None:
        """Password-gated endpoint — combined radar + HL data.

        Returns: gc_radar_1h, gc_radar_4h, gc_radar_1d (from volume),
                 hl_perp (clearinghouseState), hl_spot (spotClearinghouseState), ts.
        """
        result: dict = {}

        # 1) Radar JSONs from volume mount
        for tf in ("1h", "4h", "1d"):
            fp = os.path.join(OUT_DIR, f"gc_radar_{tf}.json")
            try:
                with open(fp) as f:
                    result[f"gc_radar_{tf}"] = json.load(f)
            except Exception as e:
                result[f"gc_radar_{tf}"] = {"error": str(e)}

        # 2) HL position data (Railway has unrestricted egress)
        for req_type, key in [
            ("clearinghouseState", "hl_perp"),
            ("spotClearinghouseState", "hl_spot"),
        ]:
            try:
                payload = json.dumps({"type": req_type, "user": HL_WALLET}).encode()
                req = _url_req.Request(
                    HL_API_URL,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _url_req.urlopen(req, timeout=15) as resp:
                    result[key] = json.loads(resp.read())
            except Exception as e:
                result[key] = {"error": str(e)}

        result["ts"] = datetime.now(timezone.utc).isoformat()
        self._send_json(200, result)

    def _send_file(self, filepath: str, content_type: str) -> None:
        try:
            with open(filepath, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404, "File not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _rescan(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        tfs = ["1h", "4h", "1d"]
        max_symbols = None
        if raw:
            try:
                body = json.loads(raw.decode())
                if isinstance(body, dict):
                    if body.get("tf"):
                        tfs = [t.strip() for t in str(body["tf"]).split(",") if t.strip()]
                    if body.get("max") is not None:
                        max_symbols = int(body["max"])
            except Exception:
                pass
        if not _scan_lock.acquire(blocking=False):
            self._send_json(409, {"ok": False, "error": "scan already running"})
            return
        try:
            ok, note = _run_scan(tfs, max_symbols=max_symbols)
        finally:
            _scan_lock.release()
        if ok:
            _log_desk_data()  # emit updated desk data after manual rescan
        if not ok:
            self._send_json(500, {"ok": False, "error": note})
            return
        self._send_json(200, {"ok": True, "note": note})


def main() -> None:
    os.chdir(ROOT)
    os.makedirs(OUT_DIR, exist_ok=True)
    if SCHEDULER_ENABLE:
        t = threading.Thread(target=_scheduler_loop, name="otr-scheduler", daemon=True)
        t.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Own Trend Radar UI → http://0.0.0.0:{PORT}/", flush=True)
    print(
        f"password_gate={'on' if PASSWORD else 'off'} railway={ON_RAILWAY} scheduler={SCHEDULER_ENABLE}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
