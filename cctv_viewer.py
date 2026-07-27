#!/usr/bin/env python3
"""CCTV Viewer — command-line entry point.

Subcommands:
  scrape    Build the ispyconnect camera-URL database (CSV).
  resolve   Fingerprint cameras from a credentials CSV and find working URLs.
  view      Open the OpenCV grid viewer (auto-resolves first).
  export    Bulk-export still frames from all cameras.

For authorized use only — operate cameras you own or have explicit permission
to access.
"""

from __future__ import annotations

import argparse
import sys

from cctv.util import get_logger, set_verbose

log = get_logger("cctv.cli")


def cmd_scrape(args: argparse.Namespace) -> int:
    from cctv.scraper import scrape
    vendors = [v.strip() for v in args.vendors.split(",")] if args.vendors else None
    n_rows, n_creds = scrape(out_cameras=args.out, out_creds=args.creds_out,
                             vendors=vendors, limit=args.limit,
                             delay=args.delay, refresh=args.refresh)
    log.info("Wrote %s (%d rows) and %s (%d default-cred entries).",
             args.out, n_rows, args.creds_out, n_creds)
    return 0


def _load_db(args) -> "object":
    from cctv.db import CameraDB
    return CameraDB.load(args.db, args.creds_db)


def _resolve(args):
    from cctv.resolver import resolve_cameras
    db = _load_db(args)
    return resolve_cameras(
        args.input, db, cache_path=args.cache,
        use_cache=args.use_cache, try_defaults=args.try_defaults,
        probe_timeout=args.timeout, workers=args.workers,
        retry_unresolved=args.retry_unresolved, retry_timeout=args.retry_timeout,
        reach_timeout=args.reach_timeout,
        max_candidates=0 if args.all_templates else args.max_candidates,
        count_channels=args.count_channels, count_max=args.count_max,
    )


def cmd_resolve(args: argparse.Namespace) -> int:
    resolved = _resolve(args)
    ok = [r for r in resolved if r.ok]
    print(f"\n{'NAME':22} {'IP':16} {'VENDOR':12} {'STATUS':11} {'CH':>3} METHOD")
    print("-" * 82)
    for r in resolved:
        ch = str(r.channels) if r.channels else "-"
        print(f"{r.name[:21]:22} {r.ip[:15]:16} {(r.vendor or '?')[:11]:12} "
              f"{r.status:11} {ch:>3} {r.method}")
    unreachable = sum(1 for r in resolved if r.status == "UNREACHABLE")
    unresolved = sum(1 for r in resolved if r.status == "UNRESOLVED")
    print(f"\n{len(ok)}/{len(resolved)} cameras resolved "
          f"({unreachable} unreachable, {unresolved} unresolved). "
          f"Full details (with URLs) cached in {args.cache}")
    return 0 if ok else 1


def cmd_view(args: argparse.Namespace) -> int:
    resolved = _resolve(args)
    if not any(r.ok for r in resolved):
        log.error("No cameras resolved — nothing to view.")
        return 1
    if args.web:
        from cctv.webviewer import serve
        serve(resolved, host=args.host, port=args.port)
    else:
        from cctv.viewer import view
        view(resolved, mode=args.mode)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from cctv.export import export_cameras
    resolved = _resolve(args)
    if not any(r.ok for r in resolved):
        log.error("No cameras resolved — nothing to export.")
        return 1
    export_cameras(resolved, out_root=args.out, frames=args.frames,
                   timeout=args.timeout, workers=args.workers,
                   all_channels=args.all_channels,
                   max_channels=args.max_channels)
    return 0


def _add_resolve_opts(p: argparse.ArgumentParser,
                      read_cache_by_default: bool) -> None:
    p.add_argument("--input", "-i", required=True,
                   help="Camera credentials CSV (ip,username,password,...).")
    p.add_argument("--db", default="data/cameras.csv", help="Scraped camera DB CSV.")
    p.add_argument("--creds-db", default="data/default_credentials.csv",
                   help="Scraped default-credentials CSV.")
    p.add_argument("--cache", default="resolved.csv",
                   help="Where to read/write resolved stream URLs (has credentials).")
    # `view`/`export` reuse the cache for fast startup; `resolve` re-resolves by
    # default (its whole job) but still writes the cache for the others to use.
    if read_cache_by_default:
        p.add_argument("--no-cache", dest="use_cache", action="store_false",
                       help="Ignore the resolved cache and re-resolve fresh.")
        p.set_defaults(use_cache=True)
    else:
        p.add_argument("--use-cache", dest="use_cache", action="store_true",
                       help="Reuse an existing resolved cache instead of "
                            "resolving fresh.")
        p.set_defaults(use_cache=False)
    p.add_argument("--try-defaults", action="store_true",
                   help="If supplied creds fail, retry with scraped vendor defaults.")
    p.add_argument("--timeout", type=float, default=8.0,
                   help="Per-URL probe timeout in seconds.")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel cameras during resolve/export.")
    p.add_argument("--retry-unresolved", action="store_true",
                   help="After the parallel pass, retry UNRESOLVED cameras "
                        "serially with a longer timeout (recovers flaky/busy devices).")
    p.add_argument("--retry-timeout", type=float, default=12.0,
                   help="Per-URL probe timeout for the --retry-unresolved pass.")
    p.add_argument("--reach-timeout", type=float, default=2.0,
                   help="Fast TCP reachability pre-check timeout; offline hosts "
                        "are marked UNREACHABLE and skipped. 0 disables the check.")
    p.add_argument("--max-candidates", type=int, default=25,
                   help="Max URL templates probed per camera (per credential "
                        "set). Default 25; higher = more thorough but slower.")
    p.add_argument("--all", dest="all_templates", action="store_true",
                   help="Probe ALL matching templates with no cap (exhaustive, "
                        "slower). Overrides --max-candidates.")
    p.add_argument("--count-channels", action="store_true",
                   help="After resolving, count each family's live channels and "
                        "store it; viewers/export then cap to that number.")
    p.add_argument("--count-max", type=int, default=64,
                   help="Highest channel probed when counting (default 64).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cctv_viewer",
        description="Fingerprint, resolve, view and export IP-camera streams "
                    "from credentials only. Authorized use only.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    ps = sub.add_parser("scrape", help="Build the ispyconnect camera-URL database.")
    ps.add_argument("--out", default="data/cameras.csv")
    ps.add_argument("--creds-out", default="data/default_credentials.csv")
    ps.add_argument("--vendors", help="Comma-separated brand slugs to limit scope.")
    ps.add_argument("--limit", type=int, help="Only scrape the first N brands.")
    ps.add_argument("--delay", type=float, default=0.5, help="Delay between requests.")
    ps.add_argument("--refresh", action="store_true", help="Ignore the HTML cache.")
    ps.set_defaults(func=cmd_scrape)

    pr = sub.add_parser("resolve", help="Fingerprint cameras and find working URLs.")
    _add_resolve_opts(pr, read_cache_by_default=False)
    pr.set_defaults(func=cmd_resolve)

    pv = sub.add_parser("view", help="Open the viewer (OpenCV grid by default).")
    _add_resolve_opts(pv, read_cache_by_default=True)
    pv.add_argument("--mode", choices=["single", "grid4", "grid9", "grid16"],
                    default="grid4", help="Initial grid layout (OpenCV viewer).")
    pv.add_argument("--web", action="store_true",
                    help="Serve a Flask web dashboard instead of the OpenCV grid.")
    pv.add_argument("--host", default="127.0.0.1", help="Web viewer bind host.")
    pv.add_argument("--port", type=int, default=5000, help="Web viewer port.")
    pv.set_defaults(func=cmd_view)

    pe = sub.add_parser("export", help="Bulk-export still frames.")
    _add_resolve_opts(pe, read_cache_by_default=True)
    pe.add_argument("--out", default="exports", help="Output root folder.")
    pe.add_argument("--frames", type=int, default=1, help="Frames per camera.")
    pe.add_argument("--all-channels", action="store_true",
                    help="Export one frame from every channel of each NVR/DVR.")
    pe.add_argument("--max-channels", type=int, default=64,
                    help="Highest channel number to probe with --all-channels.")
    pe.set_defaults(func=cmd_export)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    set_verbose(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        log.error("File not found: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
