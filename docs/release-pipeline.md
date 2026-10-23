# Module 6 release pipeline

The platform runs blueprint releases through AWS CodePipeline. Both the Releases
screen and `workshop apply 6` use the same execution:

1. **Source:** read the exact S3 object version containing the release and blueprint snapshot.
2. **Validate:** run DeployDev, then Evaluate. Deploy the selected blueprint to Dev,
   run the dataset, and compare its scores with the blueprint's thresholds.
3. **Approve:** wait for the platform's Domain approver to approve or reject.
4. **Promote:** wait for the approver's separate Promote action, then deploy Prod and
   register online evaluation.

A failed evaluation leaves the release BLOCKED and fails the Evaluate action.
Scores submitted through the evaluation API cannot bypass this gate. The CLI
follows the execution instead of running evaluation on the attendee's machine.

The shared pipeline uses queued executions. CodePipeline locks each stage:
DeployDev and Evaluate share the Validate stage so another release cannot
replace Dev while evaluation is running. A release waiting at an approval gate
can hold later releases at that stage, including releases for other projects;
earlier stages can continue processing. Resolve or reject earlier approvals
to keep releases moving.
See AWS's [queued execution behavior](https://docs.aws.amazon.com/codepipeline/latest/userguide/concepts-how-it-works.html#concepts-how-it-works-executions-queued)
for the stage-locking rules.

## Deploy

The pipeline belongs to the Platform CDK stack. Confirm the AWS profile, region,
resource ownership, and an isolated test environment before deployment. Keep
`auto-delete=no` on supported resources and run the security/tag checks before
and after deployment.

Review `cdk diff` before updating an older installation. Adding the required tag
to an existing ECS Express service replaces that service because its tag list is
create-only; validate that change in an isolated environment first.

For an existing workshop installation, build current assets and deploy the
affected Platform stack:

```bash
uv run python scripts/build_backend.py
cd foundation
npx cdk deploy AgentPlatform-Platform
```

If frontend code changed, build `frontend/` and deploy `AgentPlatform-Web` as
well. For a new installation, follow the repository's full deployment and module
setup instructions; the pipeline alone does not provision project gateways,
model keys, memories, or evaluation data.

Source and artifact buckets are private and encrypted. CodePipeline invokes the
worker through AWS service permissions, without a Lambda Function URL. Browser
users sign in through Cognito; approval and promotion retain backend role checks.
Use the authenticated platform link published by the installation.

## Operate and validate

Use the Releases screen to inspect the execution identifier, evaluation results,
approver, and promotion outcome. CodePipeline provides the corresponding stage
history. A release waiting in the queue is not failed because a job timestamp is old.

When upgrading an installation, create a new release from the same blueprint for
any unfinished release that has no native execution identifier. Its earlier
evaluation or approval cannot substitute for the new pipeline's gates.

Approve and promote through the platform UI or CLI. Answering a gate directly in
the AWS console does not substitute for the platform's recorded approval and
promotion request. CloudTrail records the backend role's AWS call; the release
records the authenticated platform actor.

Acceptance should demonstrate:

1. A failing evaluation produces BLOCKED and never deploys Prod.
2. A passing evaluation reaches PENDING_APPROVAL.
3. A developer or platform admin cannot approve or promote.
4. The Domain approver can approve, then promote.
5. The release becomes PROMOTED, the native execution succeeds, and the expected
   Prod version serves authenticated requests.
6. Security, resource tags, and the private artifact access controls pass inspection.

Do not treat a successful deployment or a synthetic passing score as evidence
that these journeys work.

## Recovery and rollback

For a failed run, inspect the failing native action and the release error, fix
the cause, and use Run again when the platform permits it. A failed promotion
can be retried only when CodePipeline permits the corresponding stage retry.
An expired or otherwise ended approval requires a new run and fresh approval.

To roll back agent behavior, release the previously accepted blueprint version
through the same evaluation and approval gates. Changing the production agent
directly would bypass the release record.

For an infrastructure rollback, redeploy the previously validated CDK and backend
revision after reviewing its compatibility with existing release records. Stop
affected executions before replacing their orchestration. Retain source
versions and execution evidence needed for investigation.

Teardown must stop owned executions before removing their project resources.
Delete only the workshop resources covered by the cleanup authorization; the
`auto-delete=no` tag does not authorize or prevent explicit cleanup.
