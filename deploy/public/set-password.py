#!/usr/bin/env python3
"""Write the login secrets file (default ~/.config/shelly-monitor/auth.env, override: SHELLY_AUTH_ENV).

Prompts for the password (getpass, twice), stores only its scrypt hash plus a fresh cookie secret,
creates the folder (700) and the file (600). Re-running it rotates the cookie secret, which
invalidates every open session. The password is never printed.
"""
import getpass
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth_server import DEFAULT_ENV, hash_password  # noqa: E402

MIN_LENGTH = 12


def main():
    out = Path(os.environ.get("SHELLY_AUTH_ENV", DEFAULT_ENV)).expanduser()
    pw = getpass.getpass("New password: ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match")
    if len(pw) < MIN_LENGTH:
        sys.exit(f"Password too short (min {MIN_LENGTH})")
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"SHELLY_AUTH_PW_HASH={hash_password(pw)}\nSHELLY_AUTH_COOKIE_SECRET={secrets.token_hex(32)}\n")
    os.chmod(out, 0o600)
    print(f"Wrote {out} (600). Restart the auth service to load it.")


if __name__ == "__main__":
    main()
