"""Small shared helpers: logging, URL assembly and placeholder substitution."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

_LOG_CONFIGURED = False


def get_logger(name: str = "cctv") -> logging.Logger:
    """Return a package logger, configuring a console handler once."""
    global _LOG_CONFIGURED
    if not _LOG_CONFIGURED:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )
        root = logging.getLogger("cctv")
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        root.propagate = False  # avoid duplicate lines via root's lastResort handler
        _LOG_CONFIGURED = True
    return logging.getLogger(name)


def set_verbose(verbose: bool) -> None:
    logging.getLogger("cctv").setLevel(logging.DEBUG if verbose else logging.INFO)


# Default port per protocol when the DB row reports port 0 ("unspecified").
DEFAULT_PORTS = {"rtsp": 554, "http": 80, "https": 443}

# Sensible defaults substituted for stream-parameter placeholders.
_DEFAULT_TOKENS = {
    "CHANNEL": "1",
    "WIDTH": "1280",
    "HEIGHT": "720",
    "STREAM": "0",
}


def normalize_protocol(proto: str) -> str:
    """'rtsp://' | 'http://' -> 'rtsp' | 'http'."""
    return (proto or "").replace("://", "").strip().lower() or "http"


def substitute(path: str, username: str, password: str,
               channel: int = 1, width: int = 1280, height: int = 720) -> str:
    """Replace ``[TOKEN]`` placeholders in a scraped path template.

    Credentials placed *inside the path* (some vendors put them in the query
    string) are URL-encoded. Credentials that go in the authority component are
    handled separately by :func:`build_url`.
    """
    tokens = dict(_DEFAULT_TOKENS)
    tokens["CHANNEL"] = str(channel)
    tokens["WIDTH"] = str(width)
    tokens["HEIGHT"] = str(height)
    tokens["USERNAME"] = quote(username, safe="")
    tokens["PASSWORD"] = quote(password, safe="")

    def repl(match: "re.Match[str]") -> str:
        key = match.group(1).upper()
        return tokens.get(key, match.group(0))

    return re.sub(r"\[([A-Za-z]+)\]", repl, path)


def build_url(protocol: str, ip: str, port: int, path: str,
              username: str = "", password: str = "",
              channel: int = 1, width: int = 1280, height: int = 720,
              embed_credentials: bool = True) -> str:
    """Assemble a full connection URL from a template path.

    ``embed_credentials`` puts ``user:pass@`` in the authority. Even when the
    path also carries credential query params, embedding in the authority is
    harmless and helps RTSP servers that expect it.
    """
    protocol = normalize_protocol(protocol)
    if not port or port == 0:
        port = DEFAULT_PORTS.get(protocol, 80)

    filled_path = substitute(path, username, password, channel, width, height)
    if not filled_path.startswith("/"):
        filled_path = "/" + filled_path

    auth = ""
    if embed_credentials and username:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"

    return f"{protocol}://{auth}{ip}:{port}{filled_path}"


def slug_to_vendor(slug: str) -> str:
    """'hikvision' / 'q-see' -> a human-ish display vendor name."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))


def normalize_key(value: str) -> str:
    """Lowercase alphanumeric key for fuzzy vendor/model matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())
