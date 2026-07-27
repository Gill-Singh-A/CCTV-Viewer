# CCTV Viewer

Point it at a list of IP cameras — **only IP address, username and password** —
and it will:

1. **Fingerprint** each camera (vendor + model) using ONVIF, then HTTP/RTSP
   header signatures.
2. **Resolve** a working stream URL by combining ONVIF's own stream URI with a
   locally scraped [ispyconnect.com](https://www.ispyconnect.com/cameras)
   database of camera connection-URL templates, validating each candidate by
   actually pulling a live frame.
3. **View** the live feeds in an OpenCV grid (single / 4 / 9 / 16 view) — or a
   Flask web dashboard in the browser.
4. **Export** still frames from every camera in bulk — including every channel
   of an NVR/DVR.

Cameras on an **NVR/DVR** expose multiple channels behind one IP. You don't have
to know how many: the channel is **switchable live in both viewers**, and
`export --all-channels` walks every channel automatically.

> ⚠️ **Authorized use only.** Use this tool exclusively on cameras you own or
> have explicit written permission to access. You are responsible for complying
> with all applicable laws.

## How camera identification works

You never tell the tool what brand a camera is. Given `ip + user + pass`, it runs
a best-signal-first chain and stops at the first URL that returns a decodable
frame:

| Step | Source | Yields |
|------|--------|--------|
| 1 | ONVIF `GetDeviceInformation` | manufacturer + model |
| 2 | ONVIF `GetStreamUri` | a ready-to-use RTSP URL |
| 3 | HTTP `Server` / `WWW-Authenticate` realm / `<title>` | vendor guess |
| 4 | RTSP `OPTIONS` `Server` header | vendor guess |
| 5 | vendor+model → scraped DB templates | candidate URLs |
| 6 | generic common RTSP/HTTP paths | last-resort candidates |
| 7 | *(optional)* scraped vendor **default credentials** | retry of the above |

Anything that never produces a frame is reported as `UNRESOLVED`.

## Install

Requires Python 3.9+ and FFmpeg on the system (used by OpenCV for RTSP).

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If your system already provides OpenCV / requests / BeautifulSoup, you can reuse
them and install only the ONVIF client:

```bash
python3 -m venv --system-site-packages venv
./venv/bin/pip install onvif-zeep WSDiscovery
```

## Usage

```bash
python cctv_viewer.py <command> [options]
```

### 1. Build the camera database (once)

```bash
python cctv_viewer.py scrape
```

Scrapes every manufacturer from ispyconnect into:

- `data/cameras.csv` — `vendor,model,type,protocol,port,url`
- `data/default_credentials.csv` — `vendor,default_username,default_password`

Raw HTML is cached under `.cache/`, so re-runs are fast (use `--refresh` to
re-download). Limit scope with `--vendors axis,hikvision,dahua` or `--limit N`.

### 2. Prepare your camera list

Copy `examples/cameras.example.csv` and fill in your cameras. Only `ip`,
`username`, `password` are required:

```csv
name,ip,username,password,http_port,rtsp_port,channel
Front Gate,192.168.1.64,admin,admin12345,,,1
Lobby,192.168.1.65,admin,password,8000,554,1
```

Blank lines and `#` comments are ignored. Missing ports default to
80/8000 (ONVIF) and 554 (RTSP). `channel` is only a *starting* channel for
NVRs/DVRs (default 1) — you can change it live in the viewer, so it's usually
fine to leave blank.

### 3. Resolve

```bash
python cctv_viewer.py resolve -i cameras.csv
```

Prints a summary table and writes `resolved.csv` (which **contains
credentials** and is git-ignored). `view` and `export` reuse this cache; pass
`--no-cache` to force re-resolution. Add `--try-defaults` to fall back to
scraped default credentials when the supplied ones fail.

### 4. View

```bash
python cctv_viewer.py view -i cameras.csv --mode grid4
```

Keyboard controls (focus the video window):

| Key | Action |
|-----|--------|
| `1` `4` `9` `6` | single / 2×2 / 3×3 / 4×4 layout |
| `n` `p` | next / previous page of cameras |
| `.` `,` (or `]` `[`) | next / previous **channel** (all channel-capable cameras) |
| `s` | save a snapshot of the current canvas |
| `e` | export one frame from every camera now |
| `q` / `Esc` | quit |

#### Web dashboard

Prefer a browser? Add `--web` (the OpenCV grid is the default):

```bash
python cctv_viewer.py view -i cameras.csv --web --port 5000
```

Then open <http://127.0.0.1:5000>. Each camera is served as an MJPEG stream in a
responsive grid with Single/4/9/16 layout buttons, paging, per-tile **channel
&minus;/&plus;** controls (for NVR/DVR cameras) and an **Export frames** button.
Use `--host 0.0.0.0` to expose it on your network (only on trusted networks —
the dashboard is unauthenticated).

### 5. Bulk export

```bash
python cctv_viewer.py export -i cameras.csv --frames 1 --out exports/
```

Writes `exports/<timestamp>/<camera>.jpg` for every resolvable camera. To grab
**every channel** of each NVR/DVR:

```bash
python cctv_viewer.py export -i cameras.csv --all-channels --max-channels 32
```

This probes channels `1..max` (stopping after a couple of empty ones once it has
found some) and writes `exports/<timestamp>/<camera>-ch<N>.jpg`.

## Common options (`resolve` / `view` / `export`)

| Option | Meaning |
|--------|---------|
| `-i, --input` | camera credentials CSV (required) |
| `--db` / `--creds-db` | scraped DB paths (default under `data/`) |
| `--cache` | resolved-URL cache path (default `resolved.csv`) |
| `--no-cache` | ignore the cache and re-resolve |
| `--try-defaults` | retry with scraped vendor default credentials |
| `--timeout` | per-URL probe timeout, seconds (default 8) |
| `--workers` | cameras resolved/exported in parallel (default 6) |
| `-v, --verbose` | debug logging |

## Project layout

```
cctv_viewer.py        CLI entry point
cctv/
  scraper.py          ispyconnect -> data/*.csv
  db.py               query the scraped DB
  fingerprint.py      ONVIF + HTTP/RTSP identification
  resolver.py         identification -> validated working URL
  capture.py          threaded OpenCV stream reader
  viewer.py           OpenCV grid viewer (default)
  webviewer.py        Flask web dashboard (view --web)
  export.py           bulk frame export
  models.py, util.py  shared types and helpers
data/                 scraped database (committed)
examples/             sample input CSV
tests/                unit tests (url + db logic)
```

## Notes & limitations

- ONVIF identification requires a reachable, authorized camera; without it the
  tool relies on HTTP/RTSP fingerprinting and DB/generic URL probing.
- RTSP is forced over TCP for reliability across NAT/Wi-Fi.
- `resolved.csv`, real input CSVs and exported images are git-ignored because
  they contain credentials or captured footage.
- Run `python -m pytest tests/` for the offline unit tests.
```
