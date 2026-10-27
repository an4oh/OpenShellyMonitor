#!/usr/bin/env python3
"""Password login sidecar for a Shelly Monitor dashboard behind nginx (stdlib only).

Secrets (never in the repo) live in ~/.config/shelly-monitor/auth.env (override: SHELLY_AUTH_ENV):
  SHELLY_AUTH_PW_HASH=scrypt$N$r$p$<salt_hex>$<hash_hex>
  SHELLY_AUTH_COOKIE_SECRET=<hex>
Written by set-password.py. Binds 127.0.0.1 (port: SHELLY_AUTH_PORT, default 18097);
nginx calls /_auth via auth_request and proxies /login and /logout here.
"""
import hashlib
import hmac
import base64
import html
import re
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

COOKIE_NAME = "sm_session"
SESSION_SECONDS = 7 * 24 * 3600
FAIL_DELAY = 1.0
MAX_BODY = 4096
DEFAULT_ENV = Path.home() / ".config/shelly-monitor/auth.env"
DEFAULT_PORT = 18097
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1


# --- password -------------------------------------------------------------
def hash_password(password: str, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r),
                            p=int(p), maxmem=128 * 1024 * 1024, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# --- cookie ---------------------------------------------------------------
def _sig(secret: bytes, exp: str) -> str:
    return hmac.new(secret, b"v1." + exp.encode(), hashlib.sha256).hexdigest()


def make_cookie_value(secret: bytes, now: float | None = None, ttl: int = SESSION_SECONDS) -> str:
    exp = str(int((time.time() if now is None else now) + ttl))
    return f"{exp}.{_sig(secret, exp)}"


def verify_cookie_value(secret: bytes, value: str | None, now: float | None = None) -> bool:
    if not value or value.count(".") != 1:
        return False
    exp, sig = value.split(".")
    if not exp.isdigit() or len(exp) > 12:
        return False
    if not hmac.compare_digest(sig, _sig(secret, exp)):
        return False
    return int(exp) > (time.time() if now is None else now)


def get_cookie(header: str | None, name: str = COOKIE_NAME) -> str | None:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


# --- next -----------------------------------------------------------------
def sanitize_next(value: str | None) -> str:
    """Only same-origin absolute paths; anything else falls back to "/" (no open redirect)."""
    if not value or len(value) > 2048:
        return "/"
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value):
        return "/"
    return value


# --- page -----------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="color-scheme" content="dark">
<title>Sign in · Shelly Monitor</title>
<style>
:root{--bg:#0f1216;--raised:#161b22;--text:#e6e9ee;--muted:#9aa4b2;--border:#2d343e;--accent:#38bdf8;--accent-ink:#06212c;--err-bg:#3a1d12;--err:#fb923c;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;color-scheme:dark}
body{margin:0;min-height:100vh;min-height:100dvh;display:flex;flex-direction:column;background:var(--bg);color:var(--text);font:1rem/1.6 var(--sans);-webkit-font-smoothing:antialiased}
header{padding:1rem 1.25rem;border-bottom:1px solid var(--border)}
.brand{font-weight:700;font-size:1.05rem}
main{flex:1;display:flex;align-items:center;justify-content:center;padding:1.5rem 1rem}
.card{width:100%;max-width:26rem;background:var(--raised);border:1px solid var(--border);border-top:3px solid var(--accent);border-radius:8px;padding:1.75rem 1.5rem}
h1{font-weight:700;font-size:1.6rem;line-height:1.15;margin:0 0 .5rem}
p{margin:0 0 1.25rem;color:var(--muted)}
label{display:block;font-weight:600;margin-bottom:.35rem}
.field{display:flex;gap:.5rem}
input[type=password],input[type=text]{flex:1;min-width:0;min-height:44px;padding:.55rem .75rem;font:inherit;color:var(--text);background:var(--bg);border:1.5px solid var(--border);border-radius:6px}
button{min-height:44px;font:inherit;font-weight:600;border-radius:6px;cursor:pointer}
.toggle{padding:0 .85rem;color:var(--text);background:transparent;border:1.5px solid var(--border)}
.toggle[hidden]{display:none}
.go{width:100%;margin-top:1.1rem;color:var(--accent-ink);background:var(--accent);border:1.5px solid var(--accent)}
.go:hover{filter:brightness(1.08)}
:focus-visible{outline:2.5px solid var(--accent);outline-offset:2px;border-radius:3px}
.err{margin:0 0 1rem;padding:.65rem .8rem;color:var(--text);background:var(--err-bg);border:1px solid var(--err);border-radius:6px;font-weight:600}
</style>
</head>
<body>
<header><span class="brand">Shelly Monitor</span></header>
<main>
<section class="card">
<h1>Sign in</h1>
<p>This dashboard is private. Enter the password to continue.</p>
__ERROR__
<form method="post" action="/login">
<input type="hidden" name="next" value="__NEXT__">
<label for="password">Password</label>
<div class="field">
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus__DESCRIBED__>
<button type="button" class="toggle" id="toggle" aria-controls="password" aria-pressed="false" hidden>Show</button>
</div>
<button type="submit" class="go">Sign in</button>
</form>
</section>
</main>
<script>
(function(){var b=document.getElementById("toggle"),i=document.getElementById("password");if(!b||!i)return;b.hidden=false;
b.addEventListener("click",function(){var show=i.type==="password";i.type=show?"text":"password";b.textContent=show?"Hide":"Show";b.setAttribute("aria-pressed",show?"true":"false")})})();
</script>
</body>
</html>
"""


def _csp_hash(block: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(block.encode()).digest()).decode() + "'"


def login_csp() -> str:
    """CSP of the login page: inline script/style allowed only by hash, never 'unsafe-inline'."""
    scripts = [_csp_hash(m) for m in re.findall(r"<script>(.*?)</script>", PAGE, re.S)]
    styles = [_csp_hash(m) for m in re.findall(r"<style>(.*?)</style>", PAGE, re.S)]
    return ("default-src 'none'; script-src " + " ".join(scripts) + "; style-src " + " ".join(styles)
            + "; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'")


def render_page(next_url: str, error: bool = False) -> bytes:
    err = '<p class="err" id="err" role="alert">Wrong password. Please try again.</p>' if error else ""
    desc = ' aria-describedby="err" aria-invalid="true"' if error else ""
    out = (PAGE.replace("__ERROR__", err).replace("__DESCRIBED__", desc)
           .replace("__NEXT__", html.escape(next_url, quote=True)))
    return out.encode()


# --- server ---------------------------------------------------------------
def load_env(path: Path) -> dict:
    cfg = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip("'\"")
    return cfg


def make_handler(pw_hash: str, secret: bytes, fail_delay: float = FAIL_DELAY):
    class Handler(BaseHTTPRequestHandler):
        server_version = "auth"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no query string or body in the logs
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args if args else fmt))

        def _send(self, status, body=b"", headers=(), ctype="text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _cookie(self, value, max_age):
            return ("Set-Cookie", f"{COOKIE_NAME}={value}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={max_age}")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlsplit(self.path)
            if u.path == "/_auth":
                ok = verify_cookie_value(secret, get_cookie(self.headers.get("Cookie")))
                return self._send(204 if ok else 401)
            if u.path == "/login":
                # nginx appends the unescaped $request_uri: everything after "next=" is the path
                nxt = sanitize_next(u.query[5:] if u.query.startswith("next=") else "")
                return self._send(200, render_page(nxt), headers=[("Content-Security-Policy", login_csp())])
            self._send(404, b"", ctype="text/plain")

        def do_POST(self):
            u = urlsplit(self.path)
            if u.path == "/logout":
                self._drain()
                return self._send(303, headers=[("Location", "/login"), self._cookie("", 0)])
            if u.path != "/login":
                return self._send(404, b"", ctype="text/plain")
            form = parse_qs(self._drain().decode("utf-8", "replace"))
            password = (form.get("password") or [""])[0]
            nxt = sanitize_next((form.get("next") or [""])[0])
            if password and verify_password(password, pw_hash):
                return self._send(303, headers=[("Location", nxt),
                                                self._cookie(make_cookie_value(secret), SESSION_SECONDS)])
            time.sleep(fail_delay)
            self._send(401, render_page(nxt, error=True),
                       headers=[("Content-Security-Policy", login_csp())])

        def _drain(self) -> bytes:
            try:
                n = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            except ValueError:
                n = 0
            return self.rfile.read(n) if n > 0 else b""

    return Handler


def make_server(host, port, pw_hash, secret, fail_delay=FAIL_DELAY):
    return ThreadingHTTPServer((host, port), make_handler(pw_hash, secret, fail_delay))


def main():
    env_path = Path(os.environ.get("SHELLY_AUTH_ENV", DEFAULT_ENV)).expanduser()
    cfg = load_env(env_path)
    secret = bytes.fromhex(cfg["SHELLY_AUTH_COOKIE_SECRET"])
    port = int(os.environ.get("SHELLY_AUTH_PORT", str(DEFAULT_PORT)))
    srv = make_server("127.0.0.1", port, cfg["SHELLY_AUTH_PW_HASH"], secret)
    print(f"shelly-monitor auth listening on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
