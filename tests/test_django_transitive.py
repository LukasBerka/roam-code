"""Tests for transitive Django model inheritance resolution.

Covers:
1. Transitive model tagging (1-level, 2-level, mixed bases)
2. Cycle detection in circular inheritance chains
3. Non-model classes left untagged
4. Inherits reference edges for transitive models
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_django_extraction.py)
# ---------------------------------------------------------------------------


def _parse_py(source_text: str, file_path: str = "example.py"):
    """Parse Python source and return (symbols, references)."""
    from tree_sitter_language_pack import get_parser

    from roam.index.parser import GRAMMAR_ALIASES
    from roam.languages.registry import get_extractor

    grammar = GRAMMAR_ALIASES.get("python", "python")
    parser = get_parser(grammar)
    source = source_text.encode("utf-8")
    tree = parser.parse(source)

    extractor = get_extractor("python")
    symbols = extractor.extract_symbols(tree, source, file_path)
    references = extractor.extract_references(tree, source, file_path)
    return symbols, references


def _sym_names(symbols, kind=None, parent=None):
    """Get symbol names, optionally filtered by kind and/or parent."""
    result = []
    for s in symbols:
        if kind and s["kind"] != kind:
            continue
        if parent is not None and s.get("parent_name") != parent:
            continue
        result.append(s["name"])
    return result


def _ref_targets(refs, kind=None, source_name=None):
    """Get reference target_names, optionally filtered by kind and/or source_name."""
    result = []
    for r in refs:
        if kind and r["kind"] != kind:
            continue
        if source_name is not None and r.get("source_name") != source_name:
            continue
        result.append(r["target_name"])
    return result


def _find_sym(symbols, name, parent=None):
    """Find a single symbol by name and optional parent."""
    for s in symbols:
        if s["name"] == name:
            if parent is not None and s.get("parent_name") != parent:
                continue
            return s
    return None


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
        syms, _ = _parse_py(src)
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
        syms, _ = _parse_py(src)
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
        syms, _ = _parse_py(src)
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
        syms, _ = _parse_py(src)
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
        syms, _ = _parse_py(src)
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
