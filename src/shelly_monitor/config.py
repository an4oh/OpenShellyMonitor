"""Home (Shelly Cloud key) configuration."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass

DEFAULT_HOME_NAME = "home"
REFRESH_REQUESTS = 5  # worst case per home in the hourly cycle: all_status, 3 discovery, get_all_lists


class ConfigError(ValueError):
    pass


def key_id(auth_key: str) -> str:
    """Stable, non-secret identity of a home: first 12 hex chars of sha256(auth_key)."""
    return hashlib.sha256(auth_key.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Home:
    """One cloud key = one Shelly home.

    `name` is the explicit name from the configuration (SHELLY_HOMES, or
    SHELLY_HOME_NAME for the single SHELLY_AUTH_KEY) or None: the name is then
    detected from the cloud, falling back to `fallback`.
    """
    name: str | None
    auth_key: str
    server: str
    fallback: str = DEFAULT_HOME_NAME

    @property
    def key_id(self) -> str:
        return key_id(self.auth_key)

    def __repr__(self) -> str:  # never print the key
        return f"Home(key_id={self.key_id!r}, name={self.name!r}, server={self.server!r})"


def default_home_name(env=os.environ) -> str:
    """Name for the single SHELLY_AUTH_KEY setup and for data migrated from 0.1.x/0.2.x."""
    return (env.get("SHELLY_HOME_NAME") or DEFAULT_HOME_NAME).strip()


def _check_name(name: str) -> None:
    if not name:
        raise ConfigError("home name is empty")
    if "/" in name:
        raise ConfigError(f"home name {name!r} must not contain '/'")


def _split_key(value: str, default_server: str, where: str) -> tuple[str, str]:
    key, _, server = value.partition("@")
    key, server = key.strip(), (server.strip() or default_server)
    if not key:
        raise ConfigError(f"{where}: empty auth key")
    if not server:
        raise ConfigError(f"{where}: no server (add '@server' or set SHELLY_SERVER)")
    return key, server


def parse_homes(env=os.environ) -> list[Home]:
    """Homes from SHELLY_AUTH_KEY, SHELLY_AUTH_KEYS ("key1[@server];key2") and
    SHELLY_HOMES ("Name=key[@server];Other=key2"), in this order; at least one is required.

    A key listed more than once is one home; a name from SHELLY_HOMES (or
    SHELLY_HOME_NAME for SHELLY_AUTH_KEY) is explicit, otherwise the name is
    detected from the cloud. Raises ConfigError with a message meant for the user.
    """
    default_server = (env.get("SHELLY_SERVER") or "").strip()
    entries: list[tuple[str | None, str, str]] = []  # (explicit name, key, server)

    single = (env.get("SHELLY_AUTH_KEY") or "").strip()
    if single:
        key, server = _split_key(single, default_server, "SHELLY_AUTH_KEY")
        explicit = (env.get("SHELLY_HOME_NAME") or "").strip() or None
        entries.append((explicit, key, server))

    for i, part in enumerate((env.get("SHELLY_AUTH_KEYS") or "").split(";"), 1):
        if part.strip():
            entries.append((None, *_split_key(part, default_server, f"SHELLY_AUTH_KEYS entry {i}")))

    for i, part in enumerate((env.get("SHELLY_HOMES") or "").split(";"), 1):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        if not sep:
            raise ConfigError(f"SHELLY_HOMES entry {i}: expected 'Name=auth_key[@server]'")
        name = name.strip()
        _check_name(name)
        entries.append((name, *_split_key(value, default_server, f"SHELLY_HOMES home {name!r}")))

    if not entries:
        raise ConfigError("set SHELLY_AUTH_KEYS (or SHELLY_AUTH_KEY, or SHELLY_HOMES) and SHELLY_SERVER")

    merged: dict[str, list] = {}
    for explicit, key, server in entries:
        kid = key_id(key)
        if kid in merged:
            if explicit:
                merged[kid][0] = explicit  # a name for a key already listed: it wins
            continue
        merged[kid] = [explicit, key, server]

    seen: set[str] = set()
    for explicit, _, _ in merged.values():
        if explicit:
            _check_name(explicit)
            if explicit.casefold() in seen:
                raise ConfigError(f"duplicate home name {explicit!r}")
            seen.add(explicit.casefold())

    many = len(merged) > 1
    return [Home(explicit, key, server, f"home-{n}" if many else DEFAULT_HOME_NAME)
            for n, (explicit, key, server) in enumerate(merged.values(), 1)]


def request_gap(env=os.environ) -> float:
    """SHELLY_REQUEST_GAP: seconds between two cloud requests (default 5, minimum 1.1)."""
    from .cloud import DEFAULT_REQUEST_GAP, MIN_REQUEST_GAP

    raw = (env.get("SHELLY_REQUEST_GAP") or "").strip()
    if not raw:
        return DEFAULT_REQUEST_GAP
    try:
        gap = float(raw)
    except ValueError:
        raise ConfigError(f"SHELLY_REQUEST_GAP must be a number of seconds (got {raw!r})") from None
    if not (math.isfinite(gap) and gap >= MIN_REQUEST_GAP):
        raise ConfigError(f"SHELLY_REQUEST_GAP must be >= {MIN_REQUEST_GAP} s: the cloud allows "
                          f"at most 1 request/second (got {raw!r})")
    return gap


def event_log_enabled(env=os.environ) -> bool:
    """SHELLY_EVENT_LOG: read the cloud activity log every cycle (default 1; 0 disables)."""
    return (env.get("SHELLY_EVENT_LOG") or "1").strip().lower() not in ("0", "false", "no", "off")


def cycle_warning(homes: int, gap: float, interval: int, per_cycle: int = 1) -> str | None:
    """Warn when a polling cycle cannot fit in the interval.

    A normal cycle makes `per_cycle` requests per home (all_status, plus the
    activity log when enabled); once an hour each home also detects its name
    (2-3 requests) and refreshes names/rooms (1 request).
    """
    normal = homes * gap * per_cycle
    with_lists = (REFRESH_REQUESTS + per_cycle - 1) * homes * gap
    if normal > interval:
        return (f"{homes} home(s) x {per_cycle} request(s) x SHELLY_REQUEST_GAP {gap:g} s = {normal:g} s exceeds "
                f"SHELLY_INTERVAL "
                f"{interval} s: cycles will overrun and polls will be skipped")
    if with_lists > interval:
        return (f"the hourly name/room refresh needs up to {with_lists:g} s ({homes} home(s) x "
                f"{REFRESH_REQUESTS + per_cycle - 1} requests x "
                f"{gap:g} s), more than SHELLY_INTERVAL {interval} s: that cycle will overrun")
    return None
