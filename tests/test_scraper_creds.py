"""Tests for merging curated default credentials with scraped placeholders."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cctv.scraper import enrich_credentials
from cctv.util import normalize_key


def _pairs(rows, vendor):
    return [(r["default_username"], r["default_password"])
            for r in rows if r["vendor"] == vendor]


def test_curated_pairs_come_first():
    scraped = {normalize_key("Axis"): ("Axis", [("admin", "admin")])}
    rows = enrich_credentials(scraped, use_curated=True)
    axis = _pairs(rows, "Axis")
    assert axis[0] == ("root", "pass")            # curated, most likely
    assert ("admin", "admin") in axis             # scraped, kept as fallback
    assert axis.count(("admin", "admin")) == 1     # de-duplicated


def test_vendor_without_curated_keeps_scraped():
    scraped = {normalize_key("Zee Cure"): ("Zee Cure", [("admin", "admin")])}
    rows = enrich_credentials(scraped, use_curated=True)
    assert _pairs(rows, "Zee Cure") == [("admin", "admin")]


def test_no_curated_flag_emits_only_scraped():
    scraped = {normalize_key("Axis"): ("Axis", [("admin", "admin")])}
    rows = enrich_credentials(scraped, use_curated=False)
    assert _pairs(rows, "Axis") == [("admin", "admin")]


def test_curated_only_vendor_still_emitted():
    # A curated vendor never seen by the scraper is still included.
    rows = enrich_credentials({}, use_curated=True)
    assert ("root", "pass") in _pairs(rows, "Axis")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
