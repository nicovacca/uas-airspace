r"""
serve_map.py - open the map with a working "Refresh" button.

Serves v2_full/ at http://127.0.0.1:8765 (this computer only) and adds two endpoints the map uses:
  GET  /api/status            -> is a refresh running, which step, last log lines
  POST /api/refresh?scope=... -> scope=fast: TFRs + stadium events (a few minutes)
                                 scope=all:  every FAA layer (about 30 minutes)
A refresh runs build_uas_layers.py --out out_full [--group fast], then make_v2_full.py.

Run: .venv\Scripts\python serve_map.py      (RUN_MAP.bat does this for you)
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SITE = HERE / "v2_full"
PORT = 8765
JOB = {"running": False, "scope": None, "step": "", "log": [], "ok": None, "finished": None}
LOCK = threading.Lock()


def run_refresh(scope):
    steps = [("Downloading FAA data", [sys.executable, "-u", str(HERE / "build_uas_layers.py"), "--out", "out_full"]
              + (["--group", "fast"] if scope == "fast" else [])),
             ("Rebuilding the map", [sys.executable, "-u", str(HERE / "make_v2_full.py")])]
    ok = True
    for i, (label, cmd) in enumerate(steps, 1):
        JOB["step"] = f"{label} (step {i} of {len(steps)})"
        p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        for line in p.stdout:
            for part in line.replace("\r", "\n").split("\n"):
                part = part.strip()
                if part and not part.endswith("..."):
                    JOB["log"] = (JOB["log"] + [part])[-12:]
                    print("  " + part)
        rc = p.wait()
        if i == len(steps) and rc != 0:          # the map build must succeed; download errors are tolerated
            ok = False
        if i == 1 and rc != 0:
            JOB["log"].append("Some layers could not be downloaded; the rest were updated.")
    JOB.update(running=False, ok=ok, step="Done" if ok else "Failed",
               finished=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")        # always serve freshly rebuilt data files
        super().end_headers()

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/api/status":
            return self._json(200, JOB)
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/refresh":
            return self._json(404, {"error": "not found"})
        if self.headers.get("X-Map-Refresh") != "1":      # blocks other websites from triggering a refresh
            return self._json(403, {"error": "forbidden"})
        scope = (parse_qs(u.query).get("scope") or ["fast"])[0]
        with LOCK:
            if JOB["running"]:
                return self._json(409, JOB)
            JOB.update(running=True, scope=scope, step="Starting", log=[], ok=None, finished=None)
            threading.Thread(target=run_refresh, args=(scope,), daemon=True).start()
        return self._json(202, JOB)

    def log_message(self, *a):
        pass


def main():
    if not (SITE / "index.html").exists():
        sys.exit("v2_full/index.html not found. Build the map first (RUN_MAP.bat does this).")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(SITE)))
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Map running at {url}  (leave this window open while you use the map; close it to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
