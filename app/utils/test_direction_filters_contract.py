"""
Test suite for inbound / outbound (direction) filter parameter normalization contract.
Validates aliases: direction, call_direction, inbound_outbound across values: inbound, outbound, entrante, saliente, all.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.utils.normalizers import normalize_direction
from fastapi import HTTPException


def test_direction_normalization():
    """Test direction parameter normalization against canonical values and aliases."""
    # Inbound variations
    assert normalize_direction("inbound") == "inbound"
    assert normalize_direction("entrante") == "inbound"
    assert normalize_direction("in") == "inbound"
    assert normalize_direction("inbound_call") == "inbound"

    # Outbound variations
    assert normalize_direction("outbound") == "outbound"
    assert normalize_direction("saliente") == "outbound"
    assert normalize_direction("out") == "outbound"
    assert normalize_direction("outbound_call") == "outbound"

    # All / None variations
    assert normalize_direction("all") is None
    assert normalize_direction("todas") is None
    assert normalize_direction("todos") is None
    assert normalize_direction("") is None
    assert normalize_direction(None) is None

    # Invalid value throws 422
    try:
        normalize_direction("invalid_dir")
        assert False, "Should have raised HTTPException"
    except HTTPException as e:
        assert e.status_code == 422

    print("[OK] test_direction_normalization passed.")


if __name__ == "__main__":
    test_direction_normalization()
    print("\n[OK] ALL direction filters contract tests PASSED successfully!")
