"""Tests for the Django cross-language bridge.

Covers:
- DjangoBridge properties (name, source/target extensions)
- detect() with Django project markers and non-Django projects
- admin_registers mechanism: @admin.register and admin.site.register
- serializes mechanism: ModelSerializer with Meta.model
- form_for mechanism: ModelForm with Meta.model
- filters mechanism: FilterSet with Meta.model
- signal_handler mechanism: @receiver(signal, sender=Model)
- celery_task mechanism: @app.task and @shared_task
- routes_to mechanism: URL path() referencing views
- Edge metadata: confidence scores, required fields
- Integration: multi-mechanism resolution, empty inputs
"""

from __future__ import annotations

import pytest

from roam.bridges import registry as bridge_registry
from roam.bridges.bridge_django import DjangoBridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_registry():
    """Clear the global bridge registry for isolation."""
    bridge_registry._BRIDGES.clear()


def _model_target(name, qname=None, framework_type="django_model"):
    """Build a minimal model symbol dict for use in target_files."""
    return {
        "name": name,
        "kind": "class",
        "qualified_name": qname or f"myapp.models.{name}",
        "framework_type": framework_type,
    }


def _class_symbol(name, qname=None, signature="", **extra):
    """Build a class symbol dict for use in source_symbols."""
    d = {
        "name": name,
        "kind": "class",
        "qualified_name": qname or name,
        "signature": signature,
    }
    d.update(extra)
    return d


def _func_symbol(name, qname=None, signature="", **extra):
    """Build a function symbol dict for use in source_symbols."""
    d = {
        "name": name,
        "kind": "function",
        "qualified_name": qname or name,
        "signature": signature,
    }
    d.update(extra)
    return d


def _property_symbol(name, qname=None, default_value="", **extra):
    """Build a property symbol dict (e.g. Meta.model)."""
    d = {
        "name": name,
        "kind": "property",
        "qualified_name": qname or name,
        "default_value": default_value,
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# DjangoBridge properties
# ---------------------------------------------------------------------------


class TestDjangoBridgeProperties:
    def setup_method(self):
        self.bridge = DjangoBridge()

    def test_name(self):
        assert self.bridge.name == "django"

    def test_source_extensions(self):
        assert self.bridge.source_extensions == frozenset({".py"})

    def test_target_extensions(self):
        assert self.bridge.target_extensions == frozenset({".py"})


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestDjangoBridgeDetect:
    def setup_method(self):
        self.bridge = DjangoBridge()

    def test_detect_with_manage_py(self):
        assert self.bridge.detect(["manage.py", "app/models.py"]) is True

    def test_detect_with_admin_py(self):
        assert self.bridge.detect(["myapp/admin.py", "myapp/models.py"]) is True

    def test_detect_with_urls_py(self):
        assert self.bridge.detect(["myapp/urls.py"]) is True

    def test_detect_with_settings_py(self):
        assert self.bridge.detect(["config/settings.py"]) is True

    def test_detect_with_serializers_py(self):
        assert self.bridge.detect(["api/serializers.py"]) is True

    def test_detect_false_for_non_django(self):
        assert self.bridge.detect(["main.py", "utils.py", "setup.py"]) is False

    def test_detect_false_for_empty(self):
        assert self.bridge.detect([]) is False

    def test_detect_false_for_go_project(self):
        assert self.bridge.detect(["main.go", "go.mod", "handler.go"]) is False


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestDjangoBridgeRegistry:
    def setup_method(self):
        self._saved = list(bridge_registry._BRIDGES)
        _reset_registry()

    def teardown_method(self):
        bridge_registry._BRIDGES.clear()
        bridge_registry._BRIDGES.extend(self._saved)

    def test_auto_registered(self):
        # Re-register by instantiating a fresh bridge (module-level
        # registration already happened at first import; we cleared the
        # registry in setup, so re-register manually to verify the
        # register_bridge path works)
        from roam.bridges.registry import register_bridge

        register_bridge(DjangoBridge())
        names = [b.name for b in bridge_registry.get_bridges()]
        assert "django" in names

    def test_bridge_django_in_auto_discover_imports(self):
        """Verify bridge_django is listed in _auto_discover() source."""
        import inspect

        source = inspect.getsource(bridge_registry._auto_discover)
        assert "bridge_django" in source


# ---------------------------------------------------------------------------
# admin_registers mechanism
# ---------------------------------------------------------------------------


class TestAdminRegisters:
    def setup_method(self):
        self.bridge = DjangoBridge()
        self.target_files = {
            "myapp/models.py": [
                _model_target("Book"),
                _model_target("Author"),
            ]
        }

    def test_admin_register_decorator(self):
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                qname="myapp.admin.BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, self.target_files)
        admin_edges = [e for e in edges if e["mechanism"] == "admin_registers"]
        assert len(admin_edges) == 1
        assert admin_edges[0]["source"] == "myapp.admin.BookAdmin"
        assert admin_edges[0]["target"] == "myapp.models.Book"
        assert admin_edges[0]["confidence"] == 0.9

    def test_admin_site_register(self):
        source_symbols = [
            _class_symbol(
                "AuthorAdmin",
                qname="myapp.admin.AuthorAdmin",
                signature="class AuthorAdmin(admin.ModelAdmin)",
            ),
            _func_symbol(
                "register_call",
                qname="myapp.admin.register_call",
                signature="admin.site.register(Author, AuthorAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, self.target_files)
        admin_edges = [e for e in edges if e["mechanism"] == "admin_registers"]
        assert len(admin_edges) >= 1
        assert any(e["target"] == "myapp.models.Author" for e in admin_edges)

    def test_admin_no_match(self):
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                signature="@admin.register(NonExistentModel)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, self.target_files)
        admin_edges = [e for e in edges if e["mechanism"] == "admin_registers"]
        assert len(admin_edges) == 0

    def test_admin_confidence_with_framework_type(self):
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, self.target_files)
        admin_edges = [e for e in edges if e["mechanism"] == "admin_registers"]
        assert len(admin_edges) == 1
        assert admin_edges[0]["confidence"] == 0.9

    def test_admin_confidence_without_framework_type(self):
        target_no_ft = {
            "myapp/models.py": [
                {"name": "Book", "kind": "class", "qualified_name": "myapp.models.Book"},
            ]
        }
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, target_no_ft)
        admin_edges = [e for e in edges if e["mechanism"] == "admin_registers"]
        assert len(admin_edges) == 1
        assert admin_edges[0]["confidence"] == 0.7

    def test_admin_returns_empty_for_non_admin(self):
        source_symbols = [
            _class_symbol("SomeView", signature="class SomeView(View)"),
        ]
        edges = self.bridge.resolve("myapp/views.py", source_symbols, self.target_files)
        admin_edges = [e for e in edges if e["mechanism"] == "admin_registers"]
        assert len(admin_edges) == 0


# ---------------------------------------------------------------------------
# serializes mechanism
# ---------------------------------------------------------------------------


class TestSerializes:
    def setup_method(self):
        self.bridge = DjangoBridge()
        self.target_files = {
            "myapp/models.py": [_model_target("Book")]
        }

    def test_model_serializer_meta_model(self):
        source_symbols = [
            _class_symbol(
                "BookSerializer",
                qname="myapp.serializers.BookSerializer",
                signature="class BookSerializer(ModelSerializer)",
            ),
            _property_symbol(
                "model",
                qname="myapp.serializers.BookSerializer.Meta.model",
                default_value="Book",
            ),
        ]
        edges = self.bridge.resolve(
            "myapp/serializers.py", source_symbols, self.target_files
        )
        ser_edges = [e for e in edges if e["mechanism"] == "serializes"]
        assert len(ser_edges) == 1
        assert ser_edges[0]["source"] == "myapp.serializers.BookSerializer"
        assert ser_edges[0]["target"] == "myapp.models.Book"

    def test_hyperlinked_serializer(self):
        source_symbols = [
            _class_symbol(
                "BookSerializer",
                qname="myapp.serializers.BookSerializer",
                signature="class BookSerializer(HyperlinkedModelSerializer)",
            ),
            _property_symbol(
                "model",
                qname="myapp.serializers.BookSerializer.Meta.model",
                default_value="Book",
            ),
        ]
        edges = self.bridge.resolve(
            "myapp/serializers.py", source_symbols, self.target_files
        )
        ser_edges = [e for e in edges if e["mechanism"] == "serializes"]
        assert len(ser_edges) == 1

    def test_serializer_confidence_with_base_class(self):
        source_symbols = [
            _class_symbol(
                "BookSerializer",
                qname="myapp.serializers.BookSerializer",
                signature="class BookSerializer(ModelSerializer)",
            ),
            _property_symbol(
                "model",
                qname="myapp.serializers.BookSerializer.Meta.model",
                default_value="Book",
            ),
        ]
        edges = self.bridge.resolve(
            "myapp/serializers.py", source_symbols, self.target_files
        )
        ser_edges = [e for e in edges if e["mechanism"] == "serializes"]
        assert len(ser_edges) == 1
        assert ser_edges[0]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# form_for mechanism
# ---------------------------------------------------------------------------


class TestFormFor:
    def setup_method(self):
        self.bridge = DjangoBridge()
        self.target_files = {
            "myapp/models.py": [_model_target("User")]
        }

    def test_model_form_meta_model(self):
        source_symbols = [
            _class_symbol(
                "UserForm",
                qname="myapp.forms.UserForm",
                signature="class UserForm(ModelForm)",
            ),
            _property_symbol(
                "model",
                qname="myapp.forms.UserForm.Meta.model",
                default_value="User",
            ),
        ]
        edges = self.bridge.resolve("myapp/forms.py", source_symbols, self.target_files)
        form_edges = [e for e in edges if e["mechanism"] == "form_for"]
        assert len(form_edges) == 1
        assert form_edges[0]["source"] == "myapp.forms.UserForm"
        assert form_edges[0]["target"] == "myapp.models.User"

    def test_form_confidence(self):
        source_symbols = [
            _class_symbol(
                "UserForm",
                qname="myapp.forms.UserForm",
                signature="class UserForm(ModelForm)",
            ),
            _property_symbol(
                "model",
                qname="myapp.forms.UserForm.Meta.model",
                default_value="User",
            ),
        ]
        edges = self.bridge.resolve("myapp/forms.py", source_symbols, self.target_files)
        form_edges = [e for e in edges if e["mechanism"] == "form_for"]
        assert len(form_edges) == 1
        assert form_edges[0]["confidence"] >= 0.7


# ---------------------------------------------------------------------------
# filters mechanism
# ---------------------------------------------------------------------------


class TestFilters:
    def setup_method(self):
        self.bridge = DjangoBridge()
        self.target_files = {
            "myapp/models.py": [_model_target("Order")]
        }

    def test_filterset_meta_model(self):
        source_symbols = [
            _class_symbol(
                "OrderFilter",
                qname="myapp.filters.OrderFilter",
                signature="class OrderFilter(FilterSet)",
            ),
            _property_symbol(
                "model",
                qname="myapp.filters.OrderFilter.Meta.model",
                default_value="Order",
            ),
        ]
        edges = self.bridge.resolve("myapp/filters.py", source_symbols, self.target_files)
        filter_edges = [e for e in edges if e["mechanism"] == "filters"]
        assert len(filter_edges) == 1
        assert filter_edges[0]["source"] == "myapp.filters.OrderFilter"
        assert filter_edges[0]["target"] == "myapp.models.Order"

    def test_filters_confidence(self):
        source_symbols = [
            _class_symbol(
                "OrderFilter",
                qname="myapp.filters.OrderFilter",
                signature="class OrderFilter(FilterSet)",
            ),
            _property_symbol(
                "model",
                qname="myapp.filters.OrderFilter.Meta.model",
                default_value="Order",
            ),
        ]
        edges = self.bridge.resolve("myapp/filters.py", source_symbols, self.target_files)
        filter_edges = [e for e in edges if e["mechanism"] == "filters"]
        assert len(filter_edges) == 1
        assert filter_edges[0]["confidence"] >= 0.7


# ---------------------------------------------------------------------------
# signal_handler mechanism
# ---------------------------------------------------------------------------


class TestSignalHandler:
    def setup_method(self):
        self.bridge = DjangoBridge()
        self.target_files = {
            "myapp/models.py": [
                _model_target("Book"),
                _model_target("Author"),
            ]
        }

    def test_receiver_post_save(self):
        source_symbols = [
            _func_symbol(
                "update_cache",
                qname="myapp.signals.update_cache",
                signature="@receiver(post_save, sender=Book)\ndef update_cache(sender, instance, **kwargs)",
            ),
        ]
        edges = self.bridge.resolve("myapp/signals.py", source_symbols, self.target_files)
        sig_edges = [e for e in edges if e["mechanism"] == "signal_handler"]
        assert len(sig_edges) == 1
        assert sig_edges[0]["source"] == "myapp.signals.update_cache"
        assert sig_edges[0]["target"] == "myapp.models.Book"
        assert sig_edges[0]["confidence"] >= 0.85

    def test_receiver_pre_delete(self):
        source_symbols = [
            _func_symbol(
                "cleanup_author",
                qname="myapp.signals.cleanup_author",
                signature="@receiver(pre_delete, sender=Author)\ndef cleanup_author(sender, **kwargs)",
            ),
        ]
        edges = self.bridge.resolve("myapp/signals.py", source_symbols, self.target_files)
        sig_edges = [e for e in edges if e["mechanism"] == "signal_handler"]
        assert len(sig_edges) == 1
        assert sig_edges[0]["target"] == "myapp.models.Author"

    def test_receiver_string_sender(self):
        source_symbols = [
            _func_symbol(
                "on_save",
                qname="myapp.signals.on_save",
                signature="@receiver(post_save, sender='Book')\ndef on_save(sender, **kwargs)",
            ),
        ]
        edges = self.bridge.resolve("myapp/signals.py", source_symbols, self.target_files)
        sig_edges = [e for e in edges if e["mechanism"] == "signal_handler"]
        assert len(sig_edges) == 1
        assert sig_edges[0]["target"] == "myapp.models.Book"

    def test_receiver_no_sender(self):
        source_symbols = [
            _func_symbol(
                "generic_handler",
                qname="myapp.signals.generic_handler",
                signature="@receiver(post_save)\ndef generic_handler(sender, **kwargs)",
            ),
        ]
        edges = self.bridge.resolve("myapp/signals.py", source_symbols, self.target_files)
        sig_edges = [e for e in edges if e["mechanism"] == "signal_handler"]
        assert len(sig_edges) == 0

    def test_signal_edge_includes_signal_name(self):
        source_symbols = [
            _func_symbol(
                "on_save",
                qname="myapp.signals.on_save",
                signature="@receiver(post_save, sender=Book)\ndef on_save(sender, **kwargs)",
            ),
        ]
        edges = self.bridge.resolve("myapp/signals.py", source_symbols, self.target_files)
        sig_edges = [e for e in edges if e["mechanism"] == "signal_handler"]
        assert len(sig_edges) == 1
        assert sig_edges[0]["signal"] == "post_save"


# ---------------------------------------------------------------------------
# celery_task mechanism
# ---------------------------------------------------------------------------


class TestCeleryTask:
    def setup_method(self):
        self.bridge = DjangoBridge()
        self.target_files = {}

    def test_shared_task_detected(self):
        source_symbols = [
            _func_symbol(
                "send_email",
                qname="myapp.tasks.send_email",
                signature="@shared_task\ndef send_email(to, subject, body)",
            ),
        ]
        edges = self.bridge.resolve("myapp/tasks.py", source_symbols, self.target_files)
        celery_edges = [e for e in edges if e["mechanism"] == "celery_task"]
        assert len(celery_edges) == 1
        assert celery_edges[0]["source"] == "myapp.tasks.send_email"
        assert celery_edges[0]["target"] == "myapp.tasks.send_email"
        assert celery_edges[0]["confidence"] == 1.0
        assert celery_edges[0]["framework_type"] == "celery_task"

    def test_app_task_detected(self):
        source_symbols = [
            _func_symbol(
                "process_order",
                qname="myapp.tasks.process_order",
                signature="@app.task\ndef process_order(order_id)",
            ),
        ]
        edges = self.bridge.resolve("myapp/tasks.py", source_symbols, self.target_files)
        celery_edges = [e for e in edges if e["mechanism"] == "celery_task"]
        assert len(celery_edges) == 1

    def test_celery_app_task_detected(self):
        source_symbols = [
            _func_symbol(
                "sync_data",
                qname="myapp.tasks.sync_data",
                signature="@celery_app.task\ndef sync_data()",
            ),
        ]
        edges = self.bridge.resolve("myapp/tasks.py", source_symbols, self.target_files)
        celery_edges = [e for e in edges if e["mechanism"] == "celery_task"]
        assert len(celery_edges) == 1

    def test_non_task_function_ignored(self):
        source_symbols = [
            _func_symbol(
                "helper",
                qname="myapp.tasks.helper",
                signature="def helper(x, y)",
            ),
        ]
        edges = self.bridge.resolve("myapp/tasks.py", source_symbols, self.target_files)
        celery_edges = [e for e in edges if e["mechanism"] == "celery_task"]
        assert len(celery_edges) == 0


# ---------------------------------------------------------------------------
# routes_to mechanism
# ---------------------------------------------------------------------------


class TestRoutesTo:
    def setup_method(self):
        self.bridge = DjangoBridge()
        self.target_files = {
            "myapp/views.py": [
                _func_symbol("book_list", qname="myapp.views.book_list"),
                _class_symbol("BookView", qname="myapp.views.BookView"),
            ]
        }

    def test_url_path_to_view_function(self):
        source_symbols = [
            _func_symbol(
                "urlpatterns",
                qname="myapp.urls.urlpatterns",
                signature="path('books/', views.book_list)",
            ),
        ]
        edges = self.bridge.resolve("myapp/urls.py", source_symbols, self.target_files)
        route_edges = [e for e in edges if e["mechanism"] == "routes_to"]
        assert len(route_edges) >= 1
        assert any(e["target"] == "myapp.views.book_list" for e in route_edges)

    def test_url_path_to_class_view(self):
        source_symbols = [
            _func_symbol(
                "urlpatterns",
                qname="myapp.urls.urlpatterns",
                signature="path('books/', BookView.as_view())",
            ),
        ]
        edges = self.bridge.resolve("myapp/urls.py", source_symbols, self.target_files)
        route_edges = [e for e in edges if e["mechanism"] == "routes_to"]
        assert len(route_edges) >= 1
        assert any(e["target"] == "myapp.views.BookView" for e in route_edges)

    def test_url_no_match(self):
        source_symbols = [
            _func_symbol(
                "urlpatterns",
                qname="myapp.urls.urlpatterns",
                signature="path('orders/', views.unknown_view)",
            ),
        ]
        edges = self.bridge.resolve("myapp/urls.py", source_symbols, self.target_files)
        route_edges = [e for e in edges if e["mechanism"] == "routes_to"]
        assert len(route_edges) == 0

    def test_routes_to_confidence(self):
        source_symbols = [
            _func_symbol(
                "urlpatterns",
                qname="myapp.urls.urlpatterns",
                signature="path('books/', views.book_list)",
            ),
        ]
        edges = self.bridge.resolve("myapp/urls.py", source_symbols, self.target_files)
        route_edges = [e for e in edges if e["mechanism"] == "routes_to"]
        assert len(route_edges) >= 1
        assert route_edges[0]["confidence"] >= 0.7

    def test_routes_to_includes_url_pattern(self):
        source_symbols = [
            _func_symbol(
                "urlpatterns",
                qname="myapp.urls.urlpatterns",
                signature="path('books/', views.book_list)",
            ),
        ]
        edges = self.bridge.resolve("myapp/urls.py", source_symbols, self.target_files)
        route_edges = [e for e in edges if e["mechanism"] == "routes_to"]
        assert len(route_edges) >= 1
        assert route_edges[0].get("url_pattern") == "books/"

    def test_non_url_file_skipped(self):
        source_symbols = [
            _func_symbol(
                "something",
                signature="path('books/', views.book_list)",
            ),
        ]
        edges = self.bridge.resolve("myapp/views.py", source_symbols, self.target_files)
        route_edges = [e for e in edges if e["mechanism"] == "routes_to"]
        assert len(route_edges) == 0


# ---------------------------------------------------------------------------
# Edge confidence scoring and metadata
# ---------------------------------------------------------------------------


class TestEdgeConfidenceScoring:
    def setup_method(self):
        self.bridge = DjangoBridge()

    def test_all_edges_have_confidence(self):
        target_files = {
            "myapp/models.py": [_model_target("Book")]
        }
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                qname="myapp.admin.BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
            _func_symbol(
                "send_email",
                qname="myapp.tasks.send_email",
                signature="@shared_task\ndef send_email()",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, target_files)
        assert len(edges) >= 1
        for edge in edges:
            assert "confidence" in edge
            assert isinstance(edge["confidence"], float)
            assert 0.0 <= edge["confidence"] <= 1.0

    def test_all_edges_have_required_fields(self):
        target_files = {
            "myapp/models.py": [_model_target("Book")]
        }
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, target_files)
        required = {"source", "target", "kind", "bridge", "mechanism", "confidence"}
        for edge in edges:
            assert required.issubset(edge.keys()), f"Missing fields: {required - edge.keys()}"

    def test_bridge_field_is_django(self):
        target_files = {
            "myapp/models.py": [_model_target("Book")]
        }
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, target_files)
        for edge in edges:
            assert edge["bridge"] == "django"

    def test_kind_is_xlang(self):
        target_files = {
            "myapp/models.py": [_model_target("Book")]
        }
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, target_files)
        for edge in edges:
            assert edge["kind"] == "x-lang"


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestResolveIntegration:
    def setup_method(self):
        self.bridge = DjangoBridge()

    def test_resolve_empty_source(self):
        target_files = {"myapp/models.py": [_model_target("Book")]}
        edges = self.bridge.resolve("myapp/admin.py", [], target_files)
        assert edges == []

    def test_resolve_empty_targets(self):
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, {})
        assert edges == []

    def test_resolve_multiple_mechanisms(self):
        target_files = {
            "myapp/models.py": [_model_target("Book")]
        }
        source_symbols = [
            _class_symbol(
                "BookAdmin",
                qname="myapp.admin.BookAdmin",
                signature="@admin.register(Book)\nclass BookAdmin(admin.ModelAdmin)",
            ),
            _func_symbol(
                "on_book_save",
                qname="myapp.signals.on_book_save",
                signature="@receiver(post_save, sender=Book)\ndef on_book_save(sender, **kwargs)",
            ),
            _func_symbol(
                "process_book",
                qname="myapp.tasks.process_book",
                signature="@shared_task\ndef process_book(book_id)",
            ),
        ]
        edges = self.bridge.resolve("myapp/admin.py", source_symbols, target_files)
        mechanisms = {e["mechanism"] for e in edges}
        assert "admin_registers" in mechanisms
        assert "signal_handler" in mechanisms
        assert "celery_task" in mechanisms
        assert len(edges) >= 3
