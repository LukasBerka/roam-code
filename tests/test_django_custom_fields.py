"""Tests for custom Django field inheritance resolution.

Covers:
1. Custom field classes detected when used as model fields
2. field_type stores custom name, field_base_type stores resolved Django base
3. Custom relationship fields (FK/M2M/O2O subclasses) create correct edges
4. Multi-level custom field inheritance chains
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (
    find_sym as _find_sym,
    make_django_db,
    parse_py as _parse_py,
    populate_django_db,
    ref_targets as _ref_targets,
)


def _parse_py_resolved(source_text: str, file_path: str = "example.py"):
    """Parse Python source with DB-level Django resolution.

    Returns (symbols, references, db_edges) where symbols include cross-file
    resolution results and db_edges contains edges created by DB resolution.
    """
    import json

    from roam.index.django_post import (
        resolve_django_custom_fields,
        resolve_django_inheritance,
        resolve_django_relationships,
    )

    symbols, references = _parse_py(source_text, file_path)

    conn = make_django_db()
    populate_django_db(conn, symbols, references)

    # Count edges before resolution to identify new ones
    pre_edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # Run DB-level resolution
    resolve_django_inheritance(conn)
    resolve_django_custom_fields(conn)
    resolve_django_relationships(conn)

    # Query back updated symbols
    rows = conn.execute(
        "SELECT id, name, qualified_name, kind, framework_type, "
        "field_type, field_base_type, call_function, field_metadata, "
        "parent_id, line_start, line_end, signature, docstring, "
        "visibility, is_exported, default_value "
        "FROM symbols ORDER BY id"
    ).fetchall()

    # Rebuild symbol dicts
    updated_symbols = []
    for i, row in enumerate(rows):
        if i < len(symbols):
            sym = dict(symbols[i])
        else:
            sym = {}
        sym["framework_type"] = row["framework_type"]
        if row["field_type"]:
            sym["django_field"] = True
            sym["field_type"] = row["field_type"]
        if row["field_base_type"]:
            sym["field_base_type"] = row["field_base_type"]
        if row["field_metadata"]:
            try:
                meta = json.loads(row["field_metadata"])
                if meta.get("target_model"):
                    sym["relationship_target"] = meta["target_model"]
                if meta.get("on_delete"):
                    sym["on_delete"] = meta["on_delete"]
                if meta.get("related_name"):
                    sym["related_name"] = meta["related_name"]
            except (json.JSONDecodeError, TypeError):
                pass
        updated_symbols.append(sym)

    # Query new edges created by resolution (after the inherits edges)
    # Build id -> name map for edge translation
    id_to_name = {}
    for row in conn.execute("SELECT id, name FROM symbols"):
        id_to_name[row["id"]] = row["name"]

    db_edges = []
    for edge in conn.execute(
        "SELECT source_id, target_id, kind, line FROM edges WHERE id > ?",
        (pre_edge_count,),
    ):
        db_edges.append({
            "source_name": id_to_name.get(edge["source_id"]),
            "target_name": id_to_name.get(edge["target_id"]),
            "kind": edge["kind"],
            "line": edge["line"],
        })

    conn.close()
    return updated_symbols, references, db_edges


def _db_edge_targets(db_edges, kind=None):
    """Get target names from DB edges, optionally filtered by kind."""
    result = []
    for e in db_edges:
        if kind and e["kind"] != kind:
            continue
        result.append(e["target_name"])
    return result


# ===========================================================================
# 1. Custom Field Detection
# ===========================================================================


class TestCustomFieldDetection:
    """Test that custom Django fields are detected with field_type and field_base_type."""

    def test_custom_charfield_detected(self):
        src = (
            "class MyCharField(CharField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    name = MyCharField()\n"
        )
        syms, _, _ = _parse_py_resolved(src)
        sym = _find_sym(syms, "name", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyCharField"
        assert sym.get("field_base_type") == "CharField"

    def test_custom_fk_creates_reference(self):
        src = (
            "class User(models.Model):\n"
            "    pass\n"
            "class MyFK(ForeignKey):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    author = MyFK(User, on_delete=models.CASCADE)\n"
        )
        syms, _, db_edges = _parse_py_resolved(src)
        sym = _find_sym(syms, "author", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyFK"
        assert sym.get("field_base_type") == "ForeignKey"
        fk_targets = _db_edge_targets(db_edges, kind="django_fk")
        assert "User" in fk_targets

    def test_custom_m2m_creates_reference(self):
        src = (
            "class Tag(models.Model):\n"
            "    pass\n"
            "class MyM2M(ManyToManyField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    tags = MyM2M(Tag)\n"
        )
        syms, _, db_edges = _parse_py_resolved(src)
        sym = _find_sym(syms, "tags", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyM2M"
        assert sym.get("field_base_type") == "ManyToManyField"
        m2m_targets = _db_edge_targets(db_edges, kind="django_m2m")
        assert "Tag" in m2m_targets

    def test_direct_field_no_base_type(self):
        src = (
            "class M(models.Model):\n"
            "    name = models.CharField(max_length=100)\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "name", parent="M")
        assert sym is not None
        assert sym.get("field_type") == "CharField"
        assert sym.get("field_base_type") is None

    def test_two_level_custom_field(self):
        src = (
            "class BaseField(CharField):\n"
            "    pass\n"
            "class MyField(BaseField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    val = MyField()\n"
        )
        syms, _, _ = _parse_py_resolved(src)
        sym = _find_sym(syms, "val", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyField"
        assert sym.get("field_base_type") == "CharField"

    def test_non_django_call_not_affected(self):
        src = (
            "class Helper:\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    x = Helper()\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "x", parent="M")
        assert sym is not None
        assert sym.get("django_field") is None

    def test_custom_field_with_models_prefix_base(self):
        src = (
            "class MyField(models.IntegerField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    val = MyField()\n"
        )
        syms, _, _ = _parse_py_resolved(src)
        sym = _find_sym(syms, "val", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyField"
        assert sym.get("field_base_type") == "IntegerField"


# ===========================================================================
# 2. Custom Relationship Field Edge Cases
# ===========================================================================


class TestCustomRelationshipFields:
    """Test custom relationship fields create correct reference edges."""

    def test_custom_o2o_creates_reference(self):
        src = (
            "class Profile(models.Model):\n"
            "    pass\n"
            "class MyO2O(OneToOneField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    profile = MyO2O(Profile, on_delete=models.CASCADE)\n"
        )
        syms, _, db_edges = _parse_py_resolved(src)
        sym = _find_sym(syms, "profile", parent="M")
        assert sym is not None
        assert sym.get("field_type") == "MyO2O"
        assert sym.get("field_base_type") == "OneToOneField"
        o2o_targets = _db_edge_targets(db_edges, kind="django_o2o")
        assert "Profile" in o2o_targets

    def test_custom_fk_extracts_on_delete(self):
        src = (
            "class User(models.Model):\n"
            "    pass\n"
            "class MyFK(ForeignKey):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    author = MyFK(User, on_delete=models.CASCADE)\n"
        )
        syms, _, _ = _parse_py_resolved(src)
        sym = _find_sym(syms, "author", parent="M")
        assert sym is not None
        assert sym.get("on_delete") == "models.CASCADE"

    def test_custom_fk_extracts_related_name(self):
        src = (
            "class User(models.Model):\n"
            "    pass\n"
            "class MyFK(ForeignKey):\n"
            "    pass\n"
            "class M(models.Model):\n"
            '    author = MyFK(User, on_delete=models.CASCADE, related_name="posts")\n'
        )
        syms, _, _ = _parse_py_resolved(src)
        sym = _find_sym(syms, "author", parent="M")
        assert sym is not None
        assert sym.get("related_name") == "posts"

    def test_custom_fk_string_target(self):
        src = (
            "class MyFK(ForeignKey):\n"
            "    pass\n"
            "class M(models.Model):\n"
            '    user = MyFK("auth.User", on_delete=models.CASCADE)\n'
        )
        syms, _, _ = _parse_py_resolved(src)
        sym = _find_sym(syms, "user", parent="M")
        assert sym is not None
        assert sym.get("relationship_target") == "auth.User"

    def test_mixed_custom_and_standard_fields(self):
        src = (
            "class MyCharField(CharField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    custom_name = MyCharField()\n"
            "    standard_name = models.CharField(max_length=100)\n"
            "    age = models.IntegerField()\n"
        )
        syms, _, _ = _parse_py_resolved(src)
        custom = _find_sym(syms, "custom_name", parent="M")
        standard = _find_sym(syms, "standard_name", parent="M")
        age = _find_sym(syms, "age", parent="M")
        assert custom.get("django_field") is True
        assert custom.get("field_type") == "MyCharField"
        assert custom.get("field_base_type") == "CharField"
        assert standard.get("django_field") is True
        assert standard.get("field_type") == "CharField"
        assert standard.get("field_base_type") is None
        assert age.get("django_field") is True
        assert age.get("field_type") == "IntegerField"
