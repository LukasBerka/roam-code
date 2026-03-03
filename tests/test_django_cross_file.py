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

    def test_cross_file_transitive_custom_field(self):
        """File A: MyBaseField inherits DecimalField (tagged django_field).
        File B: MyField inherits MyBaseField. File C: model uses MyField.

        After resolution, the property has field_base_type="DecimalField"
        even though the chain is MyField -> MyBaseField -> DecimalField.
        """
        from roam.index.django_post import resolve_django_custom_fields

        conn = _make_db()
        # File A: MyBaseField directly extends DecimalField (tagged by fast path)
        decimalfield_id = _insert_class(conn, "DecimalField", file_id=1,
                                        framework_type="django_field")
        mybase_id = _insert_class(conn, "MyBaseField", file_id=1,
                                  framework_type="django_field")
        # Simulate fast-path: field_base_type set by python_lang.py
        conn.execute(
            "UPDATE symbols SET field_base_type = 'DecimalField' WHERE id = ?",
            (mybase_id,),
        )
        _insert_inherits(conn, mybase_id, decimalfield_id)

        # File B: MyField extends MyBaseField (both in DB, edge exists)
        myfield_id = _insert_class(conn, "MyField", file_id=2)
        _insert_inherits(conn, myfield_id, mybase_id)

        # File C: model uses MyField
        model_id = _insert_class(conn, "Article", file_id=3,
                                 framework_type="django_model")
        prop_id = _insert_property(conn, "price", parent_id=model_id,
                                   file_id=3, call_function="MyField")
        conn.commit()

        count = resolve_django_custom_fields(conn)
        assert count == 1

        row = conn.execute(
            "SELECT field_type, field_base_type FROM symbols WHERE id = ?",
            (prop_id,),
        ).fetchone()
        assert row["field_type"] == "MyField"
        assert row["field_base_type"] == "DecimalField"
        conn.close()

    def test_cross_file_custom_field_no_base_in_db(self):
        """File A: MyBaseField(DecimalField) where DecimalField is NOT in DB.
        MyBaseField has framework_type=django_field and field_base_type=DecimalField
        set by python_lang.py fast path. File B: MyField(MyBaseField).
        File C: model uses MyField.

        This is the real-world scenario: Django's DecimalField is in site-packages
        and never indexed. The fast-path tag on MyBaseField bridges the gap.
        """
        from roam.index.django_post import resolve_django_custom_fields

        conn = _make_db()
        # File A: MyBaseField — DecimalField is NOT in DB (site-packages)
        mybase_id = _insert_class(conn, "MyBaseField", file_id=1,
                                  framework_type="django_field")
        conn.execute(
            "UPDATE symbols SET field_base_type = 'DecimalField' WHERE id = ?",
            (mybase_id,),
        )
        # No inherits edge to DecimalField — it's not in the DB

        # File B: MyField extends MyBaseField
        myfield_id = _insert_class(conn, "MyField", file_id=2)
        _insert_inherits(conn, myfield_id, mybase_id)

        # File C: model uses MyField
        model_id = _insert_class(conn, "Article", file_id=3,
                                 framework_type="django_model")
        prop_id = _insert_property(conn, "price", parent_id=model_id,
                                   file_id=3, call_function="MyField")
        conn.commit()

        count = resolve_django_custom_fields(conn)
        assert count == 1

        row = conn.execute(
            "SELECT field_type, field_base_type FROM symbols WHERE id = ?",
            (prop_id,),
        ).fetchone()
        assert row["field_type"] == "MyField"
        assert row["field_base_type"] == "DecimalField"
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
# 2b. Cross-File Relationship Resolution
# ===========================================================================


class TestCrossFileRelationships:
    """Test FK/O2O/M2M edge resolution across file boundaries."""

    def test_dotted_target_model(self):
        """FK with 'app.ModelName' target resolved by stripping app prefix."""
        from roam.index.django_post import resolve_django_relationships

        conn = _make_db()
        post_id = _insert_class(conn, "Post", file_id=1,
                                framework_type="django_model")
        user_id = _insert_class(conn, "User", file_id=2,
                                framework_type="django_model")
        meta = json.dumps({"target_model": "auth.User"})
        _insert_property(conn, "author", parent_id=post_id,
                         file_id=1, call_function=None,
                         field_metadata=meta)
        # Set field_type directly (simulates standard FK detection)
        conn.execute(
            "UPDATE symbols SET field_type = 'ForeignKey' WHERE name = 'author'")
        conn.commit()

        count = resolve_django_relationships(conn)
        assert count == 1

        edges = conn.execute(
            "SELECT source_id, target_id, kind FROM edges WHERE kind = 'django_fk'"
        ).fetchall()
        assert len(edges) == 1
        assert edges[0]["source_id"] == post_id
        assert edges[0]["target_id"] == user_id
        conn.close()

    def test_self_referential_fk(self):
        """FK with 'self' target resolves to the parent model."""
        from roam.index.django_post import resolve_django_relationships

        conn = _make_db()
        cat_id = _insert_class(conn, "Category", file_id=1,
                               framework_type="django_model")
        meta = json.dumps({"target_model": "self"})
        _insert_property(conn, "parent", parent_id=cat_id,
                         file_id=1, call_function=None,
                         field_metadata=meta)
        conn.execute(
            "UPDATE symbols SET field_type = 'ForeignKey' WHERE name = 'parent'")
        conn.commit()

        count = resolve_django_relationships(conn)
        assert count == 1

        edges = conn.execute(
            "SELECT source_id, target_id, kind FROM edges WHERE kind = 'django_fk'"
        ).fetchall()
        assert len(edges) == 1
        assert edges[0]["source_id"] == cat_id
        assert edges[0]["target_id"] == cat_id  # self-referential
        conn.close()

    def test_no_duplicate_edges(self):
        """Don't create duplicate edges if reference resolution already created one."""
        from roam.index.django_post import resolve_django_relationships

        conn = _make_db()
        post_id = _insert_class(conn, "Post", file_id=1,
                                framework_type="django_model")
        user_id = _insert_class(conn, "User", file_id=2,
                                framework_type="django_model")
        # Pre-existing edge (from reference resolution)
        conn.execute(
            "INSERT INTO edges (source_id, target_id, kind, line, source_file_id) "
            "VALUES (?, ?, 'django_fk', 1, 1)",
            (post_id, user_id))
        meta = json.dumps({"target_model": "User"})
        _insert_property(conn, "author", parent_id=post_id,
                         file_id=1, call_function=None,
                         field_metadata=meta)
        conn.execute(
            "UPDATE symbols SET field_type = 'ForeignKey' WHERE name = 'author'")
        conn.commit()

        count = resolve_django_relationships(conn)
        assert count == 0  # no new edges

        edges = conn.execute(
            "SELECT COUNT(*) as cnt FROM edges WHERE kind = 'django_fk'"
        ).fetchone()
        assert edges["cnt"] == 1  # still just one
        conn.close()

    def test_m2m_and_o2o_edges(self):
        """ManyToManyField and OneToOneField create correct edge kinds."""
        from roam.index.django_post import resolve_django_relationships

        conn = _make_db()
        profile_id = _insert_class(conn, "Profile", file_id=1,
                                   framework_type="django_model")
        user_id = _insert_class(conn, "User", file_id=2,
                                framework_type="django_model")
        tag_id = _insert_class(conn, "Tag", file_id=3,
                               framework_type="django_model")

        meta_o2o = json.dumps({"target_model": "auth.User"})
        _insert_property(conn, "user", parent_id=profile_id,
                         file_id=1, call_function=None,
                         field_metadata=meta_o2o)
        conn.execute(
            "UPDATE symbols SET field_type = 'OneToOneField' WHERE name = 'user'")

        meta_m2m = json.dumps({"target_model": "tagging.Tag"})
        _insert_property(conn, "tags", parent_id=profile_id,
                         file_id=1, call_function=None,
                         field_metadata=meta_m2m)
        conn.execute(
            "UPDATE symbols SET field_type = 'ManyToManyField' WHERE name = 'tags'")
        conn.commit()

        count = resolve_django_relationships(conn)
        assert count == 2

        edges = conn.execute(
            "SELECT source_id, target_id, kind FROM edges "
            "WHERE kind IN ('django_o2o', 'django_m2m') ORDER BY kind"
        ).fetchall()
        assert len(edges) == 2
        kinds = {e["kind"] for e in edges}
        assert kinds == {"django_o2o", "django_m2m"}
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
