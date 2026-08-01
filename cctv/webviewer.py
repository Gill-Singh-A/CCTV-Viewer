"""Flask web dashboard — shows the channels of one camera "family" at a time.

A *family* is one input-CSV row (one IP / NVR). Pick a family from the dropdown
and the grid tiles that family's channels; switch families or page through
channels from the browser. The layout fills the viewport and is fully
responsive. Streams are created lazily and only for the channels currently on
screen (so one NVR is never asked for more sessions than it is showing).
"""

from __future__ import annotations

import threading
import time

import cv2
from flask import Flask, Response, jsonify, render_template_string, request

from .capture import CameraStream
from .export import export_streams
from .models import ResolvedCamera
from .util import get_logger, rewrite_channel, supports_channel

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
  html, body { height: 100%; margin: 0; }
  body { display: flex; flex-direction: column; font-family: system-ui, sans-serif;
         background: #111; color: #ddd; }
  header { flex: 0 0 auto; display: flex; flex-wrap: wrap; gap: .5rem;
           align-items: center; padding: .5rem .8rem; background: #1b1b1b;
           border-bottom: 1px solid #333; }
  header h1 { font-size: 1rem; margin: 0 .5rem 0 0; font-weight: 600; }
  select, button { background: #2a2a2a; color: #ddd; border: 1px solid #444;
           border-radius: 6px; padding: .35rem .6rem; font-size: .85rem; cursor: pointer; }
  select { max-width: 46vw; }
  button:hover { background: #363636; }
  button.active { background: #2d6cdf; border-color: #2d6cdf; color: #fff; }
  .spacer { flex: 1; }
  #status { font-size: .8rem; color: #9aa; white-space: nowrap; }
  #grid { flex: 1 1 auto; display: grid; gap: 4px; padding: 4px; min-height: 0; }
  .tile { position: relative; background: #000; min-height: 0; overflow: hidden;
          border-radius: 4px; }
  .tile img { width: 100%; height: 100%; object-fit: contain; display: block;
              background: #000; }
  .tile .label { position: absolute; top: 0; left: 0; right: 0; padding: 2px 8px;
                 background: rgba(0,0,0,.55); font-size: .78rem; }
  .toast { position: fixed; bottom: 1rem; left: 50%; transform: translateX(-50%);
           background: #2d6cdf; color: #fff; padding: .6rem 1rem; border-radius: 8px;
           opacity: 0; transition: opacity .3s; pointer-events: none; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <h1>CCTV Viewer</h1>
  <label>Family
    <select id="family"></select>
  </label>
  <button data-cells="1">1</button>
  <button data-cells="4">4</button>
  <button data-cells="9">9</button>
  <button data-cells="16">16</button>
  <button id="prevCh">&laquo; Ch</button>
  <button id="nextCh">Ch &raquo;</button>
  <div class="spacer"></div>
  <span id="status"></span>
  <button id="export">Export</button>
</header>
<div id="grid"></div>
<div class="toast" id="toast"></div>
<script>
const FAMILIES = {{ families|tojson }};
let fam = 0;
let cells = {{ default_cells }};
let page = 0;   // channel page within the family

const grid = document.getElementById('grid');
const statusEl = document.getElementById('status');
const familySel = document.getElementById('family');

FAMILIES.forEach((f, i) => {
  const o = document.createElement('option');
  o.value = i;
  o.textContent = `${f.name}${f.vendor ? ' — ' + f.vendor : ''}`;
  familySel.appendChild(o);
});

function hasChannels() { return FAMILIES[fam] && FAMILIES[fam].hasChannel; }

// Highest channel to show: the counted number if known, else a soft 64 cap.
function maxCh() {
  const f = FAMILIES[fam];
  if (!f || !f.hasChannel) return 1;
  return f.channels > 0 ? f.channels : 64;
}

function visibleChannels() {
  if (!hasChannels()) return [FAMILIES[fam].channel || 1];
  const start = page * cells + 1;
  return Array.from({length: cells}, (_, i) => start + i).filter(ch => ch <= maxCh());
}

async function render() {
  const chans = visibleChannels();
  // Tell the server exactly which streams to keep alive, then draw them.
  try {
    await fetch('/api/view', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({family: fam, channels: chans})
    });
  } catch (e) {}

  const cols = Math.ceil(Math.sqrt(cells));
  const rows = Math.ceil(cells / cols);
  grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  grid.style.gridTemplateRows = `repeat(${rows}, 1fr)`;

  const t = Date.now();
  grid.innerHTML = chans.map(ch => `
    <div class="tile">
      <img src="/stream/${fam}/${ch}?t=${t}" alt="ch ${ch}">
      <span class="label">${FAMILIES[fam].name}${hasChannels() ? ' · ch ' + ch : ''}</span>
    </div>`).join('');

  const total = hasChannels() && FAMILIES[fam].channels > 0 ? '/' + FAMILIES[fam].channels : '';
  const chLabel = hasChannels()
    ? `ch ${chans[0]}–${chans[chans.length - 1]}${total}` : 'single';
  statusEl.textContent = `${FAMILIES.length} families · ${chLabel} · layout ${cells}`;
  document.querySelectorAll('[data-cells]').forEach(b =>
    b.classList.toggle('active', +b.dataset.cells === cells));
  const atEnd = (page + 1) * cells >= maxCh();
  document.getElementById('prevCh').disabled = !hasChannels() || page === 0;
  document.getElementById('nextCh').disabled = !hasChannels() || atEnd;
}

familySel.onchange = () => { fam = +familySel.value; page = 0; render(); };
document.querySelectorAll('[data-cells]').forEach(b =>
  b.onclick = () => { cells = +b.dataset.cells; render(); });
document.getElementById('nextCh').onclick = () => {
  if (hasChannels() && (page + 1) * cells < maxCh()) { page++; render(); } };
document.getElementById('prevCh').onclick = () => {
  if (hasChannels() && page > 0) { page--; render(); } };

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
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


def create_app(families: list[ResolvedCamera], export_root: str = "exports"):
    """Build the Flask app. Streams are created lazily via /api/view."""
    fams = [c for c in families if c.ok]
    streams: dict[tuple[int, int], CameraStream] = {}
    lock = threading.Lock()

    app = Flask(__name__)

    def _url_for(fam_idx: int, channel: int) -> str:
        base = fams[fam_idx].working_url
        return rewrite_channel(base, channel) if supports_channel(base) else base

    def _get_or_create(fam_idx: int, channel: int) -> CameraStream:
        key = (fam_idx, channel)
        with lock:
            s = streams.get(key)
            if s is None:
                name = f"{fams[fam_idx].label()} ch{channel}"
                s = CameraStream(name, _url_for(fam_idx, channel)).start()
                streams[key] = s
            return s

    def _mjpeg(stream: CameraStream):
        while True:
            frame = stream.read()
            if frame is None:
                time.sleep(0.1)
                continue
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + jpg.tobytes() + b"\r\n")
            time.sleep(1 / 15)

    @app.route("/")
    def index():
        fam_json = [{"id": i, "name": c.name or c.ip, "vendor": c.vendor,
                     "model": c.model, "hasChannel": supports_channel(c.working_url),
                     "channels": c.channels, "channel": 1}
                    for i, c in enumerate(fams)]
        # Open in single-channel view: one real stream per family. A grid would
        # otherwise open channels 2..N even for single-lens cameras — dead tiles
        # whose failing reconnects can knock out channel 1 on cameras with a
        # low connection limit. Users expand to 4/9/16 for actual NVRs.
        return render_template_string(INDEX_HTML, families=fam_json,
                                      default_cells=1)

    @app.route("/api/families")
    def api_families():
        return jsonify([{"id": i, "name": c.name or c.ip, "vendor": c.vendor,
                         "model": c.model,
                         "hasChannel": supports_channel(c.working_url)}
                        for i, c in enumerate(fams)])

    @app.route("/api/view", methods=["POST"])
    def api_view():
        """Set the active stream set: keep only the requested family+channels."""
        data = request.get_json(silent=True) or {}
        try:
            fam_idx = int(data["family"])
            chans = [int(c) for c in data["channels"]]
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "bad request"}), 400
        if not 0 <= fam_idx < len(fams):
            return jsonify({"error": "no such family"}), 404
        wanted = {(fam_idx, ch) for ch in chans}
        with lock:
            for key in list(streams):
                if key not in wanted:
                    streams.pop(key).stop()
        for key in wanted:            # create outside the drop loop
            _get_or_create(*key)
        return jsonify({"active": len(wanted)})

    @app.route("/stream/<int:fam_idx>/<int:channel>")
    def stream(fam_idx: int, channel: int):
        if not 0 <= fam_idx < len(fams):
            return "no such family", 404
        return Response(_mjpeg(_get_or_create(fam_idx, channel)),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/api/export", methods=["POST"])
    def api_export():
        with lock:
            active = list(streams.items())
        cams, strs = [], []
        for (fam_idx, ch), s in active:
            f = fams[fam_idx]
            cams.append(ResolvedCamera(name=f.label(), ip=f.ip, vendor=f.vendor,
                                       model=f.model, protocol=f.protocol,
                                       working_url=s.url, status="OK"))
            strs.append(s)
        folder = export_streams(cams, strs, out_root=export_root)
        saved = sum(1 for s in strs if s.read() is not None)
        return jsonify({"folder": folder, "saved": saved})

    def _shutdown():
        with lock:
            for s in streams.values():
                s.stop()

    app.stop_streams = _shutdown  # type: ignore[attr-defined]
    return app, streams


def serve(cameras: list[ResolvedCamera], host: str = "127.0.0.1",
          port: int = 5000, export_root: str = "exports") -> None:
    app, streams = create_app(cameras, export_root=export_root)
    fams = [c for c in cameras if c.ok]
    if not fams:
        log.error("No resolved cameras to serve.")
        return
    log.info("Web viewer serving %d camera families at http://%s:%d  (Ctrl-C to stop)",
             len(fams), host, port)
    try:
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
    finally:
        app.stop_streams()  # type: ignore[attr-defined]
