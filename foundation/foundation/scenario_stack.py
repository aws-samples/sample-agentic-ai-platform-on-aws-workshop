"""Scenario: the AnyCompany HR sample assets (build contract section 8) that the modules register but never build.

DynamoDB tables with seed data, the HR and Finance tool Lambdas, the policy documents in S3 behind a Managed Knowledge Base
(S3 connector data source, first ingestion run inside the stack), and the dataset bucket with the evaluation dataset and the
blueprint files. Independent of the other stacks; publishes `/agent-platform/scenario/*`.
"""

from __future__ import annotations

import hashlib
import shutil

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import custom_resources as cr
from constructs import Construct

from . import config
from .common import log_group, publish, read_lambda_code


def stage_seed_asset() -> None:
    """Assemble build/scenario-seed = foundation/seed/handler.py + scenario/data/*.json (the seed Lambda's code asset)."""
    out = config.BUILD_SEED_DIR
    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)
    shutil.copy2(config.SEED_HANDLER_DIR / "handler.py", out / "handler.py")
    for path in sorted(config.SCENARIO_DATA_DIR.glob("*.json")):
        shutil.copy2(path, out / "data" / path.name)


def directory_hash(*dirs) -> str:
    """Short sha256 over the relative paths and contents of every file below the given directories."""
    digest = hashlib.sha256()
    for directory in dirs:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


class ScenarioStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Tables --------------------------------------------------------------------------------------------
        tables = {
            key: dynamodb.Table(
                self,
                "Table" + "".join(part.title() for part in key.split("-")),
                table_name=name,
                partition_key=dynamodb.Attribute(name=pk, type=dynamodb.AttributeType.STRING),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                removal_policy=RemovalPolicy.DESTROY,
                deletion_protection=False,
            )
            for key, name, pk in (
                ("employees", config.SCENARIO_TABLES["employees"], "employee_id"),
                ("leave-requests", config.SCENARIO_TABLES["leave-requests"], "request_id"),
                ("invoices", config.SCENARIO_TABLES["invoices"], "invoice_id"),
            )
        }
        tables["leave-requests"].add_global_secondary_index(
            index_name="by_employee",
            partition_key=dynamodb.Attribute(name="employee_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.tables = tables

        # --- Seed (Lambda-backed custom resource; the rows travel inside the code asset) ------------------------
        stage_seed_asset()
        seed_fn = lam.Function(
            self,
            "SeedFunction",
            runtime=lam.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=lam.Code.from_asset(str(config.BUILD_SEED_DIR)),
            timeout=Duration.minutes(2),
            memory_size=256,
            description="Seeds the scenario tables from scenario/data/*.json on create and update",
            log_group=log_group(self, "SeedFunctionLogs", f"/aws/lambda/{config.PREFIX}-scenario-seed"),
        )
        for table in tables.values():
            table.grant_write_data(seed_fn)
        seed_provider = cr.Provider(
            self,
            "SeedProvider",
            on_event_handler=seed_fn,
            log_group=log_group(self, "SeedProviderLogs", f"/aws/lambda/{config.PREFIX}-scenario-seed-provider"),
        )
        seed = CustomResource(
            self,
            "Seed",
            service_token=seed_provider.service_token,
            resource_type="Custom::ScenarioSeed",
            properties={
                "EmployeesTable": tables["employees"].table_name,
                "LeaveRequestsTable": tables["leave-requests"].table_name,
                "InvoicesTable": tables["invoices"].table_name,
                "DataHash": directory_hash(config.SCENARIO_DATA_DIR),  # changed data -> Update -> re-seed
            },
        )
        CfnOutput(self, "SeededEmployees", value=seed.get_att_string("EmployeesTableRows"))

        # --- Tool Lambdas (code = the tool folders in scenario/tools) ------------------------------------------
        table_env = {
            "EMPLOYEES_TABLE": tables["employees"].table_name,
            "LEAVE_REQUESTS_TABLE": tables["leave-requests"].table_name,
            "INVOICES_TABLE": tables["invoices"].table_name,
        }
        hr_fn = lam.Function(
            self,
            "HrToolsFunction",
            function_name=config.HR_TOOLS_FUNCTION_NAME,
            runtime=lam.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=lam.Code.from_asset(str(config.SCENARIO_HR_TOOLS_DIR)),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment=table_env,
            description="AnyCompany HR tools: get_employee_profile, get_leave_balance, submit_leave_request, get_public_holidays",
            log_group=log_group(self, "HrToolsLogs", f"/aws/lambda/{config.HR_TOOLS_FUNCTION_NAME}"),
        )
        tables["employees"].grant_read_data(hr_fn)  # the HR tools only read profiles and balances
        tables["leave-requests"].grant_read_write_data(
            hr_fn
        )  # includes the by_employee index; submit_leave_request writes
        finance_fn = lam.Function(
            self,
            "FinanceToolsFunction",
            function_name=config.FINANCE_TOOLS_FUNCTION_NAME,
            runtime=lam.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=lam.Code.from_asset(str(config.SCENARIO_FINANCE_TOOLS_DIR)),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"INVOICES_TABLE": tables["invoices"].table_name},
            description="AnyCompany Finance tool: get_invoice_status (proves project isolation)",
            log_group=log_group(self, "FinanceToolsLogs", f"/aws/lambda/{config.FINANCE_TOOLS_FUNCTION_NAME}"),
        )
        tables["invoices"].grant_read_data(finance_fn)
        self.hr_tools = hr_fn
        self.finance_tools = finance_fn

        # --- Policy documents + Managed Knowledge Base ---------------------------------------------------------
        docs_bucket = s3.Bucket(
            self,
            "DocsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            bucket_name=f"{config.DOCS_BUCKET_PREFIX}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        # One log group for the three bucket deployments: BucketDeployment uses a single Lambda per stack, so separate
        # log groups per deployment would stay empty.
        deployment_logs = log_group(self, "ScenarioDeploymentLogs", f"/aws/lambda/{config.PREFIX}-scenario-deployments")
        docs = s3deploy.BucketDeployment(
            self,
            "Docs",
            destination_bucket=docs_bucket,
            sources=[s3deploy.Source.asset(str(config.SCENARIO_KNOWLEDGE_DIR))],  # hr/*.md(+.metadata.json), finance/*
            log_group=deployment_logs,
        )
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            role_name=config.KB_ROLE_NAME,
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": self.format_arn(service="bedrock", resource="knowledge-base/*")},
                },
            ),
            description="Managed Knowledge Base: read the policy documents and run the managed embedding/retrieval",
            inline_policies={  # inline so the KB's dependency on the role covers the permissions too
                "knowledge-base": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["s3:GetObject", "s3:ListBucket"],
                            resources=[docs_bucket.bucket_arn, docs_bucket.arn_for_objects("*")],
                        ),
                        iam.PolicyStatement(actions=["bedrock:*"], resources=["*"]),
                    ]
                )
            },
        )
        kb = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=config.KB_NAME,
            description="AnyCompany policy documents (HR policies + the Finance expense policy) with department metadata",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="MANAGED",
                managed_knowledge_base_configuration=bedrock.CfnKnowledgeBase.ManagedKnowledgeBaseConfigurationProperty(
                    embedding_model_type="MANAGED"
                ),
            ),
        )
        kb.node.add_dependency(kb_role)
        data_source = bedrock.CfnDataSource(
            self,
            "DataSource",
            name=config.KB_DATA_SOURCE_NAME,
            knowledge_base_id=kb.attr_knowledge_base_id,
            data_deletion_policy="DELETE",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                managed_knowledge_base_connector_configuration=(
                    bedrock.CfnDataSource.ManagedKnowledgeBaseConnectorConfigurationProperty(
                        connector_parameters={  # shape from .claude/spikes/spike_managed_kb.py (verified 2026-10-07)
                            "type": "S3",
                            "version": "1",
                            "connectionConfiguration": {
                                "bucketName": docs_bucket.bucket_name,
                                "bucketOwnerAccountId": self.account,
                            },
                        }
                    )
                ),
            ),
        )
        self.kb_id = kb.attr_knowledge_base_id
        self.kb_arn = kb.attr_knowledge_base_arn

        # First ingestion, run and awaited inside the stack (Provider framework: onEvent starts, isComplete polls).
        agent_read_write = iam.PolicyStatement(
            actions=["bedrock:StartIngestionJob", "bedrock:GetIngestionJob", "bedrock:ListIngestionJobs"],
            resources=[self.kb_arn],
        )
        start_fn = lam.Function(
            self,
            "IngestionStartFunction",
            runtime=lam.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lam.Code.from_inline(read_lambda_code("kb_ingestion_start.py")),
            timeout=Duration.seconds(60),
            description="Starts the knowledge base ingestion job (custom resource onEvent)",
            log_group=log_group(self, "IngestionStartLogs", f"/aws/lambda/{config.PREFIX}-scenario-ingestion-start"),
        )
        start_fn.add_to_role_policy(agent_read_write)
        poll_fn = lam.Function(
            self,
            "IngestionPollFunction",
            runtime=lam.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lam.Code.from_inline(read_lambda_code("kb_ingestion_poll.py")),
            timeout=Duration.seconds(60),
            description="Reports whether the knowledge base ingestion job is complete (custom resource isComplete)",
            log_group=log_group(self, "IngestionPollLogs", f"/aws/lambda/{config.PREFIX}-scenario-ingestion-poll"),
        )
        poll_fn.add_to_role_policy(agent_read_write)
        ingestion_provider = cr.Provider(
            self,
            "IngestionProvider",
            on_event_handler=start_fn,
            is_complete_handler=poll_fn,
            query_interval=Duration.seconds(20),
            total_timeout=Duration.minutes(25),
            log_group=log_group(
                self, "IngestionProviderLogs", f"/aws/lambda/{config.PREFIX}-scenario-ingestion-provider"
            ),
        )
        ingestion = CustomResource(
            self,
            "Ingestion",
            service_token=ingestion_provider.service_token,
            resource_type="Custom::KnowledgeBaseIngestion",
            properties={
                "KnowledgeBaseId": kb.attr_knowledge_base_id,
                "DataSourceId": data_source.attr_data_source_id,
                "DocsHash": directory_hash(config.SCENARIO_KNOWLEDGE_DIR),  # changed documents -> Update -> re-ingest
            },
        )
        ingestion.node.add_dependency(docs)
        ingestion.node.add_dependency(data_source)
        CfnOutput(self, "KnowledgeBaseDataSourceId", value=data_source.attr_data_source_id)
        CfnOutput(self, "IngestedDocuments", value=ingestion.get_att_string("DocumentsScanned"))

        # --- Dataset bucket: evaluation dataset + blueprint files (also in the repo) ----------------------------
        dataset_bucket = s3.Bucket(
            self,
            "DatasetBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            bucket_name=f"{config.DATASET_BUCKET_PREFIX}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        for prefix, source_dir in (
            ("evaluation", config.SCENARIO_EVALUATION_DIR),
            ("blueprint", config.SCENARIO_BLUEPRINT_DIR),
        ):
            s3deploy.BucketDeployment(
                self,
                f"Dataset{prefix.title()}",
                destination_bucket=dataset_bucket,
                destination_key_prefix=f"{prefix}/",
                sources=[s3deploy.Source.asset(str(source_dir))],
                prune=False,
                log_group=deployment_logs,
            )

        # --- Outputs (build contract section 1) ----------------------------------------------------------------
        publish(self, "scenario/kb-id", self.kb_id, "Managed Knowledge Base id (policy documents)")
        publish(self, "scenario/kb-arn", self.kb_arn, "Managed Knowledge Base ARN")
        publish(self, "scenario/docs-bucket", docs_bucket.bucket_name, "S3 bucket with the policy documents")
        publish(self, "scenario/tools/hr-lambda-arn", hr_fn.function_arn, "HR tools Lambda")
        publish(self, "scenario/tools/finance-lambda-arn", finance_fn.function_arn, "Finance tools Lambda")
        for key, table in tables.items():
            publish(self, f"scenario/tables/{key}", table.table_name, f"Scenario table {key}")
        publish(
            self, "scenario/dataset-bucket", dataset_bucket.bucket_name, "S3 bucket with evaluation/ and blueprint/"
        )
