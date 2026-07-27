"""Unit tests for placeholder substitution and URL assembly (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cctv.util import substitute, build_url, normalize_protocol, normalize_key
from cctv.resolver import inject_credentials


def test_substitute_stream_tokens():
    out = substitute("/axis-cgi/video.cgi?camera=[CHANNEL]&resolution=[WIDTH]x[HEIGHT]",
                     "admin", "pass", channel=2, width=640, height=480)
    assert out == "/axis-cgi/video.cgi?camera=2&resolution=640x480"


def test_substitute_credentials_in_path_are_encoded():
    out = substitute("/videostream.cgi?user=[USERNAME]&pwd=[PASSWORD]",
                     "adm in", "p@ss/word")
    assert out == "/videostream.cgi?user=adm%20in&pwd=p%40ss%2Fword"


def test_substitute_leaves_unknown_tokens():
    assert substitute("/x/[FOO]", "u", "p") == "/x/[FOO]"


def test_build_url_default_port_and_auth():
    url = build_url("rtsp://", "10.0.0.5", 0, "/Streaming/Channels/101",
                    "admin", "secret")
    assert url == "rtsp://admin:secret@10.0.0.5:554/Streaming/Channels/101"


def test_build_url_explicit_port_http():
    url = build_url("http", "10.0.0.5", 8080, "jpg/image.jpg", "u", "p")
    assert url == "http://u:p@10.0.0.5:8080/jpg/image.jpg"


def test_build_url_special_chars_in_credentials():
    url = build_url("rtsp", "10.0.0.5", 554, "/live", "ad@min", "p:ss")
    assert url == "rtsp://ad%40min:p%3Ass@10.0.0.5:554/live"


def test_normalize_protocol():
    assert normalize_protocol("rtsp://") == "rtsp"
    assert normalize_protocol("HTTP://") == "http"
    assert normalize_protocol("") == "http"


def test_normalize_key():
    assert normalize_key("DS-2CD2032-I") == normalize_key("ds2cd2032i")
    assert normalize_key("Tp-Link") == "tplink"


def test_inject_credentials():
    assert inject_credentials("rtsp://10.0.0.5:554/live", "u", "p") == \
        "rtsp://u:p@10.0.0.5:554/live"
    # already has userinfo -> unchanged
    assert inject_credentials("rtsp://a:b@10.0.0.5/live", "u", "p") == \
        "rtsp://a:b@10.0.0.5/live"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
