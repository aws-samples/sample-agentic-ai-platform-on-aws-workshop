"""Web: S3 + CloudFront hosting for the platform UI (frontend/dist when built, otherwise the placeholder page).

`config.json` is written into this bucket by the Platform stack (it needs the API URL and the Cognito ids, and the Auth
stack needs this stack's URL for the sign-in callback, so Web deploys before Auth and Platform; see the notes).
"""

from __future__ import annotations

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from . import config
from .common import log_group, publish


class WebStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.bucket = s3.Bucket(
            self,
            "UiBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        spa_fallback = [
            cloudfront.ErrorResponse(
                http_status=code, response_http_status=200, response_page_path="/index.html", ttl=Duration.seconds(0)
            )
            for code in (403, 404)
        ]
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"{config.PREFIX} UI",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                # Managed security headers (HSTS, X-Content-Type-Options nosniff, X-Frame-Options SAMEORIGIN, Referrer-Policy
                # strict-origin-when-cross-origin, X-XSS-Protection): the SPA keeps tokens in the browser, so framing and
                # MIME sniffing are refused at the CDN (review finding; verified with curl -sI after the deploy).
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
            ),
            default_root_object="index.html",
            error_responses=spa_fallback,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
        )
        self.url = f"https://{self.distribution.distribution_domain_name}"

        source_dir = config.UI_DIST_DIR if (config.UI_DIST_DIR / "index.html").exists() else config.PLACEHOLDER_DIR
        self.source_dir = source_dir
        s3deploy.BucketDeployment(
            self,
            "UiContent",
            destination_bucket=self.bucket,
            sources=[s3deploy.Source.asset(str(source_dir))],
            prune=False,  # never delete config.json, which the Platform stack writes
            distribution=self.distribution,
            distribution_paths=["/*"],
            log_group=log_group(self, "UiContentLogs", f"/aws/lambda/{config.PREFIX}-ui-content"),
        )

        publish(self, "ui/url", self.url, "Platform UI (CloudFront)")
