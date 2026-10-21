# Component implementation decision

Use one record for the component or workload decision in front of you. Complete the decision card first; use only the relevant technical prompts below. The workshop account is fixed to one AWS account. Do not ask for a choice when only one supported option meets the requirement.

## Decision card

- **Requirement:** What must this component achieve, and for whom?
- **Constraint:** What would rule an option out (identity, data location, isolation, compatibility, ownership, or cost)?
- **Options that fit:** Which supported implementations remain after applying those constraints?
- **Recommendation and trade-off:** Why this option, and what responsibility or limitation comes with it?
- **Reconsider when:** What changed requirement would cause you to choose differently?
- **Workshop action:** What will you configure, inspect, compare, or implement now?
- **Expected result:** What concrete output or behavior should a learner observe?
- **Evidence/status:** Source or resource revision, actual result, and whether this is a design exercise, implemented configuration, or verified execution.

A useful answer explains the decision. “Use a gateway because it is managed” is incomplete; name the required protocols/controls, the operating work it removes, and any integration still needed. Keep unverified results pending.

## Optional technical prompts

The remaining fields are a reference, not a questionnaire to complete in full. Skip fields outside the chosen component or workload and link to existing journey records instead of copying them.

## Component and requirement

- Capability or resource:
- Ownership scope (shared platform service or workload resource):
- Requirement IDs:
- Required capability:
- Owner:
- Operating-model decision and responsible team:
- Account placement and tenant boundary:
- Platform usage model (enterprise / ISV):
- Consumers and consumption channel:
- UI, API, backend, and external consumer responsibilities:
- Input/output contract:
- Dependencies:
- Required access, data, and operational controls:
- Network path and trusted identity at each account boundary:
- Telemetry access, correlation, and incident owner:

## Foundation and model-routing decisions

Complete relevant fields; do not ask for decisions that cannot change the selected implementation.

- Organisation/OU/account scope, SCP owner, and effective-policy evidence:
- Approved model providers, model/profile ARNs, and source Region:
- Regional / geographic CRIS / global routing and permitted processing locations:
- Destination-Region checks, approved-profile exception, or global `unspecified` handling:
- IAM/model allowlist, exception approver, affected workloads, and policy rollback:
- Client/API features (Claude Code / application SDK / ISV consumer):
- Direct Bedrock / Claude Apps Gateway / AgentCore inference targets / LiteLLM rationale:
- Gateway connector/provider configuration, model mappings, and credential owner:
- Attribution, quota/budget enforcement, bypass restrictions, and fallback constraints:
- Infrastructure source, required adapter, operator, and qualification gaps:

## Tool-gateway decisions

- Central / domain / shared with dedicated exceptions, with rationale:
- Gateway owner, account/Region, tenant/domain boundary, and incident owner:
- Client-to-gateway ingress and trusted issuer/audience or IAM role:
- Per-target service or user-delegated access, with rationale:
- Tool/action authorization and policy enforcement mode:
- Per-target account/Region, protocol, owner, private/public egress path, and DNS:
- Managed Lattice plus routing / self-managed Lattice plus RAM / OpenAPI private endpoint / Regional API with VPC Link / Lambda VPC access:
- Outbound IAM / OAuth client credentials / delegated authorization code / qualified OBO / API key:
- Whose permissions the target sees; credential ownership and rotation:
- For delegated authorization-code access: Consent portal, user/tenant binding, provider scopes, and three callback settings:
- Target-side trust, cross-tenant/domain denial, and direct-target bypass checks:
- Missing consent, private connectivity, cross-account, or application integration:

## Registry and catalog decisions

- Central organization-scoped / shared product catalog / dedicated tenant registries, with rationale:
- Owner account/Region, delegated administrator, and single-account workshop boundary:
- Auto-detection scope, READY/ACTIVE checks, provenance, and explicit publishing/sync requirements:
- RAM consumer/publisher/admin permissions and corresponding caller IAM scope:
- Metadata visibility: search/list summaries, full records, registry MCP, and management inventory:
- Tenant-to-registry mapping or application-mediated access; prevention of direct-access bypass:
- Registry discovery IAM or OAuth/JWT; IdP discovery URL, audiences, clients, scopes, and tenant rules:
- PKCE and client registration method; credential renewal and offboarding:
- Publisher/curator IAM roles and approval separation:
- Integration path: local IDE (discovery, development, testing, publishing) / hosted agent (discovery, connection, invocation), or both:
- Design-time pinned dependencies or runtime discovery, full descriptor validation, and cache/revocation policy:
- Separate registry-discovery identity and MCP/A2A endpoint invocation credentials:
- Application asset/version mapping, approval source of truth, withdrawal, and synchronization failure handling:
- Selected sample revision, current API/schema compatibility, IaC parameters, and adapter gaps:
- Tenant-A/tenant-B search/list/batch-get/MCP denial, unapproved record exclusion, and invocation denial evidence:

## Workload pattern and blueprint — Module 3

- HR policy/leave assistant or Finance invoice helper, user task and acceptance cases:
- Workload identity, gateway/environment design, target type/schema, connectivity and credential owner:
- Tool arguments versus trusted employee/tenant authorization:
- Actual allowed/denied/unknown-record evidence:
- Blueprint model/prompt/tool/knowledge references, memory need, limits, budget, dataset and thresholds:
- HR reference bindings versus Finance extension resolver/runtime/key/evaluation gaps:
- Hand-off to Module 4 access requests, caps, deployment, evaluation, approval and promotion:

## Coding-client shared access — Module 2.3

- Client/MCP transport compatibility and source of configuration:
- Registry IAM/JWT/mediated discovery, separate publish/approve IAM roles:
- Gateway choice (none shared / shared plus domain / per-domain / shared with dedicated exceptions), policy owner and environment boundaries:
- Gateway ingress identity, target identity, verified employee/tenant mapping:
- Actual discovery, allowed/denied invocation, expiry/revocation results or missing adapter:

## Runtime and gateway integration

- Central agent/distributed tools / domain-owned runtimes / shared ISV with dedicated exceptions, and rationale:
- Agent, gateway, MCP server, data, and inference placement and owners:
- Consumer/application, Runtime, model, tool, and data request/response contracts:
- Returned data crossing domain/customer boundaries and handling requirements:
- Per-hop private/public connectivity, DNS, routes, resource sharing, and target constraints:
- Domain onboarding approval, schema/version compatibility, discovery and invocation checks:
- Template/provisioning source, deployment dependencies, endpoint outputs, and qualification gaps:
- Release/evaluation gates, endpoint-to-version binding, rollback, and cleanup order:
- Timeout/retry/idempotency contracts and unavailable-domain behaviour:
- Request correlation, data-plane audit configuration, cross-account telemetry, and incident owners:
- Shared model/gateway usage metering, billing reconciliation, and cost allocation:

## Runtime ingress identity, if an external gateway is needed

- Native IAM/JWT sufficiency and reason for adding an external gateway:
- Gateway product/edition/version and feature/licence availability:
- Pattern 1 OBO JWT / Pattern 2 SigV4 + forwarded JWT / Pattern 3 SigV4 + trusted identity headers, with rationale:
- Subject, actor, tenant, issuer/audience, and delegation authorization contract:
- Token verifier, signing keys, exchange service, or trusted claim-mapping owner:
- Header allowlist, client-header sanitization, and user-ID delegation permissions:
- Direct-Runtime bypass, forged claims, wrong audience, and cross-tenant denial evidence:
- Separate outbound tool authorization and consent requirements; tool-gateway JWT audience and user mapping to the Consent portal:

## Landing-zone observability — Module 1 account and tenant boundaries

- Reference Transaction Search/GenAI observability or optional Omni/other OTel backend, and operational need:
- Account domain or Organization domain; space account/Region; domain URL and application-Domain distinction:
- Local spaces / central operations / hybrid; centralization source/destination, residency, and owner:
- Transaction Search per source/receiver, Dataset integration, permitted log groups, and replication evidence:
- IAM Identity Center Region/replication or IAM sign-in; application authentication handled separately:
- Viewer/Editor/Admin/Custom grants, group-grant union, space role, and denied-access checks:
- Tenant boundary; metrics/dataset/export/AI-feature coverage beyond log/trace data scopes:
- CloudWatch agent / existing collector / ADOT / OpenInference per component; SDK/version and one tracer-provider owner:
- Trace propagation, service/environment/version, session.id, workload/tool/skill/job attribution:
- Captured fields, capture-time redaction, log masking, judge data path, and synthetic test evidence:
- Source/Dataset/centralized-copy/export retention and deletion; KMS policies and key owner:
- Trace sampling versus online-evaluation sampling; span-derived metric limitations and cost/billing evidence:
- Dashboard/query, alarm versus Omni alert, profile, thresholds, NODATA handling, notification/recovery evidence:
- Optional DevOps Agent/skills integration, permissions, operator review, and investigation ownership:
- CDK/Terraform support, configuration receipt, manual/API steps, known gaps, and workload onboarding contract:

## Approaches considered

| Approach | Prerequisites | Integration work | Operational responsibility | Limitations | Qualification status |
| --- | --- | --- | --- | --- | --- |
| A | | | | | Proposed |
| B | | | | | Proposed |

## Decision

- Selected approach:
- Source availability (available code / partial / planned integration):
- Delivery status (unqualified / qualified package / verified here):
- Reason it fits the requirements:
- Default assumption, if used:
- Other supported candidates and trade-offs, if any:
- Unsupported requirement or missing recipe, if any:
- Source/package revision:
- Configuration revision:
- Supported component combination:
- Unresolved prerequisites:

## Build and integration

- Qualified procedure:
- Deployment or resource references:
- Identity and scope mapping:
- Catalog/resource binding:
- Consumer compatibility:
- Recovery target:

## Common acceptance checks

| Check | Expected result | Actual result | Evidence reference |
| --- | --- | --- | --- |
| Required capability | | Pending | |
| Allowed access | | Pending | |
| Denied access | | Pending | |
| Integration with consumers | | Pending | |
| Failure and recovery | | Pending | |
| Required telemetry | | Pending | |

Record whether the implementation exists before assessing its acceptance evidence. A planned integration or its design does not satisfy a core build checkpoint. A qualified alternative must meet the agreed capability and control criteria; completing another approach does not duplicate core points.

## Handoff and cleanup

- Verification outcome and reviewer:
- Known limitations:
- Consumer handoff:
- Resources created:
- Cleanup procedure:
- Retained resources and authorized owner:
