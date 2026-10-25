"""Hermess Desktop ownership rules for its private Hermes HTTP/WS listener."""

import os


def enabled() -> bool:
    return os.environ.get("HERMESS_DESKTOP_PRIVATE_API") == "1"


def require_loopback_host(host: str) -> None:
    if enabled() and host != "127.0.0.1":
        raise SystemExit("Private desktop API must bind to 127.0.0.1")


def withhold_token(other_reason: bool) -> bool:
    return other_reason or enabled()


def reject_browser_origin(origin: str) -> bool:
    return enabled() and bool(origin)
