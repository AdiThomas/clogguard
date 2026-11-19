from __future__ import annotations

from functools import cache

import pytest

from clogguard.core.schema import Run
from clogguard.sim.scenarios import generate_run, preset


@cache
def _baseline(name: str, seed: int = 0) -> Run:
    return generate_run(preset(name, seed=seed))


@pytest.fixture(scope="session")
def baseline():
    """Cached no-intervention runs of named presets (each full run takes about 1 s)."""
    return _baseline
