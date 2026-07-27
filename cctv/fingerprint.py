"""Identify a camera's vendor/model from just IP + credentials.

Strategy, best signal first:
  1. ONVIF ``GetDeviceInformation`` (manufacturer + model) and ``GetStreamUri``
     (a ready-to-use RTSP URL) — most cameras support this.
  2. HTTP fingerprinting: ``Server`` header, 401 ``WWW-Authenticate`` realm and
     the page ``<title>`` matched against a vendor signature table.
  3. RTSP ``OPTIONS`` ``Server`` header.

ONVIF support is optional at import time; if ``onvif-zeep`` is missing we simply
skip step 1 and rely on HTTP/RTSP fingerprinting.
"""

from __future__ import annotations

import re
import socket
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from .models import CameraInput, FingerprintResult
from .util import get_logger

log = get_logger("cctv.fingerprint")

try:  # ONVIF is optional
    from onvif import ONVIFCamera  # type: ignore
    _HAS_ONVIF = True
except Exception:  # pragma: no cover
    _HAS_ONVIF = False

ONVIF_PORTS = [80, 8000, 8899, 2020, 8080]

# Substrings (lowercased) -> canonical vendor name. Matched against Server
# header, WWW-Authenticate realm and page title.
VENDOR_SIGNATURES = {
    "hikvision": "Hikvision", "dvrdvs": "Hikvision", "dnvrs-webs": "Hikvision",
    "dahua": "Dahua", "webs": "Dahua",
    "axis": "Axis",
    "amcrest": "Amcrest",
    "reolink": "Reolink",
    "foscam": "Foscam",
    "vivotek": "Vivotek",
    "mobotix": "Mobotix",
    "bosch": "Bosch",
    "samsung": "Samsung", "hanwha": "Hanwha",
    "sony": "Sony",
    "panasonic": "Panasonic",
    "ubiquiti": "Ubiquiti", "unifi": "Ubiquiti",
    "d-link": "D-Link", "dlink": "D-Link",
    "tp-link": "Tp-link", "tplink": "Tp-link", "tapo": "Tp-link",
    "trendnet": "Trendnet",
    "acti": "Acti",
    "geovision": "Geovision",
    "wyze": "Wyze",
    "lorex": "Lorex",
    "swann": "Swann",
    "uniview": "Uniview",
}


def _match_signature(*texts: Optional[str]) -> Optional[str]:
    blob = " ".join(t.lower() for t in texts if t)
    for needle, vendor in VENDOR_SIGNATURES.items():
        if needle in blob:
            return vendor
    return None


def _onvif_probe(cam: CameraInput, fp: FingerprintResult) -> None:
    if not _HAS_ONVIF:
        fp.add("onvif: library not installed")
        return
    ports = ([cam.http_port] if cam.http_port else []) + ONVIF_PORTS
    tried: set[int] = set()
    for port in ports:
        if not port or port in tried:
            continue
        tried.add(port)
        try:
            device = ONVIFCamera(cam.ip, port, cam.username, cam.password)
            info = device.devicemgmt.GetDeviceInformation()
            fp.vendor = fp.vendor or (info.Manufacturer or "").strip() or None
            fp.model = fp.model or (info.Model or "").strip() or None
            fp.confidence = "high"
            fp.add(f"onvif@{port}: {fp.vendor} {fp.model}")
            # Try to get a direct stream URI.
            try:
                media = device.create_media_service()
                profiles = media.GetProfiles()
                if profiles:
                    token = profiles[0].token
                    req = media.create_type("GetStreamUri")
                    req.ProfileToken = token
                    req.StreamSetup = {
                        "Stream": "RTP-Unicast",
                        "Transport": {"Protocol": "RTSP"},
                    }
                    uri = media.GetStreamUri(req)
                    if uri and getattr(uri, "Uri", None):
                        fp.direct_uri = uri.Uri
                        fp.add(f"onvif stream uri: {uri.Uri}")
            except Exception as exc:  # media service optional
                fp.add(f"onvif media failed: {exc}")
            return
        except Exception as exc:
            log.debug("onvif %s:%s failed: %s", cam.ip, port, exc)
    fp.add("onvif: no response on tried ports")


def _http_probe(cam: CameraInput, fp: FingerprintResult) -> None:
    port = cam.http_port or 80
    scheme = "https" if port == 443 else "http"
    url = f"{scheme}://{cam.ip}:{port}/"
    for auth in (None, HTTPBasicAuth(cam.username, cam.password),
                 HTTPDigestAuth(cam.username, cam.password)):
        try:
            resp = requests.get(url, auth=auth, timeout=6, verify=False,
                                allow_redirects=True)
        except requests.RequestException as exc:
            log.debug("http %s failed: %s", url, exc)
            continue
        server = resp.headers.get("Server", "")
        realm = resp.headers.get("WWW-Authenticate", "")
        title = ""
        m = re.search(r"<title>(.*?)</title>", resp.text or "", re.I | re.S)
        if m:
            title = m.group(1).strip()
        vendor = _match_signature(server, realm, title)
        if server or realm or title:
            fp.add(f"http@{port}: server='{server}' realm='{realm[:40]}' title='{title[:40]}'")
        if vendor and not fp.vendor:
            fp.vendor = vendor
            fp.confidence = fp.confidence if fp.confidence == "high" else "medium"
            fp.add(f"http signature -> {vendor}")
        if resp.status_code not in (401, 403):
            break  # got a real page; no point retrying with other auth


def _rtsp_probe(cam: CameraInput, fp: FingerprintResult) -> None:
    port = cam.rtsp_port or 554
    try:
        with socket.create_connection((cam.ip, port), timeout=5) as sock:
            req = (f"OPTIONS rtsp://{cam.ip}:{port} RTSP/1.0\r\n"
                   f"CSeq: 1\r\n\r\n")
            sock.sendall(req.encode())
            data = sock.recv(2048).decode("latin-1", "replace")
        m = re.search(r"(?im)^Server:\s*(.+)$", data)
        server = m.group(1).strip() if m else ""
        if server:
            fp.add(f"rtsp@{port}: server='{server}'")
            vendor = _match_signature(server)
            if vendor and not fp.vendor:
                fp.vendor = vendor
                fp.confidence = fp.confidence if fp.confidence == "high" else "low"
                fp.add(f"rtsp signature -> {vendor}")
    except OSError as exc:
        log.debug("rtsp %s:%s failed: %s", cam.ip, port, exc)


def fingerprint(cam: CameraInput) -> FingerprintResult:
    """Run the full identification chain against one camera."""
    fp = FingerprintResult()
    _onvif_probe(cam, fp)
    if fp.vendor is None or fp.direct_uri is None:
        # suppress noisy urllib3 warnings for self-signed camera certs
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass
        _http_probe(cam, fp)
    if fp.vendor is None:
        _rtsp_probe(cam, fp)
    log.info("[%s] fingerprint: vendor=%s model=%s conf=%s%s",
             cam.label(), fp.vendor, fp.model, fp.confidence,
             " (+direct uri)" if fp.direct_uri else "")
    return fp
