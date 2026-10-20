"""GET /governance, POST /governance/gateways/{gatewayId}/mode, GET /governance/model-gateway/credentials, GET /governance/project-options."""

from __future__ import annotations

import logging

from .. import validation
from ..services import governance as governance_service

logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}


def get(request) -> dict:
    return governance_service.summary()


def set_gateway_mode(request) -> dict:
    body = request.require_body()
    mode = validation.choice(body, "mode", governance_service.GATEWAY_MODES)
    return governance_service.set_gateway_mode(request.params["gatewayId"], mode)


def model_gateway_credentials(request) -> tuple[int, dict, dict]:
    """Admin only (route table): the LiteLLM admin UI sign-in, shown masked in the Governance screen.

    Logged (without the secret) so CloudWatch shows which admin read it; answered with Cache-Control: no-store."""
    logger.info("%s read the model gateway credentials", request.principal.username if request.principal else "?")
    return 200, governance_service.model_gateway_credentials(), NO_STORE


def project_options(request) -> dict:
    """Admin only: pick lists for the project form (tool Lambdas, knowledge bases)."""
    return governance_service.project_options()
