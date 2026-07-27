"""Turn an input camera list (credentials only) into working stream URLs.

Resolution chain per camera (first validated live frame wins):
  1. ONVIF ``GetStreamUri`` (credentials injected) — from fingerprint.
  2. Vendor + model DB templates.
  3. Vendor-wide DB templates.
  4. Generic common-path probe.
  5. Optional: retry the above with scraped default credentials.
Anything that never yields a frame is marked ``UNRESOLVED`` (or ``UNREACHABLE``).
"""

from __future__ import annotations

import csv
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from .capture import probe_frame
from .db import CameraDB
from .fingerprint import fingerprint
from .models import CameraInput, ResolvedCamera, TemplateRow
from .util import build_url, get_logger, reachable

log = get_logger("cctv.resolver")

RESOLVED_HEADER = ["name", "ip", "vendor", "model", "protocol",
                   "status", "method", "working_url"]


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------
def _int_or_none(value: str) -> Optional[int]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HEADER_TOKENS = {"ip", "username", "password", "name", "http_port",
                  "rtsp_port", "channel", "user", "pass"}
# Positional column order for headerless CSVs (matches the documented header).
_POSITIONAL = ["name", "ip", "username", "password", "http_port",
               "rtsp_port", "channel"]


def _looks_like_header(fields: list[str]) -> bool:
    low = {f.strip().lower() for f in fields}
    return "ip" in low or len(low & _HEADER_TOKENS) >= 2


def _positional_row(fields: list[str]) -> dict:
    """Map a headerless row to the documented columns.

    Supports both ``name,ip,...`` (name first) and ``ip,...`` (no name) by
    detecting which field is the IPv4 address.
    """
    f = [x.strip() for x in fields]
    cols = _POSITIONAL
    if f and _IPV4_RE.match(f[0]):        # ip-first, no name column
        cols = _POSITIONAL[1:]
    return {col: (f[i] if i < len(f) else "") for i, col in enumerate(cols)}


def _make_camera(row: dict) -> Optional[CameraInput]:
    row = {(k or "").strip().lower(): (v or "").strip()
           for k, v in row.items() if k}
    ip = row.get("ip", "")
    if not ip:
        return None
    return CameraInput(
        name=row.get("name", "") or ip,
        ip=ip,
        username=row.get("username", ""),
        password=row.get("password", ""),
        http_port=_int_or_none(row.get("http_port", "")),
        rtsp_port=_int_or_none(row.get("rtsp_port", "")),
        channel=_int_or_none(row.get("channel", "")) or 1,
    )


def load_camera_list(path: str) -> list[CameraInput]:
    """Read the user's credentials CSV into ``CameraInput`` objects.

    Accepts either a headered CSV (columns in any order) or a headerless one
    (columns read positionally as name,ip,username,password,http_port,
    rtsp_port,channel — or ip-first when the first field is an IP address).
    Ignores blank lines and ``#`` comments; requires an ip per row.
    """
    cams: list[CameraInput] = []
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return cams

    first = next(csv.reader([lines[0]]))
    if _looks_like_header(first):
        for row in csv.DictReader(lines):
            cam = _make_camera(row)
            if cam:
                cams.append(cam)
    else:
        log.warning("Input '%s' has no header row — reading columns positionally "
                    "(name,ip,username,password,http_port,rtsp_port,channel).", path)
        for fields in csv.reader(lines):
            cam = _make_camera(_positional_row(fields))
            if cam:
                cams.append(cam)
    return cams


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------
def inject_credentials(uri: str, username: str, password: str) -> str:
    """Insert ``user:pass@`` into a URL that has no userinfo (e.g. ONVIF uri)."""
    parts = urlsplit(uri)
    if "@" in parts.netloc or not username:
        return uri
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit((parts.scheme, auth + parts.netloc, parts.path,
                       parts.query, parts.fragment))


def _url_from_template(cam: CameraInput, tr: TemplateRow,
                       username: str, password: str) -> str:
    port = 0
    if tr.protocol == "rtsp":
        port = cam.rtsp_port or tr.port
    elif tr.protocol in ("http", "https"):
        port = cam.http_port or tr.port
    return build_url(tr.protocol, cam.ip, port, tr.path,
                     username=username, password=password, channel=cam.channel)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------
class Resolver:
    def __init__(self, db: CameraDB, probe_timeout: float = 8.0,
                 max_candidates: int = 25, try_defaults: bool = False,
                 reach_timeout: float = 2.0):
        self.db = db
        self.probe_timeout = probe_timeout
        self.max_candidates = max_candidates
        self.try_defaults = try_defaults
        # Fast TCP pre-check timeout; <= 0 disables the reachability check.
        self.reach_timeout = reach_timeout
        # Many cameras/NVRs cap simultaneous RTSP sessions, so probing the same
        # IP from two workers at once makes one of them fail. Serialize per IP.
        self._ip_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def _ip_lock(self, ip: str) -> threading.Lock:
        with self._locks_guard:
            return self._ip_locks[ip]

    def _candidate_urls(self, cam: CameraInput, fp,
                        username: str, password: str) -> list[tuple[str, str]]:
        """Ordered (method, url) candidates for one credential set."""
        cands: list[tuple[str, str]] = []

        if fp.direct_uri:
            cands.append(("onvif", inject_credentials(fp.direct_uri, username, password)))

        templates = self.db.templates_for(fp.vendor, fp.model)
        method = "db-model" if fp.model else "db-vendor"
        for tr in templates:
            cands.append((method, _url_from_template(cam, tr, username, password)))

        for tr in self.db.generic_templates():
            cands.append(("generic", _url_from_template(cam, tr, username, password)))

        # de-dup preserving order, cap total probes
        seen, out = set(), []
        for method, url in cands:
            if url not in seen:
                seen.add(url)
                out.append((method, url))
            if len(out) >= self.max_candidates:
                break
        return out

    def resolve_one(self, cam: CameraInput) -> ResolvedCamera:
        # Hold the per-IP lock across fingerprint + probing so cameras that
        # share an IP (or duplicate rows) don't contend for RTSP sessions.
        with self._ip_lock(cam.ip):
            return self._resolve_one_locked(cam)

    def _resolve_one_locked(self, cam: CameraInput) -> ResolvedCamera:
        # Fast reachability pre-check: skip the whole candidate chain for hosts
        # that aren't even accepting connections (offline / wrong subnet).
        if self.reach_timeout > 0:
            ports = {cam.rtsp_port or 554, cam.http_port or 80}
            if not reachable(cam.ip, ports, self.reach_timeout):
                log.warning("[%s] UNREACHABLE (no TCP on %s)",
                            cam.label(), ",".join(str(p) for p in sorted(ports)))
                return ResolvedCamera(name=cam.name, ip=cam.ip,
                                      status="UNREACHABLE")

        fp = fingerprint(cam)
        rc = ResolvedCamera(name=cam.name, ip=cam.ip,
                            vendor=fp.vendor or "", model=fp.model or "")

        # Attempt 1: supplied credentials.
        cred_sets = [(cam.username, cam.password, False)]
        # Attempt 2 (optional): scraped default credentials for this vendor.
        if self.try_defaults:
            defs = self.db.default_creds_for(fp.vendor, fp.model)
            if defs and (defs[0], defs[1]) != (cam.username, cam.password):
                cred_sets.append((defs[0], defs[1], True))

        for username, password, is_default in cred_sets:
            for method, url in self._candidate_urls(cam, fp, username, password):
                if probe_frame(url, timeout=self.probe_timeout):
                    rc.working_url = url
                    rc.protocol = urlsplit(url).scheme
                    rc.status = "OK"
                    rc.method = method + ("+defaults" if is_default else "")
                    log.info("[%s] RESOLVED via %s -> %s",
                             cam.label(), rc.method, rc.redacted_url())
                    return rc

        rc.status = "UNRESOLVED"
        log.warning("[%s] UNRESOLVED (vendor=%s model=%s)",
                    cam.label(), rc.vendor or "?", rc.model or "?")
        return rc

    def resolve_all(self, cams: Iterable[CameraInput],
                    workers: int = 6) -> list[ResolvedCamera]:
        cams = list(cams)
        results: list[Optional[ResolvedCamera]] = [None] * len(cams)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self.resolve_one, c): i for i, c in enumerate(cams)}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
        return [r for r in results if r is not None]


# --------------------------------------------------------------------------
# Cache I/O
# --------------------------------------------------------------------------
def write_resolved(path: str, cams: list[ResolvedCamera]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RESOLVED_HEADER)
        w.writeheader()
        for c in cams:
            w.writerow({"name": c.name, "ip": c.ip, "vendor": c.vendor,
                        "model": c.model, "protocol": c.protocol,
                        "status": c.status, "method": c.method,
                        "working_url": c.working_url})


def read_resolved(path: str) -> list[ResolvedCamera]:
    out: list[ResolvedCamera] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(ResolvedCamera(
                name=row.get("name", ""), ip=row.get("ip", ""),
                vendor=row.get("vendor", ""), model=row.get("model", ""),
                protocol=row.get("protocol", ""),
                working_url=row.get("working_url", ""),
                status=row.get("status", "UNRESOLVED"),
                method=row.get("method", ""),
            ))
    return out


def resolve_cameras(input_csv: str, db: CameraDB, cache_path: str = "resolved.csv",
                    use_cache: bool = True, try_defaults: bool = False,
                    probe_timeout: float = 8.0, workers: int = 6,
                    retry_unresolved: bool = False, retry_timeout: float = 12.0,
                    reach_timeout: float = 2.0) -> list[ResolvedCamera]:
    """High-level helper used by the CLI's resolve/view/export commands."""
    if use_cache and Path(cache_path).exists():
        cached = read_resolved(cache_path)
        log.info("Using cached resolution from %s (%d cameras). "
                 "Pass --no-cache to re-resolve.", cache_path, len(cached))
        return cached
    cams = load_camera_list(input_csv)
    if not cams:
        log.error("No cameras loaded from %s — check the file has rows with an "
                  "IP address (header optional).", input_csv)
        return []
    log.info("Resolving %d camera(s) ...", len(cams))
    resolver = Resolver(db, probe_timeout=probe_timeout, try_defaults=try_defaults,
                        reach_timeout=reach_timeout)
    resolved = resolver.resolve_all(cams, workers=workers)

    if retry_unresolved:
        # Only retry UNRESOLVED (found the host but no working URL); UNREACHABLE
        # hosts are definitively down, so retrying them just wastes time.
        pending = [i for i, r in enumerate(resolved) if r.status == "UNRESOLVED"]
        if pending:
            log.info("Retrying %d unresolved camera(s) serially (timeout %.0fs) ...",
                     len(pending), retry_timeout)
            retry_resolver = Resolver(db, probe_timeout=retry_timeout,
                                      try_defaults=try_defaults,
                                      reach_timeout=reach_timeout)
            retried = retry_resolver.resolve_all([cams[i] for i in pending], workers=1)
            recovered = 0
            for slot, i in enumerate(pending):
                if retried[slot].ok:
                    resolved[i] = retried[slot]
                    recovered += 1
            log.info("Retry pass recovered %d/%d camera(s).", recovered, len(pending))

    write_resolved(cache_path, resolved)
    ok = sum(1 for r in resolved if r.ok)
    log.info("Resolved %d/%d cameras. Cache written to %s.",
             ok, len(resolved), cache_path)
    return resolved
