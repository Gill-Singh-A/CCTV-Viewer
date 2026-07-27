"""Scrape the ispyconnect.com camera connection database.

Produces two CSVs:
  * ``data/cameras.csv``            -> vendor,model,type,protocol,port,url
  * ``data/default_credentials.csv`` -> vendor,default_username,default_password

The brand pages are plain server-rendered HTML. Each connection row exposes its
fields as ``data-*`` attributes on the ``<tr>`` and one ``<span class="model-anchor">``
per compatible model.

Note on default credentials: ispyconnect only pre-fills a generic ``admin``/
``admin`` into its ``#txtCamUser`` / ``#txtCamPass`` form regardless of vendor —
it is a "try these" placeholder, not a real factory default. So the scraped
value is merged with the curated ``CURATED_DEFAULT_CREDENTIALS`` table (real,
publicly documented per-vendor defaults) which takes priority; the scraped
``admin``/``admin`` is kept only as a last-resort fallback.
"""

from __future__ import annotations

import csv
import hashlib
import os
import time
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .util import get_logger, normalize_key, normalize_protocol, slug_to_vendor

log = get_logger("cctv.scraper")

BASE_URL = "https://www.ispyconnect.com"
LIST_URL = f"{BASE_URL}/cameras"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CAMERAS_HEADER = ["vendor", "model", "type", "protocol", "port", "url"]
CREDS_HEADER = ["vendor", "default_username", "default_password"]

# ispyconnect's connection form only ever pre-fills a generic ``admin``/``admin``
# — it is a "try these" placeholder, not a real per-vendor factory default. This
# curated table fills that gap with the publicly documented factory defaults for
# the major CCTV / IP-camera vendors. Each vendor maps to a priority-ordered list
# of ``(username, password)`` pairs (many brands ship several well-known defaults
# across firmware revisions and regions); an empty string means a blank password.
#
# These are documented defaults, not guarantees: verify against the specific
# model/firmware, as newer devices increasingly force a password change on first
# boot. Canonical vendor names here are matched to scraped/fingerprinted vendors
# via ``normalize_key`` (case- and punctuation-insensitive).
CURATED_DEFAULT_CREDENTIALS: dict[str, list[tuple[str, str]]] = {
    "Hikvision":     [("admin", "12345"), ("admin", "admin")],
    "Dahua":         [("admin", "admin"), ("admin", "123456"),
                      ("888888", "888888"), ("666666", "666666")],
    "Axis":          [("root", "pass"), ("root", "root"), ("admin", "admin")],
    "Amcrest":       [("admin", "admin"), ("admin", "")],
    "Reolink":       [("admin", ""), ("admin", "admin")],
    "Foscam":        [("admin", ""), ("admin", "admin"), ("admin", "foscam")],
    "Vivotek":       [("root", ""), ("root", "root")],
    "Mobotix":       [("admin", "meinsm")],
    "Bosch":         [("service", "service"), ("live", "live"), ("admin", "admin")],
    "Samsung":       [("admin", "4321"), ("admin", "1111111"), ("root", "root"),
                      ("admin", "admin")],
    "Hanwha":        [("admin", "4321"), ("admin", "1111111"), ("admin", "admin")],
    "Sony":          [("admin", "admin")],
    "Panasonic":     [("admin", "12345"), ("admin", "admin")],
    "Ubiquiti":      [("ubnt", "ubnt")],
    "D-Link":        [("admin", ""), ("admin", "admin")],
    "Tp-link":       [("admin", "admin")],
    "Trendnet":      [("admin", "admin")],
    "Acti":          [("admin", "123456"), ("Admin", "123456")],
    "Geovision":     [("admin", "admin")],
    "Lorex":         [("admin", "000000"), ("admin", "admin")],
    "Swann":         [("admin", "12345"), ("admin", "")],
    "Uniview":       [("admin", "123456"), ("admin", "admin")],
    "Honeywell":     [("administrator", "1234"), ("admin", "1234")],
    "Pelco":         [("admin", "admin")],
    "Avigilon":      [("administrator", "")],
    "Arecont Vision": [("admin", ""), ("admin", "admin")],
    "Arecont":       [("admin", ""), ("admin", "admin")],  # ispyconnect brand name
    "Grandstream":   [("admin", "admin")],
    "Digital Watchdog": [("admin", "admin"), ("admin", "")],
    "Toshiba":       [("root", "ikwd"), ("admin", "admin")],
    "Sanyo":         [("admin", "admin")],
    "Cisco":         [("admin", "admin")],
    "Instar":        [("admin", "instar"), ("admin", "")],
    "Zavio":         [("admin", "admin")],
    "Brickcom":      [("admin", "admin")],
    "Interlogix":    [("admin", "1234")],
    "Wisenet":       [("admin", "4321"), ("admin", "1111111")],
    "Speco":         [("admin", "1234"), ("admin", "admin")],
    "Lts":           [("admin", "12345"), ("admin", "admin")],
    "Annke":         [("admin", ""), ("admin", "admin")],
    "Y-cam":         [("admin", "")],
    "Airlive":       [("admin", "airlive")],
    "Planet":        [("admin", "admin")],
    "Ipx-ddk":       [("root", "admin"), ("root", "Admin")],
    "Basler":        [("admin", "admin")],
}


def enrich_credentials(scraped: dict, use_curated: bool = True) -> list[dict]:
    """Merge curated per-vendor defaults with scraped ispyconnect defaults.

    ``scraped`` maps a normalized vendor key to ``(display_name, [(user, pass),
    ...])``. Curated pairs are emitted first (most-likely factory defaults), then
    any scraped pair not already present, so the generic ``admin/admin`` becomes
    a last-resort fallback rather than the only entry. Returns a list of
    ``CREDS_HEADER`` row dicts, de-duplicated per vendor.
    """
    curated_by_key = {normalize_key(v): (v, pairs)
                      for v, pairs in CURATED_DEFAULT_CREDENTIALS.items()}
    keys = list(scraped.keys()) + [k for k in curated_by_key if k not in scraped]

    rows: list[dict] = []
    for key in keys:
        display, scraped_pairs = scraped.get(key, (None, []))
        cur_display, cur_pairs = curated_by_key.get(key, (None, []))
        name = display or cur_display
        if not name:
            continue
        ordered, seen = [], set()
        for u, p in (cur_pairs if use_curated else []):
            if (u, p) not in seen:
                seen.add((u, p))
                ordered.append((u, p))
        for u, p in scraped_pairs:
            if (u, p) not in seen:
                seen.add((u, p))
                ordered.append((u, p))
        for u, p in ordered:
            rows.append({"vendor": name,
                         "default_username": u, "default_password": p})
    return rows


class Scraper:
    def __init__(self, cache_dir: str = ".cache", delay: float = 0.5,
                 timeout: float = 20.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    # -- fetching ---------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        return self.cache_dir / f"{digest}.html"

    def fetch(self, url: str, use_cache: bool = True) -> Optional[str]:
        cache_file = self._cache_path(url)
        if use_cache and cache_file.exists():
            return cache_file.read_text(encoding="utf-8", errors="replace")
        for attempt in range(1, 4):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    cache_file.write_text(resp.text, encoding="utf-8")
                    time.sleep(self.delay)  # be polite
                    return resp.text
                log.warning("GET %s -> HTTP %s", url, resp.status_code)
            except requests.RequestException as exc:
                log.warning("GET %s failed (try %d/3): %s", url, attempt, exc)
            time.sleep(self.delay * attempt * 2)
        return None

    # -- parsing ----------------------------------------------------------
    def brand_slugs(self, html: str) -> list[str]:
        """Extract every ``camera/<slug>`` brand slug from the A-Z index table."""
        soup = BeautifulSoup(html, "lxml")
        slugs: list[str] = []
        seen: set[str] = set()
        for a in soup.select('a[href*="camera/"]'):
            href = a.get("href", "")
            # normalise ./camera/x , ../camera/x , /camera/x , full urls
            marker = "camera/"
            idx = href.rfind(marker)
            if idx == -1:
                continue
            slug = href[idx + len(marker):].strip("/").split("?")[0]
            if not slug or "/" in slug or slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
        return slugs

    def parse_brand(self, slug: str, html: str):
        """Return (list[camera_row_dict], default_creds_dict|None)."""
        soup = BeautifulSoup(html, "lxml")
        vendor = self._vendor_name(soup, slug)

        rows = []
        for tr in soup.select("tr[data-path]"):
            path = (tr.get("data-path") or "").strip()
            if not path:
                continue
            protocol = normalize_protocol(tr.get("data-protocol") or "http://")
            conn = (tr.get("data-conn") or "").strip().upper()
            try:
                port = int(tr.get("data-port") or 0)
            except ValueError:
                port = 0
            models = [span.get("id", "").strip()
                      for span in tr.select("span.model-anchor")
                      if span.get("id", "").strip()]
            if not models:
                models = ["*"]  # generic entry applying to all models of vendor
            for model in models:
                rows.append({
                    "vendor": vendor,
                    "model": model,
                    "type": conn,
                    "protocol": protocol,
                    "port": port,
                    "url": path,
                })

        creds = self._default_creds(soup, vendor)
        return rows, creds

    @staticmethod
    def _vendor_name(soup: BeautifulSoup, slug: str) -> str:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            text = h1.get_text(strip=True)
            # h1 is often like "Axis IP Camera Connection URLs"
            for tail in (" IP Camera", " Camera", " Connection"):
                if tail in text:
                    text = text.split(tail)[0]
                    break
            if text:
                return text.strip()
        return slug_to_vendor(slug)

    @staticmethod
    def _default_creds(soup: BeautifulSoup, vendor: str) -> Optional[dict]:
        user_el = soup.select_one("#txtCamUser")
        pass_el = soup.select_one("#txtCamPass")
        if user_el is None and pass_el is None:
            return None
        user = (user_el.get("value") if user_el else "") or ""
        pwd = (pass_el.get("value") if pass_el else "") or ""
        if not user and not pwd:
            return None
        return {"vendor": vendor,
                "default_username": user.strip(),
                "default_password": pwd.strip()}

    # -- orchestration ----------------------------------------------------
    def run(self, out_cameras: str, out_creds: str,
            vendors: Optional[Iterable[str]] = None,
            limit: Optional[int] = None,
            refresh: bool = False,
            curated: bool = True) -> tuple[int, int]:
        list_html = self.fetch(LIST_URL, use_cache=not refresh)
        if not list_html:
            raise RuntimeError(f"Could not fetch brand index: {LIST_URL}")

        slugs = self.brand_slugs(list_html)
        if vendors:
            wanted = {v.lower() for v in vendors}
            slugs = [s for s in slugs if s.lower() in wanted]
        if limit:
            slugs = slugs[:limit]
        log.info("Scraping %d brands ...", len(slugs))

        Path(out_cameras).parent.mkdir(parents=True, exist_ok=True)
        n_rows = 0
        # normalized vendor key -> (display_name, [(user, pass), ...])
        scraped_creds: dict[str, tuple[str, list[tuple[str, str]]]] = {}
        with open(out_cameras, "w", newline="", encoding="utf-8") as cam_f:
            cam_w = csv.DictWriter(cam_f, fieldnames=CAMERAS_HEADER)
            cam_w.writeheader()

            for i, slug in enumerate(slugs, 1):
                url = f"{BASE_URL}/camera/{slug}"
                html = self.fetch(url, use_cache=not refresh)
                if not html:
                    log.warning("[%d/%d] skip %s (no html)", i, len(slugs), slug)
                    continue
                rows, creds = self.parse_brand(slug, html)
                for r in rows:
                    cam_w.writerow(r)
                n_rows += len(rows)
                if creds:
                    key = normalize_key(creds["vendor"])
                    _, pairs = scraped_creds.setdefault(key, (creds["vendor"], []))
                    pair = (creds["default_username"], creds["default_password"])
                    if pair not in pairs:
                        pairs.append(pair)
                if i % 25 == 0 or i == len(slugs):
                    log.info("[%d/%d] %s -> %d rows (total %d)",
                             i, len(slugs), slug, len(rows), n_rows)

        cred_rows = enrich_credentials(scraped_creds, use_curated=curated)
        with open(out_creds, "w", newline="", encoding="utf-8") as cred_f:
            cred_w = csv.DictWriter(cred_f, fieldnames=CREDS_HEADER)
            cred_w.writeheader()
            cred_w.writerows(cred_rows)
        n_creds = len(cred_rows)
        n_vendors = len({normalize_key(r["vendor"]) for r in cred_rows})

        log.info("Done. %d camera rows, %d default-cred pairs across %d vendors "
                 "(curated=%s).", n_rows, n_creds, n_vendors, curated)
        return n_rows, n_creds


def scrape(out_cameras: str = "data/cameras.csv",
           out_creds: str = "data/default_credentials.csv",
           vendors: Optional[Iterable[str]] = None,
           limit: Optional[int] = None,
           delay: float = 0.5,
           refresh: bool = False,
           curated: bool = True,
           cache_dir: str = ".cache") -> tuple[int, int]:
    """Convenience wrapper used by the CLI."""
    scraper = Scraper(cache_dir=cache_dir, delay=delay)
    return scraper.run(out_cameras, out_creds, vendors=vendors,
                       limit=limit, refresh=refresh, curated=curated)
