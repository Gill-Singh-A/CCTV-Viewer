"""Threaded stream capture built on OpenCV's FFmpeg backend.

``probe_frame`` is a cheap one-shot used by the resolver to decide whether a
candidate URL actually yields video. ``CameraStream`` is the long-lived,
self-reconnecting reader used by the viewer and exporter.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

import numpy as np

from .util import get_logger

log = get_logger("cctv.capture")

# Force RTSP over TCP (far more reliable than UDP across NAT/Wi-Fi) and cap the
# open/read timeout so dead hosts fail fast. Must be set before cv2 import use.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",  # stimeout is microseconds
)

import cv2  # noqa: E402  (import after env var is set)


def _open(url: str) -> "cv2.VideoCapture":
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # minimise latency
    except cv2.error:
        pass
    return cap


def probe_frame(url: str, timeout: float = 8.0) -> bool:
    """Return True if a single decodable frame can be pulled from ``url``.

    Runs the blocking capture in a daemon thread so a wedged connection can't
    hang the whole resolve pass beyond ``timeout``.
    """
    result = {"ok": False}

    def worker():
        cap = None
        try:
            cap = _open(url)
            if cap.isOpened():
                ok, frame = cap.read()
                result["ok"] = bool(ok and frame is not None and frame.size > 0)
        except Exception as exc:  # pragma: no cover - backend quirks
            log.debug("probe error %s: %s", url, exc)
        finally:
            if cap is not None:
                cap.release()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.debug("probe timed out: %s", url)
        return False
    return result["ok"]


def grab_frame(url: str, timeout: float = 8.0) -> Optional["np.ndarray"]:
    """Open ``url``, return a single decoded frame (or None), then release."""
    result = {"frame": None}

    def worker():
        cap = None
        try:
            cap = _open(url)
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None and frame.size > 0:
                    result["frame"] = frame
        except Exception as exc:  # pragma: no cover
            log.debug("grab error %s: %s", url, exc)
        finally:
            if cap is not None:
                cap.release()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    return result["frame"]


class CameraStream:
    """Background reader that always exposes the latest decoded frame."""

    def __init__(self, name: str, url: str, reconnect_delay: float = 3.0):
        self.name = name
        self.url = url
        self.reconnect_delay = reconnect_delay
        self._frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._switch = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.online = False
        self.last_frame_ts = 0.0

    def switch(self, url: str) -> None:
        """Point the stream at a new URL and reconnect immediately (live)."""
        if url == self.url:
            return
        self.url = url
        with self._lock:
            self._frame = None
        self._switch.set()

    def start(self) -> "CameraStream":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self._switch.clear()
            cap = _open(self.url)
            if not cap.isOpened():
                cap.release()
                self.online = False
                if self._stop.wait(self.reconnect_delay):
                    break
                continue
            log.debug("connected: %s", self.name)
            switched = False
            while not self._stop.is_set():
                if self._switch.is_set():
                    switched = True
                    break
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                with self._lock:
                    self._frame = frame
                self.online = True
                self.last_frame_ts = time.time()
            cap.release()
            self.online = False
            if switched:
                continue  # reconnect to the new URL without the backoff delay
            if self._stop.wait(self.reconnect_delay):
                break

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self, join_timeout: float = 0.3):
        # Short join keeps UI actions (family/channel switch) snappy; the
        # capture thread is a daemon and exits on its own after its current
        # blocking read returns.
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
