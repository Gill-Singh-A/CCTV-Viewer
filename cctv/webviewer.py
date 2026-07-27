"""Flask web dashboard — an alternative to the OpenCV grid viewer.

Serves each resolved camera as an MJPEG stream and a single-page dashboard with
selectable layouts (single / 4 / 9 / 16), paging and a bulk-export button. Reuses
the same :class:`~cctv.capture.CameraStream` backend as the desktop viewer.
"""

from __future__ import annotations

import time

import cv2
from flask import Flask, Response, jsonify, render_template_string, request

from .capture import CameraStream
from .export import export_streams
from .models import ResolvedCamera
from .util import get_logger

log = get_logger("cctv.web")

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CCTV Viewer</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #111; color: #ddd; }
  header { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
           padding: .6rem 1rem; background: #1b1b1b; border-bottom: 1px solid #333;
           position: sticky; top: 0; z-index: 5; }
  header h1 { font-size: 1rem; margin: 0 1rem 0 0; font-weight: 600; }
  button { background: #2a2a2a; color: #ddd; border: 1px solid #444; border-radius: 6px;
           padding: .35rem .7rem; cursor: pointer; font-size: .85rem; }
  button:hover { background: #363636; }
  button.active { background: #2d6cdf; border-color: #2d6cdf; color: #fff; }
  .spacer { flex: 1; }
  #status { font-size: .8rem; color: #9aa; }
  #grid { display: grid; gap: 4px; padding: 4px; }
  .tile { position: relative; background: #000; aspect-ratio: 4/3; overflow: hidden;
          border-radius: 4px; }
  .tile img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .tile .label { position: absolute; top: 0; left: 0; right: 0; padding: 2px 8px;
                 background: rgba(0,0,0,.55); font-size: .78rem; display: flex;
                 align-items: center; gap: 6px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #4c4; }
  .toast { position: fixed; bottom: 1rem; left: 50%; transform: translateX(-50%);
           background: #2d6cdf; color: #fff; padding: .6rem 1rem; border-radius: 8px;
           opacity: 0; transition: opacity .3s; pointer-events: none; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>CCTV Viewer</h1>
  <button data-cells="1">Single</button>
  <button data-cells="4">4</button>
  <button data-cells="9">9</button>
  <button data-cells="16">16</button>
  <button id="prev">&laquo; Prev</button>
  <button id="next">Next &raquo;</button>
  <div class="spacer"></div>
  <span id="status"></span>
  <button id="export">Export frames</button>
</header>
<div id="grid"></div>
<div class="toast" id="toast"></div>
<script>
const CAMERAS = {{ cameras|tojson }};
let cells = {{ default_cells }};
let page = 0;

const grid = document.getElementById('grid');
const statusEl = document.getElementById('status');

function pages() { return Math.max(1, Math.ceil(CAMERAS.length / cells)); }

function render() {
  if (page >= pages()) page = pages() - 1;
  const cols = Math.ceil(Math.sqrt(cells));
  grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  const start = page * cells;
  const slice = CAMERAS.slice(start, start + cells);
  grid.innerHTML = slice.map(c => `
    <div class="tile">
      <img src="/stream/${c.id}?t=${Date.now()}" alt="${c.name}">
      <span class="label"><span class="dot"></span>${c.name}</span>
    </div>`).join('');
  statusEl.textContent =
    `${CAMERAS.length} cams · layout ${cells} · page ${page + 1}/${pages()}`;
  document.querySelectorAll('[data-cells]').forEach(b =>
    b.classList.toggle('active', +b.dataset.cells === cells));
}

document.querySelectorAll('[data-cells]').forEach(b =>
  b.onclick = () => { cells = +b.dataset.cells; page = 0; render(); });
document.getElementById('prev').onclick = () => { page = (page - 1 + pages()) % pages(); render(); };
document.getElementById('next').onclick = () => { page = (page + 1) % pages(); render(); };

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}
document.getElementById('export').onclick = async () => {
  toast('Exporting…');
  try {
    const r = await fetch('/api/export', { method: 'POST' });
    const j = await r.json();
    toast(`Exported ${j.saved} frame(s) → ${j.folder}`);
  } catch (e) { toast('Export failed'); }
};

render();
</script>
</body>
</html>
"""


def create_app(cameras: list[ResolvedCamera], export_root: str = "exports"):
    """Build the Flask app plus the list of live streams it drives."""
    ok_cams = [c for c in cameras if c.ok]
    streams = [CameraStream(c.name or c.ip, c.working_url).start() for c in ok_cams]

    app = Flask(__name__)

    def _mjpeg(stream: CameraStream):
        while True:
            frame = stream.read()
            if frame is None:
                time.sleep(0.1)
                continue
            ok, jpg = cv2.imencode(".jpg", frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + jpg.tobytes() + b"\r\n")
            time.sleep(1 / 15)

    @app.route("/")
    def index():
        cams = [{"id": i, "name": c.name or c.ip}
                for i, c in enumerate(ok_cams)]
        default_cells = 1 if len(cams) <= 1 else 4
        return render_template_string(INDEX_HTML, cameras=cams,
                                      default_cells=default_cells)

    @app.route("/stream/<int:idx>")
    def stream(idx: int):
        if not 0 <= idx < len(streams):
            return "no such camera", 404
        return Response(_mjpeg(streams[idx]),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/api/cameras")
    def api_cameras():
        return jsonify([{"id": i, "name": c.name or c.ip, "vendor": c.vendor,
                         "model": c.model} for i, c in enumerate(ok_cams)])

    @app.route("/api/export", methods=["POST"])
    def api_export():
        folder = export_streams(ok_cams, streams, out_root=export_root)
        saved = sum(1 for s in streams if s.read() is not None)
        return jsonify({"folder": folder, "saved": saved})

    return app, streams


def serve(cameras: list[ResolvedCamera], host: str = "127.0.0.1",
          port: int = 5000, export_root: str = "exports") -> None:
    app, streams = create_app(cameras, export_root=export_root)
    if not streams:
        log.error("No resolved cameras to serve.")
        return
    log.info("Web viewer serving %d camera(s) at http://%s:%d  (Ctrl-C to stop)",
             len(streams), host, port)
    try:
        app.run(host=host, port=port, threaded=True, debug=False,
                use_reloader=False)
    finally:
        for s in streams:
            s.stop()
