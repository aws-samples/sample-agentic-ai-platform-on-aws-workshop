"""Platform API Lambda entry (build contract section 4).

HTTP API v2 proxy events go to `platform_api.router.dispatch` (OPTIONS -> empty 204, GET /health public, everything else behind the
Cognito JWT authorizer with the role table in the router). Events without a `requestContext` are EventBridge (Agent Registry) or
direct invocations: they are logged and acknowledged.
"""

from __future__ import annotations

import json
import logging

from platform_api import router
from platform_api.handlers import catalogue

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    if not isinstance(event, dict) or "requestContext" not in event:
        if isinstance(event, dict) and event.get("source") == "aws.agent-registry":
            return catalogue.sync_event(event)
        logger.info("non-HTTP event: %s", json.dumps(event, default=str)[:4000])
        return {"ok": True}
    return router.dispatch(event)
