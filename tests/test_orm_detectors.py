"""Tests for ORM analysis detectors in detectors.py.

Covers:
- Registration of all three new detectors in _MATH_DETECTORS
- Detectors are callable
- Detectors return lists when called with a minimal DB connection
"""

from __future__ import annotations

import sqlite3

import pytest

from roam.catalog.detectors import (
    _MATH_DETECTORS,
    detect_missing_eager_loading,
    detect_queryset_chain_complexity,
    detect_raw_sql_usage,
)
from roam.db.schema import SCHEMA_SQL


@pytest.fixture()
def empty_db():
    """Create a minimal in-memory SQLite database with the required schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestOrmDetectorRegistration:
    def test_missing_eager_loading_registered(self):
        task_ids = [t[0] for t in _MATH_DETECTORS]
        assert "missing-eager-loading" in task_ids

    def test_raw_sql_usage_registered(self):
        task_ids = [t[0] for t in _MATH_DETECTORS]
        assert "raw-sql-usage" in task_ids

    def test_queryset_chain_complexity_registered(self):
        task_ids = [t[0] for t in _MATH_DETECTORS]
        assert "queryset-chain-complexity" in task_ids

    def test_total_detector_count(self):
        assert len(_MATH_DETECTORS) == 26


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestDetectorSchema:
    def test_missing_eager_loading_callable(self):
        assert callable(detect_missing_eager_loading)

    def test_raw_sql_callable(self):
        assert callable(detect_raw_sql_usage)

    def test_chain_complexity_callable(self):
        assert callable(detect_queryset_chain_complexity)


# ---------------------------------------------------------------------------
# Behavior tests (minimal DB)
# ---------------------------------------------------------------------------


class TestDetectorBehavior:
    def test_missing_eager_loading_returns_list(self, empty_db):
        result = detect_missing_eager_loading(empty_db)
        assert isinstance(result, list)

    def test_raw_sql_returns_list(self, empty_db):
        result = detect_raw_sql_usage(empty_db)
        assert isinstance(result, list)

    def test_chain_complexity_returns_list(self, empty_db):
        result = detect_queryset_chain_complexity(empty_db)
        assert isinstance(result, list)
