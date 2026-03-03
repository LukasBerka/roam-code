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


def _parse_py_resolved(source_text: str, file_path: str = "example.py"):
    """Parse Python source with DB-level Django resolution.

    Returns (symbols, references) where symbols include cross-file
    resolution results (framework_type, field_type, field_base_type).
    """
    import json
    import sqlite3

    from roam.index.django_post import (
        resolve_django_custom_fields,
        resolve_django_inheritance,
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
        # Also map by short name for reference resolution
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
