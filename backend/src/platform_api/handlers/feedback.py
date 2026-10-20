"""POST /feedback and GET /feedback?projectId= (thumbs up / down from the playground; contract section 3)."""

from __future__ import annotations

from .. import db, validation
from .projects import load_project

RATINGS = ("up", "down")
MAX_ROWS = 200


def create(request):
    body = request.require_body()
    project_id = validation.string(body, "projectId")
    load_project(project_id)
    env = validation.env(validation.string(body, "env"))
    session_id = validation.string(body, "sessionId", max_length=256)
    rating = validation.choice(body, "rating", RATINGS)
    comment = validation.string(body, "comment", required=False, default="", max_length=4000) or ""
    created_at, feedback_id = db.now_iso(), db.new_id()
    item = {
        **db.feedback_key(project_id, created_at, session_id, feedback_id),
        **db.feedback_gsi(created_at, feedback_id),
        "feedbackId": feedback_id,
        "projectId": project_id,
        "env": env,
        "sessionId": session_id,
        "rating": rating,
        "comment": comment,
        "createdBy": request.principal.username,
        "createdAt": created_at,
    }
    db.put(item)
    return 201, db.strip_keys(item)


def list_feedback(request) -> list:
    """Newest first; all projects when projectId is omitted."""
    project_id = (request.query.get("projectId") or "").strip()
    if project_id:
        load_project(project_id)
        items = list(reversed(db.query(f"FEEDBACK#{project_id}")))
    else:
        items = db.query_gsi1("FEEDBACK", descending=True)
    return [db.strip_keys(item) for item in items[:MAX_ROWS]]
