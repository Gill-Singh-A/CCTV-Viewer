"""Tests for channel counting and its persistence in resolved.csv."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cctv.resolver as R
from cctv.models import ResolvedCamera
from cctv.resolver import count_channels, read_resolved, write_resolved

NVR = "rtsp://u:p@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"


def _patch(monkeypatch, live):
    def fake_probe(url, timeout=0):
        return int(re.search(r"channel=(\d+)", url).group(1)) in live
    monkeypatch.setattr(R, "probe_frame", fake_probe)


def test_count_contiguous(monkeypatch):
    _patch(monkeypatch, set(range(1, 6)))
    assert count_channels(NVR, timeout=1, max_channels=64) == 5


def test_count_single_lens_camera():
    # URL with no channel component -> always one channel, no probing needed
    assert count_channels("rtsp://u:p@10.0.0.5:554/profile1") == 1


def test_count_tolerates_gap_within_a_batch(monkeypatch):
    # 50 channels with channel 16 dead — must still discover up to 50.
    live = set(range(1, 51)) - {16}
    _patch(monkeypatch, live)
    assert count_channels(NVR, timeout=1, max_channels=64, batch_size=10) == 50


def test_count_stops_on_fully_empty_batch(monkeypatch):
    _patch(monkeypatch, {1, 2, 3})
    # batch 1..10 has 1-3, batch 11..20 empty -> stop at highest live = 3
    assert count_channels(NVR, timeout=1, max_channels=64, batch_size=10) == 3


def test_count_gap_larger_than_batch_truncates(monkeypatch):
    # A gap >= batch_size ends discovery (documents the batch-size trade-off).
    live = set(range(1, 11)) | set(range(31, 41))
    _patch(monkeypatch, live)
    assert count_channels(NVR, timeout=1, max_channels=64, batch_size=10) == 10


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
