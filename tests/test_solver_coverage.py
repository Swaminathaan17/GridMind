"""Stage 5E multi-solver coverage classification and probes."""
from __future__ import annotations

import json

import pytest

from benchmarks import solver_coverage as sc
from helpers import SOLVER_MSG, SOLVER_OK

needs_solver = pytest.mark.skipif(not SOLVER_OK, reason=f"solver unavailable: {SOLVER_MSG}")


def _record(*, available=True, supports_mip=True, lp="passed", mip="passed"):
    return {
        "available": available,
        "supports_mip": supports_mip,
        "lp": {"status": lp},
        "mip": {"status": mip},
    }


def test_objective_equivalence_uses_relative_tolerance():
    assert sc._objective_equivalent(100.0, 100.0)
    assert sc._objective_equivalent(100.00001, 100.0)
    assert not sc._objective_equivalent(100.01, 100.0)
    assert not sc._objective_equivalent(None, 100.0)
    assert not sc._objective_equivalent(float("nan"), 100.0)


def test_unavailable_and_unsupported_are_reported_not_failed():
    summary = sc._classify_coverage({
        "glpk": _record(supports_mip=False, mip="unsupported"),
        "cbc": _record(available=False, lp="unavailable", mip="unavailable"),
        "appsi_highs": _record(),
    }, include_mip=True)
    assert summary["coverage_complete"] is True
    assert summary["unavailable_solvers"] == ["cbc"]
    assert summary["mip_unsupported"] == ["glpk"]
    assert summary["mip_failed"] == []
    assert summary["mip_expected"] == ["appsi_highs"]


def test_available_lp_or_mip_failure_breaks_coverage():
    lp_failed = sc._classify_coverage({
        "appsi_highs": _record(lp="failed"),
    }, include_mip=True)
    mip_failed = sc._classify_coverage({
        "appsi_highs": _record(mip="failed"),
    }, include_mip=True)
    assert lp_failed["coverage_complete"] is False
    assert mip_failed["coverage_complete"] is False


def test_coverage_refuses_existing_output_directory(tmp_path):
    (tmp_path / "existing").mkdir()
    with pytest.raises(sc.QualificationError, match="refusing to overwrite"):
        sc.run_solver_coverage(tmp_path / "existing")


@needs_solver
def test_available_solver_produces_passing_lp_and_mip_evidence(tmp_path):
    payload = sc.run_solver_coverage(tmp_path / "coverage", ["appsi_highs"])
    record = payload["solvers"]["appsi_highs"]
    assert record["lp"]["status"] == "passed"
    assert record["lp"]["solver"] == "appsi_highs"
    assert record["lp"]["validation_passed"] is True
    assert record["lp"]["objective_usd"] == pytest.approx(20300.0)
    assert record["mip"]["status"] == "passed"
    assert record["mip"]["validation_passed"] is True
    assert record["mip"]["objective_usd"] == pytest.approx(400.0)
    assert payload["summary"]["coverage_complete"] is True
    assert all(c["equivalent"] for c in payload["comparisons"])
    written = json.loads((tmp_path / "coverage" / "coverage.json").read_text(encoding="utf-8"))
    assert written["provenance"]["harness_version"]
