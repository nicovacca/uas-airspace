"""
Password gate for the v2_full map on Posit Connect (Flask).

Every request - the page AND its data files - requires a login with one shared password.
The password is NOT stored in code: set it on Connect as the environment variable MAP_PASSWORD
(Content > Vars). Optional: SECRET_KEY (random string) keeps people logged in across restarts.

Local test:  set MAP_PASSWORD=... then  python app.py   -> http://127.0.0.1:5000
"""

import hmac
import os
import secrets
import time
from pathlib import Path

from flask import Flask, abort, redirect, request, send_from_directory, session, url_for

SITE = Path(__file__).resolve().parent / "site"          # copy of v2_full (made by build_site.py)
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
                  PERMANENT_SESSION_LIFETIME=12 * 3600)

LOGIN = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Drone Airspace Map</title><style>
body{margin:0;height:100vh;display:grid;place-items:center;background:#1d1d20;color:#eceef3;font:15px Lato,system-ui,sans-serif}
form{background:#26272b;border:1px solid #36373d;border-radius:12px;padding:28px 30px;width:320px;box-shadow:0 8px 30px rgba(0,0,0,.5)}
h1{font-size:19px;margin:0 0 6px} p{color:#9a9ca6;margin:0 0 18px;font-size:13px}
input{width:100%;box-sizing:border-box;background:#1b1c1f;border:1px solid #36373d;color:#eceef3;border-radius:8px;padding:10px 12px;font:inherit}
button{margin-top:12px;width:100%;background:#2ea8ff;color:#04121f;border:0;border-radius:8px;padding:10px;font-weight:700;cursor:pointer}
.err{color:#ff7a90;font-size:13px;margin-top:10px}</style></head><body>
<form method="post"><h1>Drone Airspace Map</h1><p>Enter the shared password to continue.</p>
<input type="password" name="password" autofocus autocomplete="current-password" required>
<button type="submit">Open map</button>{err}</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        expected = os.environ.get("MAP_PASSWORD", "")
        if expected and hmac.compare_digest(request.form.get("password", "").encode(), expected.encode()):
            session.permanent, session["ok"] = True, True
            return redirect(url_for("serve", path="index.html"))
        time.sleep(1.5)                                   # slow down guessing
        err = '<div class="err">Wrong password.</div>' if expected else '<div class="err">MAP_PASSWORD is not set on the server.</div>'
    return LOGIN.replace("{err}", err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def serve(path):
    if not session.get("ok"):
        return redirect(url_for("login"))
    if not (SITE / path).resolve().is_relative_to(SITE):
        abort(404)
    return send_from_directory(SITE, path)


if __name__ == "__main__":
    app.config["SESSION_COOKIE_SECURE"] = False           # plain http locally
    app.run(port=5000)
