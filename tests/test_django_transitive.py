"""Tests for transitive Django model inheritance resolution.

Covers:
1. Transitive model tagging (1-level, 2-level, mixed bases)
2. Cycle detection in circular inheritance chains
3. Non-model classes left untagged
4. Inherits reference edges for transitive models
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
    sym_names as _sym_names,
)


def _parse_py_resolved(source_text: str, file_path: str = "example.py"):
    """Parse Python source with DB-level Django resolution.

    Returns (symbols, references) where symbols include cross-file
    resolution results (framework_type, field_type, field_base_type).
    """
    import json

    from roam.index.django_post import (
        resolve_django_custom_fields,
        resolve_django_inheritance,
    )

    symbols, references = _parse_py(source_text, file_path)

    conn = make_django_db()
    populate_django_db(conn, symbols, references)

    # Run DB-level resolution
    resolve_django_inheritance(conn)
    resolve_django_custom_fields(conn)

    # Query back updated symbols
    rows = conn.execute(
        "SELECT id, name, qualified_name, kind, framework_type, "
        "field_type, field_base_type, call_function, field_metadata, "
        "parent_id, line_start, line_end, signature, docstring, "
        "visibility, is_exported, default_value "
        "FROM symbols ORDER BY id"
    ).fetchall()

    # Rebuild symbol dicts, merging DB updates with original in-memory data
    updated_symbols = []
    for i, row in enumerate(rows):
        # Start from original symbol to preserve non-DB fields
        if i < len(symbols):
            sym = dict(symbols[i])
        else:
            sym = {}

        # Overlay DB-resolved values
        sym["framework_type"] = row["framework_type"]
        if row["field_type"]:
            sym["django_field"] = True
            sym["field_type"] = row["field_type"]
        if row["field_base_type"]:
            sym["field_base_type"] = row["field_base_type"]

        # Reconstruct relationship metadata from field_metadata JSON
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

    conn.close()
    return updated_symbols, references


# ===========================================================================
# 1. Transitive Model Inheritance
# ===========================================================================


class TestTransitiveModelInheritance:
    """Test transitive Django model inheritance tagging."""

    def test_direct_model_still_works(self):
        """Regression guard: direct models.Model subclass still tagged."""
        src = "class A(models.Model):\n    pass\n"
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "A")
        assert sym is not None
        assert sym.get("framework_type") == "django_model"

    def test_one_level_transitive(self):
        """Child inheriting from a direct Django model is also tagged."""
        src = (
            "class Base(models.Model):\n"
            "    pass\n"
            "class Child(Base):\n"
            "    pass\n"
        )
        syms, _ = _parse_py_resolved(src)
        base = _find_sym(syms, "Base")
        child = _find_sym(syms, "Child")
        assert base is not None
        assert base.get("framework_type") == "django_model"
        assert child is not None
        assert child.get("framework_type") == "django_model"

    def test_two_level_transitive(self):
        """Three-level chain: Base -> Middle -> Concrete, all tagged."""
        src = (
            "class Base(models.Model):\n"
            "    pass\n"
            "class Middle(Base):\n"
            "    pass\n"
            "class Concrete(Middle):\n"
            "    pass\n"
        )
        syms, _ = _parse_py_resolved(src)
        assert _find_sym(syms, "Base").get("framework_type") == "django_model"
        assert _find_sym(syms, "Middle").get("framework_type") == "django_model"
        assert _find_sym(syms, "Concrete").get("framework_type") == "django_model"

    def test_non_model_class_untouched(self):
        """A class not inheriting from Django model bases is NOT tagged."""
        src = "class Service(BaseService):\n    pass\n"
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "Service")
        assert sym is not None
        assert sym.get("framework_type") is None

    def test_cycle_detection(self):
        """Circular inheritance does not cause infinite loop; neither is tagged."""
        src = (
            "class A(B):\n"
            "    pass\n"
            "class B(A):\n"
            "    pass\n"
        )
        syms, _ = _parse_py_resolved(src)
        a = _find_sym(syms, "A")
        b = _find_sym(syms, "B")
        assert a is not None
        assert b is not None
        assert a.get("framework_type") is None
        assert b.get("framework_type") is None

    def test_mixed_bases_one_is_model(self):
        """Class with multiple bases, one of which is a Django model, is tagged."""
        src = (
            "class Base(models.Model):\n"
            "    pass\n"
            "class M(SomeMixin, Base):\n"
            "    pass\n"
        )
        syms, _ = _parse_py_resolved(src)
        m = _find_sym(syms, "M")
        assert m is not None
        assert m.get("framework_type") == "django_model"

    def test_multiple_independent_chains(self):
        """Two separate inheritance chains in the same file are resolved independently."""
        src = (
            "class ModelA(models.Model):\n"
            "    pass\n"
            "class ChildA(ModelA):\n"
            "    pass\n"
            "class ServiceBase:\n"
            "    pass\n"
            "class ServiceChild(ServiceBase):\n"
            "    pass\n"
        )
        syms, _ = _parse_py_resolved(src)
        assert _find_sym(syms, "ModelA").get("framework_type") == "django_model"
        assert _find_sym(syms, "ChildA").get("framework_type") == "django_model"
        assert _find_sym(syms, "ServiceBase").get("framework_type") is None
        assert _find_sym(syms, "ServiceChild").get("framework_type") is None


# ===========================================================================
# 2. Transitive Inherits Edges
# ===========================================================================


class TestTransitiveInheritsEdges:
    """Test that inherits reference edges are correct for transitive models."""

    def test_transitive_model_has_inherits_ref(self):
        """Child(Base) where Base(models.Model) generates inherits ref from Child to Base."""
        src = (
            "class Base(models.Model):\n"
            "    pass\n"
            "class Child(Base):\n"
            "    pass\n"
        )
        _, refs = _parse_py(src)
        child_inherits = _ref_targets(refs, kind="inherits", source_name="Child")
        assert "Base" in child_inherits

    def test_transitive_model_preserves_all_inherits(self):
        """Multi-level chain generates correct inherits edges at each level."""
        src = (
            "class Base(models.Model):\n"
            "    pass\n"
            "class Middle(Base):\n"
            "    pass\n"
            "class Concrete(Middle):\n"
            "    pass\n"
        )
        _, refs = _parse_py(src)
        base_inherits = _ref_targets(refs, kind="inherits", source_name="Base")
        middle_inherits = _ref_targets(refs, kind="inherits", source_name="Middle")
        concrete_inherits = _ref_targets(refs, kind="inherits", source_name="Concrete")
        assert "Model" in base_inherits
        assert "Base" in middle_inherits
        assert "Middle" in concrete_inherits

    def test_direct_model_base_still_in_pending(self):
        """A directly tagged model still has its _pending_inherits entry for inherits edges."""
        src = "class MyModel(models.Model):\n    pass\n"
        _, refs = _parse_py(src)
        inherits = _ref_targets(refs, kind="inherits", source_name="MyModel")
        assert "Model" in inherits
