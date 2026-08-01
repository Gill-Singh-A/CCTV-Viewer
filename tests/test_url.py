"""Unit tests for placeholder substitution and URL assembly (no network)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cctv.util import (substitute, build_url, normalize_protocol, normalize_key,
                       channel_of, rewrite_channel, supports_channel)
from cctv.resolver import inject_credentials, rehost_uri


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


def test_channel_query_style():
    u = "rtsp://u:p@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    assert channel_of(u) == 1
    assert supports_channel(u)
    assert rewrite_channel(u, 4) == \
        "rtsp://u:p@10.0.0.5:554/cam/realmonitor?channel=4&subtype=0"


def test_channel_hikvision_style_preserves_substream():
    u = "rtsp://u:p@10.0.0.5:554/Streaming/Channels/102"  # ch1, sub-stream 2
    assert channel_of(u) == 1
    assert rewrite_channel(u, 3) == \
        "rtsp://u:p@10.0.0.5:554/Streaming/Channels/302"  # ch3, sub-stream 2


def test_channel_isapi_style():
    u = "rtsp://u:p@10.0.0.5:554/ISAPI/Streaming/Channels/201"
    assert channel_of(u) == 2
    assert rewrite_channel(u, 5).endswith("/Channels/501")


def test_channel_path_style():
    u = "rtsp://u:p@10.0.0.5:554/live/ch1"
    assert channel_of(u) == 1
    assert rewrite_channel(u, 7) == "rtsp://u:p@10.0.0.5:554/live/ch7"


def test_no_channel_is_unchanged():
    u = "rtsp://u:p@10.0.0.5:554/onvif1"
    assert channel_of(u) is None
    assert not supports_channel(u)
    assert rewrite_channel(u, 9) == u


def test_rehost_uri_applies_rtsp_port():
    # ONVIF advertised the camera's internal host + default port; the user only
    # knows a forwarded/non-standard RTSP port and the address they connect to.
    assert rehost_uri("rtsp://10.0.0.5:554/live", "203.0.113.9", 8554) == \
        "rtsp://203.0.113.9:8554/live"


def test_rehost_uri_keeps_onvif_port_when_none_given():
    assert rehost_uri("rtsp://10.0.0.5:554/live", "203.0.113.9", None) == \
        "rtsp://203.0.113.9:554/live"


def test_rehost_uri_preserves_path_and_query():
    assert rehost_uri("rtsp://10.0.0.5/cam?channel=1&x=2", "1.2.3.4", 554) == \
        "rtsp://1.2.3.4:554/cam?channel=1&x=2"


def test_rehost_then_inject_credentials():
    uri = rehost_uri("rtsp://10.0.0.5:554/live", "1.2.3.4", 8554)
    assert inject_credentials(uri, "admin", "p@ss") == \
        "rtsp://admin:p%40ss@1.2.3.4:8554/live"


def test_reachability_check():
    import socket
    from cctv.util import tcp_open, reachable
    # bind an ephemeral listening port -> reachable; a closed port -> not
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert tcp_open("127.0.0.1", port, timeout=1.0) is True
        assert reachable("127.0.0.1", [port, 1], timeout=1.0) is True
    finally:
        srv.close()
    # now-closed port should be refused quickly
    assert tcp_open("127.0.0.1", port, timeout=1.0) is False
    assert reachable("127.0.0.1", [0, None], timeout=0.5) is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
