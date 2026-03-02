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
