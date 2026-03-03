"""Tests for custom Django field inheritance resolution.

Covers:
1. Custom field classes detected when used as model fields
2. field_type stores custom name, field_base_type stores resolved Django base
3. Custom relationship fields (FK/M2M/O2O subclasses) create correct edges
4. Multi-level custom field inheritance chains
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


def _find_sym(symbols, name, parent=None):
    """Find a single symbol by name and optional parent."""
    for s in symbols:
        if s["name"] == name:
            if parent is not None and s.get("parent_name") != parent:
                continue
            return s
    return None


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
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "name", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyCharField"
        assert sym.get("field_base_type") == "CharField"

    def test_custom_fk_creates_reference(self):
        src = (
            "class MyFK(ForeignKey):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    author = MyFK(User, on_delete=models.CASCADE)\n"
        )
        syms, refs = _parse_py(src)
        sym = _find_sym(syms, "author", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyFK"
        assert sym.get("field_base_type") == "ForeignKey"
        fk_targets = _ref_targets(refs, kind="django_fk")
        assert "User" in fk_targets

    def test_custom_m2m_creates_reference(self):
        src = (
            "class MyM2M(ManyToManyField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    tags = MyM2M(Tag)\n"
        )
        syms, refs = _parse_py(src)
        sym = _find_sym(syms, "tags", parent="M")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "MyM2M"
        assert sym.get("field_base_type") == "ManyToManyField"
        m2m_targets = _ref_targets(refs, kind="django_m2m")
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
        syms, _ = _parse_py(src)
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
        syms, _ = _parse_py(src)
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
            "class MyO2O(OneToOneField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    profile = MyO2O(Profile, on_delete=models.CASCADE)\n"
        )
        syms, refs = _parse_py(src)
        sym = _find_sym(syms, "profile", parent="M")
        assert sym is not None
        assert sym.get("field_type") == "MyO2O"
        assert sym.get("field_base_type") == "OneToOneField"
        o2o_targets = _ref_targets(refs, kind="django_o2o")
        assert "Profile" in o2o_targets

    def test_custom_fk_extracts_on_delete(self):
        src = (
            "class MyFK(ForeignKey):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    author = MyFK(User, on_delete=models.CASCADE)\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "author", parent="M")
        assert sym is not None
        assert sym.get("on_delete") == "models.CASCADE"

    def test_custom_fk_extracts_related_name(self):
        src = (
            "class MyFK(ForeignKey):\n"
            "    pass\n"
            "class M(models.Model):\n"
            '    author = MyFK(User, on_delete=models.CASCADE, related_name="posts")\n'
        )
        syms, _ = _parse_py(src)
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
        syms, refs = _parse_py(src)
        sym = _find_sym(syms, "user", parent="M")
        assert sym is not None
        assert sym.get("relationship_target") == "auth.User"
        fk_targets = _ref_targets(refs, kind="django_fk")
        assert "auth.User" in fk_targets

    def test_mixed_custom_and_standard_fields(self):
        src = (
            "class MyCharField(CharField):\n"
            "    pass\n"
            "class M(models.Model):\n"
            "    custom_name = MyCharField()\n"
            "    standard_name = models.CharField(max_length=100)\n"
            "    age = models.IntegerField()\n"
        )
        syms, _ = _parse_py(src)
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
