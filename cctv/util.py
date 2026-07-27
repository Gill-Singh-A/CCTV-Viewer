"""Small shared helpers: logging, URL assembly and placeholder substitution."""

from __future__ import annotations

import logging
import re
import socket
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


def tcp_open(ip: str, port: int, timeout: float = 2.0) -> bool:
    """True if a TCP connection to ip:port can be established quickly."""
    if not port:
        return False
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def reachable(ip: str, ports, timeout: float = 2.0) -> bool:
    """True if any of ``ports`` accepts a TCP connection (host is alive)."""
    for p in ports:
        if tcp_open(ip, p, timeout):
            return True
    return False


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


# Channel handling -----------------------------------------------------------
# Different vendors encode the NVR/DVR channel differently in the stream URL.
# These helpers read and rewrite the channel in place so it can be changed
# live from the viewer and enumerated for bulk export.
_CHANNEL_QUERY_RE = re.compile(r"([?&](?:channel|chn|chID|channelId|ch)=)(\d+)", re.I)
_CHANNEL_HIK_RE = re.compile(r"(/(?:ISAPI/)?[Ss]treaming/[Cc]hannels/)(\d+)")
_CHANNEL_PATH_RE = re.compile(r"(/(?:live/|cam/)?ch)(\d+)\b", re.I)


def channel_of(url: str) -> Optional[int]:
    """Return the channel number encoded in ``url``, or None if it has none."""
    m = _CHANNEL_QUERY_RE.search(url)
    if m:
        return int(m.group(2))
    m = _CHANNEL_HIK_RE.search(url)
    if m:
        num = int(m.group(2))
        return num // 100 if num >= 100 else num
    m = _CHANNEL_PATH_RE.search(url)
    if m:
        return int(m.group(2))
    return None


def rewrite_channel(url: str, channel: int) -> str:
    """Return ``url`` pointing at ``channel``; unchanged if it has no channel.

    Handles query-style (``channel=N``), Hikvision ``Channels/CSS`` (where the
    trailing two digits are the substream) and simple ``/chN`` path styles.
    """
    if _CHANNEL_QUERY_RE.search(url):
        return _CHANNEL_QUERY_RE.sub(lambda m: f"{m.group(1)}{channel}", url)
    m = _CHANNEL_HIK_RE.search(url)
    if m:
        num = int(m.group(2))
        stream = num % 100 if num >= 100 else 1  # preserve main/sub stream
        new = channel * 100 + stream
        return url[:m.start(2)] + str(new) + url[m.end(2):]
    m = _CHANNEL_PATH_RE.search(url)
    if m:
        return url[:m.start(2)] + str(channel) + url[m.end(2):]
    return url


def supports_channel(url: str) -> bool:
    return channel_of(url) is not None


def slug_to_vendor(slug: str) -> str:
    """'hikvision' / 'q-see' -> a human-ish display vendor name."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))


def normalize_key(value: str) -> str:
    """Lowercase alphanumeric key for fuzzy vendor/model matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())
