"""Tests for channel counting and its persistence in resolved.csv."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cctv.resolver as R
from cctv.models import ResolvedCamera
from cctv.resolver import count_channels, read_resolved, write_resolved

NVR = "rtsp://u:p@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"


def test_count_contiguous(monkeypatch):
    live = {1, 2, 3, 4, 5}

    def fake_probe(url, timeout=0):
        return int(re.search(r"channel=(\d+)", url).group(1)) in live
    monkeypatch.setattr(R, "probe_frame", fake_probe)
    assert count_channels(NVR, timeout=1, max_channels=64) == 5


def test_count_single_lens_camera():
    # URL with no channel component -> always one channel, no probing needed
    assert count_channels("rtsp://u:p@10.0.0.5:554/profile1") == 1


def test_count_stops_after_miss_streak(monkeypatch):
    live = {1, 2, 3}

    def fake_probe(url, timeout=0):
        return int(re.search(r"channel=(\d+)", url).group(1)) in live
    monkeypatch.setattr(R, "probe_frame", fake_probe)
    # channels 4 and 5 empty -> stop; count is 3 even with max_channels=64
    assert count_channels(NVR, timeout=1, max_channels=64, miss_streak=2) == 3


def test_channels_persist_in_resolved_csv(tmp_path):
    p = tmp_path / "resolved.csv"
    cams = [
        ResolvedCamera(name="NVR", ip="10.0.0.5", vendor="CPPLUS", protocol="rtsp",
                       working_url=NVR, status="OK", method="onvif", channels=8),
        ResolvedCamera(name="Cam", ip="10.0.0.6", protocol="rtsp",
                       working_url="rtsp://u:p@10.0.0.6:554/profile1",
                       status="OK", method="onvif", channels=1),
    ]
    write_resolved(str(p), cams)
    back = read_resolved(str(p))
    assert [c.channels for c in back] == [8, 1]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
