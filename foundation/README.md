# Foundation (CDK, Python)

The CDK application that deploys the workshop platform foundation into one AWS account: network, model gateway (LiteLLM),
identity (Cognito), control-plane API and state, web hosting and the shared services. Everything an attendee account needs
before the modules start. The landing zone (network, model gateway) is **private**: no internet gateway, no NAT, no public IP; AWS
services are reached through VPC endpoints and the proxy from outside the VPC only through a relay Lambda
(`.claude/docs/private-landing-zone-design.md`). The attendee-facing description is `docs/workshop-guide.html`; the stack and parameter names below are the
contract the backend, the `workshop` command and the UI are built against.

## Deploy, verify, destroy

```bash
# from the repository root, once per machine
uv sync && npm install
export AWS_PROFILE=workshop AWS_DEFAULT_REGION=us-east-1   # any credentials with administrator access
(cd foundation && npx cdk bootstrap)                       # once per account/region; the CDK app (cdk.json) lives in foundation/

# everything (assembles build/backend first, deploys all seven stacks with --concurrency 4, runs the doctor): about 16 minutes
scripts/deploy.sh
# the learning path's split: the control plane and the sample data before the day (about 12 minutes), the landing zone in module 1
scripts/deploy.sh --pre-day                                # Web, Auth, Platform, Scenario (also: `uv run workshop deploy pre-day`)
scripts/deploy.sh --landing-zone                           # Network, SharedServices, ModelGateway (what `workshop apply 1` runs)
scripts/doctor.sh                                          # re-check any time (--show-passwords prints the persona passwords)

# or by hand
uv run python scripts/build_backend.py
cd foundation && npx cdk deploy --all --concurrency 4 --require-approval never
uv run python ../scripts/doctor.py

# tear down (leaves only the account-level Transaction Search setting and the CDK bootstrap), then sweep leftovers;
# the relay Lambda's ENIs can hold the Network/ModelGateway deletion for up to about 20 minutes
cd foundation && npx cdk destroy --all --force --concurrency 4
../scripts/cleanup.sh --yes
```

The same commands are in the attendee guide (`docs/workshop-guide.html`, "Rebuild it at your own pace"); keep the two in sync.

`cdk.json` runs the app with `uv run --project .. python app.py`, so `npx cdk ...` works from `foundation/` on any OS with uv.
`app.py` calls `scripts/build_backend.py`'s `ensure_bundle()` at synth time, so `cdk deploy --all` is the only command needed.
It also resolves the three availability zones (`foundation/azs.py`, see the gotchas) with one `ec2:DescribeAvailabilityZones`
call, cached in `cdk.context.json`; without credentials it falls back to `us-east-1a,b,c` with a warning instead of failing.
Tests: `uv run pytest foundation/tests` (synthesizes for a fictitious account with the zone names pre-seeded in the context,
no credentials needed; `test_azs.py` and `test_relay.py` unit-test the resolver and the relay handler with fakes).

## Stack map (deployment order)

| Stack | Contents | Publishes (SSM `/agent-platform/...` + CloudFormation outputs) |
| --- | --- | --- |
| `AgentPlatform-Network` | Private VPC 10.0.0.0/16 in the 3 AZs AgentCore Runtime supports for VPC mode (`use1-az1`, `use1-az2`, `use1-az4`; zone names resolved per account at synth, `foundation/azs.py`); isolated subnets `app` (LiteLLM tasks + their internal load balancer, harness ENIs, relay Lambda, interface endpoints) and `data` (RDS); no internet gateway, no NAT, no public IP; S3 gateway endpoint on every route table + 9 interface endpoints with private DNS in the first 2 AZs (Secrets Manager, Logs, Monitoring, X-Ray, `ecr.api`, `ecr.dkr`, `bedrock-runtime`, `bedrock-agentcore`, `bedrock-agentcore.gateway`); security groups `agent-platform-endpoints` (443 from the VPC CIDR), `agent-platform-harness` and `agent-platform-relay` (egress only) | `network/vpc-id`, `network/app-subnet-ids`, `network/harness-subnet-ids` (= the app subnets), `network/harness-security-group-id`, `network/relay-security-group-id`, `network/availability-zones`, `network/private` (`true`); outputs VpcId, AppSubnetIds, DataSubnetIds |
| `AgentPlatform-SharedServices` | Bedrock guardrail `hr-baseline` (+ numbered version), CloudWatch Transaction Search enablement, `agent-platform-evaluation-role` | `guardrail/id`, `guardrail/arn`, `guardrail/version`, `roles/evaluation-role-arn` |
| `AgentPlatform-ModelGateway` | ECR repository `agent-platform/litellm` filled at deploy time with a copy of `ghcr.io/berriai/litellm:v1.104.1` (`cdk-ecr-deployment`, no Docker anywhere), LiteLLM on ECS Express Mode (1 vCPU/2 GB, 1 task) on the `app` subnets, hence an **internal** load balancer, RDS PostgreSQL 18.6 `db.t3.micro`, secrets (master key, salt key, DB), S3 config bucket with `config.yaml`, ALB idle timeout 600 s, relay Lambda `agent-platform-model-gateway-relay` (Python 3.13, 512 MB, 120 s, in the VPC: the only way to reach the proxy from outside it) | `model-gateway/url`, `model-gateway/master-key-secret-arn`, `model-gateway/models`, `model-gateway/relay-function-name`, `model-gateway/relay-function-arn`, `model-gateway/private` (`true`) |
| `AgentPlatform-Web` | S3 bucket + CloudFront (OAC, SPA fallback) serving `frontend/dist` if built, else `placeholder/` | `ui/url` |
| `AgentPlatform-Auth` | Cognito pool `agent-platform-users`, Hosted UI domain, app client `agent-platform-ui`, the empty users secret (the groups, the 3 persona users and their passwords are created by `workshop apply 2`, module 2 of the learning path) | `auth/user-pool-id`, `auth/client-id`, `auth/domain`, `auth/users-secret-arn` |
| `AgentPlatform-Platform` | DynamoDB `agent-platform`, backend role (may invoke the relay: string ARN), Lambdas `agent-platform-api` and `agent-platform-worker`, HTTP API with Cognito JWT authorizer, EventBridge rule, `config.json` for the UI, the two AgentCore Runtime service-linked roles created up front (no reference to the ModelGateway stack: the backend finds the model gateway through SSM at run time and reaches it through the relay) | `api/url`, `table/name`, `backend/worker-function-name`, `roles/backend-role-arn` |
| `AgentPlatform-Scenario` | DynamoDB `agent-platform-employees` / `-leave-requests` (GSI `by_employee`) / `-invoices` seeded from `scenario/data`, Lambdas `agent-platform-hr-tools` and `agent-platform-finance-tools`, docs bucket `agent-platform-scenario-docs-<account>` + Managed Knowledge Base `agent-platform-policies` (S3 connector data source `policy-docs`, first ingestion awaited in the stack), dataset bucket `agent-platform-scenario-dataset-<account>` (`evaluation/`, `blueprint/`) | `scenario/kb-id`, `scenario/kb-arn`, `scenario/docs-bucket`, `scenario/tools/hr-lambda-arn`, `scenario/tools/finance-lambda-arn`, `scenario/tables/employees`, `scenario/tables/leave-requests`, `scenario/tables/invoices`, `scenario/dataset-bucket` |

Why this order: the Cognito app client needs the CloudFront URL as its callback (Web before Auth); the API authorizer needs the
pool and client (Auth before Platform); `config.json` needs the API URL, so the Platform stack writes it into the Web bucket.
Two independent chains result: the landing zone (Network -> SharedServices -> ModelGateway) and the control plane (Web -> Auth ->
Platform); the Scenario stack is independent of all others. The learning path deploys the control plane and the Scenario before the
day (`config.PRE_DAY_STACKS`) and lets the learners deploy the landing zone in module 1 (`config.LANDING_ZONE_STACKS`). Cross-stack
values are plain CDK references (rendered as `Fn::GetStackOutput`).
Deploy with `--concurrency 4` (as `scripts/deploy.sh` does): the CDK CLI then runs Web/Auth and Scenario alongside ModelGateway,
and a fresh account finishes in about 16 minutes instead of about 27 with the default sequential deploy.

To add a stack: create `foundation/foundation/<name>_stack.py`, instantiate it in `app.py` next to the others,
use `common.publish(self, "<key>", value)` for every SSM parameter and output, and keep removal policies DESTROY.

## Custom resources (what runs beyond plain CloudFormation)

| Resource | Stack | What it does |
| --- | --- | --- |
| `Custom::TransactionSearchLogsPolicy` / `...Destination` / `...Indexing` | SharedServices | Three `AwsCustomResource` SDK calls, in order, on create and update: `logs:PutResourcePolicy TransactionSearchXRayAccess`, `xray:UpdateTraceSegmentDestination CloudWatchLogs`, `xray:UpdateIndexingRule Default 1%`. Nothing on delete (account setting stays). |
| `Custom::LiteLLMMasterKey` | ModelGateway | Python 3.13 Lambda (Provider framework) that writes `sk-` + 40 url-safe random characters into the empty secret `agent-platform/litellm-master-key`, only if it has no value yet. |
| `Custom::CDKECRDeployment` | ModelGateway | `cdk-ecr-deployment` 4.2.74: its Go Lambda (1024 MB, outside the VPC, 3 attempts) streams `ghcr.io/berriai/litellm:v1.104.1` (amd64, 23 layers, 401 MB compressed) into `agent-platform/litellm:v1.104.1` on create and update; the Express service depends on it. About 2 to 4 minutes. Nothing on delete: the repository is `EmptyOnDelete`. Its log group has a generated name (see the redeploy gotcha). |
| `Custom::CDKBucketDeployment` (config) | ModelGateway | Uploads the rendered `litellm/config.yaml.tpl` (guardrail id and version substituted) as `config.yaml`. |
| `Custom::AlbIdleTimeout` | ModelGateway | `elasticloadbalancingv2:ModifyLoadBalancerAttributes idle_timeout.timeout_seconds=600` on the Express-managed load balancer. |
| `Custom::CDKBucketDeployment` (UI, config.json) | Web, Platform | Site content (`prune=False`) and the UI runtime configuration `config.json` with a CloudFront invalidation. |
| `Custom::AgentCoreRuntimeServiceLinkedRole`, `Custom::AgentCoreNetworkServiceLinkedRole` | Platform | `iam:CreateServiceLinkedRole` for `runtime-identity.bedrock-agentcore.amazonaws.com` and `network.bedrock-agentcore.amazonaws.com` on create (InvalidInput = exists already, ignored), nothing on delete. AgentCore Runtime creates the first on the account's first CreateHarness and the second on the first harness in VPC mode (its ENIs in our subnets), with the caller's permissions; that failed with 403 from the backend role in a fresh account (15 Oct 2026). The backend role may create exactly these two roles as well. |
| `Custom::ScenarioSeed` | Scenario | Python 3.13 Lambda (Provider framework) whose code asset is `build/scenario-seed/` = `foundation/seed/handler.py` + copies of `scenario/data/*.json` staged at synth time; writes every row (numbers as `Decimal`) on create and on update (a hash of the data files is a resource property), nothing on delete. |
| `Custom::KnowledgeBaseIngestion` | Scenario | Provider framework with `isComplete`: `kb_ingestion_start.py` calls `StartIngestionJob` (adopts the newest job on `ConflictException`), `kb_ingestion_poll.py` polls `GetIngestionJob` every 20 s until `COMPLETE` (FAILED/STOPPED fail the stack); re-runs when the documents change (hash property). Nothing on delete. |
| `Custom::CDKBucketDeployment` (scenario) | Scenario | `scenario/knowledge/**` into the docs bucket (keeps `hr/`, `finance/` prefixes and the `.metadata.json` sidecars); `scenario/evaluation/**` and `scenario/blueprint/**` into the dataset bucket under `evaluation/` and `blueprint/`. |
| `Custom::S3AutoDeleteObjects` | ModelGateway, Web, Scenario | Empties the buckets on `cdk destroy`. |

## Runtime facts

- Model gateway URL: `https://<service>.ecs.us-east-1.on.aws` (OpenAI base = URL + `/v1`). **Private**: the load balancer is
  internal, the name resolves to private addresses and only VPC residents (harnesses in VPC mode, the relay) can connect; from a
  laptop or the backend the proxy is reached through the relay Lambda `agent-platform-model-gateway-relay` (event
  `{"method", "path", "query", "headers", "body", "timeout"}`, answer `{"status", "headers", "body"}`; the caller passes the
  `Authorization` header, the relay adds nothing). The admin UI at `/ui` (user `admin`, password = master key) is therefore not
  reachable from a browser; the Governance screen still shows the credentials, kept in the `agent-platform/litellm-master-key`
  secret. Health: `/health/liveliness` through the relay.
- Network: 3 AZs (`/agent-platform/network/availability-zones`), app subnets = harness subnets, `network/private` = `true`.
- Model aliases: ten bootstrap-configured options from Anthropic, OpenAI and Amazon, each with the baseline guardrail bound. Sonnet 5 remains the default. See [the model list](../MODEL_CATALOG.md); keep `MODELS` in `foundation/config.py` and `litellm/config.yaml.tpl` aligned. Provider access varies by account and Region.
  (`guardrailConfig` in `litellm_params`) and explicit prices.
- Personas: `admin`, `developer`, `approver` (emails `<persona>@anycompany.example`); passwords in the users secret.
  Sign in with `USER_PASSWORD_AUTH` (the `workshop` command, tests) or the Hosted UI (web app). The API expects the **ID token**.
- `GET /health` is public; every other route requires `Authorization: Bearer <idToken>` (401 otherwise).
- CORS: API Gateway adds the CORS headers (all origins, common headers, GET/POST/PUT/PATCH/DELETE/OPTIONS). Because the `$default`
  route is protected by the JWT authorizer, an unauthenticated `OPTIONS /{proxy+}` route exists and **the API Lambda must answer
  OPTIONS with an empty 204** (the skeleton does; keep it in the Phase 2A backend).

## Account prerequisites and gotchas (learned on the dev account)

- **No internet path, by design**: the VPC has no internet gateway and no NAT, so nothing in it reaches the public internet; AWS
  services are reached through the S3 gateway endpoint and the nine interface endpoints (private DNS on). Consequences: the LiteLLM
  image is copied into ECR at deploy time (tasks cannot pull from ghcr.io), the Express load balancer is internal (`curl` from a
  laptop times out; use the relay), and every caller outside the VPC (backend, `workshop` CLI, doctor, scripts) goes through the
  relay Lambda. VPC Block Public Access no longer matters (there is no internet gateway to block); the former exclusion custom
  resource and its Lambda are gone.
- **The load balancer's scheme is fixed per VPC**: the first Express service in a VPC decides whether its load balancer is
  internal or internet-facing, and an existing public deployment cannot be flipped in place. Moving an account to the private
  design means destroying the landing zone (`workshop cleanup --stacks` or `cdk destroy` of Network/SharedServices/ModelGateway)
  or using a fresh account.
- **Zone ids for the harness**: AgentCore Runtime's VPC mode only accepts subnets in `use1-az1`, `use1-az2`, `use1-az4`
  (`config.HARNESS_AZ_IDS`, a documentation table). The zone *names* that carry them differ per account, so `app.py` resolves them
  with `ec2:DescribeAvailabilityZones` at synth and caches them in `cdk.context.json` (`agent-platform:harness-azs:<account>:<region>`;
  the file is git-ignored). Without credentials the synth prints `[azs] ...` and falls back to `us-east-1a,b,c`: fine for tests,
  wrong for a deploy, so if `CreateHarness` ever rejects the subnets check `/agent-platform/network/availability-zones`, delete the
  cached entry and synth again with credentials. The `Vpc` construct validates explicit names against the CLI's own
  `availability-zones:account=..:region=..` context entry; when the CLI has none cached, the Network stack seeds that key with
  the same three names, so the CLI never makes a lookup of its own: `cdk synth` for any account works without credentials, and
  one `DescribeAvailabilityZones` call per account is all that ever happens (a cached CLI entry is kept and still checks the
  names).
- **Destroy takes longer**: the ENIs of the relay Lambda and of the interface endpoints delay the ModelGateway and Network stack
  deletion (Lambda ENIs by up to about 20 minutes). The ECR repository is emptied automatically (`EmptyOnDelete`).
- **Service-linked roles**: the first load balancer in a fresh account fails with `AccessDenied` if the ELB service-linked role was
  created a second earlier; the ModelGateway stack therefore creates the ELB, Application Auto Scaling and ECS service-linked roles
  up front (idempotent custom resources).
- **Transaction Search**: enabling it needs `logs:*` on `aws/spans`, `application-signals:StartDiscovery`, `iam:CreateServiceLinkedRole`
  and `cloudtrail:CreateServiceLinkedChannel`; the destination stays `PENDING` for about 4 minutes before it is `ACTIVE`.
- **Express service rollbacks**: if the Express service fails to create, CloudFormation may roll back the infrastructure role before
  the service is gone and the service then stays `DRAINING`; recreate a role with the same name (managed policy attached) to let it
  finish, then delete the stack.
- **ECS Express service is create-only in four properties**: `ServiceName`, `Cluster`, `InfrastructureRoleArn` and `Tags` cannot be
  updated on `AWS::ECS::ExpressGatewayService` (`aws cloudformation describe-type` lists them as `createOnlyProperties`, `tagUpdatable: false`).
  Any change to them makes CloudFormation replace the service, which fails for a service with a fixed name (or yields a new
  `ag-<hex>.ecs...` endpoint that invalidates `/agent-platform/model-gateway/url` and every rendered harness). The service therefore
  carries its two tags explicitly (the app-wide `Tags` aspect excludes this resource type) and the infrastructure role must never be
  renamed or replaced. Adding account-wide tags for cost reports is still fine: add them to the aspect in `app.py` and to the
  `tags=` list of the service only if you accept a replacement.
- **Guardrail changes need a new version**: `AWS::Bedrock::GuardrailVersion` is created once; CloudFormation never re-snapshots it. Bump
  `config.GUARDRAIL_VERSION_LABEL` (v2 -> v3) with every guardrail change so a new numbered version is published; the model gateway task
  restarts automatically (the guardrail id and version are in its environment) and the aliases pick the new version up.
- **Redeploying into the same account after `cdk destroy`**: some of the fixed-name log groups of Lambdas that run during the stack
  deletion (bucket deployments, provider framework, SDK-call singletons) come back after the destroy: their last log delivery recreates
  the group after CloudFormation removed it. Which ones varies per run (2026-10-08: `/aws/lambda/agent-platform-config-deployment`,
  `-master-key-provider`, `-model-gateway-sdk-calls`, `-shared-services-sdk-calls`, `-ui-config`, `-ui-content`; 2026-10-09:
  `-model-gateway-sdk-calls`, `-scenario-deployments`, `-scenario-seed-provider`, `-ui-config`, `-ui-content`, `-users-secret-provider`,
  plus the ModelGateway stack's generated-name image-copy provider group `/aws/lambda/AgentPlatform-ModelGateway-CustomCDKECRDeployment...`),
  so assume that every deletion-time Lambda's group can come back. The
  next deploy then fails at change-set validation with "log group already exists". Run `scripts/cleanup.sh` (dry run) and act on its
  list, or `scripts/cleanup.sh --yes`, between destroy and redeploy (verified 2026-10-08 and 2026-10-09); do not delete the groups by
  hand from a fixed list. Fresh accounts are not affected. The same applies after a failed create (log groups marked `DELETE_SKIPPED`).

## Time and cost

- First deploy of all seven stacks into a fresh account, measured on 2026-10-08 with `--concurrency 4` **on the public design**:
  **15 min 46 s** end to end (about 27 min with the CLI's default sequential deploy). CloudFormation time per stack:

  | Stack | Minutes | Notes |
  | --- | --- | --- |
  | Network | 1.8 | VPC, 6 subnets, the S3 gateway endpoint and 9 interface endpoints (created in parallel); measured 16 Oct 2026 |
  | SharedServices | 1.4 | guardrail + version, Transaction Search, evaluation role |
  | Web | 3.9 | CloudFront distribution |
  | Scenario | 5.4 | Managed KB 15 s, first ingestion 153 s |
  | ModelGateway | 16.0 | RDS 6.8 + Express service 5.6 + the relay Lambda in the VPC 3.9; the image copy itself took about 10 s; measured 16 Oct 2026 (critical path Network -> SharedServices -> ModelGateway) |
  | Auth | 1.2 | since 14 Oct 2026 without groups, users and the password Lambda (`workshop apply 2` creates them in seconds) |
  | Platform | 2.0 | |

  The pre-day scope (Web, Auth, Platform, Scenario) finished in 7.3 minutes on 16 Oct 2026 (a fresh account needs the CDK bootstrap
  and the frontend build on top); the landing zone the learners deploy in module 1 (Network, SharedServices, ModelGateway) took
  19.5 minutes end to end on the private design (synth, asset publishing and the doctor included; 13 on the public design).

  An update that changes the LiteLLM configuration or the guardrail takes about 10 minutes because the Express service drains the old task.
- Running cost per account about **0.28 USD/h** (was 0.10 on the public design): Fargate 1 vCPU/2 GB ~0.05, ALB ~0.02, RDS db.t3.micro
  ~0.02, 9 interface endpoints x 2 AZs x ~0.01 USD per AZ-hour ≈ 0.18 (`config.ENDPOINT_AZ_COUNT = 2`: the third zone reaches them
  cross-AZ for cents), ECR storage for the 401 MB image and CloudFront/Lambda/DynamoDB within free tiers or cents. Bedrock usage is
  on top.
- `cdk destroy --all --force --concurrency 4` removes the stacks (buckets and the ECR repository are auto-emptied, log groups created
  by the stacks deleted); the relay Lambda's ENIs can hold the deletion for up to about 20 minutes.
  Left behind on purpose or by CloudFormation: the account-level Transaction Search setting, the auto-created log groups of the CDK
  provider Lambdas (`/aws/lambda/AgentPlatform-*`), and any secret still in its recovery window. `scripts/cleanup.sh --yes` sweeps the
  latter two (dry run without `--yes`) and reports anything else with the `agent-platform` prefix; run it before redeploying into the
  same account (a secret pending deletion blocks a redeploy under the same name).
