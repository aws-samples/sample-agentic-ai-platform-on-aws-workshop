# Workshop journey record

## Context and implementation

- Requirement/outcome, owner, audience, and operating model:
- Content revision and implementation package revision/path:
- Account, Region, caller role, pre-day stacks, and implementation package availability:
- Current chapter, last observed checkpoint, next action:
- `design-record.yaml` path (keep the `module_1` through `module_7` build-phase keys):

## Module 1: Requirements, boundaries, landing zone

| Requirement | Owner | Acceptance check | Trade-off / reconsider when |
| --- | --- | --- | --- |
| | | | |

- Enterprise/ISV consumption channel and ownership:
- Customer account topology versus actual lab placement:
- Tenant definition and verified caller membership:
- Network paths, IAM/SCP scope, model processing destinations:
- Telemetry source/storage accounts, reader grants, correlation, and incident owner:
- Content/redaction, retention/deletion, sampling, alert/no-data policy:
- Event central Okta provisioning account versus platform/workload account roles:

## Module 2: Shared platform

| Checkpoint | Owner/interface | Configuration/source | Expected allowed/denied result | Actual result |
| --- | --- | --- | --- | --- |
| Foundation and identity, including telemetry setup | | | | Pending |
| Model route and safety/processing decisions | | | | Pending |
| Registry ownership, metadata audience, approval | | | | Pending |
| Gateway choice: no shared gateway / shared plus domain / per-domain / shared with dedicated exceptions | | | | Design |
| Coding-client registry/gateway identity and adapter | | | | Pending |
| Control plane, personas, projects, roles, budgets | | | | Pending |

## Module 3: HR and Finance pattern records

Complete one row per pattern. HR uses the supplied agent lifecycle; Finance agent deployment is an extension.

| Field | HR | Finance |
| --- | --- | --- |
| User task and success/failure case | | |
| Agent ingress identity and target's identity | | |
| Workload gateway layout and environment separation | | |
| Target schema/protocol, private connectivity, credentials | | |
| Data authorization / KB scope | | |
| Actual allowed/denied tool evidence | | |
| Blueprint model, prompt, tool/knowledge references | | |
| Memory/state required, actor binding, retention | | |
| Limits, budget, dataset/threshold design | | |
| Unresolved binding/deployment work and owner | | |

## Module 4: Access and AgentOps

| Request | Requester/project/env | Resource/version/actions/data scope | Cap/duration | Approver/grant evidence | Revocation owner |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

- Catalogue approval versus actual invocation grant:
- HR Dev key/provider reference, model restriction, cap and team budget:
- Normal inference, guardrail, wrong-model, budget refusal, restoration results:
- Isolation review and pending controls:
- HR v1/v2 blueprint/runtime versions, prompt diff, dataset, actual scores:
- BLOCKED / PENDING_APPROVAL / APPROVED / promoted states and evidence:
- Domain approver, release ID, Prod live version and actual invocation:
- Dev OBO variant identity evidence (separate from approved Prod release):
- Optional export and proposed CI/CD / rollback owner:

## Module 5: Operations

- Observation start/end; actual answered traffic including promotion seed:
- Runtime metrics, trace/session references, policy mode and decisions:
- Online evaluator, sampling/quiet timeout, mean/count/time window:
- Feedback source and Dev long-term memory readback:
- Spend window/delta, successful-outcome denominator, unit cost, billing reconciliation:
- Incident/change evidence, notification/recovery test or pending integration:

## Production backlog

| Gap | Action | Owner | Acceptance check | Priority/status |
| --- | --- | --- | --- | --- |
| | | | | |

## Evidence and cleanup

| Chapter | Resource/configuration/version | Check | Actual result/time | Sanitized evidence | Reviewer |
| --- | --- | --- | --- | --- | --- |
| | | | Pending | | |

| Resource | Owner | Cleanup scope | Deleted/retained/pending | Evidence and follow-up |
| --- | --- | --- | --- | --- |
| | | | | |

Do not record tokens, passwords, raw API keys, or private conversation content. Keep resource state until cleanup is reconciled. Design, source availability, and executed proof remain separate fields.
