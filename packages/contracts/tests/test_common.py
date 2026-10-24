import pytest
from contracts.common import Money, SourceRef
from pydantic import ValidationError


def test_money_accepts_canonical_integer_string():
    m = Money(amount_cents="150000")
    assert m.as_int() == 150000
    assert m.currency == "USD"


def test_money_rejects_float():
    with pytest.raises(ValidationError):
        Money(amount_cents=1500.0)  # type: ignore[arg-type]


def test_money_rejects_non_integer_string():
    with pytest.raises(ValidationError):
        Money(amount_cents="abc")


def test_money_rejects_unsupported_currency():
    with pytest.raises(ValidationError):
        Money(amount_cents="100", currency="EUR")  # type: ignore[arg-type]


def test_money_allows_negative_for_adjustments():
    m = Money(amount_cents="-500")
    assert m.as_int() == -500


def test_source_ref_round_trip():
    ref = SourceRef(
        source_id="src_123",
        source_version=2,
        content_hash="sha256:abc",
        locator_kind="page",
        locator="p3",
        quoted_excerpt="15% commission",
    )
    assert ref.locator_kind == "page"
    assert ref.quoted_excerpt == "15% commission"
