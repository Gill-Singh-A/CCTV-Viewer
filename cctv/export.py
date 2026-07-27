"""Bulk-export still frames from many cameras.

Two entry points:
  * :func:`export_cameras` — open each resolved camera fresh, grab N frames,
    write to ``exports/<timestamp>/``. Used by the ``export`` CLI command.
  * :func:`export_streams` — grab the latest frame from already-running
    :class:`~cctv.capture.CameraStream` objects. Used by the viewer's ``e`` key.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2

from .capture import CameraStream
from .models import ResolvedCamera
from .util import get_logger

log = get_logger("cctv.export")


def _safe_name(cam: ResolvedCamera) -> str:
    base = (cam.name or cam.ip or "camera").strip()
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in base)


def _grab_frames(cam: ResolvedCamera, out_dir: str, frames: int,
                 timeout: float) -> int:
    """Open the stream, save up to ``frames`` frames. Returns count saved."""
    stream = CameraStream(cam.name or cam.ip, cam.working_url).start()
    saved = 0
    deadline = time.time() + timeout
    try:
        for i in range(frames):
            frame = None
            while time.time() < deadline:
                frame = stream.read()
                if frame is not None:
                    break
                time.sleep(0.2)
            if frame is None:
                break
            name = _safe_name(cam)
            suffix = f"-{i + 1}" if frames > 1 else ""
            path = os.path.join(out_dir, f"{name}{suffix}.jpg")
            if cv2.imwrite(path, frame):
                saved += 1
            if frames > 1:
                time.sleep(0.3)  # small gap between multi-frame grabs
    finally:
        stream.stop()
    return saved


def export_cameras(cameras: list[ResolvedCamera], out_root: str = "exports",
                   frames: int = 1, timeout: float = 10.0,
                   workers: int = 6) -> str:
    """Export frames from every resolvable camera into a timestamped folder."""
    targets = [c for c in cameras if c.ok]
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(out_root, ts)
    os.makedirs(out_dir, exist_ok=True)
    log.info("Exporting %d frame(s) from %d camera(s) -> %s",
             frames, len(targets), out_dir)

    results: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_grab_frames, c, out_dir, frames, timeout): c
                for c in targets}
        for fut in as_completed(futs):
            cam = futs[fut]
            try:
                results[cam.label()] = fut.result()
            except Exception as exc:  # pragma: no cover
                results[cam.label()] = 0
                log.warning("[%s] export failed: %s", cam.label(), exc)

    ok = sum(1 for v in results.values() if v > 0)
    for label, n in results.items():
        status = f"{n} frame(s)" if n else "FAILED"
        log.info("  %-30s %s", label, status)
    skipped = len(cameras) - len(targets)
    log.info("Export complete: %d/%d cameras produced frames%s. Folder: %s",
             ok, len(targets),
             f" ({skipped} unresolved skipped)" if skipped else "", out_dir)
    return out_dir


def export_streams(cameras: list[ResolvedCamera],
                   streams: list[CameraStream],
                   out_root: str = "exports") -> str:
    """Snapshot the latest frame from live streams (viewer 'e' shortcut)."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(out_root, ts)
    os.makedirs(out_dir, exist_ok=True)
    saved = 0
    for cam, stream in zip(cameras, streams):
        frame = stream.read()
        if frame is None:
            continue
        path = os.path.join(out_dir, f"{_safe_name(cam)}.jpg")
        if cv2.imwrite(path, frame):
            saved += 1
    log.info("Exported %d live frame(s) -> %s", saved, out_dir)
    return out_dir
