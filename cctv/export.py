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
import threading
import time
from collections import defaultdict
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
    """Save up to ``frames`` frames from the camera's current channel.

    A single snapshot uses the lightweight one-shot :func:`grab_frame` (open,
    read one frame, release immediately) — far cheaper than a continuous decoder
    and it frees the connection right away, which matters when many cameras are
    exported at once. Only multi-frame requests keep a stream open.
    """
    if frames <= 1:
        frame = grab_frame(cam.working_url, timeout=timeout)
        if frame is None:
            return 0
        path = os.path.join(out_dir, f"{_safe_name(cam)}.jpg")
        return 1 if cv2.imwrite(path, frame) else 0

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
            path = os.path.join(out_dir, f"{_safe_name(cam)}-{i + 1}.jpg")
            if cv2.imwrite(path, frame):
                saved += 1
            time.sleep(0.3)
    finally:
        stream.stop()
    return saved


def _grab_all_channels(cam: ResolvedCamera, out_dir: str, max_channels: int,
                       timeout: float, batch_size: int) -> int:
    """Save one frame per live channel of an NVR/DVR.

    If resolve already counted this family's channels, grab exactly that range.
    Otherwise probe in batches of ``batch_size`` and stop only when a whole
    batch is empty — tolerating dead channels in the middle of the range.
    """
    base = cam.working_url
    if not supports_channel(base):
        return _grab_current(cam, out_dir, 1, timeout)

    def save(ch: int) -> bool:
        frame = grab_frame(rewrite_channel(base, ch), timeout=timeout)
        if frame is None:
            return False
        path = os.path.join(out_dir, f"{_safe_name(cam)}-ch{ch}.jpg")
        return bool(cv2.imwrite(path, frame))

    # Known count: grab exactly channels 1..N (dead ones simply don't save).
    if cam.channels and cam.channels > 0:
        return sum(1 for ch in range(1, cam.channels + 1) if save(ch))

    # Unknown: batch-probe, stopping when an entire batch yields nothing.
    saved = 0
    start = 1
    while start <= max_channels:
        end = min(start + batch_size, max_channels + 1)
        got = [ch for ch in range(start, end) if save(ch)]
        saved += len(got)
        if not got:
            break
        start = end
    return saved


def export_cameras(cameras: list[ResolvedCamera], out_root: str = "exports",
                   frames: int = 1, timeout: float = 10.0, workers: int = 6,
                   all_channels: bool = False, max_channels: int = 64,
                   batch_size: int = 10) -> str:
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

    # Serialize per IP so multiple channels/cameras on one NVR don't open
    # simultaneous sessions and hit its connection limit (distinct IPs still
    # run in parallel), mirroring the resolver.
    ip_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
    locks_guard = threading.Lock()

    def do_grab(cam: ResolvedCamera, t: float) -> int:
        if all_channels:
            return _grab_all_channels(cam, out_dir, max_channels, t, batch_size)
        return _grab_current(cam, out_dir, frames, t)

    def work(cam: ResolvedCamera) -> int:
        with locks_guard:
            lock = ip_locks[cam.ip]
        with lock:
            return do_grab(cam, timeout)

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

    # Opening many streams at once saturates CPU/NVR sessions, so some live
    # cameras miss their window. Retry the failures serially with a longer
    # timeout — the same completeness-over-speed pass the resolver uses.
    if workers > 1:
        failed = [c for c in targets if not results.get(c.label())]
        if failed:
            retry_timeout = max(timeout, 15.0)
            log.info("Retrying %d camera(s) serially (timeout %.0fs) ...",
                     len(failed), retry_timeout)
            recovered = 0
            for cam in failed:
                n = do_grab(cam, retry_timeout)
                results[cam.label()] = n
                recovered += 1 if n else 0
            log.info("Retry pass recovered %d/%d camera(s).", recovered, len(failed))

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
