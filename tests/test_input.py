"""Tests for camera-list input parsing (headered and headerless CSVs)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cctv.resolver import load_camera_list


def test_headerless_name_first(tmp_path):
    p = tmp_path / "cams.csv"
    p.write_text(
        "Camera 1,172.28.64.107,admin,123456,80,554\n"
        "Camera 2,172.28.64.108,admin,admin@123,80,554\n"
    )
    cams = load_camera_list(str(p))
    assert len(cams) == 2
    assert cams[0].name == "Camera 1"
    assert cams[0].ip == "172.28.64.107"
    assert cams[0].username == "admin"
    assert cams[1].password == "admin@123"
    assert cams[0].http_port == 80 and cams[0].rtsp_port == 554


def test_headerless_ip_first(tmp_path):
    p = tmp_path / "cams.csv"
    p.write_text("172.28.64.107,admin,123456\n")
    cams = load_camera_list(str(p))
    assert len(cams) == 1
    assert cams[0].ip == "172.28.64.107"
    assert cams[0].name == "172.28.64.107"  # falls back to ip
    assert cams[0].username == "admin" and cams[0].password == "123456"


def test_headered_any_order(tmp_path):
    p = tmp_path / "cams.csv"
    p.write_text(
        "ip,password,username,name\n"
        "10.0.0.5,secret,admin,Gate\n"
    )
    cams = load_camera_list(str(p))
    assert len(cams) == 1
    assert cams[0].ip == "10.0.0.5"
    assert cams[0].username == "admin"
    assert cams[0].password == "secret"
    assert cams[0].name == "Gate"


def test_comments_and_blanks_ignored(tmp_path):
    p = tmp_path / "cams.csv"
    p.write_text(
        "# my cameras\n\n"
        "Gate,10.0.0.5,admin,pw\n"
    )
    cams = load_camera_list(str(p))
    assert len(cams) == 1 and cams[0].ip == "10.0.0.5"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
