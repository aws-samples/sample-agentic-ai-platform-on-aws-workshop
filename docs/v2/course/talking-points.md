# Talking points

What to say in each part of the day, in course order. The course pages hold the detail; these points are the thread
the facilitator keeps. Two conventions apply throughout:

- **Recommended** marks the reference option for a production platform; **This workshop** marks what the lab builds.
  They can differ. Say which is which when you present a decision.
- Each decision says what kind it is: **choose one** (options exclude each other), **patterns combine** (used together,
  each in its own situation) or **depends on context** (no universal winner). Do not present a combining pattern as a
  forced choice.

## The 30-minute presentation: Agentic AI Platform

1. **Why a platform (5 min).** Without one, independent teams each repeat sign-in, tool access, model keys,
   guardrails, budgets, release checks and monitoring. The platform builds them once.
2. **Shared capabilities (7 min).** Walk the logical overview (`arch/platform-overview.svg`): identity, governance,
   tool and model access, delivery, operations. Projects consume them through the platform's interfaces.
3. **Who uses it (5 min).** Platform team, project teams, domain approvers. The HR assistant is the agent taken all the
   way to Prod; it runs under its own service identity, and a separate on-behalf-of variant in Dev shows an employee's
   identity reaching a tool. The Finance project has one tool and no agent: it proves isolation and is never released.
4. **How to read a decision (5 min).** Recommended versus this workshop; the three decision kinds.
5. **The build path (5 min).** Seven modules, one `workshop apply N` each, and the design record that names the
   workshop's choices.
6. **Questions (3 min).** Then Get started.

## Get started and module 1 · Landing zone

- Three ways to start: the event account (run `workshop apply 1`), an event account whose landing zone the
  facilitator pre-provisioned with `workshop deploy landing-zone` (run `workshop apply 1 --no-deploy`), or your own
  account after `workshop deploy pre-day`. Standard event provisioning deploys the pre-day stacks only.
- 1.1: the recommended reference is a platform account plus workload accounts; the lab uses one account because an
  event account is one account. Organisations can combine layouts by domain or workload.
- 1.2: both options are private. The lab removes the internet exit entirely, which is why the LiteLLM image is copied
  to ECR and why callers outside the VPC use the relay.
- Ownership, infrastructure and delivery decisions are discussed in module 6, beside the delivery they shape.
- The doctor reports `Keycloak IdP URL: not deployed` until module 2; that is expected.

## Module 2 · Identity

- Frame the module with three goals: identity propagation, scope attenuation (least-privilege delegation to the next
  resource) and autonomous workload identity.
- Be precise about what the lab shows: the on-behalf-of chain binds the token's audience at every hop. OAuth scope
  enforcement is a production design; the realm defines no client scopes and no tool checks one.
- Identity providers support these goals unevenly: Cognito cannot exchange a token, so the lab runs Keycloak as the
  employees' login. Moving the chain to another provider needs validation of RFC 8693 and grant support, exchange
  permissions, claims, audiences and scopes; it is not a URL change.
- 2.2 to 2.4 each use more than one pattern; say which component uses which. 2.2: the Dev and Prod harnesses sign
  with the agent role (A, SigV4, IAM gateways); the on-behalf-of twin presents a token (B, JWT gateway). 2.3: IAM roles
  for AWS calls (the gateway role invokes the tools, LiteLLM's task role calls Bedrock), the vault's API keys for
  LiteLLM, OAuth2 providers for the two exchanges: all three are used. 2.4: the twin uses A, the exchange; the Dev and
  Prod agents use C, a service identity; B, raw token forwarding, is never used. Limits stay: no scope check anywhere,
  no Cedar on the on-behalf-of route, and the HR tool still trusts the `employee_id` argument.
- Cognito stays the platform's sign-in for the three personas. The recommended reference is a corporate IdP brokered
  into it.
- After `workshop apply 2`, the doctor prints the Keycloak URL and its admin console link; passwords stay in Secrets
  Manager.

## Module 3 · Control plane and governance

- Governance levels are scopes that complement each other: domain (release approval), platform (models, guardrail,
  policy engine, catalogue approval, default caps) and organisation (SCPs, discussed, not deployed). In the HR example,
  the project owns its blueprint and thresholds within the platform's allow-lists, roles, keys and budgets.
- 3.3, the catalogue: A is one organization-wide registry shared across accounts (recommended with the platform and
  workload accounts of 1.1); B, the lab, is one registry in one account with no automatic visibility from other
  accounts; C keeps a registry per account or tenant, and discovery across them is your own code, not a managed
  federation.
- Catalogue approval is a label today; learners approve the records in module 5's hands-on.
- Approvals moved to module 6; the personas used in this module's hands-on are unchanged.

## Module 4 · Tool plane

- 4.1: topology (per project, per domain, one platform gateway, platform plus domain) is separate from environment
  isolation. Topologies coexist. The lab builds one gateway per project, once for Dev and once for Prod; there is no
  staging environment.
- Both routes to a tool are used: IAM SigV4 to Lambda targets on the Dev and Prod gateways, and token exchange to the
  HR API on the on-behalf-of gateway. Passthrough is never used.
- 4.2: walk the figure top to bottom. The top row is conceptual; the two rows below are what the lab builds: IAM with
  Cedar, and the on-behalf-of route with two exchanges (T0 to T1, then T1 to T2 at the gateway) and no policy engine.
  The identity provider decides trust (workload identity, trusted clients, audiences and scopes, consent, exchange
  permissions); the gateway's Cedar policies add business rules; the resource server enforces what it requires.
  Scopes restrict a tool call only where the gateway or tool enforces them.
- Business rules such as "a payment above 10,000 needs a verified approval" or "support access expires after two hours
  by a trusted clock" are reference patterns. The lab's only conditional rule is the leave-days limit; there is no
  transaction-amount, approval or time-limited rule and no per-user data isolation. The on-behalf-of gateway has no
  policy engine.
- "How tools and knowledge connect", after 4.2, is background rather than a decision: the target types (Lambda,
  OpenAPI with token exchange, knowledge-base connector), the project allow-list and the pinned knowledge filter.
- 4.3: recommend LOG_ONLY during development and testing, ENFORCE in production. In the lab one `gateway_mode` applies
  to all three IAM gateways, HR Prod included; learners flip HR Dev to ENFORCE, which module 6 requires.

## Module 5 · Model gateway

- 5.1 compares two paths: a model gateway product (recommended, and the lab's LiteLLM) and direct Bedrock calls with an
  IAM allow-list. Choose the governed path per workload.
- 5.2: separate budget scopes from enforcement (`arch/budget-layers.svg`). The lab enforces a project team cap and a
  capped key per agent, environment and model; no cap covers a project's other services.
- 5.3: the data class decides where a request may be processed. Cross-account LiteLLM fan-out
  (`arch/model-fanout.svg`) is a pattern to evaluate, not built here: it can add usable capacity once each account's
  quotas, model access and failure domains are validated, and it does not change where a request may be processed.

## Module 6 · Project blueprints and delivery

- 6.1: degrees of project freedom coexist: the blueprint path (recommended default, the lab), a provisioned environment
  where the team brings its own code and pipeline, a stack per project, or a full platform copy.
- 6.2: three complementary infrastructure and delivery patterns, all used here: A, platform-governed infrastructure as
  code for the shared foundation; B, workload delivery from blueprints through the control plane and the pipeline; C,
  development and prototyping on the laptop (`workshop export --run`). C is never a path to Prod on its own.
- 6.3: which operating model fits each workload: centralised, federated or hybrid. The question is accountability and
  support: who is on call, where an incident escalates, who accepts behaviour. The lab is federated.
- 6.4: CI/CD pipeline ownership follows the path from 6.1: one platform-owned pipeline here; a project build pipeline
  handing releases to the platform gate is discussed, not built.
- 6.5: gates and approvals stack. Built: the evaluation and human approval. Code quality, static and dynamic security
  scans and penetration tests are recommended for code paths and are not built. Approval patterns combine by
  environment and risk; the API checks the approver group, not whether the approver also submitted the release.
- The pipeline in one line: Source, then Validate (DeployDev and Evaluate under one lock), then Approve, then Promote, all
  in the same execution. A failed threshold ends the execution as BLOCKED; a fix is a new release. Approve and Promote
  are separate manual gates, both answered by the Domain approver.
- The course calls the module "Project blueprints and delivery"; the artifact kinds stay `Project` and
  `AgentBlueprint`, and the command output still prints its older module label.

## Module 7 · Operate and observe

- 7.2: offline (a dataset), online (live traffic) and feedback (users) combine in different scenarios; the figure gives
  them equal weight, and the lab runs all three. Timing is a separate question (in the pipeline, on demand, continuous
  sampling, periodic batch). The lab runs the offline dataset in the pipeline and registers online evaluation at
  promotion; there is no switch for offline only.
- 7.3: metadata only is the recommended production default; the lab keeps full content because its data is synthetic.
- The recap turns each workshop choice into a production action with an owner, written as the backlog in the design
  record.
