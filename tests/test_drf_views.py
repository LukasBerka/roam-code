"""Tests for transitive DRF view detection via resolve_drf_views.

Covers:
1. Direct DRF inheritance (APIView, ViewSet, etc.)
2. Transitive DRF inheritance (multi-level chains)
3. No false positives for non-DRF classes
4. Does not overwrite existing django_model tags
5. Multiple DRF base classes
6. Cross-file detection
7. Cycle detection
"""

from __future__ import annotations

import sqlite3


# ---------------------------------------------------------------------------
# Helpers -- direct DB construction (no parsing, tests django_post in isolation)
# ---------------------------------------------------------------------------


def _make_db():
    """Create an in-memory SQLite DB with symbols and edges tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER DEFAULT 1,
        name TEXT NOT NULL,
        qualified_name TEXT,
        kind TEXT NOT NULL,
        signature TEXT,
        line_start INTEGER,
        line_end INTEGER,
        docstring TEXT,
        visibility TEXT DEFAULT 'public',
        is_exported INTEGER DEFAULT 1,
        parent_id INTEGER,
        default_value TEXT,
        framework_type TEXT,
        call_function TEXT,
        field_type TEXT,
        field_base_type TEXT,
        field_metadata TEXT
    )""")
    conn.execute("""CREATE TABLE edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER,
        target_id INTEGER,
        kind TEXT,
        line INTEGER,
        source_file_id INTEGER
    )""")
    return conn


def _insert_class(conn, name, file_id=1, framework_type=None):
    """Insert a class symbol and return its ID."""
    conn.execute(
        "INSERT INTO symbols (file_id, name, qualified_name, kind, framework_type) "
        "VALUES (?, ?, ?, 'class', ?)",
        (file_id, name, name, framework_type),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_inherits(conn, source_id, target_id):
    """Insert an inherits edge."""
    conn.execute(
        "INSERT INTO edges (source_id, target_id, kind, line, source_file_id) "
        "VALUES (?, ?, 'inherits', 1, 1)",
        (source_id, target_id),
    )


# ===========================================================================
# 1. Direct DRF Inheritance
# ===========================================================================


class TestDirectDRFInheritance:
    """Test direct inheritance from DRF base classes."""

    def test_direct_apiview_inheritance(self):
        """class MyView(APIView) -- tagged as drf_view."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        apiview_id = _insert_class(conn, "APIView", file_id=1)
        myview_id = _insert_class(conn, "MyView", file_id=1)
        _insert_inherits(conn, myview_id, apiview_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 1

        row = conn.execute(
            "SELECT framework_type FROM symbols WHERE id = ?", (myview_id,)
        ).fetchone()
        assert row["framework_type"] == "drf_view"
        conn.close()


# ===========================================================================
# 2. Transitive DRF Inheritance
# ===========================================================================


class TestTransitiveDRFInheritance:
    """Test transitive DRF view inheritance resolution."""

    def test_two_level_chain(self):
        """Base(APIView), Child(Base) -- both tagged."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        apiview_id = _insert_class(conn, "APIView", file_id=1)
        base_id = _insert_class(conn, "Base", file_id=1)
        child_id = _insert_class(conn, "Child", file_id=1)
        _insert_inherits(conn, base_id, apiview_id)
        _insert_inherits(conn, child_id, base_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 2

        for sym_id in (base_id, child_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] == "drf_view"
        conn.close()

    def test_three_level_chain(self):
        """Base(ViewSet) -> Middle(Base) -> Concrete(Middle) -- all tagged."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        viewset_id = _insert_class(conn, "ViewSet", file_id=1)
        base_id = _insert_class(conn, "Base", file_id=1)
        middle_id = _insert_class(conn, "Middle", file_id=1)
        concrete_id = _insert_class(conn, "Concrete", file_id=1)
        _insert_inherits(conn, base_id, viewset_id)
        _insert_inherits(conn, middle_id, base_id)
        _insert_inherits(conn, concrete_id, middle_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 3

        for sym_id in (base_id, middle_id, concrete_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] == "drf_view"
        conn.close()


# ===========================================================================
# 3. No False Positives
# ===========================================================================


class TestNoFalsePositives:
    """Test that non-DRF classes are not tagged."""

    def test_plain_class_not_tagged(self):
        """class MyService(object) -- NOT tagged."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        obj_id = _insert_class(conn, "object", file_id=1)
        service_id = _insert_class(conn, "MyService", file_id=1)
        _insert_inherits(conn, service_id, obj_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 0

        row = conn.execute(
            "SELECT framework_type FROM symbols WHERE id = ?", (service_id,)
        ).fetchone()
        assert row["framework_type"] is None
        conn.close()


# ===========================================================================
# 4. Does Not Overwrite django_model
# ===========================================================================


class TestNoOverwriteDjangoModel:
    """Test that existing framework_type='django_model' is preserved."""

    def test_does_not_overwrite_django_model(self):
        """class MyModelViewSet(ModelViewSet) with framework_type='django_model' stays as django_model."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        modelviewset_id = _insert_class(conn, "ModelViewSet", file_id=1)
        my_id = _insert_class(conn, "MyModelViewSet", file_id=1,
                              framework_type="django_model")
        _insert_inherits(conn, my_id, modelviewset_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 0

        row = conn.execute(
            "SELECT framework_type FROM symbols WHERE id = ?", (my_id,)
        ).fetchone()
        assert row["framework_type"] == "django_model"
        conn.close()


# ===========================================================================
# 5. Multiple DRF Bases
# ===========================================================================


class TestMultipleDRFBases:
    """Test classes inheriting from different DRF base classes."""

    def test_multiple_drf_base_classes(self):
        """Classes inheriting from ModelViewSet, GenericAPIView, etc. all detected."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        mvs_id = _insert_class(conn, "ModelViewSet", file_id=1)
        gav_id = _insert_class(conn, "GenericAPIView", file_id=1)
        romvs_id = _insert_class(conn, "ReadOnlyModelViewSet", file_id=1)

        view1_id = _insert_class(conn, "UserViewSet", file_id=2)
        view2_id = _insert_class(conn, "ItemDetailView", file_id=2)
        view3_id = _insert_class(conn, "ReadOnlyItems", file_id=2)

        _insert_inherits(conn, view1_id, mvs_id)
        _insert_inherits(conn, view2_id, gav_id)
        _insert_inherits(conn, view3_id, romvs_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 3

        for sym_id in (view1_id, view2_id, view3_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] == "drf_view"
        conn.close()


# ===========================================================================
# 6. Cross-File Detection
# ===========================================================================


class TestCrossFileDetection:
    """Test DRF view detection across file boundaries."""

    def test_cross_file_drf_view(self):
        """APIView in file 1, subclass in file 2 -- subclass tagged."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        apiview_id = _insert_class(conn, "APIView", file_id=1)
        myview_id = _insert_class(conn, "MyView", file_id=2)
        _insert_inherits(conn, myview_id, apiview_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 1

        row = conn.execute(
            "SELECT framework_type FROM symbols WHERE id = ?", (myview_id,)
        ).fetchone()
        assert row["framework_type"] == "drf_view"
        conn.close()


# ===========================================================================
# 7. Cycle Detection
# ===========================================================================


class TestCycleDetection:
    """Test cycle detection prevents infinite loops."""

    def test_cycle_no_infinite_loop(self):
        """A inherits B, B inherits A -- no infinite loop, neither tagged."""
        from roam.index.django_post import resolve_drf_views

        conn = _make_db()
        a_id = _insert_class(conn, "A", file_id=1)
        b_id = _insert_class(conn, "B", file_id=2)
        _insert_inherits(conn, a_id, b_id)
        _insert_inherits(conn, b_id, a_id)
        conn.commit()

        count = resolve_drf_views(conn)
        assert count == 0

        for sym_id in (a_id, b_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] is None
        conn.close()
