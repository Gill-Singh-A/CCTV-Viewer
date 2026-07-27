"""Bulk-export still frames from many cameras (optionally every NVR channel).

Entry points:
  * :func:`export_cameras` — open each resolved camera fresh, grab N frames (or
    one frame per channel when ``all_channels=True``), write to
    ``exports/<timestamp>/``. Used by the ``export`` CLI command.
  * :func:`export_streams` — grab the latest frame from already-running
    :class:`~cctv.capture.CameraStream` objects. Used by the viewers.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2

from .capture import CameraStream, grab_frame
from .models import ResolvedCamera
from .util import channel_of, get_logger, rewrite_channel, supports_channel

log = get_logger("cctv.export")


def _safe_name(cam: ResolvedCamera) -> str:
    base = (cam.name or cam.ip or "camera").strip()
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in base)


def _grab_current(cam: ResolvedCamera, out_dir: str, frames: int,
                  timeout: float) -> int:
    """Save up to ``frames`` frames from the camera's current channel."""
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
            suffix = f"-{i + 1}" if frames > 1 else ""
            path = os.path.join(out_dir, f"{_safe_name(cam)}{suffix}.jpg")
            if cv2.imwrite(path, frame):
                saved += 1
            if frames > 1:
                time.sleep(0.3)
    finally:
        stream.stop()
    return saved


def _grab_all_channels(cam: ResolvedCamera, out_dir: str, max_channels: int,
                       timeout: float, miss_streak: int) -> int:
    """Save one frame per live channel of an NVR/DVR.

    Stops after ``miss_streak`` consecutive empty channels once at least one
    channel has produced a frame (channels are normally contiguous from 1).
    """
    base = cam.working_url
    if not supports_channel(base):
        return _grab_current(cam, out_dir, 1, timeout)

    # If resolve counted this family's channels, grab exactly that many; else
    # probe up to max_channels and stop after a run of empty channels.
    if cam.channels and cam.channels > 0:
        upper, use_miss_streak = cam.channels, False
    else:
        upper, use_miss_streak = max_channels, True

    saved = misses = 0
    for ch in range(1, upper + 1):
        frame = grab_frame(rewrite_channel(base, ch), timeout=timeout)
        if frame is not None:
            path = os.path.join(out_dir, f"{_safe_name(cam)}-ch{ch}.jpg")
            if cv2.imwrite(path, frame):
                saved += 1
            misses = 0
        elif use_miss_streak and saved:
            misses += 1
            if misses >= miss_streak:
                break
    return saved


def export_cameras(cameras: list[ResolvedCamera], out_root: str = "exports",
                   frames: int = 1, timeout: float = 10.0, workers: int = 6,
                   all_channels: bool = False, max_channels: int = 64,
                   miss_streak: int = 2) -> str:
    """Export frames from every resolvable camera into a timestamped folder."""
    targets = [c for c in cameras if c.ok]
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(out_root, ts)
    os.makedirs(out_dir, exist_ok=True)
    if all_channels:
        log.info("Exporting all channels (max %d) from %d camera(s) -> %s",
                 max_channels, len(targets), out_dir)
    else:
        log.info("Exporting %d frame(s) from %d camera(s) -> %s",
                 frames, len(targets), out_dir)

    def work(cam: ResolvedCamera) -> int:
        if all_channels:
            return _grab_all_channels(cam, out_dir, max_channels, timeout, miss_streak)
        return _grab_current(cam, out_dir, frames, timeout)

    results: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, c): c for c in targets}
        for fut in as_completed(futs):
            cam = futs[fut]
            try:
                results[cam.label()] = fut.result()
            except Exception as exc:  # pragma: no cover
                results[cam.label()] = 0
                log.warning("[%s] export failed: %s", cam.label(), exc)

    ok = sum(1 for v in results.values() if v > 0)
    total = sum(results.values())
    for label, n in results.items():
        log.info("  %-30s %s", label, f"{n} frame(s)" if n else "FAILED")
    skipped = len(cameras) - len(targets)
    log.info("Export complete: %d image(s) from %d/%d cameras%s. Folder: %s",
             total, ok, len(targets),
             f" ({skipped} unresolved skipped)" if skipped else "", out_dir)
    return out_dir


def export_streams(cameras: list[ResolvedCamera],
                   streams: list[CameraStream],
                   out_root: str = "exports") -> str:
    """Snapshot the latest frame from live streams (viewer export shortcut)."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(out_root, ts)
    os.makedirs(out_dir, exist_ok=True)
    saved = 0
    for cam, stream in zip(cameras, streams):
        frame = stream.read()
        if frame is None:
            continue
        ch = channel_of(stream.url)
        suffix = f"-ch{ch}" if ch is not None else ""
        path = os.path.join(out_dir, f"{_safe_name(cam)}{suffix}.jpg")
        if cv2.imwrite(path, frame):
            saved += 1
    log.info("Exported %d live frame(s) -> %s", saved, out_dir)
    return out_dir
