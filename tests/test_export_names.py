"""Tests for collision-free export filenames when families share a name."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cctv.export import _unique_stems, _safe_name
from cctv.models import ResolvedCamera


def test_distinct_names_unchanged():
    cams = [ResolvedCamera(name="Gate", ip="10.0.0.1"),
            ResolvedCamera(name="Lobby", ip="10.0.0.2")]
    stems = _unique_stems(cams)
    assert set(stems.values()) == {"Gate", "Lobby"}


def test_shared_name_disambiguated_by_ip():
    a = ResolvedCamera(name="Gate", ip="10.0.0.1")
    b = ResolvedCamera(name="Gate", ip="10.0.0.2")
    stems = _unique_stems([a, b])
    assert stems[id(a)] != stems[id(b)]
    assert stems[id(a)] == "Gate_10.0.0.1"
    assert stems[id(b)] == "Gate_10.0.0.2"


def test_exact_duplicate_rows_get_counter():
    a = ResolvedCamera(name="Gate", ip="10.0.0.1")
    b = ResolvedCamera(name="Gate", ip="10.0.0.1")   # identical name + ip
    stems = _unique_stems([a, b])
    vals = {stems[id(a)], stems[id(b)]}
    assert len(vals) == 2                    # never the same file
    assert "Gate_10.0.0.1" in vals and "Gate_10.0.0.1_2" in vals


def test_all_stems_unique_across_many_collisions():
    cams = [ResolvedCamera(name="Cam", ip=f"10.0.0.{i}") for i in range(5)]
    cams += [ResolvedCamera(name="Cam", ip="10.0.0.1")]  # collides with first
    stems = _unique_stems(cams)
    assert len(set(stems.values())) == len(cams)


def test_safe_name_sanitizes():
    assert _safe_name(ResolvedCamera(name="Front Gate #1", ip="1.2.3.4")) == \
        "Front_Gate__1"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
