"""Tests for Django endpoint pattern detection in cmd_endpoints.py.

Covers:
- include() detected as INCLUDE method
- ViewClass.as_view() detected with class name handler
- DRF router.register() CRUD endpoint synthesis
- No false positives on non-Django files
- Basic path() regression test
- Handler .as_view suffix cleanup
- Full URL path resolution via _resolve_django_includes
- Nested include() chain resolution
- include() expansion into child endpoints
"""

from __future__ import annotations

from pathlib import Path

from roam.commands.cmd_endpoints import (
    _join_url_paths,
    _resolve_django_includes,
    _scan_python,
)


class TestDjangoEndpointPatterns:
    def test_include_detected(self):
        source = (
            "from django.urls import path, include\n"
            "urlpatterns = [\n"
            "    path('api/', include('myapp.urls')),\n"
            "]\n"
        )
        endpoints = _scan_python(source, "/fake/urls.py", "urls.py")
        include_eps = [e for e in endpoints if e["method"] == "INCLUDE"]
        assert len(include_eps) == 1
        assert include_eps[0]["path"] == "/api/"
        assert include_eps[0]["handler"] == "myapp.urls"
        assert include_eps[0]["framework"] == "django"

    def test_as_view_detected(self):
        source = (
            "from django.urls import path\n"
            "from myapp.views import BookView\n"
            "urlpatterns = [\n"
            "    path('books/', BookView.as_view()),\n"
            "]\n"
        )
        endpoints = _scan_python(source, "/fake/urls.py", "urls.py")
        as_view_eps = [e for e in endpoints if e["handler"] == "BookView"]
        assert len(as_view_eps) >= 1
        assert as_view_eps[0]["method"] == "ANY"
        assert as_view_eps[0]["path"] == "/books/"
        assert as_view_eps[0]["framework"] == "django"

    def test_drf_router_detected(self):
        source = (
            "from rest_framework.routers import DefaultRouter\n"
            "from myapp.views import UserViewSet\n"
            "router = DefaultRouter()\n"
            "router.register(r'users', UserViewSet)\n"
        )
        endpoints = _scan_python(source, "/fake/urls.py", "urls.py")
        drf_eps = [e for e in endpoints if e["framework"] == "drf"]
        assert len(drf_eps) == 6
        methods = [e["method"] for e in drf_eps]
        assert "GET" in methods
        assert "POST" in methods
        assert "PUT" in methods
        assert "PATCH" in methods
        assert "DELETE" in methods
        assert all(e["handler"] == "UserViewSet" for e in drf_eps)

    def test_non_django_file_no_false_positives(self):
        source = (
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "@app.route('/hello')\n"
            "def hello():\n"
            "    return 'world'\n"
        )
        endpoints = _scan_python(source, "/fake/app.py", "app.py")
        django_eps = [e for e in endpoints if e["framework"] in ("django", "drf")]
        assert len(django_eps) == 0

    def test_basic_path_still_works(self):
        source = (
            "from django.urls import path\n"
            "from myapp import views\n"
            "urlpatterns = [\n"
            "    path('books/', views.book_list),\n"
            "]\n"
        )
        endpoints = _scan_python(source, "/fake/urls.py", "urls.py")
        path_eps = [e for e in endpoints if e["framework"] == "django" and e["path"] == "/books/"]
        assert len(path_eps) >= 1
        assert path_eps[0]["handler"] == "views.book_list"


class TestHandlerAsViewCleanup:
    """Test that .as_view suffix is never present in handler names."""

    def test_handler_no_as_view_suffix(self):
        """path('books/', BookView.as_view()) produces handler 'BookView', not 'BookView.as_view'."""
        source = (
            "from django.urls import path\n"
            "from myapp.views import BookView\n"
            "urlpatterns = [\n"
            "    path('books/', BookView.as_view()),\n"
            "]\n"
        )
        endpoints = _scan_python(source, "/fake/urls.py", "urls.py")
        for ep in endpoints:
            assert not ep["handler"].endswith(".as_view"), (
                f"Handler {ep['handler']!r} still has .as_view suffix"
            )
        # Verify the CBV was detected with the clean class name
        cbv_eps = [e for e in endpoints if e["handler"] == "BookView"]
        assert len(cbv_eps) >= 1

    def test_multiple_as_view_handlers_all_clean(self):
        """Multiple CBV patterns all produce clean handler names."""
        source = (
            "from django.urls import path\n"
            "from myapp.views import BookView, AuthorView\n"
            "urlpatterns = [\n"
            "    path('books/', BookView.as_view()),\n"
            "    path('authors/', AuthorView.as_view()),\n"
            "]\n"
        )
        endpoints = _scan_python(source, "/fake/urls.py", "urls.py")
        handlers = [e["handler"] for e in endpoints]
        assert "BookView" in handlers
        assert "AuthorView" in handlers
        assert all(".as_view" not in h for h in handlers)


class TestJoinUrlPaths:
    """Test the URL path joining helper."""

    def test_simple_join(self):
        assert _join_url_paths("/api/", "/books/") == "/api/books/"

    def test_no_double_slash(self):
        assert _join_url_paths("/api/", "/books/") == "/api/books/"
        assert _join_url_paths("/api", "books/") == "/api/books/"

    def test_ensures_leading_slash(self):
        assert _join_url_paths("api/", "books/") == "/api/books/"

    def test_empty_prefix(self):
        assert _join_url_paths("/", "/books/") == "/books/"

    def test_nested_paths(self):
        assert _join_url_paths("/api/v1/", "/users/") == "/api/v1/users/"


class TestResolveDjangoIncludes:
    """Test _resolve_django_includes for full path resolution."""

    def test_full_path_with_include(self):
        """Root include('myapp.urls') with prefix /api/ resolves child /books/ to /api/books/."""
        # Simulate endpoints from scanning two files:
        # Root urls.py: path('api/', include('myapp.urls'))
        # myapp/urls.py: path('books/', views.book_list)
        endpoints = [
            {
                "method": "INCLUDE",
                "path": "/api/",
                "handler": "myapp.urls",
                "file": "urls.py",
                "line": 3,
                "framework": "django",
            },
            {
                "method": "ANY",
                "path": "/books/",
                "handler": "views.book_list",
                "file": "myapp/urls.py",
                "line": 3,
                "framework": "django",
            },
        ]
        file_paths = ["urls.py", "myapp/urls.py"]
        result = _resolve_django_includes(endpoints, Path("/fake"), file_paths)

        # The original child endpoint with partial path should be present
        partial = [e for e in result if e["path"] == "/books/" and e["method"] == "ANY"]
        assert len(partial) == 1

        # An expanded endpoint with full path should also be present
        full = [e for e in result if e["path"] == "/api/books/" and e["method"] == "ANY"]
        assert len(full) == 1
        assert full[0]["handler"] == "views.book_list"

        # The INCLUDE entry should be kept as a group marker
        group_markers = [e for e in result if e.get("group") == "myapp.urls"]
        assert len(group_markers) == 1

    def test_nested_include_full_path(self):
        """Three-level include chain: root -> api -> myapp produces correct full paths."""
        endpoints = [
            # Root: path('v1/', include('api.urls'))
            {
                "method": "INCLUDE",
                "path": "/v1/",
                "handler": "api.urls",
                "file": "urls.py",
                "line": 3,
                "framework": "django",
            },
            # api/urls.py: path('resources/', include('myapp.urls'))
            {
                "method": "INCLUDE",
                "path": "/resources/",
                "handler": "myapp.urls",
                "file": "api/urls.py",
                "line": 3,
                "framework": "django",
            },
            # myapp/urls.py: path('books/', views.book_list)
            {
                "method": "ANY",
                "path": "/books/",
                "handler": "views.book_list",
                "file": "myapp/urls.py",
                "line": 3,
                "framework": "django",
            },
        ]
        file_paths = ["urls.py", "api/urls.py", "myapp/urls.py"]
        result = _resolve_django_includes(endpoints, Path("/fake"), file_paths)

        # Should have the fully resolved path: /v1/resources/books/
        full = [e for e in result if e["path"] == "/v1/resources/books/"]
        assert len(full) == 1
        assert full[0]["handler"] == "views.book_list"

    def test_include_expanded_to_children(self):
        """INCLUDE entries are expanded and child endpoints appear with full paths."""
        endpoints = [
            {
                "method": "INCLUDE",
                "path": "/api/",
                "handler": "myapp.urls",
                "file": "urls.py",
                "line": 3,
                "framework": "django",
            },
            {
                "method": "ANY",
                "path": "/books/",
                "handler": "views.book_list",
                "file": "myapp/urls.py",
                "line": 3,
                "framework": "django",
            },
            {
                "method": "ANY",
                "path": "/authors/",
                "handler": "views.author_list",
                "file": "myapp/urls.py",
                "line": 5,
                "framework": "django",
            },
        ]
        file_paths = ["urls.py", "myapp/urls.py"]
        result = _resolve_django_includes(endpoints, Path("/fake"), file_paths)

        # Both child endpoints should be expanded with full paths
        expanded_paths = [e["path"] for e in result if e["method"] == "ANY"]
        assert "/api/books/" in expanded_paths
        assert "/api/authors/" in expanded_paths

    def test_unresolvable_include_kept_as_group(self):
        """Include referencing a module not in file_paths is kept as a group marker."""
        endpoints = [
            {
                "method": "INCLUDE",
                "path": "/api/",
                "handler": "unknown.urls",
                "file": "urls.py",
                "line": 3,
                "framework": "django",
            },
        ]
        file_paths = ["urls.py"]
        result = _resolve_django_includes(endpoints, Path("/fake"), file_paths)

        # Should have the group marker but no expanded children
        group_markers = [e for e in result if e.get("group") == "unknown.urls"]
        assert len(group_markers) == 1
        non_include = [e for e in result if e["method"] != "INCLUDE"]
        assert len(non_include) == 0

    def test_depth_limit_prevents_infinite_loop(self):
        """Circular or deep includes stop at depth 5."""
        # Create a chain of 7 includes -- only first 5 should be expanded
        endpoints = []
        file_paths = []
        for i in range(7):
            file_paths.append(f"level{i}/urls.py")
            if i < 6:
                endpoints.append({
                    "method": "INCLUDE",
                    "path": f"/l{i}/",
                    "handler": f"level{i + 1}.urls",
                    "file": f"level{i}/urls.py",
                    "line": 3,
                    "framework": "django",
                })
        # Leaf endpoint at level 6
        endpoints.append({
            "method": "ANY",
            "path": "/leaf/",
            "handler": "views.leaf",
            "file": "level6/urls.py",
            "line": 3,
            "framework": "django",
        })

        result = _resolve_django_includes(endpoints, Path("/fake"), file_paths)

        # The leaf should NOT be expanded from level0 because depth >= 5
        # level0 -> level1 -> level2 -> level3 -> level4 -> level5 -> level6 (depth 6)
        fully_expanded = [e for e in result if "l0" in e["path"] and e["method"] == "ANY"]
        assert len(fully_expanded) == 0


def _dedup(endpoints: list[dict]) -> list[dict]:
    """Replicate the handler-based dedup logic from _collect_endpoints."""
    best: dict[tuple, dict] = {}
    for ep in endpoints:
        key = (ep["method"], ep["handler"], ep["file"], ep["line"])
        prev = best.get(key)
        if prev is None or len(ep["path"]) > len(prev["path"]):
            best[key] = ep
    return list(best.values())


class TestEndpointDedup:
    """Test the handler-based dedup logic used in _collect_endpoints."""

    def test_dedup_keeps_longest_path(self):
        """Same handler/file/line with different paths keeps only the longest."""
        endpoints = [
            {"method": "ANY", "path": "/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
            {"method": "ANY", "path": "/api/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
            {"method": "ANY", "path": "/v1/api/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
        ]
        result = _dedup(endpoints)
        assert len(result) == 1
        assert result[0]["path"] == "/v1/api/books/"

    def test_dedup_preserves_different_handlers(self):
        """Endpoints at different file:line with different handlers both survive."""
        endpoints = [
            {"method": "ANY", "path": "/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
            {"method": "ANY", "path": "/authors/", "handler": "views.author_list",
             "file": "myapp/urls.py", "line": 7, "framework": "django"},
        ]
        result = _dedup(endpoints)
        assert len(result) == 2
        handlers = {ep["handler"] for ep in result}
        assert handlers == {"views.book_list", "views.author_list"}

    def test_dedup_nested_include_single_result(self):
        """3-level include chain produces exactly 1 non-INCLUDE endpoint per handler."""
        # Simulate what _resolve_django_includes returns for a 3-level chain:
        # original /books/ + expanded /resources/books/ + expanded /v1/resources/books/
        endpoints = [
            {"method": "INCLUDE", "path": "/v1/", "handler": "api.urls",
             "file": "urls.py", "line": 3, "framework": "django"},
            {"method": "INCLUDE", "path": "/resources/", "handler": "myapp.urls",
             "file": "api/urls.py", "line": 3, "framework": "django"},
            {"method": "ANY", "path": "/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
            {"method": "ANY", "path": "/resources/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
            {"method": "ANY", "path": "/v1/resources/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
        ]
        result = _dedup(endpoints)
        non_include = [ep for ep in result if ep["method"] != "INCLUDE"]
        assert len(non_include) == 1
        assert non_include[0]["path"] == "/v1/resources/books/"

    def test_dedup_different_methods_same_handler_preserved(self):
        """Same handler at same file:line with different methods both survive."""
        endpoints = [
            {"method": "GET", "path": "/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
            {"method": "POST", "path": "/books/", "handler": "views.book_list",
             "file": "myapp/urls.py", "line": 5, "framework": "django"},
        ]
        result = _dedup(endpoints)
        assert len(result) == 2
        methods = {ep["method"] for ep in result}
        assert methods == {"GET", "POST"}
