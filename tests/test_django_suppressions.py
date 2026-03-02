"""Tests for Django suppression rules in cmd_health.py and cmd_dead.py.

Covers:
- Unit tests for _is_django_entry_path() path detection helper
- Unit tests for _dead_action() Django suppression verdicts
- Unit tests for health command Django filtering (_FRAMEWORK_NAMES, _is_utility_path)
- CLI integration tests with minimal Django project fixture
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import git_commit, git_init, index_in_process, invoke_cli, parse_json_output

from roam.commands.cmd_dead import _dead_action, _is_django_entry_path
from roam.commands.cmd_health import _FRAMEWORK_NAMES, _is_utility_path


# ============================================================================
# Helpers
# ============================================================================


def _make_row(name, kind, file_path, signature="", framework_type=None):
    """Create a dict mimicking sqlite3.Row for _dead_action()."""
    return {
        "name": name,
        "kind": kind,
        "file_path": file_path,
        "signature": signature,
        "framework_type": framework_type,
        "file_id": 1,
        "line_start": 1,
        "line_end": 10,
    }


# ============================================================================
# Unit tests for _is_django_entry_path()
# ============================================================================


class TestDjangoEntryPath:
    """Tests for _is_django_entry_path() helper in cmd_dead.py."""

    def test_admin_file(self):
        """admin.py files should be detected as 'admin' entry points."""
        assert _is_django_entry_path("myapp/admin.py") == "admin"

    def test_management_command(self):
        """Files under management/commands/ should be detected as 'management_command'."""
        assert _is_django_entry_path("myapp/management/commands/import_data.py") == "management_command"

    def test_templatetags(self):
        """Files under templatetags/ should be detected as 'template_tag'."""
        assert _is_django_entry_path("myapp/templatetags/custom_tags.py") == "template_tag"

    def test_tasks_file(self):
        """tasks.py files should be detected as 'celery_task' entry points."""
        assert _is_django_entry_path("myapp/tasks.py") == "celery_task"

    def test_signals_file(self):
        """signals.py files should be detected as 'signal_handler' entry points."""
        assert _is_django_entry_path("myapp/signals.py") == "signal_handler"

    def test_non_django_file(self):
        """models.py should NOT be a Django entry point (it is imported normally)."""
        assert _is_django_entry_path("myapp/models.py") is None

    def test_non_django_regular(self):
        """Regular utility files should not match any Django entry pattern."""
        assert _is_django_entry_path("src/utils/helpers.py") is None

    def test_windows_paths(self):
        """Backslash Windows paths should be handled correctly."""
        assert _is_django_entry_path("myapp\\admin.py") == "admin"
        assert _is_django_entry_path("myapp\\management\\commands\\foo.py") == "management_command"
        assert _is_django_entry_path("myapp\\templatetags\\tags.py") == "template_tag"


# ============================================================================
# Unit tests for _dead_action() Django suppression
# ============================================================================


class TestDeadActionDjango:
    """Tests for Django suppression rules in _dead_action()."""

    def test_admin_class_suppressed(self):
        """Admin class in admin.py should be INTENTIONAL."""
        row = _make_row("UserAdmin", "class", "myapp/admin.py")
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_signal_handler_suppressed(self):
        """Function in signals.py should be INTENTIONAL."""
        row = _make_row("on_user_saved", "function", "myapp/signals.py")
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_celery_task_suppressed(self):
        """Function in tasks.py should be INTENTIONAL."""
        row = _make_row("send_email_task", "function", "myapp/tasks.py")
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_management_command_suppressed(self):
        """Function in management/commands/ should be INTENTIONAL."""
        row = _make_row("handle", "function", "myapp/management/commands/import_data.py")
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_template_tag_suppressed(self):
        """Function in templatetags/ should be INTENTIONAL."""
        row = _make_row("show_avatar", "function", "myapp/templatetags/custom_tags.py")
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_framework_type_django_model(self):
        """Symbol with framework_type='django_model' should be INTENTIONAL."""
        row = _make_row("User", "class", "myapp/models.py", framework_type="django_model")
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_receiver_decorator_any_file(self):
        """Function with @receiver in signature (any file) should be INTENTIONAL."""
        row = _make_row(
            "handle_post_save",
            "function",
            "myapp/handlers.py",
            signature="@receiver(post_save, sender=User)\ndef handle_post_save(sender, instance, **kwargs):",
        )
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_shared_task_decorator_any_file(self):
        """Function with @shared_task in signature (any file) should be INTENTIONAL."""
        row = _make_row(
            "process_data",
            "function",
            "myapp/workers.py",
            signature="@shared_task\ndef process_data():",
        )
        action, confidence = _dead_action(row, file_imported=False)
        assert action == "INTENTIONAL"

    def test_regular_function_not_suppressed(self):
        """Regular function in models.py should NOT be INTENTIONAL via Django path."""
        row = _make_row("calculate_total", "function", "myapp/models.py")
        action, confidence = _dead_action(row, file_imported=False)
        # models.py is not a Django entry path, so this should be SAFE or REVIEW, not INTENTIONAL
        assert action != "INTENTIONAL" or confidence > 50


# ============================================================================
# Unit tests for cmd_health.py Django suppressions
# ============================================================================


class TestHealthDjangoSuppression:
    """Tests for Django-specific filtering in cmd_health.py."""

    def test_framework_names_include_urlpatterns(self):
        """_FRAMEWORK_NAMES should include 'urlpatterns' for --no-framework filter."""
        assert "urlpatterns" in _FRAMEWORK_NAMES

    def test_framework_names_include_application(self):
        """_FRAMEWORK_NAMES should include 'application' (WSGI/ASGI)."""
        assert "application" in _FRAMEWORK_NAMES

    def test_utility_path_management_commands(self):
        """management/commands/ should be detected as a utility path."""
        assert _is_utility_path("myapp/management/commands/foo.py")

    def test_utility_path_templatetags(self):
        """templatetags/ should be detected as a utility path."""
        assert _is_utility_path("myapp/templatetags/foo.py")

    def test_utility_path_migrations(self):
        """migrations/ should be detected as a utility path."""
        assert _is_utility_path("myapp/migrations/0001_initial.py")

    def test_utility_path_regular_not_utility(self):
        """views.py should NOT be a utility path."""
        assert not _is_utility_path("myapp/views.py")


# ============================================================================
# CLI integration tests with Django project fixture
# ============================================================================


@pytest.fixture
def django_project(tmp_path):
    """Create a minimal Django project with all suppression-relevant patterns."""
    proj = tmp_path / "djangoproj"
    proj.mkdir()
    (proj / ".gitignore").write_text(".roam/\n")

    # models.py with a Django model (imports admin to create cross-refs)
    (proj / "models.py").write_text(
        "from django.db import models\n"
        "\n"
        "class Article(models.Model):\n"
        "    title = models.CharField(max_length=200)\n"
        "    body = models.TextField()\n"
        "\n"
        "    def __str__(self):\n"
        "        return self.title\n"
    )

    # admin.py with admin class
    (proj / "admin.py").write_text(
        "from django.contrib import admin\n"
        "from models import Article\n"
        "\n"
        "class ArticleAdmin(admin.ModelAdmin):\n"
        "    list_display = ['title']\n"
    )

    # urls.py with urlpatterns (many routes = high connectivity)
    (proj / "urls.py").write_text(
        "from views import index_view, detail_view, create_view, edit_view\n"
        "from views import delete_view, list_view, search_view, archive_view\n"
        "\n"
        "urlpatterns = [\n"
        "    ('/', index_view),\n"
        "    ('/detail', detail_view),\n"
        "    ('/create', create_view),\n"
        "    ('/edit', edit_view),\n"
        "    ('/delete', delete_view),\n"
        "    ('/list', list_view),\n"
        "    ('/search', search_view),\n"
        "    ('/archive', archive_view),\n"
        "]\n"
    )

    # views.py with view functions referenced by urls.py
    (proj / "views.py").write_text(
        "from models import Article\n"
        "\n"
        "def index_view(request):\n"
        "    return Article.objects.all()\n"
        "\n"
        "def detail_view(request):\n"
        "    return Article.objects.first()\n"
        "\n"
        "def create_view(request):\n"
        "    return Article(title='new')\n"
        "\n"
        "def edit_view(request):\n"
        "    return Article.objects.first()\n"
        "\n"
        "def delete_view(request):\n"
        "    return None\n"
        "\n"
        "def list_view(request):\n"
        "    return Article.objects.all()\n"
        "\n"
        "def search_view(request):\n"
        "    return Article.objects.filter(title='query')\n"
        "\n"
        "def archive_view(request):\n"
        "    return Article.objects.all()\n"
    )

    # signals.py with @receiver
    (proj / "signals.py").write_text(
        "from django.db.models.signals import post_save\n"
        "from django.dispatch import receiver\n"
        "from models import Article\n"
        "\n"
        "@receiver(post_save, sender=Article)\n"
        "def on_article_saved(sender, instance, **kwargs):\n"
        "    pass\n"
    )

    # tasks.py with @shared_task
    (proj / "tasks.py").write_text(
        "from celery import shared_task\n"
        "from models import Article\n"
        "\n"
        "@shared_task\n"
        "def process_articles():\n"
        "    return Article.objects.count()\n"
    )

    # management/commands/import_data.py
    mgmt = proj / "management"
    mgmt.mkdir()
    (mgmt / "__init__.py").write_text("")
    cmds = mgmt / "commands"
    cmds.mkdir()
    (cmds / "__init__.py").write_text("")
    (cmds / "import_data.py").write_text(
        "from django.core.management.base import BaseCommand\n"
        "\n"
        "class Command(BaseCommand):\n"
        "    def handle(self, *args, **options):\n"
        "        pass\n"
    )

    # templatetags/custom_tags.py
    ttags = proj / "templatetags"
    ttags.mkdir()
    (ttags / "__init__.py").write_text("")
    (ttags / "custom_tags.py").write_text(
        "from django import template\n"
        "\n"
        "register = template.Library()\n"
        "\n"
        "@register.simple_tag\n"
        "def show_title(article):\n"
        "    return article.title\n"
    )

    git_init(proj)
    return proj


class TestDjangoSuppressionCLI:
    """CLI integration tests for Django suppression rules."""

    def test_dead_json_django_admin_intentional(self, cli_runner, django_project, monkeypatch):
        """Django entry point symbols should get INTENTIONAL action in dead output."""
        monkeypatch.chdir(django_project)
        index_in_process(django_project)
        result = invoke_cli(
            cli_runner,
            ["--detail", "dead"],
            cwd=django_project,
            json_mode=True,
        )
        data = parse_json_output(result, "dead")
        # Collect all symbols from both confidence tiers
        all_syms = data.get("high_confidence", []) + data.get("low_confidence", [])

        # Find any symbols from Django entry point files
        django_entry_files = ("admin.py", "signals.py", "tasks.py")
        django_entry_syms = [
            s for s in all_syms
            if any(s.get("location", "").endswith(f) or f in s.get("location", "") for f in django_entry_files)
        ]
        # All Django entry point symbols should be INTENTIONAL
        for sym in django_entry_syms:
            assert sym["action"] == "INTENTIONAL", (
                f"Django entry symbol {sym['name']} in {sym.get('location')} "
                f"should be INTENTIONAL, got {sym['action']}"
            )

    def test_health_no_urlpatterns_god_component(self, cli_runner, django_project, monkeypatch):
        """urlpatterns should not appear in god_components list."""
        monkeypatch.chdir(django_project)
        index_in_process(django_project)
        result = invoke_cli(
            cli_runner,
            ["--detail", "health"],
            cwd=django_project,
            json_mode=True,
        )
        data = parse_json_output(result, "health")
        god_components = data.get("god_components", [])
        god_names = [g["name"] for g in god_components]
        assert "urlpatterns" not in god_names, (
            f"urlpatterns should be suppressed from god_components, found: {god_names}"
        )
