"""Query layer over the scraped ispyconnect CSVs."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .models import TemplateRow
from .util import get_logger, normalize_key

log = get_logger("cctv.db")

# Last-resort RTSP/HTTP paths tried when the vendor/model is unknown. Ordered by
# real-world prevalence across the ispyconnect database.
GENERIC_PATHS: list[tuple[str, str]] = [
    ("rtsp", "/Streaming/Channels/101"),            # Hikvision & clones
    ("rtsp", "/cam/realmonitor?channel=1&subtype=0"),  # Dahua & clones
    ("rtsp", "/live"),
    ("rtsp", "/live/ch0"),
    ("rtsp", "/live/ch00_0"),
    ("rtsp", "/h264/ch1/main/av_stream"),
    ("rtsp", "/onvif1"),
    ("rtsp", "/11"),
    ("rtsp", "/stream1"),
    ("rtsp", "/video1"),
    ("rtsp", "/media/video1"),
    ("rtsp", "/user=[USERNAME]_password=[PASSWORD]_channel=1_stream=0.sdp"),
    ("rtsp", "/ISAPI/Streaming/Channels/101"),
    ("http", "/video.mjpg"),
    ("http", "/mjpg/video.mjpg"),
    ("http", "/videostream.cgi?user=[USERNAME]&pwd=[PASSWORD]"),
]

# Generic credential pairs tried (with --try-defaults) after any vendor-specific
# default, for cameras whose vendor isn't in the curated list. Ordered by
# real-world prevalence. Empty string = blank password.
COMMON_DEFAULTS: list[tuple[str, str]] = [
    ("admin", "admin"),
    ("admin", "12345"),
    ("admin", "123456"),
    ("admin", ""),
    ("admin", "password"),
    ("root", "pass"),
    ("root", "root"),
    ("admin", "888888"),
]


class CameraDB:
    def __init__(self):
        self._by_vendor: dict[str, list[TemplateRow]] = defaultdict(list)
        self._by_vendor_model: dict[tuple[str, str], list[TemplateRow]] = defaultdict(list)
        # normalized vendor key -> ordered list of (username, password) pairs
        self._defaults: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.loaded = False

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, cameras_csv: str = "data/cameras.csv",
             creds_csv: str = "data/default_credentials.csv") -> "CameraDB":
        db = cls()
        cam_path = Path(cameras_csv)
        if not cam_path.exists():
            log.warning("Camera DB not found at %s — run `scrape` first.", cameras_csv)
            return db
        with open(cam_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    port = int(row.get("port") or 0)
                except ValueError:
                    port = 0
                tr = TemplateRow(
                    vendor=row["vendor"], model=row["model"],
                    conn=(row.get("type") or "").upper(),
                    protocol=(row.get("protocol") or "http").lower(),
                    port=port, path=row["url"],
                )
                vk = normalize_key(tr.vendor)
                db._by_vendor[vk].append(tr)
                db._by_vendor_model[(vk, normalize_key(tr.model))].append(tr)
        creds_path = Path(creds_csv)
        if creds_path.exists():
            with open(creds_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = normalize_key(row["vendor"])
                    pair = (row.get("default_username", ""),
                            row.get("default_password", ""))
                    if pair not in db._defaults[key]:
                        db._defaults[key].append(pair)
        db.loaded = True
        log.info("Loaded DB: %d vendors, %d template rows, %d default-cred entries.",
                 len(db._by_vendor),
                 sum(len(v) for v in db._by_vendor.values()),
                 len(db._defaults))
        return db

    # -- queries ----------------------------------------------------------
    @staticmethod
    def _rank(rows: list[TemplateRow]) -> list[TemplateRow]:
        """Streams first (best for live view), then by protocol preference."""
        proto_pref = {"rtsp": 0, "http": 1, "https": 2}
        return sorted(rows, key=lambda r: (not r.is_stream,
                                           proto_pref.get(r.protocol, 3)))

    def templates_for(self, vendor: Optional[str],
                      model: Optional[str] = None) -> list[TemplateRow]:
        """Candidate templates, most-likely first.

        Exact vendor+model matches rank ahead of vendor-wide (or wildcard)
        templates. De-duplicated on (protocol, port, path).
        """
        if not vendor:
            return []
        vk = normalize_key(vendor)
        ordered: list[TemplateRow] = []
        if model:
            ordered.extend(self._by_vendor_model.get((vk, normalize_key(model)), []))
        # vendor-wide (includes wildcard '*' rows and every model's rows)
        ordered.extend(self._by_vendor.get(vk, []))

        result, seen = [], set()
        for r in self._rank(ordered):
            key = (r.protocol, r.port, r.path)
            if key not in seen:
                seen.add(key)
                result.append(r)
        return result

    def default_creds_for(self, vendor: Optional[str],
                          model: Optional[str] = None) -> list[tuple[str, str]]:
        """Ordered default credential pairs to try for a vendor.

        A known vendor returns its curated + scraped pairs (curated first). An
        unknown/unmatched vendor falls back to the common generic pairs.
        """
        pairs = list(self._defaults.get(normalize_key(vendor), [])) if vendor else []
        return pairs if pairs else list(COMMON_DEFAULTS)

    @staticmethod
    def generic_templates() -> list[TemplateRow]:
        return [TemplateRow(vendor="", model="*", conn="FFMPEG",
                            protocol=proto, port=0, path=path)
                for proto, path in GENERIC_PATHS]

    def known_vendor(self, vendor: Optional[str]) -> bool:
        return bool(vendor) and normalize_key(vendor) in self._by_vendor
