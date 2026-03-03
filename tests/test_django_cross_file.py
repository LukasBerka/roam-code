"""Tests for cross-file Django inheritance resolution.

Covers:
1. Cross-file transitive model inheritance tagging via resolve_django_inheritance
2. Cross-file custom field resolution via resolve_django_custom_fields
3. Cycle detection across file boundaries
"""

from __future__ import annotations

import json
import sqlite3


# ---------------------------------------------------------------------------
# Helpers — direct DB construction (no parsing, tests django_post in isolation)
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


def _insert_property(conn, name, parent_id, file_id=1, call_function=None,
                     field_metadata=None):
    """Insert a property symbol and return its ID."""
    conn.execute(
        "INSERT INTO symbols (file_id, name, qualified_name, kind, parent_id, "
        "call_function, field_metadata, line_start) "
        "VALUES (?, ?, ?, 'property', ?, ?, ?, 1)",
        (file_id, name, name, parent_id, call_function, field_metadata),
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
# 1. Cross-File Model Inheritance
# ===========================================================================


class TestCrossFileModelInheritance:
    """Test transitive Django model inheritance across file boundaries."""

    def test_cross_file_transitive_model(self):
        """File A: Base(models.Model), File B: Child(Base). Child gets tagged."""
        from roam.index.django_post import resolve_django_inheritance

        conn = _make_db()
        base_id = _insert_class(conn, "Base", file_id=1, framework_type="django_model")
        child_id = _insert_class(conn, "Child", file_id=2)
        _insert_inherits(conn, child_id, base_id)
        conn.commit()

        count = resolve_django_inheritance(conn)
        assert count == 1

        row = conn.execute(
            "SELECT framework_type FROM symbols WHERE id = ?", (child_id,)
        ).fetchone()
        assert row["framework_type"] == "django_model"
        conn.close()

    def test_cross_file_three_level_chain(self):
        """File A: Base, File B: Middle(Base), File C: Concrete(Middle). All tagged."""
        from roam.index.django_post import resolve_django_inheritance

        conn = _make_db()
        base_id = _insert_class(conn, "Base", file_id=1, framework_type="django_model")
        middle_id = _insert_class(conn, "Middle", file_id=2)
        concrete_id = _insert_class(conn, "Concrete", file_id=3)
        _insert_inherits(conn, middle_id, base_id)
        _insert_inherits(conn, concrete_id, middle_id)
        conn.commit()

        count = resolve_django_inheritance(conn)
        assert count == 2

        for sym_id in (middle_id, concrete_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] == "django_model"
        conn.close()

    def test_cross_file_diamond_inheritance(self):
        """File A: Base, B: Left(Base), C: Right(Base), D: Diamond(Left, Right). All tagged."""
        from roam.index.django_post import resolve_django_inheritance

        conn = _make_db()
        base_id = _insert_class(conn, "Base", file_id=1, framework_type="django_model")
        left_id = _insert_class(conn, "Left", file_id=2)
        right_id = _insert_class(conn, "Right", file_id=3)
        diamond_id = _insert_class(conn, "Diamond", file_id=4)
        _insert_inherits(conn, left_id, base_id)
        _insert_inherits(conn, right_id, base_id)
        _insert_inherits(conn, diamond_id, left_id)
        _insert_inherits(conn, diamond_id, right_id)
        conn.commit()

        count = resolve_django_inheritance(conn)
        assert count == 3

        for sym_id in (left_id, right_id, diamond_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] == "django_model"
        conn.close()

    def test_cross_file_no_false_positive(self):
        """File A: ServiceBase (no framework_type), File B: Service(ServiceBase). Neither tagged."""
        from roam.index.django_post import resolve_django_inheritance

        conn = _make_db()
        service_base_id = _insert_class(conn, "ServiceBase", file_id=1)
        service_id = _insert_class(conn, "Service", file_id=2)
        _insert_inherits(conn, service_id, service_base_id)
        conn.commit()

        count = resolve_django_inheritance(conn)
        assert count == 0

        for sym_id in (service_base_id, service_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] is None
        conn.close()


# ===========================================================================
# 2. Cross-File Custom Fields
# ===========================================================================


class TestCrossFileCustomFields:
    """Test custom field resolution across file boundaries."""

    def test_cross_file_custom_field(self):
        """File A: MyField inherits CharField. File B: model uses MyField.

        After resolve_django_custom_fields, the property has field_type
        and field_base_type set.
        """
        from roam.index.django_post import resolve_django_custom_fields

        conn = _make_db()
        # File A: custom field class inheriting from CharField
        myfield_id = _insert_class(conn, "MyField", file_id=1)
        charfield_id = _insert_class(conn, "CharField", file_id=1,
                                     framework_type="django_field")
        _insert_inherits(conn, myfield_id, charfield_id)

        # File B: model with property using MyField
        model_id = _insert_class(conn, "Article", file_id=2,
                                 framework_type="django_model")
        prop_id = _insert_property(conn, "title", parent_id=model_id,
                                   file_id=2, call_function="MyField")
        conn.commit()

        count = resolve_django_custom_fields(conn)
        assert count == 1

        row = conn.execute(
            "SELECT field_type, field_base_type FROM symbols WHERE id = ?",
            (prop_id,),
        ).fetchone()
        assert row["field_type"] == "MyField"
        assert row["field_base_type"] == "CharField"
        conn.close()

    def test_cross_file_custom_fk(self):
        """File A: MyFK inherits ForeignKey. File B: model with MyFK property.

        After resolution, a django_fk edge should be created.
        """
        from roam.index.django_post import resolve_django_custom_fields

        conn = _make_db()
        # File A: custom FK class
        myfk_id = _insert_class(conn, "MyFK", file_id=1)
        fk_id = _insert_class(conn, "ForeignKey", file_id=1,
                               framework_type="django_field")
        _insert_inherits(conn, myfk_id, fk_id)

        # File B: model with FK property referencing User
        model_id = _insert_class(conn, "Post", file_id=2,
                                 framework_type="django_model")
        user_id = _insert_class(conn, "User", file_id=3,
                                framework_type="django_model")
        meta = json.dumps({"target_model": "User"})
        _insert_property(conn, "author", parent_id=model_id,
                         file_id=2, call_function="MyFK",
                         field_metadata=meta)
        conn.commit()

        resolve_django_custom_fields(conn)

        edges = conn.execute(
            "SELECT source_id, target_id, kind FROM edges WHERE kind = 'django_fk'"
        ).fetchall()
        assert len(edges) == 1
        assert edges[0]["source_id"] == model_id
        assert edges[0]["target_id"] == user_id
        assert edges[0]["kind"] == "django_fk"
        conn.close()


# ===========================================================================
# 3. Cross-File Cycle Detection
# ===========================================================================


class TestCrossFileCycleDetection:
    """Test cycle detection prevents infinite loops across files."""

    def test_cross_file_cycle(self):
        """File A: A inherits B. File B: B inherits A. Neither tagged."""
        from roam.index.django_post import resolve_django_inheritance

        conn = _make_db()
        a_id = _insert_class(conn, "A", file_id=1)
        b_id = _insert_class(conn, "B", file_id=2)
        _insert_inherits(conn, a_id, b_id)
        _insert_inherits(conn, b_id, a_id)
        conn.commit()

        count = resolve_django_inheritance(conn)
        assert count == 0

        for sym_id in (a_id, b_id):
            row = conn.execute(
                "SELECT framework_type FROM symbols WHERE id = ?", (sym_id,)
            ).fetchone()
            assert row["framework_type"] is None
        conn.close()
