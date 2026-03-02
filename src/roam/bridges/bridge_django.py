"""Django cross-language bridge: implicit Django relationship resolution.

Resolves cross-references between Django files that are implicit in the
framework but not visible in static imports:
- Admin classes -> Model classes (via @admin.register or admin.site.register)
- Serializer classes -> Model classes (via Meta.model)
- Form classes -> Model classes (via Meta.model)
- FilterSet classes -> Model classes (via Meta.model)
- Signal handlers -> Model classes (via @receiver(signal, sender=Model))
- URL configs -> View classes/functions (via path()/re_path())
- Celery task tagging (via @app.task/@shared_task)
"""

from __future__ import annotations

import re

from roam.bridges.base import LanguageBridge
from roam.bridges.registry import register_bridge

# Django project marker filenames
_DJANGO_MARKERS = frozenset(
    {
        "manage.py",
        "admin.py",
        "models.py",
        "settings.py",
        "urls.py",
        "signals.py",
        "serializers.py",
        "forms.py",
        "views.py",
        "tasks.py",
        "filters.py",
    }
)

# --- Admin mechanism regexes ---
_ADMIN_REGISTER_RE = re.compile(r"@admin\.register\((\w+)")
_ADMIN_SITE_REGISTER_RE = re.compile(r"admin\.site\.register\((\w+)")

# --- Meta.model mechanism: base class detection ---
_SERIALIZER_BASES = frozenset(
    {"ModelSerializer", "Serializer", "HyperlinkedModelSerializer"}
)
_FORM_BASES = frozenset({"ModelForm", "Form"})
_FILTER_BASES = frozenset({"FilterSet"})

# Map base class -> (mechanism, file hint keywords)
_META_MODEL_MECHANISMS: list[tuple[frozenset[str], str, tuple[str, ...]]] = [
    (_SERIALIZER_BASES, "serializes", ("serializer",)),
    (_FORM_BASES, "form_for", ("form",)),
    (_FILTER_BASES, "filters", ("filter",)),
]

# --- Signal handler regexes ---
_RECEIVER_RE = re.compile(
    r"@receiver\(\s*(\w+)\s*,\s*sender\s*=\s*(\w+)"
)
_RECEIVER_STR_RE = re.compile(
    r"@receiver\(\s*(\w+)\s*,\s*sender\s*=\s*['\"](\w+)['\"]"
)

# --- Celery task regexes ---
_CELERY_TASK_RE = re.compile(
    r"@(?:app\.task|shared_task|celery_app\.task)"
)

# --- URL routing regexes ---
_URL_PATH_RE = re.compile(
    r"(?:path|re_path|url)\(\s*['\"]([^'\"]*)['\"],\s*(\w+(?:\.\w+)*)"
)
_AS_VIEW_RE = re.compile(r"(\w+)\.as_view\(\)")


def _build_model_index(
    target_files: dict[str, list[dict]],
) -> dict[str, str]:
    """Build a name -> qualified_name index of model classes in target files.

    Prefers symbols with framework_type='django_model' but also indexes
    plain classes as fallback.
    """
    models: dict[str, tuple[str, bool]] = {}  # name -> (qname, is_django_model)
    for _path, symbols in target_files.items():
        for sym in symbols:
            if sym.get("kind") != "class":
                continue
            name = sym.get("name", "")
            qname = sym.get("qualified_name", name)
            is_django = sym.get("framework_type") == "django_model"
            existing = models.get(name)
            if existing is None or (is_django and not existing[1]):
                models[name] = (qname, is_django)
    return {name: (qname, is_dm) for name, (qname, is_dm) in models.items()}


def _build_symbol_index(
    target_files: dict[str, list[dict]],
) -> dict[str, str]:
    """Build a name -> qualified_name index of all symbols in target files."""
    index: dict[str, str] = {}
    for _path, symbols in target_files.items():
        for sym in symbols:
            name = sym.get("name", "")
            qname = sym.get("qualified_name", name)
            if name not in index:
                index[name] = qname
    return index


class DjangoBridge(LanguageBridge):
    """Bridge for implicit Django relationships across Python files."""

    @property
    def name(self) -> str:
        return "django"

    @property
    def source_extensions(self) -> frozenset[str]:
        return frozenset({".py"})

    @property
    def target_extensions(self) -> frozenset[str]:
        return frozenset({".py"})

    def detect(self, file_paths: list[str]) -> bool:
        """Return True if any file path ends with a Django marker filename."""
        for fp in file_paths:
            # Check if basename matches a Django marker
            basename = fp.rsplit("/", 1)[-1] if "/" in fp else fp
            basename = basename.rsplit("\\", 1)[-1] if "\\" in basename else basename
            if basename in _DJANGO_MARKERS:
                return True
        return False

    def resolve(
        self,
        source_path: str,
        source_symbols: list[dict],
        target_files: dict[str, list[dict]],
    ) -> list[dict]:
        """Resolve implicit Django relationships.

        Scans source_symbols for Django patterns (admin registrations,
        Meta.model references, signal receivers, celery decorators, URL
        configs) and matches them against target_files symbols.
        """
        edges: list[dict] = []
        model_index = _build_model_index(target_files)

        edges.extend(self._resolve_admin(source_path, source_symbols, model_index))
        edges.extend(self._resolve_meta_model(source_path, source_symbols, model_index))
        edges.extend(self._resolve_signals(source_symbols, model_index))
        edges.extend(self._resolve_celery(source_symbols))
        edges.extend(self._resolve_urls(source_path, source_symbols, target_files))

        return edges

    # ------------------------------------------------------------------
    # Admin mechanism
    # ------------------------------------------------------------------

    def _resolve_admin(
        self,
        source_path: str,
        source_symbols: list[dict],
        model_index: dict[str, tuple[str, bool]],
    ) -> list[dict]:
        edges: list[dict] = []
        for sym in source_symbols:
            if sym.get("kind") != "class":
                continue
            sig = sym.get("signature", "") or ""
            qname = sym.get("qualified_name", sym.get("name", ""))

            # @admin.register(Model) decorator
            for m in _ADMIN_REGISTER_RE.finditer(sig):
                model_name = m.group(1)
                edge = self._make_admin_edge(qname, model_name, model_index)
                if edge:
                    edges.append(edge)

            # admin.site.register(Model, ...) in signature
            for m in _ADMIN_SITE_REGISTER_RE.finditer(sig):
                model_name = m.group(1)
                edge = self._make_admin_edge(qname, model_name, model_index)
                if edge:
                    edges.append(edge)

        # Also scan non-class symbols for admin.site.register calls
        for sym in source_symbols:
            if sym.get("kind") == "class":
                continue
            sig = sym.get("signature", "") or ""
            qname = sym.get("qualified_name", sym.get("name", ""))
            for m in _ADMIN_SITE_REGISTER_RE.finditer(sig):
                model_name = m.group(1)
                edge = self._make_admin_edge(qname, model_name, model_index)
                if edge:
                    edges.append(edge)

        return edges

    def _make_admin_edge(
        self,
        source_qname: str,
        model_name: str,
        model_index: dict[str, tuple[str, bool]],
    ) -> dict | None:
        entry = model_index.get(model_name)
        if entry is None:
            return None
        target_qname, is_django_model = entry
        return {
            "source": source_qname,
            "target": target_qname,
            "kind": "x-lang",
            "bridge": self.name,
            "mechanism": "admin_registers",
            "confidence": 0.9 if is_django_model else 0.7,
        }

    # ------------------------------------------------------------------
    # Meta.model mechanisms (serializes, form_for, filters)
    # ------------------------------------------------------------------

    def _resolve_meta_model(
        self,
        source_path: str,
        source_symbols: list[dict],
        model_index: dict[str, tuple[str, bool]],
    ) -> list[dict]:
        edges: list[dict] = []

        # Build index of child symbols by parent qualified_name prefix
        child_map: dict[str, list[dict]] = {}
        for sym in source_symbols:
            qname = sym.get("qualified_name", "")
            if "." in qname:
                parent_prefix = qname.rsplit(".", 1)[0]
                child_map.setdefault(parent_prefix, []).append(sym)

        for sym in source_symbols:
            if sym.get("kind") != "class":
                continue
            sig = sym.get("signature", "") or ""
            qname = sym.get("qualified_name", sym.get("name", ""))
            path_lower = source_path.lower()

            mechanism = self._detect_meta_mechanism(sig, path_lower)
            if mechanism is None:
                continue

            # Find model reference via child symbols (Meta.model property)
            model_name = self._find_meta_model_name(qname, child_map, source_symbols)
            if model_name is None:
                continue

            entry = model_index.get(model_name)
            if entry is None:
                continue

            target_qname, is_django_model = entry
            # Base class match in signature = 0.9, file path hint only = 0.7
            has_base_match = self._has_base_class_match(sig, mechanism)
            confidence = 0.9 if has_base_match else 0.7

            edges.append(
                {
                    "source": qname,
                    "target": target_qname,
                    "kind": "x-lang",
                    "bridge": self.name,
                    "mechanism": mechanism,
                    "confidence": confidence,
                }
            )

        return edges

    def _detect_meta_mechanism(self, sig: str, path_lower: str) -> str | None:
        """Determine the mechanism type from base class or file path hints."""
        for bases, mechanism, path_hints in _META_MODEL_MECHANISMS:
            for base in bases:
                if base in sig:
                    return mechanism
            for hint in path_hints:
                if hint in path_lower:
                    return mechanism
        return None

    def _has_base_class_match(self, sig: str, mechanism: str) -> bool:
        """Check if the signature has an explicit base class match."""
        for bases, mech, _ in _META_MODEL_MECHANISMS:
            if mech == mechanism:
                return any(base in sig for base in bases)
        return False

    def _find_meta_model_name(
        self,
        class_qname: str,
        child_map: dict[str, list[dict]],
        source_symbols: list[dict],
    ) -> str | None:
        """Find the model name from Meta.model child symbol or meta_model ref."""
        # Check for Meta.model child property
        # Look for children of the class or of Class.Meta
        meta_qname = f"{class_qname}.Meta"
        for prefix in (meta_qname, class_qname):
            children = child_map.get(prefix, [])
            for child in children:
                if child.get("name") == "model" and child.get("kind") == "property":
                    # default_value often holds the model class name
                    val = child.get("default_value", "") or ""
                    if val:
                        return val
                    # signature may hold it
                    child_sig = child.get("signature", "") or ""
                    if child_sig:
                        # Extract simple identifier
                        m = re.match(r"(\w+)", child_sig)
                        if m:
                            return m.group(1)

        # Fallback: check for meta_model references among source symbols
        for sym in source_symbols:
            ref_kind = sym.get("kind", "")
            if ref_kind == "meta_model":
                ref_qname = sym.get("qualified_name", "")
                if ref_qname.startswith(class_qname):
                    target = sym.get("name", "")
                    if target:
                        return target

        return None

    # ------------------------------------------------------------------
    # Signal handler mechanism
    # ------------------------------------------------------------------

    def _resolve_signals(
        self,
        source_symbols: list[dict],
        model_index: dict[str, tuple[str, bool]],
    ) -> list[dict]:
        edges: list[dict] = []
        for sym in source_symbols:
            if sym.get("kind") not in ("function", "method"):
                continue
            sig = sym.get("signature", "") or ""
            qname = sym.get("qualified_name", sym.get("name", ""))

            # Try class reference: @receiver(post_save, sender=Model)
            for m in _RECEIVER_RE.finditer(sig):
                signal_name = m.group(1)
                sender_name = m.group(2)
                edge = self._make_signal_edge(qname, sender_name, signal_name, model_index)
                if edge:
                    edges.append(edge)

            # Try string reference: @receiver(post_save, sender='Model')
            for m in _RECEIVER_STR_RE.finditer(sig):
                signal_name = m.group(1)
                sender_name = m.group(2)
                edge = self._make_signal_edge(qname, sender_name, signal_name, model_index)
                if edge:
                    edges.append(edge)

        return edges

    def _make_signal_edge(
        self,
        source_qname: str,
        sender_name: str,
        signal_name: str,
        model_index: dict[str, tuple[str, bool]],
    ) -> dict | None:
        entry = model_index.get(sender_name)
        if entry is None:
            return None
        target_qname, _is_django = entry
        return {
            "source": source_qname,
            "target": target_qname,
            "kind": "x-lang",
            "bridge": self.name,
            "mechanism": "signal_handler",
            "confidence": 0.9,
            "signal": signal_name,
        }

    # ------------------------------------------------------------------
    # Celery task mechanism
    # ------------------------------------------------------------------

    def _resolve_celery(self, source_symbols: list[dict]) -> list[dict]:
        edges: list[dict] = []
        for sym in source_symbols:
            if sym.get("kind") not in ("function", "method"):
                continue
            sig = sym.get("signature", "") or ""
            qname = sym.get("qualified_name", sym.get("name", ""))

            if _CELERY_TASK_RE.search(sig):
                edges.append(
                    {
                        "source": qname,
                        "target": qname,
                        "kind": "x-lang",
                        "bridge": self.name,
                        "mechanism": "celery_task",
                        "confidence": 1.0,
                        "framework_type": "celery_task",
                    }
                )

        return edges

    # ------------------------------------------------------------------
    # URL routing mechanism
    # ------------------------------------------------------------------

    def _resolve_urls(
        self,
        source_path: str,
        source_symbols: list[dict],
        target_files: dict[str, list[dict]],
    ) -> list[dict]:
        edges: list[dict] = []
        path_lower = source_path.lower()
        if "urls" not in path_lower and "url" not in path_lower:
            return edges

        # Build symbol index for view matching
        symbol_index = _build_symbol_index(target_files)

        for sym in source_symbols:
            sig = sym.get("signature", "") or ""
            qname = sym.get("qualified_name", sym.get("name", ""))

            # Match path()/re_path()/url() calls
            for m in _URL_PATH_RE.finditer(sig):
                url_pattern = m.group(1)
                view_ref = m.group(2)
                edge = self._make_url_edge(qname, view_ref, url_pattern, symbol_index)
                if edge:
                    edges.append(edge)

            # Match Class.as_view() references
            for m in _AS_VIEW_RE.finditer(sig):
                view_class = m.group(1)
                edge = self._make_url_edge(qname, view_class, "", symbol_index)
                if edge:
                    edges.append(edge)

        return edges

    def _make_url_edge(
        self,
        source_qname: str,
        view_ref: str,
        url_pattern: str,
        symbol_index: dict[str, str],
    ) -> dict | None:
        # Extract the final name from dotted ref (e.g. views.BookView -> BookView)
        view_name = view_ref.rsplit(".", 1)[-1] if "." in view_ref else view_ref
        target_qname = symbol_index.get(view_name)
        if target_qname is None:
            return None
        edge: dict = {
            "source": source_qname,
            "target": target_qname,
            "kind": "x-lang",
            "bridge": self.name,
            "mechanism": "routes_to",
            "confidence": 0.85,
        }
        if url_pattern:
            edge["url_pattern"] = url_pattern
        return edge


# Auto-register on import
register_bridge(DjangoBridge())
