"""OpenCV grid viewer — shows the channels of one camera "family" at a time.

A *family* is one row of the input CSV (one IP / NVR). The grid tiles the
channels of the currently-selected family; use the keyboard to switch families
or page through channels. The canvas is sized to the screen and the window is
resizable, so it adapts to the display.

Keyboard controls (focus the video window):
    1 4 9 6    layout: single / 2x2 / 3x3 / 4x4 channels per page
    ] [        next / previous camera family (CSV row)
    n p        next / previous page of channels within the family
    s          save a snapshot of the current canvas
    e          export one frame from every visible channel now
    q / Esc    quit
"""

from __future__ import annotations

import math
import os
import time

import cv2
import numpy as np

from .capture import CameraStream
from .models import ResolvedCamera
from .util import channel_of, get_logger, rewrite_channel, supports_channel

log = get_logger("cctv.viewer")

LAYOUTS = {ord("1"): 1, ord("4"): 4, ord("9"): 9, ord("6"): 16}
WINDOW = "CCTV Viewer"

MODE_TO_CELLS = {"single": 1, "grid4": 4, "grid9": 9, "grid16": 16}


def _grid_dims(cells: int) -> tuple[int, int]:
    cols = int(math.ceil(math.sqrt(cells)))
    rows = int(math.ceil(cells / cols))
    return rows, cols


def _screen_size(default: tuple[int, int] = (1280, 720)) -> tuple[int, int]:
    """Best-effort screen resolution via tkinter; falls back if unavailable."""
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return default


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
    cv2.putText(cell, name[:48], (24, 16), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (240, 240, 240), 1, cv2.LINE_AA)


class GridViewer:
    """Family-centric viewer: one family's channels tiled across the grid."""

    def __init__(self, cameras: list[ResolvedCamera], mode: str = "grid4",
                 max_channels: int = 64, snapshot_dir: str = "snapshots"):
        self.families = [c for c in cameras if c.ok]
        self.family_idx = 0
        self.cells = MODE_TO_CELLS.get(mode, 4)
        self.channel_page = 0
        self.max_channels = max_channels
        self.snapshot_dir = snapshot_dir
        sw, sh = _screen_size()
        # Aim for ~88% of the screen; the window stays resizable regardless.
        self.canvas_w = min(1920, int(sw * 0.88))
        self.canvas_h = min(1080, int(sh * 0.88))
        # streams for the currently-visible channels: list of (channel, stream)
        self.streams: list[tuple[int, CameraStream]] = []
        self._build_streams()

    # -- family / channel model ------------------------------------------
    def _family(self) -> ResolvedCamera:
        return self.families[self.family_idx]

    def _has_channels(self) -> bool:
        return supports_channel(self._family().working_url)

    def _visible_channels(self) -> list[int]:
        if not self._has_channels():
            return [channel_of(self._family().working_url) or 1]
        start = self.channel_page * self.cells + 1
        return [ch for ch in range(start, start + self.cells)
                if ch <= self.max_channels]

    def _cell_size(self) -> tuple[int, int]:
        rows, cols = _grid_dims(self.cells)
        hud = 26
        return (max(160, self.canvas_w // cols),
                max(120, (self.canvas_h - hud) // rows))

    def _build_streams(self) -> None:
        """(Re)build the live streams for the current family + channel page."""
        for _, s in self.streams:
            s.stop()
        self.streams = []
        fam = self._family()
        base = fam.working_url
        for ch in self._visible_channels():
            url = rewrite_channel(base, ch) if self._has_channels() else base
            name = f"{fam.label()} ch{ch}" if self._has_channels() else fam.label()
            self.streams.append((ch, CameraStream(name, url).start()))

    def _switch_family(self, delta: int) -> None:
        if len(self.families) <= 1:
            return
        self.family_idx = (self.family_idx + delta) % len(self.families)
        self.channel_page = 0
        log.info("family -> %d/%d: %s", self.family_idx + 1, len(self.families),
                 self._family().label())
        self._build_streams()

    def _page_channels(self, delta: int) -> None:
        if not self._has_channels():
            return
        self.channel_page = max(0, self.channel_page + delta)
        self._build_streams()

    def _set_layout(self, cells: int) -> None:
        if cells != self.cells:
            self.cells = cells
            self._build_streams()

    # -- rendering --------------------------------------------------------
    def _compose(self) -> np.ndarray:
        rows, cols = _grid_dims(self.cells)
        cw, ch = self._cell_size()
        tiles = []
        for slot in range(self.cells):
            if slot < len(self.streams):
                channel, stream = self.streams[slot]
                frame = stream.read()
                if frame is None:
                    cell = _placeholder(cw, ch, "CONNECTING...", stream.name)
                else:
                    cell = _fit(frame, cw, ch)
                _label(cell, stream.name, stream.online)
            else:
                cell = np.zeros((ch, cw, 3), np.uint8)
            tiles.append(cell)
        grid_rows = [np.hstack(tiles[r * cols:(r + 1) * cols]) for r in range(rows)]
        return np.vstack(grid_rows)

    def _hud(self, canvas: np.ndarray) -> None:
        fam = self._family()
        online = sum(1 for _, s in self.streams if s.online)
        vis = self._visible_channels()
        if self._has_channels() and vis:
            chan = f"ch {vis[0]}-{vis[-1]}"
        else:
            chan = "single"
        txt = (f"family {self.family_idx + 1}/{len(self.families)}: "
               f"{fam.label()} [{fam.vendor or '?'}]  {chan}  online:{online}  "
               f"[ ][ ]family [n/p]channels [1/4/9/6] [s]nap [e]xport [q]uit")
        h = canvas.shape[0]
        cv2.rectangle(canvas, (0, h - 22), (canvas.shape[1], h), (0, 0, 0), -1)
        cv2.putText(canvas, txt[:170], (8, h - 7), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (200, 200, 200), 1, cv2.LINE_AA)

    # -- controls ---------------------------------------------------------
    def _snapshot(self, canvas: np.ndarray) -> None:
        os.makedirs(self.snapshot_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.snapshot_dir, f"snapshot-{ts}.jpg")
        cv2.imwrite(path, canvas)
        log.info("Saved snapshot %s", path)

    def _export_visible(self) -> None:
        from .export import export_streams
        fam = self._family()
        cams = [ResolvedCamera(name=fam.label(), ip=fam.ip, vendor=fam.vendor,
                               model=fam.model, protocol=fam.protocol,
                               working_url=s.url, status="OK")
                for _, s in self.streams]
        export_streams(cams, [s for _, s in self.streams])

    def run(self) -> None:
        if not self.families:
            log.error("No resolved cameras to display. Run resolve first.")
            return
        cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        cv2.resizeWindow(WINDOW, self.canvas_w, self.canvas_h)
        log.info("Viewer started: %d camera families. Focus the window; q to quit.",
                 len(self.families))
        try:
            while True:
                canvas = self._compose()
                self._hud(canvas)
                cv2.imshow(WINDOW, canvas)
                key = cv2.waitKey(30) & 0xFF
                if key in (ord("q"), 27):
                    break
                elif key in LAYOUTS:
                    self._set_layout(LAYOUTS[key])
                elif key == ord("]"):
                    self._switch_family(+1)
                elif key == ord("["):
                    self._switch_family(-1)
                elif key == ord("n"):
                    self._page_channels(+1)
                elif key == ord("p"):
                    self._page_channels(-1)
                elif key == ord("s"):
                    self._snapshot(canvas)
                elif key == ord("e"):
                    self._export_visible()
                if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            for _, s in self.streams:
                s.stop()
            cv2.destroyAllWindows()


def view(cameras: list[ResolvedCamera], mode: str = "grid4") -> None:
    GridViewer(cameras, mode=mode).run()
