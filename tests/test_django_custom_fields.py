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


def _parse_py_resolved(source_text: str, file_path: str = "example.py"):
    """Parse Python source with DB-level Django resolution.

    Returns (symbols, references, db_edges) where symbols include cross-file
    resolution results and db_edges contains edges created by DB resolution.
    """
    import json
    import sqlite3

    from roam.index.django_post import (
        resolve_django_custom_fields,
        resolve_django_inheritance,
        resolve_django_relationships,
    )

    symbols, references = _parse_py(source_text, file_path)

    # Create in-memory DB with minimal schema
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

    # Insert symbols
    sym_id_map = {}  # name -> id
    for sym in symbols:
        # Support both old (_pending_field_call) and new (call_function) field names
        call_function = sym.get("call_function") or sym.get("_pending_field_call")
        field_metadata = sym.get("field_metadata")
        if not field_metadata and sym.get("_pending_field_meta"):
            meta = sym["_pending_field_meta"]
            meta_filtered = {k: v for k, v in meta.items() if v is not None}
            if meta_filtered:
                field_metadata = json.dumps(meta_filtered)

        conn.execute(
            """INSERT INTO symbols
               (name, qualified_name, kind, signature, line_start, line_end,
                docstring, visibility, is_exported, default_value,
                framework_type, call_function, field_type, field_base_type,
                field_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sym["name"], sym["qualified_name"], sym["kind"],
                sym["signature"], sym["line_start"], sym["line_end"],
                sym["docstring"], sym["visibility"],
                1 if sym["is_exported"] else 0,
                sym.get("default_value"), sym.get("framework_type"),
                call_function, sym.get("field_type"),
                sym.get("field_base_type"), field_metadata,
            ),
        )
        row = conn.execute("SELECT last_insert_rowid()").fetchone()
        sym_id_map[sym["qualified_name"] or sym["name"]] = row[0]
        if sym["name"] not in sym_id_map:
            sym_id_map[sym["name"]] = row[0]

    # Set parent_id for nested symbols
    for sym in symbols:
        if sym.get("parent_name"):
            parent_id = sym_id_map.get(sym["parent_name"])
            sym_id = sym_id_map.get(sym["qualified_name"] or sym["name"])
            if parent_id and sym_id:
                conn.execute(
                    "UPDATE symbols SET parent_id = ? WHERE id = ?",
                    (parent_id, sym_id),
                )

    # Insert inherits edges from references
    for ref in references:
        if ref["kind"] == "inherits":
            source_id = sym_id_map.get(ref.get("source_name"))
            target_id = sym_id_map.get(ref.get("target_name"))
            if source_id and target_id:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, kind, line, source_file_id) "
                    "VALUES (?, ?, 'inherits', ?, 1)",
                    (source_id, target_id, ref.get("line", 0)),
                )

    conn.commit()

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
