from decimal import Decimal

from invoicing.core import calculate_total


def test_half_up_rounding():
    assert calculate_total([{"price": "2.50", "qty": 1}], 0.07) == Decimal("2.68")


def test_no_tax():
    assert calculate_total([{"price": "19.99", "qty": 3}], 0) == Decimal("59.97")


def test_empty_invoice_is_zero_cents():
    assert calculate_total([], 0.2) == Decimal("0.00")
