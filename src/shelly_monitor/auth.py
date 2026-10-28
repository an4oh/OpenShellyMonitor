"""Optional password login of the web dashboard (stdlib only).

Enabled by SHELLY_WEB_PASSWORD (plain text, hashed with scrypt at startup) or
SHELLY_WEB_PASSWORD_HASH (`scrypt$N$r$p$<salt_hex>$<hash_hex>`). Sessions are stateless signed
cookies `tag.exp.sig`: `exp` is the expiry (epoch seconds), `tag` an HMAC of the password
material (a new password invalidates every cookie), `sig` an HMAC-SHA256 of both with
SHELLY_WEB_COOKIE_SECRET (hex; random at startup when unset, so sessions end at every restart).

The password, cookie and page primitives mirror deploy/public/auth_server.py (standalone sidecar
for nginx): keep the two aligned.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import os
import re
import threading
import time
from collections import deque

from .config import ConfigError

COOKIE_NAME = "sm_session"
FAIL_DELAY = 1.0
MAX_BODY = 4096
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
MAX_FAILURES, FAIL_WINDOW = 10, 600  # failed logins per client IP in the window -> 429
MIN_SECRET_BYTES = 16


# --- password -------------------------------------------------------------
def hash_password(password: str, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes]:
    """(n, r, p, salt, hash) of a `scrypt$N$r$p$salt$hash` string; ValueError if malformed."""
    scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
    if scheme != "scrypt":
        raise ValueError("not an scrypt hash")
    n_, r_, p_ = int(n), int(r), int(p)
    salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    if n_ < 2 or n_ & (n_ - 1) or r_ < 1 or p_ < 1 or not salt or len(expected) < 16:
        raise ValueError("invalid scrypt parameters")
    return n_, r_, p_, salt, expected


def verify_password(password: str, stored: str) -> bool:
    try:
        n, r, p, salt, expected = parse_hash(stored)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024,
                            dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# --- cookie ---------------------------------------------------------------
def _sig(secret: bytes, tag: str, exp: str) -> str:
    return hmac.new(secret, f"v2.{tag}.{exp}".encode(), hashlib.sha256).hexdigest()


def password_tag(secret: bytes, material: str) -> str:
    """Short keyed digest of the password material: binds cookies to the current password
    without exposing anything that can be brute forced offline."""
    return hmac.new(secret, b"pw." + material.encode(), hashlib.sha256).hexdigest()[:16]


def make_cookie_value(secret: bytes, tag: str, ttl: int, now: float | None = None) -> str:
    exp = str(int((time.time() if now is None else now) + ttl))
    return f"{tag}.{exp}.{_sig(secret, tag, exp)}"


def verify_cookie_value(secret: bytes, tag: str, value: str | None, now: float | None = None) -> bool:
    if not value or value.count(".") != 2:
        return False
    got_tag, exp, sig = value.split(".")
    if not exp.isdigit() or len(exp) > 12:
        return False
    if not hmac.compare_digest(got_tag, tag) or not hmac.compare_digest(sig, _sig(secret, tag, exp)):
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

WRONG_PASSWORD = "Wrong password. Please try again."
TOO_MANY = "Too many failed attempts. Please wait a few minutes and try again."


def _csp_hash(block: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(block.encode()).digest()).decode() + "'"


def login_csp() -> str:
    """CSP of the login page: inline script/style allowed only by hash, never 'unsafe-inline'."""
    scripts = [_csp_hash(m) for m in re.findall(r"<script>(.*?)</script>", PAGE, re.S)]
    styles = [_csp_hash(m) for m in re.findall(r"<style>(.*?)</style>", PAGE, re.S)]
    return ("default-src 'none'; script-src " + " ".join(scripts) + "; style-src " + " ".join(styles)
            + "; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'")


def render_page(next_url: str, error: str | None = None) -> bytes:
    err = f'<p class="err" id="err" role="alert">{html.escape(error)}</p>' if error else ""
    desc = ' aria-describedby="err" aria-invalid="true"' if error else ""
    out = (PAGE.replace("__ERROR__", err).replace("__DESCRIBED__", desc)
           .replace("__NEXT__", html.escape(next_url, quote=True)))
    return out.encode()


# --- rate limit -----------------------------------------------------------
class RateLimiter:
    """Failed logins per client in a sliding window, in memory (reset at restart)."""

    MAX_CLIENTS = 10_000

    def __init__(self, max_failures: int = MAX_FAILURES, window: float = FAIL_WINDOW):
        self.max_failures, self.window = max_failures, window
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque:
        q = self._fails.get(key) or deque()
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            return len(self._recent(key, now)) >= self.max_failures

    def fail(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            if len(self._fails) >= self.MAX_CLIENTS:  # bound memory: drop clients with no recent failure
                for k in [k for k, q in self._fails.items() if not self._recent(k, now)]:
                    del self._fails[k]
            q = self._recent(key, now)
            q.append(now)
            self._fails[key] = q


# --- configuration --------------------------------------------------------
def _flag(env, name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


class WebAuth:
    """Login state of one dashboard process."""

    def __init__(self, pw_hash: str, secret: bytes, session_days: int = 7, trust_proxy: bool = False,
                 secure_cookie: bool = False, fail_delay: float = FAIL_DELAY, material: str | None = None):
        self.pw_hash = pw_hash
        self.secret = secret
        self.session_seconds = int(session_days) * 86400
        self.trust_proxy = trust_proxy
        self.secure_cookie = secure_cookie
        self.fail_delay = fail_delay
        self.tag = password_tag(secret, pw_hash if material is None else material)
        self.limiter = RateLimiter()
        self._revoked: dict[str, int] = {}  # cookie value -> expiry (logout), in memory
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env=os.environ, scrypt_params: dict | None = None) -> "WebAuth | None":
        """None when no password is configured. Raises ConfigError on invalid values."""
        plain = env.get("SHELLY_WEB_PASSWORD") or ""
        stored = (env.get("SHELLY_WEB_PASSWORD_HASH") or "").strip()
        if not plain and not stored:
            return None
        if plain and stored:
            raise ConfigError("set only one of SHELLY_WEB_PASSWORD and SHELLY_WEB_PASSWORD_HASH")
        if stored:
            try:
                parse_hash(stored)
            except (ValueError, TypeError):
                raise ConfigError("SHELLY_WEB_PASSWORD_HASH must be scrypt$N$r$p$<salt_hex>$<hash_hex>") from None
        raw_secret = (env.get("SHELLY_WEB_COOKIE_SECRET") or "").strip()
        if raw_secret:
            try:
                secret = bytes.fromhex(raw_secret)
            except ValueError:
                raise ConfigError("SHELLY_WEB_COOKIE_SECRET must be hexadecimal") from None
            if len(secret) < MIN_SECRET_BYTES:
                raise ConfigError(f"SHELLY_WEB_COOKIE_SECRET must be at least {MIN_SECRET_BYTES} bytes "
                                  f"({2 * MIN_SECRET_BYTES} hex characters)")
        else:
            secret = os.urandom(32)
        raw_days = (env.get("SHELLY_WEB_SESSION_DAYS") or "7").strip()
        try:
            days = int(raw_days)
        except ValueError:
            days = 0
        if not 1 <= days <= 365:
            raise ConfigError(f"SHELLY_WEB_SESSION_DAYS must be an integer from 1 to 365 (got {raw_days!r})")
        if plain:
            # The salt is random at every start: bind cookies to the password itself (through the
            # keyed tag), so that sessions survive a restart when the cookie secret is fixed.
            pw_hash, material = hash_password(plain, **(scrypt_params or {})), "plain:" + plain
        else:
            pw_hash, material = stored, stored
        return cls(pw_hash, secret, days, trust_proxy=_flag(env, "SHELLY_WEB_TRUST_PROXY"),
                   secure_cookie=_flag(env, "SHELLY_WEB_SECURE_COOKIE"), material=material)

    def check(self, password: str) -> bool:
        return bool(password) and verify_password(password, self.pw_hash)

    def issue(self, now: float | None = None) -> str:
        return make_cookie_value(self.secret, self.tag, self.session_seconds, now)

    def valid(self, value: str | None, now: float | None = None) -> bool:
        if not verify_cookie_value(self.secret, self.tag, value, now):
            return False
        with self._lock:
            return value not in self._revoked

    def revoke(self, value: str | None) -> None:
        if not self.valid(value):
            return
        now = int(time.time())
        with self._lock:
            for k in [k for k, exp in self._revoked.items() if exp <= now]:
                del self._revoked[k]
            self._revoked[value] = int(value.split(".")[1])
