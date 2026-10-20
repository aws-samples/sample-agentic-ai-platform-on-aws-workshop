"""Shared services: the baseline Bedrock guardrail, CloudWatch Transaction Search and the AgentCore evaluation role."""

from __future__ import annotations

import json

from aws_cdk import Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import custom_resources as cr
from constructs import Construct

from . import config
from .common import log_group, publish


class SharedServicesStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # (a) Baseline guardrail bound to every approved model alias in the model gateway (design v2 section 4)
        guardrail = bedrock.CfnGuardrail(
            self,
            "HrBaseline",
            name=config.GUARDRAIL_NAME,
            description="AnyCompany HR baseline: no legal advice, no SSNs or card numbers, prompt attacks blocked",
            blocked_input_messaging="Sorry, I can't help with that topic. Please contact HR directly.",
            blocked_outputs_messaging="Sorry, I can't share that information.",
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="LegalAdvice",
                        type="DENY",
                        definition=(
                            "Requests for legal advice, legal opinions, or how to sue or litigate against the company "
                            "or colleagues."
                        ),
                        examples=[
                            "Can I sue my manager for this?",
                            "What legal action can I take against HR?",
                            "Help me build a lawsuit against my employer.",
                        ],
                    )
                ]
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="US_SOCIAL_SECURITY_NUMBER", action="BLOCK"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="CREDIT_DEBIT_CARD_NUMBER", action="BLOCK"),
                ]
            ),
            # Prompt-attack filter on the input only (output strength must be NONE for this filter type); promised to
            # attendees in the guide and relied on by risk register row R-03.
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"
                    )
                ]
            ),
        )
        # A guardrail change only reaches the model gateway through a new numbered version: CloudFormation creates a
        # GuardrailVersion once and never re-snapshots it, so bump the description whenever the guardrail above changes.
        version = bedrock.CfnGuardrailVersion(
            self,
            "HrBaselineVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=config.GUARDRAIL_VERSION_LABEL,
        )
        self.guardrail_id = guardrail.attr_guardrail_id
        self.guardrail_arn = guardrail.attr_guardrail_arn
        self.guardrail_version = version.attr_version
        publish(self, "guardrail/id", self.guardrail_id, "Baseline guardrail id")
        publish(self, "guardrail/arn", self.guardrail_arn, "Baseline guardrail ARN")
        publish(self, "guardrail/version", self.guardrail_version, "Baseline guardrail version")

        # (b) CloudWatch Transaction Search (account-level; AgentCore observability needs it). Three calls in order,
        # on create and update only; nothing on delete so the setting survives `cdk destroy`.
        self._enable_transaction_search()

        # (c) Execution role for AgentCore Evaluations (online evaluation configs created on promotion, module 6).
        # Trust conditions and permissions follow the AgentCore Evaluations prerequisites page (read 2026-10-08): the
        # service assumes the role only for this account's evaluators and online evaluation configs.
        logs_arn = f"arn:aws:logs:{self.region}:{self.account}:log-group:"
        eval_role = iam.Role(
            self,
            "EvaluationRole",
            role_name=config.EVALUATION_ROLE_NAME,
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account, "aws:ResourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": [
                            self.format_arn(service="bedrock-agentcore", resource="evaluator/*"),
                            self.format_arn(service="bedrock-agentcore", resource="online-evaluation-config/*"),
                        ]
                    },
                },
            ),
            description="AgentCore Evaluations: read harness logs, write results and metrics, call judge models",
            inline_policies={
                "evaluation": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "logs:DescribeLogGroups",
                                "logs:DescribeLogStreams",
                                "logs:GetLogEvents",
                                "logs:FilterLogEvents",
                                "logs:StartQuery",
                                "logs:GetQueryResults",
                                "logs:StopQuery",
                            ],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                            resources=[f"{logs_arn}/aws/bedrock-agentcore/evaluations/*"],
                        ),
                        # "Configure log indexing for trace analysis" (documented requirement; the spike role had logs:*)
                        iam.PolicyStatement(
                            actions=["logs:DescribeIndexPolicies", "logs:PutIndexPolicy"],
                            resources=[f"{logs_arn}aws/spans", f"{logs_arn}aws/spans:*"],
                        ),
                        iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"]),
                        iam.PolicyStatement(
                            actions=["bedrock:InvokeModel*"],
                            resources=[
                                f"arn:aws:bedrock:{self.region}::foundation-model/*",
                                self.format_arn(service="bedrock", resource="inference-profile/*"),
                            ],
                        ),
                        # Kept from the spike role (verified working there); the documented minimal policy has no
                        # bedrock-agentcore actions, so this is a safety margin, not a requirement.
                        iam.PolicyStatement(actions=["bedrock-agentcore:*"], resources=["*"]),
                    ]
                )
            },
        )
        self.evaluation_role = eval_role
        publish(self, "roles/evaluation-role-arn", eval_role.role_arn, "AgentCore Evaluations execution role")

    def _enable_transaction_search(self) -> None:
        region, account = self.region, self.account
        sdk_logs = log_group(self, "SdkCallLogs", f"/aws/lambda/{config.PREFIX}-shared-services-sdk-calls")
        policy_document = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "TransactionSearchXRayAccess",
                        "Effect": "Allow",
                        "Principal": {"Service": "xray.amazonaws.com"},
                        "Action": "logs:PutLogEvents",
                        "Resource": [
                            f"arn:aws:logs:{region}:{account}:log-group:aws/spans:*",
                            f"arn:aws:logs:{region}:{account}:log-group:/aws/application-signals/data:*",
                        ],
                        "Condition": {
                            "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{region}:{account}:*"},
                            "StringEquals": {"aws:SourceAccount": account},
                        },
                    }
                ],
            }
        )
        logs_policy_call = cr.AwsSdkCall(
            service="CloudWatchLogs",
            action="PutResourcePolicy",
            parameters={"policyName": config.TRANSACTION_SEARCH_POLICY_NAME, "policyDocument": policy_document},
            physical_resource_id=cr.PhysicalResourceId.of(config.TRANSACTION_SEARCH_POLICY_NAME),
        )
        step1 = cr.AwsCustomResource(
            self,
            "TransactionSearchLogsPolicy",
            resource_type="Custom::TransactionSearchLogsPolicy",
            on_create=logs_policy_call,
            on_update=logs_policy_call,
            log_group=sdk_logs,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["logs:PutResourcePolicy", "logs:DescribeResourcePolicies"], resources=["*"]
                    )
                ]
            ),
        )
        destination_call = cr.AwsSdkCall(
            service="XRay",
            action="UpdateTraceSegmentDestination",
            parameters={"Destination": "CloudWatchLogs"},
            physical_resource_id=cr.PhysicalResourceId.of("xray-trace-segment-destination"),
            # Already enabled (or mid-transition) accounts answer InvalidRequestException ("The destination is already set
            # to CloudWatchLogs" / "Updates are not allowed while the current status is PENDING"): treat as done.
            ignore_error_codes_matching="InvalidRequestException",
        )
        step2 = cr.AwsCustomResource(
            self,
            "TransactionSearchDestination",
            resource_type="Custom::TransactionSearchDestination",
            on_create=destination_call,
            on_update=destination_call,
            log_group=sdk_logs,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=[
                            "xray:UpdateTraceSegmentDestination",
                            "xray:GetTraceSegmentDestination",
                            "xray:UpdateIndexingRule",
                            "xray:GetIndexingRules",
                            # UpdateTraceSegmentDestination creates the aws/spans log group (retention, default stream)
                            # on the caller's behalf ...
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:DescribeLogStreams",
                            "logs:PutRetentionPolicy",
                            "logs:DescribeLogGroups",
                            # ... and starts Application Signals discovery (service-linked role + CloudTrail channel).
                            # Set verified by assuming a test role with exactly these permissions (notes, 2026-10-08).
                            "application-signals:StartDiscovery",
                        ],
                        resources=["*"],
                    ),
                    iam.PolicyStatement(
                        actions=["iam:CreateServiceLinkedRole"],
                        resources=[
                            "arn:aws:iam::*:role/aws-service-role/application-signals.cloudwatch.amazonaws.com/*"
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["cloudtrail:CreateServiceLinkedChannel"],
                        resources=["arn:aws:cloudtrail:*:*:channel/aws-service-channel/application-signals/*"],
                    ),
                ]
            ),
        )
        step2.node.add_dependency(step1)
        indexing_call = cr.AwsSdkCall(
            service="XRay",
            action="UpdateIndexingRule",
            parameters={"Name": "Default", "Rule": {"Probabilistic": {"DesiredSamplingPercentage": 1}}},
            physical_resource_id=cr.PhysicalResourceId.of("xray-indexing-rule-default"),
        )
        step3 = cr.AwsCustomResource(
            self,
            "TransactionSearchIndexing",
            resource_type="Custom::TransactionSearchIndexing",
            on_create=indexing_call,
            on_update=indexing_call,
            log_group=sdk_logs,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [iam.PolicyStatement(actions=["xray:UpdateIndexingRule", "xray:GetIndexingRules"], resources=["*"])]
            ),
        )
        step3.node.add_dependency(step2)
