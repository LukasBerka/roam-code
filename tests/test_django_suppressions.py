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

from roam.commands.cmd_dead import _is_django_entry_path


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
