"""INR localization tests (traceable, non-fabricating)."""

from __future__ import annotations

import json

from gridmind.currency import (DEFAULT_USD_TO_INR, INR_CURRENCY, SOURCE_CURRENCY,
                               ConversionConfig, add_currency_metadata,
                               convert_numeric_tree, format_inr, inr_to_usd,
                               traceability_table, usd_to_inr)


def test_usd_to_inr_preserves_canonical_and_adds_derived():
    cfg = ConversionConfig(usd_to_inr=83.5, source="test", date="2026-01-01")
    assert usd_to_inr(1000.0, cfg) == 83500.0
    assert inr_to_usd(83500.0, cfg) == 1000.0


def test_conversion_is_explicit_and_provenance_recorded():
    cfg = ConversionConfig(usd_to_inr=84.0, source="RBI/reference", date="2026-10-24")
    meta = add_currency_metadata({"cost_usd": 100.0}, cfg)
    assert meta["currency"]["canonical"] == SOURCE_CURRENCY == "USD"
    assert meta["currency"]["display"] == INR_CURRENCY == "INR"
    assert meta["currency"]["conversion"]["usd_to_inr"] == 84.0
    assert meta["currency"]["conversion"]["source"] == "RBI/reference"
    assert meta["currency"]["conversion"]["date"] == "2026-10-24"


def test_does_not_relabel_old_usd_values():
    data = {"controller_cost_usd": 599839.247668842, "note": "keep USD"}
    out = convert_numeric_tree(data)
    # USD preserved, INR derived separately
    assert out["controller_cost_usd"] == 599839.247668842
    assert out["controller_cost_inr"] == 599839.247668842 * DEFAULT_USD_TO_INR


def test_indian_formatting():
    assert format_inr(1234567.89) == "₹12,34,567.89"
    assert format_inr(1234.5) == "₹1,234.50"
    assert format_inr(None) == "n/a"


def test_traceability_table_is_complete():
    cfg = ConversionConfig(usd_to_inr=83.0, source="test")
    rows = traceability_table([{"label": "lp_cost", "value_usd": 599839.25}], cfg)
    assert rows[0]["value_usd"] == 599839.25
    assert rows[0]["value_inr"] == 599839.25 * 83.0
    assert rows[0]["source_currency"] == "USD"
    assert rows[0]["target_currency"] == "INR"
    assert rows[0]["usd_to_inr"] == 83.0


def test_rejects_invalid_rate_or_direction():
    try:
        ConversionConfig(usd_to_inr=0)
        assert False
    except ValueError:
        pass
    try:
        ConversionConfig(direction="INR_to_USD")
        assert False
    except ValueError:
        pass
