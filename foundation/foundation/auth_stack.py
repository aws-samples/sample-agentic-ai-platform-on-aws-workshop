"""Auth: Cognito user pool, Hosted UI domain, the SPA app client and the (empty) users secret.

The three groups and the persona users are not part of this stack: `workshop apply 2` (module 2 of the learning path) creates
them and fills the users secret, so learners build the people side of identity themselves. The pool and the client are deployed
before the day because the platform API's JWT authorizer (Platform stack) needs them.
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

from . import config
from .common import publish


class AuthStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, ui_url: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=config.USER_POOL_NAME,
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            mfa=cognito.Mfa.OFF,
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_lowercase=True, require_uppercase=True, require_digits=True, require_symbols=True
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )
        domain = self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"{config.PREFIX}-{self.account}"),
        )
        self.auth_domain = domain.base_url()

        self.client = self.user_pool.add_client(
            "UiClient",
            user_pool_client_name=config.UI_CLIENT_NAME,
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),  # public client: PKCE
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[f"{ui_url}/", config.LOCAL_UI_URL],
                logout_urls=[f"{ui_url}/", config.LOCAL_UI_URL],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            id_token_validity=Duration.hours(8),
            access_token_validity=Duration.hours(8),
        )

        # The persona passwords: an empty secret that `workshop apply 2` fills as JSON (platform_sdk.auth reads it).
        users_secret = sm.CfnSecret(
            self,
            "UsersSecret",
            name=config.USERS_SECRET_NAME,
            description=(
                'Workshop personas, written by `workshop apply 2`: {"admin": {"username", "password", "email"}, '
                '"developer": {...}, "approver": {...}}'
            ),
        )
        users_secret.apply_removal_policy(RemovalPolicy.DESTROY)
        self.users_secret_arn = users_secret.attr_id

        publish(self, "auth/user-pool-id", self.user_pool.user_pool_id, "Cognito user pool id")
        publish(self, "auth/client-id", self.client.user_pool_client_id, "Cognito SPA app client id")
        publish(self, "auth/domain", self.auth_domain, "Cognito Hosted UI base URL")
        publish(self, "auth/users-secret-arn", users_secret.attr_id, "Secret with the persona usernames and passwords")
