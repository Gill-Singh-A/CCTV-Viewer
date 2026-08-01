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


def test_headerless_credentials_before_ip(tmp_path):
    # order: name,username,password,ip,http_port,rtsp_port (IP located by value)
    p = tmp_path / "cams.csv"
    p.write_text(
        "Camera 10,admin,admin,172.31.100.118,,9943\n"
        "Camera 20,admin,admin@123,172.24.124.25,,554\n"
    )
    cams = load_camera_list(str(p))
    assert len(cams) == 2
    assert cams[0].name == "Camera 10"
    assert cams[0].ip == "172.31.100.118"        # not 'admin'
    assert cams[0].username == "admin" and cams[0].password == "admin"
    assert cams[0].rtsp_port == 9943
    # distinct user/pass must NOT be interchanged
    assert cams[1].username == "admin"
    assert cams[1].password == "admin@123"
    assert cams[1].ip == "172.24.124.25"


def test_headerless_no_field_interchange(tmp_path):
    # Every field distinct so any swap would be caught, in credentials-before-ip
    # order and in the documented ip-second order.
    p = tmp_path / "cams.csv"
    p.write_text(
        "GateA,myuser,mypass,10.0.0.5,8080,554\n"      # name,user,pass,ip,http,rtsp
        "GateB,10.0.0.6,otheruser,otherpass,81,555\n"  # name,ip,user,pass,http,rtsp
    )
    a, b = load_camera_list(str(p))
    assert (a.name, a.ip, a.username, a.password, a.http_port, a.rtsp_port) == \
        ("GateA", "10.0.0.5", "myuser", "mypass", 8080, 554)
    assert (b.name, b.ip, b.username, b.password, b.http_port, b.rtsp_port) == \
        ("GateB", "10.0.0.6", "otheruser", "otherpass", 81, 555)


def test_headerless_numeric_credentials_not_mistaken_for_ports(tmp_path):
    p = tmp_path / "cams.csv"
    p.write_text("Cam,888888,888888,172.26.94.132,,554\n")
    cams = load_camera_list(str(p))
    assert cams[0].ip == "172.26.94.132"
    assert cams[0].username == "888888" and cams[0].password == "888888"
    assert cams[0].rtsp_port == 554


def test_headerless_empty_credentials_with_ip(tmp_path):
    p = tmp_path / "cams.csv"
    p.write_text("Camera 1,,,172.26.70.68,,8888\n")
    cams = load_camera_list(str(p))
    assert len(cams) == 1
    assert cams[0].ip == "172.26.70.68" and cams[0].rtsp_port == 8888
    assert cams[0].username == "" and cams[0].password == ""


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
