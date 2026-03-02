"""Tests for Django model field extraction, relationship refs, model tagging, and Meta parsing.

Covers:
1. Django field type detection (CharField, ForeignKey, etc.)
2. FK/M2M/O2O relationship reference creation
3. Django model subclass tagging (framework_type='django_model')
4. Meta inner class model= attribute and fields= list parsing
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_python_extractor_v2.py)
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
# 1. Django Field Detection
# ===========================================================================


class TestDjangoFieldDetection:
    """Test that Django model fields are detected with field_type metadata."""

    def test_charfield_detected(self):
        src = (
            "class Article(models.Model):\n"
            "    name = models.CharField(max_length=100)\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "name", parent="Article")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "CharField"

    def test_foreignkey_detected(self):
        src = (
            "class Post(models.Model):\n"
            "    author = models.ForeignKey(User, on_delete=models.CASCADE)\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "author", parent="Post")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "ForeignKey"

    def test_many_to_many_detected(self):
        src = (
            "class Post(models.Model):\n"
            "    tags = models.ManyToManyField(Tag)\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "tags", parent="Post")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "ManyToManyField"

    def test_bare_field_without_models_prefix(self):
        src = (
            "class Article(Model):\n"
            "    name = CharField(max_length=50)\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "name", parent="Article")
        assert sym is not None
        assert sym.get("django_field") is True
        assert sym.get("field_type") == "CharField"

    def test_non_django_call_not_tagged(self):
        src = (
            "class Bag:\n"
            "    items = list()\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "items", parent="Bag")
        assert sym is not None
        assert sym.get("django_field") is None
        assert sym.get("field_type") is None

    def test_multiple_field_types(self):
        src = (
            "class Profile(models.Model):\n"
            "    name = models.CharField(max_length=200)\n"
            "    age = models.IntegerField()\n"
            "    active = models.BooleanField(default=True)\n"
        )
        syms, _ = _parse_py(src)
        name_sym = _find_sym(syms, "name", parent="Profile")
        age_sym = _find_sym(syms, "age", parent="Profile")
        active_sym = _find_sym(syms, "active", parent="Profile")
        assert name_sym["field_type"] == "CharField"
        assert age_sym["field_type"] == "IntegerField"
        assert active_sym["field_type"] == "BooleanField"


# ===========================================================================
# 2. Django Relationship References
# ===========================================================================


class TestDjangoRelationshipRefs:
    """Test that FK/M2M/O2O fields create proper references."""

    def test_fk_creates_reference(self):
        src = (
            "class Post(models.Model):\n"
            "    author = models.ForeignKey(User, on_delete=models.CASCADE)\n"
        )
        _, refs = _parse_py(src)
        fk_targets = _ref_targets(refs, kind="django_fk")
        assert "User" in fk_targets

    def test_m2m_creates_reference(self):
        src = (
            "class Post(models.Model):\n"
            "    tags = models.ManyToManyField(Tag)\n"
        )
        _, refs = _parse_py(src)
        m2m_targets = _ref_targets(refs, kind="django_m2m")
        assert "Tag" in m2m_targets

    def test_o2o_creates_reference(self):
        src = (
            "class UserProfile(models.Model):\n"
            "    user = models.OneToOneField(Profile, on_delete=models.CASCADE)\n"
        )
        _, refs = _parse_py(src)
        o2o_targets = _ref_targets(refs, kind="django_o2o")
        assert "Profile" in o2o_targets

    def test_fk_string_target(self):
        src = (
            "class Comment(models.Model):\n"
            '    user = models.ForeignKey("auth.User", on_delete=models.CASCADE)\n'
        )
        syms, refs = _parse_py(src)
        sym = _find_sym(syms, "user", parent="Comment")
        assert sym["relationship_target"] == "auth.User"
        fk_targets = _ref_targets(refs, kind="django_fk")
        assert "auth.User" in fk_targets

    def test_fk_self_reference(self):
        src = (
            "class Category(models.Model):\n"
            '    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True)\n'
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "parent", parent="Category")
        assert sym["relationship_target"] == "self"

    def test_fk_on_delete_extracted(self):
        src = (
            "class Post(models.Model):\n"
            "    author = models.ForeignKey(User, on_delete=models.CASCADE)\n"
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "author", parent="Post")
        assert sym.get("on_delete") == "models.CASCADE"

    def test_fk_related_name_extracted(self):
        src = (
            "class Post(models.Model):\n"
            '    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="posts")\n'
        )
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "author", parent="Post")
        assert sym.get("related_name") == "posts"


# ===========================================================================
# 3. Django Model Tagging
# ===========================================================================


class TestDjangoModelTagging:
    """Test that Model subclasses are tagged with framework_type='django_model'."""

    def test_model_subclass_tagged(self):
        src = "class Article(models.Model):\n    pass\n"
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "Article")
        assert sym is not None
        assert sym.get("framework_type") == "django_model"

    def test_model_short_import_tagged(self):
        src = "class Article(Model):\n    pass\n"
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "Article")
        assert sym is not None
        assert sym.get("framework_type") == "django_model"

    def test_non_model_class_not_tagged(self):
        src = "class Service(BaseService):\n    pass\n"
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "Service")
        assert sym is not None
        assert sym.get("framework_type") is None

    def test_abstract_model_tagged(self):
        src = "class TimeStampedModel(models.Model):\n    pass\n"
        syms, _ = _parse_py(src)
        sym = _find_sym(syms, "TimeStampedModel")
        assert sym is not None
        assert sym.get("framework_type") == "django_model"


# ===========================================================================
# 4. Meta Class Parsing
# ===========================================================================


class TestMetaClassParsing:
    """Test Meta inner class model= and fields= parsing."""

    def test_meta_model_creates_reference(self):
        src = (
            "class UserSerializer:\n"
            "    class Meta:\n"
            "        model = User\n"
        )
        _, refs = _parse_py(src)
        meta_targets = _ref_targets(refs, kind="meta_model")
        assert "User" in meta_targets

    def test_meta_model_reference_source(self):
        """The meta_model reference source should be the grandparent class, not Meta."""
        src = (
            "class UserSerializer:\n"
            "    class Meta:\n"
            "        model = User\n"
        )
        _, refs = _parse_py(src)
        meta_refs = [r for r in refs if r["kind"] == "meta_model"]
        assert len(meta_refs) == 1
        assert meta_refs[0]["source_name"] == "UserSerializer"
        assert meta_refs[0]["target_name"] == "User"

    def test_meta_fields_extracted(self):
        src = (
            "class UserSerializer:\n"
            "    class Meta:\n"
            "        model = User\n"
            "        fields = ['name', 'email']\n"
        )
        syms, _ = _parse_py(src)
        fields_sym = _find_sym(syms, "fields", parent="UserSerializer.Meta")
        assert fields_sym is not None
        assert fields_sym.get("meta_fields") == ["name", "email"]

    def test_meta_in_admin_class(self):
        src = (
            "class UserAdmin(admin.ModelAdmin):\n"
            "    class Meta:\n"
            "        model = User\n"
        )
        _, refs = _parse_py(src)
        meta_targets = _ref_targets(refs, kind="meta_model")
        assert "User" in meta_targets

    def test_meta_without_model_no_ref(self):
        src = (
            "class Config:\n"
            "    class Meta:\n"
            "        ordering = ['-created']\n"
        )
        _, refs = _parse_py(src)
        meta_targets = _ref_targets(refs, kind="meta_model")
        assert meta_targets == []


# ===========================================================================
# 5. Integration
# ===========================================================================


class TestIntegratedDjango:
    """Test all Django extraction features working together."""

    def test_full_django_model(self):
        src = (
            "class Article(models.Model):\n"
            "    title = models.CharField(max_length=200)\n"
            "    body = models.TextField()\n"
            "    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='articles')\n"
            "    tags = models.ManyToManyField(Tag)\n"
            "    published = models.BooleanField(default=False)\n"
            "\n"
            "    class Meta:\n"
            "        ordering = ['-created']\n"
        )
        syms, refs = _parse_py(src)

        # Model tagging
        article = _find_sym(syms, "Article")
        assert article["framework_type"] == "django_model"

        # Field detection
        title = _find_sym(syms, "title", parent="Article")
        assert title["field_type"] == "CharField"

        body = _find_sym(syms, "body", parent="Article")
        assert body["field_type"] == "TextField"

        author = _find_sym(syms, "author", parent="Article")
        assert author["field_type"] == "ForeignKey"
        assert author["relationship_target"] == "User"
        assert author["on_delete"] == "models.CASCADE"
        assert author["related_name"] == "articles"

        tags = _find_sym(syms, "tags", parent="Article")
        assert tags["field_type"] == "ManyToManyField"

        published = _find_sym(syms, "published", parent="Article")
        assert published["field_type"] == "BooleanField"

        # References
        fk_targets = _ref_targets(refs, kind="django_fk")
        assert "User" in fk_targets

        m2m_targets = _ref_targets(refs, kind="django_m2m")
        assert "Tag" in m2m_targets

        # Inheritance ref to Model
        inherits = _ref_targets(refs, kind="inherits")
        assert "Model" in inherits

    def test_serializer_with_meta(self):
        """Test serializer class with Meta model and fields."""
        src = (
            "class ArticleSerializer:\n"
            "    class Meta:\n"
            "        model = Article\n"
            "        fields = ['title', 'body', 'author']\n"
        )
        syms, refs = _parse_py(src)

        # Meta model reference
        meta_targets = _ref_targets(refs, kind="meta_model")
        assert "Article" in meta_targets

        # Meta fields extraction
        fields_sym = _find_sym(syms, "fields", parent="ArticleSerializer.Meta")
        assert fields_sym["meta_fields"] == ["title", "body", "author"]
