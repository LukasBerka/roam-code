"""Tests for Django endpoint pattern detection in cmd_endpoints.py.

Covers:
- include() detected as INCLUDE method
- ViewClass.as_view() detected with class name handler
- DRF router.register() CRUD endpoint synthesis
- No false positives on non-Django files
- Basic path() regression test
"""

from __future__ import annotations

from roam.commands.cmd_endpoints import _scan_python


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
