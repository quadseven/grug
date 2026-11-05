"""Shared helpers for the every-route-requires-auth tests (#1636)."""

from __future__ import annotations

import re

from fastapi.routing import APIRoute


def routes_of(router) -> list[APIRoute]:
    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    assert routes, "router has no routes"
    return routes


def top_level_deps(route: APIRoute) -> list:
    return [d.call for d in route.dependant.dependencies]


def concrete_path(route: APIRoute) -> str:
    """Fill every `{param}` with a harmless value so the request reaches
    the auth dependency instead of failing path validation."""
    return re.sub(r"\{[^}]+\}", "1", route.path)


def requests_for(route: APIRoute):
    for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
        yield method, concrete_path(route)
