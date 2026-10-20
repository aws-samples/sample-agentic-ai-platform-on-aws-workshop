"""GET /me: the caller as the JWT authorizer verified it. POST /me/switch: the tokens of another persona (workshop shortcut)."""

from __future__ import annotations

import logging

from platform_sdk import auth as sdk_auth

from .. import errors, validation
from ..services import clients

logger = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "no-store"}


def get(request) -> dict:
    return request.principal.to_dict()


def switch(request) -> tuple[int, dict, dict]:
    """Any signed-in user becomes another persona without typing its password: the "Switch persona" menu of the UI.

    A workshop convenience, never a production pattern: the API signs in as the persona with USER_PASSWORD_AUTH on the SPA app
    client (the password comes from the users secret, the path `platform_sdk.auth.login` and `workshop apply 2` already use) and
    hands the tokens to the browser, which stores them where oidc-client-ts keeps the signed-in user and reloads. Stated on
    purpose: with this route a developer or approver token is as powerful as an admin token. The switch is logged (caller ->
    persona, never the tokens) and answered with Cache-Control: no-store. 400 for an unknown persona; a refused sign-in, an
    empty users secret (AuthError) or a hand-edited secret that is not the expected JSON is 502 SIGN_IN_FAILED.
    """
    persona = validation.choice(request.require_body(), "persona", sdk_auth.PERSONAS)
    try:
        tokens = sdk_auth.login(
            persona, cfg=clients.cfg(), cognito_client=clients.cognito_idp(), secrets_client=clients.secrets_manager()
        )
    except (
        ValueError,
        KeyError,
    ) as exc:  # the users secret is not the expected JSON (hand-edited): same remedy as an empty one
        raise errors.upstream(
            f"the users secret {clients.cfg().users_secret_arn} is not the expected JSON ({type(exc).__name__}: {exc}); "
            "run `workshop apply 2` to rewrite it",
            code="SIGN_IN_FAILED",
        ) from exc
    logger.info("%s switched to %s", request.principal.username if request.principal else "?", persona)
    body = {
        "persona": persona,
        "username": tokens.username,
        "idToken": tokens.id_token,
        "accessToken": tokens.access_token,
        "refreshToken": tokens.refresh_token,
        "expiresIn": tokens.expires_in,
        "claims": tokens.claims,
    }
    return 200, body, NO_STORE
