"""Shared dataclasses used across the CCTV Viewer package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TemplateRow:
    """One connection-URL template scraped from ispyconnect.

    ``path`` still contains placeholder tokens such as ``[USERNAME]``,
    ``[PASSWORD]``, ``[CHANNEL]``, ``[WIDTH]`` and ``[HEIGHT]``.
    ``port`` of 0 means "use the protocol default" (554 rtsp / 80 http).
    """

    vendor: str
    model: str
    conn: str            # JPEG | MJPEG | FFMPEG | VLC
    protocol: str        # rtsp | http
    port: int            # 0 => protocol default
    path: str            # url path template with [TOKEN] placeholders

    @property
    def is_stream(self) -> bool:
        """True for continuous video (preferred for live viewing)."""
        return self.conn in ("FFMPEG", "MJPEG", "VLC")


@dataclass
class CameraInput:
    """A single row of the user-supplied camera list (credentials only)."""

    name: str
    ip: str
    username: str
    password: str
    http_port: Optional[int] = None
    rtsp_port: Optional[int] = None
    channel: int = 1

    def label(self) -> str:
        return self.name or self.ip


@dataclass
class FingerprintResult:
    """Outcome of identifying a camera from IP + credentials."""

    vendor: Optional[str] = None
    model: Optional[str] = None
    direct_uri: Optional[str] = None      # e.g. ONVIF GetStreamUri result (no creds)
    confidence: str = "none"              # high | medium | low | none
    evidence: list = field(default_factory=list)

    def add(self, note: str) -> None:
        self.evidence.append(note)


@dataclass
class ResolvedCamera:
    """A camera after the resolution pipeline has run."""

    name: str
    ip: str
    vendor: str = ""
    model: str = ""
    protocol: str = ""
    working_url: str = ""      # full url including credentials (sensitive!)
    status: str = "UNRESOLVED"  # OK | UNRESOLVED | UNREACHABLE
    method: str = ""            # how it was resolved (onvif / db / generic / defaults)
    channels: int = 0           # live channels in this family (0 = uncounted)

    @property
    def ok(self) -> bool:
        return self.status == "OK" and bool(self.working_url)

    def label(self) -> str:
        return self.name or self.ip

    def redacted_url(self) -> str:
        """Working URL with credentials masked for display/logging."""
        import re
        return re.sub(r"://[^@/]+@", "://****:****@", self.working_url)
