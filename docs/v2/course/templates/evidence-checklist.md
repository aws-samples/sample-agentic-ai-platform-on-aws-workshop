# Workshop evidence checklist

Record the implementation/environment, exact resource or version, fixture, actual result, timestamp, and reviewer. Keep design and execution evidence distinct. Both workload designs are included; the implementation deploys the HR agent and a Finance invoice-tool fixture.

## Scorecard

| Stage | Maximum points | Status/evidence |
| --- | --- | --- |
| Requirements | 10 | Pending |
| Architecture, including account/tenant telemetry | 20 | Pending |
| Shared platform | 19 | Pending |
| HR and Finance tool/blueprint patterns | 12 | Pending |
| Access, governance, and HR AgentOps | 24 | Pending |
| Operations | 10 | Pending |
| Handoff and cleanup | 5 | Pending |
| Total | 100 | |

## Architecture and platform

- [ ] Requirements have owners, success conditions, and denied cases.
- [ ] Operating model, usage channel, account placement, and tenant boundaries are distinct.
- [ ] Landing-zone telemetry includes readers, storage, content, retention, sampling, cost and notification ownership.
- [ ] Foundation stacks, network endpoints, health/doctor, and persona denial are observed (6 platform points including telemetry setup).
- [ ] Model route, safety/processing location, and budget ownership are recorded (3).
- [ ] Registry discovery/publish/invoke authorities, C/D policy design, and coding-client identity/acceptance matrix are reviewed (7). Deployment/client checks remain pending if no adapter exists.
- [ ] Control-plane domains/projects/roles/budgets and direct-Lambda denials are observed (3).

## Workload designs (6 points each)

For HR and Finance: task and identity 2; tool/target configuration and allowed/denied evidence 2; blueprint and explicit bindings/limits/implementation gaps 2.

- [ ] Apply 4 completed in 2.3.2 for both patterns; readiness, any reruns, and actual gateway modes recorded.
- [ ] HR retrieval scope, leave policy LOG_ONLY/ENFORCE results, and OBO gateway identity propagation observed.
- [ ] Finance invoice fixture, unknown-invoice error, and cross-domain refusal observed.
- [ ] HR blueprint references, prompt variants, memory, limits, and thresholds reviewed.
- [ ] Finance blueprint design completed; missing model key/resolver/runtime/evaluation/promotion remain explicit.
- [ ] Coding client is a shared access pattern, not a third workload or score track.

## Governance and AgentOps (24)

- [ ] Access request and scope recorded; catalogue approval distinguished from permission grants (4).
- [ ] Model key, normal/guardrail/wrong-model probes, cap refusal and restoration observed (4).
- [ ] Isolation reviewed across IAM, policy, target/data, memory, and metadata/telemetry (4).
- [ ] HR v1 deployment/evaluation, diagnosis, v2 deployment/evaluation, and OBO agent evidence retained (8).
- [ ] Independent approval, actual Prod live version, invocation and seed traffic verified (4).

A successful tool command does not establish agent deployment. A submitted score is not an independent production gate. A skipped first-use key probe is not a fresh result.

## Operations (10)

- [ ] Cost window and answered-outcome denominator match, with billing/infra allocation gaps recorded (3).
- [ ] Trace, runtime/policy metrics, release/CloudTrail evidence, and incident/recovery assessment recorded (4).
- [ ] Online score mean/count/window, synthetic feedback, Dev memory readback, and owned production backlog recorded (3).

## Handoff and cleanup (5)

- [ ] Evidence excludes secrets, tokens, passwords, and private content.
- [ ] Resource state/inventory reconciled with cleanup dry run and actual terminal results.
- [ ] Retained resources and external Okta credentials have accepting owners and revocation/retention plans.
- [ ] Failed or unexecuted AWS checks remain pending; content preview is not hosted lab acceptance.
