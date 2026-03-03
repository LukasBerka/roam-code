"""Integration tests for Django URL full path resolution and DRF view detection.

End-to-end tests that:
1. Create a multi-file Django project with include() chains
2. Index it (which runs django_post.resolve_all_django)
3. Run cmd_endpoints and verify full paths, clean handlers, DRF tags
"""

from __future__ import annotations

import os

from tests.conftest import invoke_cli, parse_json_output


# ===========================================================================
# 1. DRF View Detection Integration
# ===========================================================================


class TestDRFViewDetectionIntegration:
    """Test that DRF views are transitively tagged in the DB after indexing."""

    def test_transitive_drf_view_tagged(self, project_factory):
        """Index a project with APIView subclass and verify drf_view tag."""
        proj = project_factory({
            "myapp/__init__.py": "",
            "myapp/views.py": (
                "class APIView:\n"
                "    pass\n"
                "\n"
                "class BookView(APIView):\n"
                "    def get(self, request):\n"
                "        return []\n"
            ),
        })
        from roam.db.connection import open_db
        old_cwd = os.getcwd()
        try:
            os.chdir(str(proj))
            with open_db(readonly=True) as conn:
                row = conn.execute(
                    "SELECT framework_type FROM symbols "
                    "WHERE name = 'BookView' AND kind = 'class'"
                ).fetchone()
                assert row is not None
                assert row["framework_type"] == "drf_view"
        finally:
            os.chdir(old_cwd)

    def test_non_drf_view_not_tagged(self, project_factory):
        """A view not inheriting from DRF bases should not be tagged."""
        proj = project_factory({
            "myapp/__init__.py": "",
            "myapp/views.py": (
                "class MyBaseView:\n"
                "    pass\n"
                "\n"
                "class BookView(MyBaseView):\n"
                "    pass\n"
            ),
        })
        from roam.db.connection import open_db
        old_cwd = os.getcwd()
        try:
            os.chdir(str(proj))
            with open_db(readonly=True) as conn:
                row = conn.execute(
                    "SELECT framework_type FROM symbols "
                    "WHERE name = 'BookView' AND kind = 'class'"
                ).fetchone()
                assert row is not None
                assert row["framework_type"] is None or row["framework_type"] != "drf_view"
        finally:
            os.chdir(old_cwd)


# ===========================================================================
# 2. URL Full Path Resolution Integration
# ===========================================================================


class TestURLFullPathIntegration:
    """Test that include() chains produce full URL paths in endpoint output."""

    def test_include_produces_full_paths(self, project_factory, cli_runner):
        """Include chain resolves to full URL paths in endpoint output."""
        proj = project_factory({
            "myapp/__init__.py": "",
            "myapp/views.py": (
                "def book_list(request):\n"
                "    return []\n"
            ),
            "myapp/urls.py": (
                "from django.urls import path\n"
                "from myapp import views\n"
                "urlpatterns = [\n"
                "    path('books/', views.book_list),\n"
                "]\n"
            ),
            "urls.py": (
                "from django.urls import path, include\n"
                "urlpatterns = [\n"
                "    path('api/v1/', include('myapp.urls')),\n"
                "]\n"
            ),
        })
        result = invoke_cli(cli_runner, ["endpoints", "--framework", "django"], cwd=proj, json_mode=True)
        data = parse_json_output(result, "endpoints")
        endpoints = data.get("endpoints", [])

        full_paths = [e["path"] for e in endpoints if e["method"] != "INCLUDE"]
        assert any("/api/v1/books/" in p for p in full_paths), (
            f"Expected full path /api/v1/books/ in {full_paths}"
        )

    def test_nested_include_full_paths(self, project_factory, cli_runner):
        """Nested include() chains produce correctly prefixed paths."""
        proj = project_factory({
            "books/__init__.py": "",
            "books/views.py": (
                "def detail(request, pk):\n"
                "    return {}\n"
            ),
            "books/urls.py": (
                "from django.urls import path\n"
                "from books import views\n"
                "urlpatterns = [\n"
                "    path('<int:pk>/', views.detail),\n"
                "]\n"
            ),
            "api/__init__.py": "",
            "api/urls.py": (
                "from django.urls import path, include\n"
                "urlpatterns = [\n"
                "    path('books/', include('books.urls')),\n"
                "]\n"
            ),
            "urls.py": (
                "from django.urls import path, include\n"
                "urlpatterns = [\n"
                "    path('api/v1/', include('api.urls')),\n"
                "]\n"
            ),
        })
        result = invoke_cli(cli_runner, ["endpoints", "--framework", "django"], cwd=proj, json_mode=True)
        data = parse_json_output(result, "endpoints")
        endpoints = data.get("endpoints", [])
        full_paths = [e["path"] for e in endpoints if e["method"] != "INCLUDE"]
        assert any("/api/v1/books/" in p and "<int:pk>" in p for p in full_paths), (
            f"Expected nested full path in {full_paths}"
        )


# ===========================================================================
# 3. Handler Cleanup Integration
# ===========================================================================


class TestHandlerCleanupIntegration:
    """Test that handler names are clean in real endpoint output."""

    def test_as_view_stripped_in_output(self, project_factory, cli_runner):
        """Handler names should not contain .as_view suffix."""
        proj = project_factory({
            "myapp/__init__.py": "",
            "myapp/views.py": (
                "class BookView:\n"
                "    pass\n"
            ),
            "urls.py": (
                "from django.urls import path\n"
                "from myapp.views import BookView\n"
                "urlpatterns = [\n"
                "    path('books/', BookView.as_view()),\n"
                "]\n"
            ),
        })
        result = invoke_cli(cli_runner, ["endpoints", "--framework", "django"], cwd=proj, json_mode=True)
        data = parse_json_output(result, "endpoints")
        endpoints = data.get("endpoints", [])
        handlers = [e["handler"] for e in endpoints]
        for h in handlers:
            assert not h.endswith(".as_view"), f"Handler '{h}' still has .as_view suffix"
        assert any("BookView" in h for h in handlers)


# ===========================================================================
# 4. DRF Framework Tag in Endpoints Output
# ===========================================================================


class TestDRFFrameworkTagIntegration:
    """Test that DRF-tagged views show framework='drf' in endpoint output."""

    def test_drf_view_shows_drf_framework(self, project_factory, cli_runner):
        """Views tagged as drf_view should show framework='drf' in endpoints."""
        proj = project_factory({
            "myapp/__init__.py": "",
            "myapp/views.py": (
                "class APIView:\n"
                "    pass\n"
                "\n"
                "class BookListView(APIView):\n"
                "    def get(self, request):\n"
                "        return []\n"
            ),
            "urls.py": (
                "from django.urls import path\n"
                "from myapp.views import BookListView\n"
                "urlpatterns = [\n"
                "    path('books/', BookListView.as_view()),\n"
                "]\n"
            ),
        })
        result = invoke_cli(cli_runner, ["endpoints"], cwd=proj, json_mode=True)
        data = parse_json_output(result, "endpoints")
        endpoints = data.get("endpoints", [])
        book_eps = [e for e in endpoints if "BookListView" in e.get("handler", "")]
        assert any(e["framework"] == "drf" for e in book_eps), (
            f"Expected framework='drf' for BookListView endpoints, got: {book_eps}"
        )
