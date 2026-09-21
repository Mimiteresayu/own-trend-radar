#!/usr/bin/env python3
"""Own Trend Radar UI — local :8787 or Railway (PORT, COCKPIT_PASSWORD).

Local:  python serve.py  → http://127.0.0.1:8787/  (no password unless set)
Railway: password gate + POST /api/sync; /api/rescan disabled when RAILWAY=1
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
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT") or "8787")
HOST = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY") else "0.0.0.0")
SCAN_SCRIPT = os.path.join(ROOT, "scan_gc_radar.py")
UI_PATH = os.path.join(ROOT, "ui.html")
RESCAN_TIMEOUT_S = 1800
PASSWORD = (os.environ.get("COCKPIT_PASSWORD") or "").strip()
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY"))
COOKIE_NAME = "otr_session"
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or PASSWORD or "dev-local").encode()

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
            self._send_json(200, {"ok": True})
            return
        if self._need_auth():
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
            if ON_RAILWAY:
                self._send_json(403, {"ok": False, "error": "rescan disabled on Railway; use /api/sync"})
                return
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
        for rel, content in files.items():
            safe = self._safe_rel(str(rel))
            if not safe:
                self._send_json(400, {"ok": False, "error": f"bad path: {rel}"})
                return
            dest = os.path.join(ROOT, safe)
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
        self._send_json(200, {"ok": True, "written": written})

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
        if length:
            self.rfile.read(length)
        if not os.path.isfile(SCAN_SCRIPT):
            self._send_json(500, {"ok": False, "error": "scan_gc_radar.py missing"})
            return
        try:
            proc = subprocess.run(
                [sys.executable, SCAN_SCRIPT, "--tf", "all"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=RESCAN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            self._send_json(504, {"ok": False, "error": f"rescan timed out (>{RESCAN_TIMEOUT_S}s)"})
            return
        except OSError as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "scan failed")[-2000:]
            self._send_json(500, {"ok": False, "error": err, "returncode": proc.returncode})
            return
        self._send_json(200, {"ok": True, "note": "scanned 1h+4h+1d"})


def main() -> None:
    os.chdir(ROOT)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Own Trend Radar UI → http://0.0.0.0:{PORT}/", flush=True)
    print(f"password_gate={'on' if PASSWORD else 'off'} railway={ON_RAILWAY}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
