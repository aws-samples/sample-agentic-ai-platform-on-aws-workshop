"""GET /health (public)."""

from __future__ import annotations

from .. import __version__, db


def get(request) -> dict:
    return {"status": "ok", "version": __version__, "time": db.now_iso()}
