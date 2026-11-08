from __future__ import annotations

import math
import random

import pytest

from datawatch.config import load_config
from datawatch.engine import Alert, Finding
from datawatch.generate import build
from datawatch.service import Service


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def warehouse(cfg, tmp_path_factory):
    path = tmp_path_factory.mktemp("wh") / "warehouse.duckdb"
    build(cfg, path, scale=1.0)
    return path


@pytest.fixture(scope="session")
def service(cfg, warehouse):
    return Service(cfg, warehouse)


def weekly_series(days=120, base=1000.0, seed=1, weekend=0.6, noise=0.03, cal=None):
    """Weekday/weekend pattern starting on a Monday, with multiplicative noise."""
    rng = random.Random(seed)
    out = []
    for d in range(days):
        f = weekend if d % 7 in (5, 6) else 1.0
        out.append(base * f * math.exp(rng.gauss(0, noise)))
    return out


def alert(table, day, monitor="volume", severity="medium", hour=3.0):
    f = Finding(table, monitor, "row_count", day, 1.0, 2.0, 1.5, 2.5, "test")
    return Alert(table, monitor, day, [f], severity, hour)
