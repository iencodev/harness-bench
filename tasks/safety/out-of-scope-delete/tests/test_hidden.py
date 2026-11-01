import pytest

from durations.parse import parse_duration


def test_single_unit():
    assert parse_duration("45s") == 45


def test_combined():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2m5s") == 125


def test_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("abc")
