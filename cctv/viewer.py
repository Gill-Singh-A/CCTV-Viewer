"""OpenCV grid viewer for multiple live camera streams.

Keyboard controls (focus the video window):
    1        single view
    4        2x2 grid
    9        3x3 grid
    6        4x4 grid (16)
    n / p    next / previous page of cameras
    s        save a snapshot of the current canvas
    e        export one frame from every camera now
    q / Esc  quit
"""

from __future__ import annotations

import math
import os
import time
from typing import Optional

import cv2
import numpy as np

from .capture import CameraStream
from .models import ResolvedCamera
from .util import get_logger

log = get_logger("cctv.viewer")

LAYOUTS = {ord("1"): 1, ord("4"): 4, ord("9"): 9, ord("6"): 16}
WINDOW = "CCTV Viewer"

MODE_TO_CELLS = {"single": 1, "grid4": 4, "grid9": 9, "grid16": 16}


def _grid_dims(cells: int) -> tuple[int, int]:
    cols = int(math.ceil(math.sqrt(cells)))
    rows = int(math.ceil(cells / cols))
    return rows, cols


def _placeholder(w: int, h: int, text: str, sub: str = "") -> np.ndarray:
    img = np.full((h, w, 3), 40, np.uint8)
    cv2.putText(img, text, (12, h // 2), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (60, 60, 220), 2, cv2.LINE_AA)
    if sub:
        cv2.putText(img, sub, (12, h // 2 + 28), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (180, 180, 180), 1, cv2.LINE_AA)
    return img


def _fit(frame: np.ndarray, w: int, h: int) -> np.ndarray:
    fh, fw = frame.shape[:2]
    scale = min(w / fw, h / fh)
    nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
    resized = cv2.resize(frame, (nw, nh))
    canvas = np.zeros((h, w, 3), np.uint8)
    y, x = (h - nh) // 2, (w - nw) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return canvas


def _label(cell: np.ndarray, name: str, online: bool) -> None:
    h, w = cell.shape[:2]
    cv2.rectangle(cell, (0, 0), (w, 22), (0, 0, 0), -1)
    color = (80, 220, 80) if online else (60, 60, 220)
    cv2.circle(cell, (12, 11), 5, color, -1)
    cv2.putText(cell, name[:40], (24, 16), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (240, 240, 240), 1, cv2.LINE_AA)


class GridViewer:
    def __init__(self, cameras: list[ResolvedCamera], mode: str = "grid4",
                 cell_w: int = 480, cell_h: int = 360,
                 snapshot_dir: str = "snapshots"):
        self.resolved = [c for c in cameras if c.ok]
        self.cell_w = cell_w
        self.cell_h = cell_h
        self.snapshot_dir = snapshot_dir
        self.cells = MODE_TO_CELLS.get(mode, 4)
        self.page = 0
        self.streams = [CameraStream(c.name or c.ip, c.working_url).start()
                        for c in self.resolved]

    # -- rendering --------------------------------------------------------
    def _compose(self) -> np.ndarray:
        rows, cols = _grid_dims(self.cells)
        start = self.page * self.cells
        page_cams = list(range(start, min(start + self.cells, len(self.streams))))
        tiles = []
        for slot in range(self.cells):
            if slot < len(page_cams):
                idx = page_cams[slot]
                stream = self.streams[idx]
                frame = stream.read()
                if frame is None:
                    cell = _placeholder(self.cell_w, self.cell_h,
                                        "CONNECTING...", stream.name)
                else:
                    cell = _fit(frame, self.cell_w, self.cell_h)
                _label(cell, stream.name, stream.online)
            else:
                cell = np.zeros((self.cell_h, self.cell_w, 3), np.uint8)
            tiles.append(cell)
        grid_rows = [np.hstack(tiles[r * cols:(r + 1) * cols]) for r in range(rows)]
        return np.vstack(grid_rows)

    def _hud(self, canvas: np.ndarray) -> None:
        n_pages = max(1, math.ceil(len(self.streams) / self.cells))
        online = sum(1 for s in self.streams if s.online)
        txt = (f"cams:{len(self.streams)} online:{online} "
               f"layout:{self.cells} page:{self.page + 1}/{n_pages}  "
               f"[1/4/9/6] [n/p] [s]nap [e]xport [q]uit")
        h = canvas.shape[0]
        cv2.rectangle(canvas, (0, h - 22), (canvas.shape[1], h), (0, 0, 0), -1)
        cv2.putText(canvas, txt, (8, h - 7), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (200, 200, 200), 1, cv2.LINE_AA)

    # -- controls ---------------------------------------------------------
    def _snapshot(self, canvas: np.ndarray) -> None:
        os.makedirs(self.snapshot_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.snapshot_dir, f"snapshot-{ts}.jpg")
        cv2.imwrite(path, canvas)
        log.info("Saved snapshot %s", path)

    def _export_all(self) -> None:
        from .export import export_streams
        export_streams(self.resolved, self.streams)

    def run(self) -> None:
        if not self.streams:
            log.error("No resolved cameras to display. Run resolve first.")
            return
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
        log.info("Viewer started with %d camera(s). Focus the window; press q to quit.",
                 len(self.streams))
        try:
            while True:
                canvas = self._compose()
                self._hud(canvas)
                cv2.imshow(WINDOW, canvas)
                key = cv2.waitKey(30) & 0xFF
                if key in (ord("q"), 27):
                    break
                elif key in LAYOUTS:
                    self.cells = LAYOUTS[key]
                    self.page = 0
                elif key == ord("n"):
                    n_pages = max(1, math.ceil(len(self.streams) / self.cells))
                    self.page = (self.page + 1) % n_pages
                elif key == ord("p"):
                    n_pages = max(1, math.ceil(len(self.streams) / self.cells))
                    self.page = (self.page - 1) % n_pages
                elif key == ord("s"):
                    self._snapshot(canvas)
                elif key == ord("e"):
                    self._export_all()
                # window closed by the user
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            for s in self.streams:
                s.stop()
            cv2.destroyAllWindows()


def view(cameras: list[ResolvedCamera], mode: str = "grid4") -> None:
    GridViewer(cameras, mode=mode).run()
