"""Unit tests for the DB query layer using a small fixture CSV."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cctv.db import CameraDB


def _write_fixture(tmp_path):
    cams = tmp_path / "cameras.csv"
    cams.write_text(
        "vendor,model,type,protocol,port,url\n"
        "Hikvision,ds-2cd2032,FFMPEG,rtsp,0,/Streaming/Channels/101\n"
        "Hikvision,ds-2cd2032,JPEG,http,80,/onvif-http/snapshot?Profile_1\n"
        "Hikvision,*,MJPEG,http,80,/videostream.cgi\n"
        "Axis,206,FFMPEG,rtsp,0,/onvif-media/media.amp\n"
    )
    creds = tmp_path / "creds.csv"
    creds.write_text(
        "vendor,default_username,default_password\n"
        "Hikvision,admin,12345\n"
        "Hikvision,admin,admin\n"
    )
    return str(cams), str(creds)


def test_templates_prefer_model_and_stream(tmp_path):
    cams, creds = _write_fixture(tmp_path)
    db = CameraDB.load(cams, creds)
    rows = db.templates_for("Hikvision", "ds-2cd2032")
    # RTSP/FFMPEG stream should rank first
    assert rows[0].protocol == "rtsp"
    assert rows[0].path == "/Streaming/Channels/101"
    # de-duplication: 3 distinct hikvision rows
    assert len({(r.protocol, r.port, r.path) for r in rows}) == 3


def test_templates_vendor_only(tmp_path):
    cams, creds = _write_fixture(tmp_path)
    db = CameraDB.load(cams, creds)
    rows = db.templates_for("Axis")
    assert any(r.path == "/onvif-media/media.amp" for r in rows)


def test_fuzzy_vendor_match(tmp_path):
    cams, creds = _write_fixture(tmp_path)
    db = CameraDB.load(cams, creds)
    # different casing / spacing still matches (normalize_key strips non-alnum)
    assert db.known_vendor("hik vision") is True
    assert db.known_vendor("HIKVISION") is True
    assert db.known_vendor("Nonexistent") is False


def test_default_creds(tmp_path):
    from cctv.db import COMMON_DEFAULTS
    cams, creds = _write_fixture(tmp_path)
    db = CameraDB.load(cams, creds)
    # known vendor: multiple pairs, curated/scraped order preserved
    assert db.default_creds_for("Hikvision") == [("admin", "12345"), ("admin", "admin")]
    # unknown vendor: common generic fallback
    assert db.default_creds_for("Unknown") == list(COMMON_DEFAULTS)
    assert db.default_creds_for(None) == list(COMMON_DEFAULTS)


def test_generic_templates_present():
    rows = CameraDB.generic_templates()
    assert any("Streaming/Channels" in r.path for r in rows)
    assert all(r.port == 0 for r in rows)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
