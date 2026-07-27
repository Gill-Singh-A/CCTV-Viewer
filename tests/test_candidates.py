"""Tests for candidate-URL building and the --max-candidates/--all cap."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cctv.db import CameraDB
from cctv.models import CameraInput, FingerprintResult
from cctv.resolver import Resolver


def _db(tmp_path):
    cams = tmp_path / "cameras.csv"
    rows = "vendor,model,type,protocol,port,url\n"
    for i in range(6):
        rows += f"Acme,m{i},FFMPEG,rtsp,0,/path{i}\n"
    cams.write_text(rows)
    creds = tmp_path / "creds.csv"
    creds.write_text("vendor,default_username,default_password\n")
    return CameraDB.load(str(cams), str(creds))


def _cam():
    return CameraInput(name="x", ip="10.0.0.1", username="u", password="p")


def test_cap_limits_candidates(tmp_path):
    db = _db(tmp_path)
    r = Resolver(db, max_candidates=3)
    cands = r._candidate_urls(_cam(), FingerprintResult(vendor="Acme"), "u", "p")
    assert len(cands) == 3


def test_all_templates_no_cap(tmp_path):
    db = _db(tmp_path)
    capped = Resolver(db, max_candidates=3)._candidate_urls(
        _cam(), FingerprintResult(vendor="Acme"), "u", "p")
    uncapped = Resolver(db, max_candidates=0)._candidate_urls(
        _cam(), FingerprintResult(vendor="Acme"), "u", "p")
    # 6 vendor templates + 16 generic common paths, all distinct URLs here
    assert len(uncapped) == 6 + 16
    assert len(uncapped) > len(capped)


def test_onvif_uri_ranks_first(tmp_path):
    db = _db(tmp_path)
    fp = FingerprintResult(vendor="Acme", direct_uri="rtsp://10.0.0.1:554/onvif")
    cands = Resolver(db, max_candidates=25)._candidate_urls(_cam(), fp, "u", "p")
    assert cands[0][0] == "onvif"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
