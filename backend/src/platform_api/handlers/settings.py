"""GET /settings (any) and PUT /settings (admin, merges)."""

from __future__ import annotations

from .. import settings as settings_store


def get(request) -> dict:
    return settings_store.load()


def put(request) -> dict:
    return settings_store.update(request.require_body(), by=request.principal.username)
