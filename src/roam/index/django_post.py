"""Post-indexing Django inheritance and custom field resolution.

Runs after all files are parsed and edges are resolved. Queries the DB
to build a cross-file inheritance graph, then batch-updates framework_type
and field metadata for Django models and custom fields.
"""

from __future__ import annotations

import json

# Duplicated from python_lang.py to avoid circular imports.
# These are stable constants unlikely to diverge.
_DJANGO_MODEL_BASES = frozenset({
    "Model", "models.Model", "TimeStampedModel",
    "AbstractUser", "AbstractBaseUser",
})

_DJANGO_FIELD_TYPES = frozenset({
    "CharField", "IntegerField", "FloatField", "DecimalField",
    "BooleanField", "TextField", "DateField", "DateTimeField",
    "TimeField", "EmailField", "URLField", "UUIDField",
    "SlugField", "FileField", "ImageField", "JSONField",
    "BinaryField", "AutoField", "BigAutoField", "SmallAutoField",
    "BigIntegerField", "SmallIntegerField", "PositiveIntegerField",
    "PositiveSmallIntegerField", "PositiveBigIntegerField",
    "DurationField", "GenericIPAddressField", "FilePathField",
    "ForeignKey", "OneToOneField", "ManyToManyField",
})

_DJANGO_RELATIONSHIP_FIELDS = frozenset({
    "ForeignKey", "OneToOneField", "ManyToManyField",
})

_DJANGO_REL_KIND = {
    "ForeignKey": "django_fk",
    "OneToOneField": "django_o2o",
    "ManyToManyField": "django_m2m",
}


def resolve_django_inheritance(conn) -> int:
    """Resolve transitive Django model inheritance across all indexed files.

    Queries inherits edges and symbols table to build a full inheritance
    graph, walks transitively with cycle detection, and batch-updates
    framework_type='django_model' on all transitive Django model descendants.

    Returns the number of symbols updated.
    """
    # 1. Load class symbols: {id: name, qualified_name, framework_type}
    class_rows = conn.execute(
        "SELECT id, name, qualified_name, framework_type "
        "FROM symbols WHERE kind = 'class'"
    ).fetchall()
    if not class_rows:
        return 0

    # Build lookup maps
    class_by_id = {r["id"]: dict(r) for r in class_rows}
    # Map name -> set of symbol IDs (multiple classes can share a name)
    ids_by_name = {}
    for r in class_rows:
        ids_by_name.setdefault(r["name"], set()).add(r["id"])

    # 2. Load inherits edges: source_id inherits from target_id
    inherits_rows = conn.execute(
        "SELECT source_id, target_id FROM edges WHERE kind = 'inherits'"
    ).fetchall()

    # parent_ids[child_id] = set of parent symbol IDs
    parent_ids = {}
    for r in inherits_rows:
        src, tgt = r["source_id"], r["target_id"]
        if src in class_by_id:
            parent_ids.setdefault(src, set()).add(tgt)

    # 3. Already-tagged: symbols with framework_type='django_model' (fast-path)
    already_tagged = {
        sid for sid, info in class_by_id.items()
        if info["framework_type"] == "django_model"
    }

    # 4. Transitive resolution with memoization
    resolved = {}  # symbol_id -> bool

    def _is_django_model(sid, visited):
        if sid in resolved:
            return resolved[sid]
        if sid in already_tagged:
            resolved[sid] = True
            return True
        if sid in visited:
            resolved[sid] = False
            return False
        visited = visited | {sid}

        # Check if any parent is a django model
        for pid in parent_ids.get(sid, set()):
            if pid in already_tagged:
                resolved[sid] = True
                return True
            if pid in class_by_id:
                if _is_django_model(pid, visited):
                    resolved[sid] = True
                    return True

        resolved[sid] = False
        return False

    # 5. Walk all class symbols
    to_update = []
    for sid in class_by_id:
        if sid in already_tagged:
            continue
        if _is_django_model(sid, set()):
            to_update.append(sid)

    # 6. Batch update
    if to_update:
        with conn:
            for sid in to_update:
                conn.execute(
                    "UPDATE symbols SET framework_type = 'django_model' WHERE id = ?",
                    (sid,),
                )

    return len(to_update)
