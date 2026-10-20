# Workshop Studio packaging

AWS Workshop Studio creates one CloudFormation stack in every attendee account before the event and shows the stack outputs on the
attendee's event page. The foundation is a CDK application (seven `AgentPlatform-*` stacks, Lambda assets, an S3-hosted UI), so the
event template is a small **deployer** (`deployer.yaml`) that runs the CDK deployment inside the attendee account with AWS CodeBuild.
Its `DeployScope` parameter defaults to `pre-day`: the control plane (Web, Auth, Platform) and the sample data (Scenario), about 12
minutes. The learners deploy the landing zone (Network, SharedServices, ModelGateway) in module 1 with `workshop apply 1` and create the
personas in module 2 with `workshop apply 2`. `DeployScope=all` deploys the seven stacks at once (the self-paced path: `workshop apply 1` and `2` then find their stacks already
deployed and go straight to their checks; about 20 minutes).

1. Workshop Studio creates the deployer stack with `AssetsBucketName` and `AssetsBucketPrefix` pointing at the event assets.
2. The custom resource `Foundation` (a 60-line Lambda) starts one CodeBuild build whose source is `repo.zip` from the assets bucket.
3. The build installs `uv` (pinned, parameter `UvVersion`) and Python 3.13, runs `uv sync --frozen` and `npm ci`, then
   `npx cdk bootstrap`, `npx cdk deploy <the scope's stacks> --concurrency 4` and `scripts/doctor.py` (which reports the landing zone
   and the personas as "not deployed" instead of failing), and signals the custom resource with the UI, API and model gateway URLs,
   the users secret ARN and the doctor verdict (`workshop-studio/cfn_signal.py`, standard library only; values the learners create
   later read "not deployed yet").
4. Stack outputs: `UiUrl`, `ApiUrl`, `ModelGatewayUrl`, `UsersSecretArn`, `PersonaPasswordsCommand`, `DoctorStatus`, `DeployerBuildId`,
   `DeployerBuildLogs`.
5. Deleting the deployer stack starts a `destroy` build (`cdk destroy --all --force --concurrency 4` plus `scripts/cleanup.py --yes`), so
   Workshop Studio's end-of-event cleanup removes the foundation too (not yet exercised end to end, see "Behaviour worth knowing").

Why CodeBuild: Workshop Studio accounts are brand new (no CDK bootstrap), the deployment needs Node and Python, and a plain
CloudFormation template cannot run CDK asset publishing. CodeBuild is available in fresh accounts, needs no Docker and keeps the
same `cdk deploy --all` path that self-paced users run from a laptop (`scripts/deploy.sh`).

Measured on 9 October 2026 in account 990851549965 (stand-in for an attendee account, CDK already bootstrapped there):
see `.claude/docs/timings.md` for the full table. Headline numbers: the deployer stack goes from `CREATE_IN_PROGRESS` to
`CREATE_COMPLETE` in **23 min 19 s** (CodeBuild build 22 min 19 s: tools 9 s, `cdk bootstrap` 63 s,
`cdk deploy --all` 19 min 27 s, doctor 13 s); the seven foundation stacks are `CREATE_COMPLETE` and `doctor.py` reports every check green (`DoctorStatus` PASS). Those numbers predate the private landing zone (16 Oct 2026): the VPC Block Public Access exclusion that cost about four minutes of that run no longer exists (the VPC has no internet gateway at all), and the ModelGateway stack now copies the LiteLLM image into ECR instead (2 to 4 minutes), so expect the same order of magnitude; a fresh Workshop Studio account should finish in about 20 minutes, to be re-measured on the next fresh deployment.

## Files in this directory

| File | Purpose |
| --- | --- |
| `deployer.yaml` | The event CloudFormation template (CodeBuild project, start-build Lambda, custom resource, outputs). |
| `cfn_signal.py` | Runs in the build's `post_build` phase and sends the SUCCESS/FAILED response to CloudFormation (works with the image's `python3` even when the build failed before `uv` was installed). |
| `attendee-policy.json` | IAM policy for the Workshop Studio participant role (admin-like with denies on account, organisation, billing, IAM-user and access-key actions). |
| `contentspec.yaml` | Sample Workshop Studio content specification wiring the three files above into an event. |

## Package

```bash
(cd frontend && npm ci && npm run build)                 # ship the built UI (or let the deployer build it: --skip-ui-check)
uv run python scripts/package_for_workshop_studio.py
# -> build/workshop-studio/repo.zip (about 4 MB, roughly 280 files; the packager prints the exact count, 282 on 9 Oct 2026) and build/workshop-studio/deployer.yaml
```

`repo.zip` is the repository without `.git`, `.venv`, `node_modules`, `build/`, `cdk.out`, caches, `.claude/` (internal notes), test
results, `.workshop/` (the state files) and `exports/`; `frontend/dist` is included. Re-run the packager after every repository change that
should reach attendees (the deployer deploys exactly what is in the archive).

## Register the event in Workshop Studio

1. In the workshop's content repository (Workshop Studio's git hosting), copy `build/workshop-studio/repo.zip`,
   `workshop-studio/deployer.yaml` and `workshop-studio/attendee-policy.json` into `static/`. Workshop Studio uploads `static/` to
   the event assets bucket under the event prefix, which is exactly where `deployer.yaml` expects `repo.zip`
   (`s3://<AssetsBucketName>/<AssetsBucketPrefix>repo.zip`).
2. Add the template and the participant policy to `contentspec.yaml` (sample in this directory): `infrastructure.cloudformationTemplates`
   with `templateLocation: static/deployer.yaml`, parameters `AssetsBucketName = {{.AssetsBucketName}}` and
   `AssetsBucketPrefix = {{.AssetsBucketPrefix}}` (Workshop Studio substitutes both), `participantVisibleStackOutputs` listing the outputs
   below; `awsAccountConfig.participantRole.iamPolicies: [static/attendee-policy.json]`; region `us-east-1` as the only accessible
   and deployable region (the foundation publishes its configuration in us-east-1 and the modules assume that region).
3. Publish the content, create a test event in us-east-1 with one or two accounts and check an account: the deployer stack must be
   `CREATE_COMPLETE` (about 20 minutes after the account is provisioned), `DoctorStatus` = `PASS`, and
   `aws cloudformation describe-stacks --stack-name <event stack>` must show the seven `AgentPlatform-*` stacks' values in its outputs.
   The build log is in CloudWatch log group `/aws/codebuild/<stack name>-deployer`.
4. Create the real event with the account count plus spares; Workshop Studio provisions and deploys the accounts in the background,
   so start the event a day ahead of time: a failed deployment (`CREATE_FAILED`, then rolled back by a destroy build) needs a new account
   or a manual deployment by a facilitator. Workshop Studio creates the event stack once, when it provisions the account; do not expect
   it to recreate a stack that was deleted by hand (confirm in the test event and record the answer here and in
   `docs/facilitator-runbook.md`, T-1). Manual redeploy in the attendee account, with its credentials: delete the rolled-back stack,
   then `aws cloudformation deploy --stack-name <event stack> --template-file workshop-studio/deployer.yaml --capabilities CAPABILITY_IAM
   --parameter-overrides AssetsBucketName=<from the failed stack's parameters> AssetsBucketPrefix=<same>` (about 20 minutes), or
   `scripts/deploy.sh` from a laptop (about 16 minutes), then `scripts/doctor.py`.

What attendees see on their event page: the stack outputs. They need `UiUrl` (sign in with the personas once module 2 created them),
`PersonaPasswordsCommand` (prints the admin / developer / approver passwords from Secrets Manager after `workshop apply 2`;
`scripts/doctor.py --show-passwords` does the same from the repository), `ApiUrl` and `ModelGatewayUrl` (the latter reads "not deployed
yet" until module 1), `DoctorStatus` and `DeployerBuildLogs` (where to look when something is off). Attendees then copy the event's AWS
credentials, clone the repository, `uv sync && npm install`, and follow `docs/v2/course/index.html` with the `workshop` command.

## Test outside Workshop Studio (any account)

```bash
export AWS_PROFILE=<profile> AWS_DEFAULT_REGION=us-east-1
B=agent-platform-ws-assets-$(aws sts get-caller-identity --query Account --output text)   # any bucket you own; this name is kept by cleanup.py
aws s3api create-bucket --bucket $B --region us-east-1
aws s3api put-public-access-block --bucket $B --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-tagging --bucket $B --tagging 'TagSet=[{Key=Project,Value=workshop-agent-platform},{Key=ManagedBy,Value=manual}]'
aws s3 cp build/workshop-studio/repo.zip s3://$B/ws/repo.zip
aws s3 cp build/workshop-studio/deployer.yaml s3://$B/ws/deployer.yaml
aws cloudformation deploy --stack-name agent-platform-deployer --template-file workshop-studio/deployer.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides AssetsBucketName=$B AssetsBucketPrefix=ws/
aws cloudformation describe-stacks --stack-name agent-platform-deployer --query 'Stacks[0].Outputs'
aws cloudformation delete-stack --stack-name agent-platform-deployer   # destroys the foundation too (a destroy build; not yet exercised end to end: expect about 15 minutes = cdk destroy about 13 min + cleanup.py)
```

Follow the build: `aws logs tail /aws/codebuild/agent-platform-deployer-deployer --follow`; the build prints `== TIMING <step>: N s`
after each step. `aws cloudformation deploy` returns when the stack is complete (it waits), so run it in the background or in a second
terminal if you want to watch.

## Behaviour worth knowing

- **Everything runs in the build phase.** CodeBuild skips `post_build` when `install` fails, and `post_build` is where CloudFormation
  is signalled; a build that never signals leaves the stack `CREATE_IN_PROGRESS` until CloudFormation's one-hour custom-resource
  timeout. With every command in `build`, a failed `uv sync`, `npm ci`, `cdk bootstrap` or `cdk deploy` signals `FAILED` within
  seconds, CloudFormation rolls back and the rollback's Delete starts a destroy build that removes whatever was created. The only
  path left to the one-hour timeout is a build that hits its own 55-minute limit.
- **The doctor never fails the stack.** A red check after a successful `cdk deploy` is reported through `DoctorStatus` = `FAIL` (details
  in the build log); a facilitator decides whether to fix the account by hand or recycle it. A deployment that fails in CloudFormation
  does fail the stack.
- **Deleting the deployer stack destroys the foundation** through the destroy build. Delete the modules' resources first (`workshop cleanup`), otherwise
  the gateways' roles and the policy engine attachments can make the stack deletion slow or leave AgentCore resources behind
  (`workshop cleanup --dry-run` lists them).
- **Open item: the destroy path has not been exercised end to end.** On 9 Oct 2026 the same commands were run by hand with exit 0
  (`cdk destroy --all --force --concurrency 4` 13 min 03 s, `cleanup.sh --yes` about 1.5 min), but the stack deletion -> destroy build
  -> signal choreography was not observed because the workshop state had to stay in place. Exercise it once on a throwaway account or a
  spare test-event account before the event (facilitator runbook, T-7) and replace the "about 15 minutes" estimate with the measurement.
- **Same-account redeploy.** After a destroy, some of the fixed-name log groups of Lambdas that run during the stack deletion come back
  through late log delivery (which ones varies per run: a different six on 8 and 9 Oct 2026; the Network stack has no custom resource
  any more) and block the next `cdk deploy` with "already exists" (`foundation/README.md`); the destroy build already runs
  `scripts/cleanup.py --yes`, and a manual `scripts/cleanup.sh --yes` does the same from a laptop (do not delete them by hand from a
  fixed list: run the dry run and act on its list). Fresh Workshop Studio accounts are not affected. `cleanup.py` deletes only
  buckets tagged `ManagedBy=cdk`, so an assets bucket named `agent-platform-ws-assets-*` (tagged `ManagedBy=manual`) survives it.
- **Private landing zone, no public networking.** The platform VPC has no internet gateway, no NAT and no public IP (isolated subnets,
  VPC endpoints, an internal load balancer for LiteLLM), so an account's VPC Block Public Access setting is irrelevant and the former
  BPA exclusion custom resource is gone. `workshop apply 1` now copies the LiteLLM image from ghcr.io into the account's ECR repository
  (`cdk-ecr-deployment`, about 2 to 4 minutes more) and the account needs no public networking at all.
- **The deployer role is AdministratorAccess** (CDK bootstrap and the foundation create IAM roles); acceptable in attendee accounts.
  The template does not create the CDK bootstrap bucket itself; `cdk bootstrap` inside the build does, so the same account can later run
  `npx cdk deploy` from a laptop as well.
- **Time budget.** Workshop Studio provisions accounts well before the event; the build takes about 20 minutes (CDK deploy about 16 of
  them). Attendee prerequisites and the day's agenda are in `docs/`.
