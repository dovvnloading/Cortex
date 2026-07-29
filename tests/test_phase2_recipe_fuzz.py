"""Deterministic parser-fuzz qualification stays bounded and reproducible."""

from __future__ import annotations

import pytest

from tools.execution_spikes.recipe_parser_fuzz import run_fuzz


def test_recipe_parser_fuzz_corpus_is_reproducible_and_fail_closed():
    first = run_fuzz(iterations=900, seed=20260728)
    second = run_fuzz(iterations=900, seed=20260728)

    assert first == second
    assert first["status"] == "passed"
    assert first["accepted"] > 0
    assert first["rejected"] > 0
    assert first["unexpected"] == 0
    assert first["failures"] == []


@pytest.mark.parametrize("iterations", [0, 10_001])
def test_recipe_parser_fuzz_budget_is_bounded(iterations):
    with pytest.raises(ValueError, match="iterations"):
        run_fuzz(iterations=iterations)
