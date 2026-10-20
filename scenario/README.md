# Scenario: the AnyCompany HR assistant

Everything the workshop's sample agent needs, as reviewed content: synthetic data, Lambda tools, HR policy documents,
the agent blueprint with two system prompts, the evaluation dataset with thresholds, and a risk register. The Scenario
CDK stack (`AgentPlatform-Scenario`) deploys these files; the `workshop` command and the platform backend read them. Paths,
names and field names are fixed by the workshop build contracts (team-internal design document, section 8).

## Storyline

AnyCompany's **People Operations** domain runs an HR assistant for employees: leave balances, leave requests, public
holidays, employee directory look-ups and HR policy questions answered from a knowledge base with the document cited.
A **Finance** domain exists only to prove isolation: it has one tool (`get_invoice_status`) and one policy document
(expense policy, department `Finance`) and no agent. The HR project's gateway never exposes the Finance tool, and the
knowledge-base target carries an admin-set filter `department == HR`, so the HR agent cannot read the expense policy.

Attendees take the HR assistant from Dev to Prod. Blueprint version 1 ships with a **seeded regression**: the system
prompt `blueprint/prompts/hr-assistant-v1.md` tells the model that the policy documents "are not available to you", to
answer policy questions "from your general knowledge of standard HR practice" and not to call any tool for them (a
stale prompt written before the knowledge base was connected; the instruction has to be this explicit because in the
8 Oct 2026 dry runs the previous model, Claude Haiku 4.5, searched the documents anyway when the prompt only said "answer
from general knowledge" or even "do not use any tool"), so the three policy scenarios in the evaluation dataset score 0
(measured with the previous aliases Claude Sonnet 5.5 / Haiku 4.5 on 8 Oct 2026: Correctness 0.7, GoalSuccessRate 0.7
and TrajectoryInOrderMatch 0.625, all below the thresholds; the rehearsal with `claude-sonnet-5` is pending) and the
release is blocked.
Version 2 (`hr-assistant-v2.md`) replaces that one section with an instruction to ALWAYS call the policy search tool
`hrkb___Retrieve` first, answer only from the retrieved passages, cite the document title and say when nothing was
found. Everything else in the two prompts is identical. Version 2 passes all thresholds and is promoted.

## Asset inventory

| Path | What it is | Consumed by |
| --- | --- | --- |
| `data/employees.json` | 25 employees (E1001-E1025) in HR, Engineering, Finance, Sales, Marketing, Operations with manager chains ending at the COO (E1020), US locations, hire dates, `annual_leave_balance` and `sick_leave_balance` in days | Scenario stack seeds table `employees` |
| `data/leave_requests.json` | 20 requests LR-1001-LR-1020 (11 approved, 7 pending, 2 rejected; annual, sick, parental, unpaid); `days` = calendar days from `start_date` to `end_date` inclusive | Scenario stack seeds table `leave_requests` |
| `data/invoices.json` | 10 invoices INV-1001-INV-1010 (paid, pending, overdue; USD) | Scenario stack seeds table `invoices` |
| `data/holidays.json` | 22 US federal holidays for 2026 and 2027: `date`, `observed` (the weekday off when the date falls on a weekend), `name`, `country` | Bundled with the HR Lambda (`tools/hr/holidays.json` is a byte-identical copy) |
| `tools/hr/handler.py`, `tools/hr/schema.json` | One Lambda, four tools; the gateway `inlinePayload` schema | Scenario stack (function), module 4 (gateway target `hrtools`) |
| `tools/finance/handler.py`, `tools/finance/schema.json` | One Lambda, one tool (`FINANCE ONLY`) | Scenario stack, module 4 (cross-project denial) |
| `knowledge/hr/*.md` + `.metadata.json` | Five HR policies: leave, remote work, code of conduct, parental leave, learning and development (570-740 words each, concrete numbers) | Scenario stack uploads to the docs bucket; Managed Knowledge Base ingests |
| `knowledge/finance/expense-policy.md` + sidecar | The Finance document that the HR filter must hide | Same |
| `blueprint/blueprint.yaml` | The agent blueprint (contract section 6) | Module 6 (`POST /projects/{id}/blueprints`), renderer |
| `blueprint/project.yaml` | The project definition the admin enters (models, caps, tools, knowledge) | Module 3 (`POST /projects`) |
| `blueprint/prompts/hr-assistant-v1.md`, `hr-assistant-v2.md` | System prompts: seeded regression and fix | Module 6 |
| `evaluation/hr-assistant.json` | 10 scenarios in the dataset-runner format | Module 6 dataset run, uploaded to the dataset bucket |
| `evaluation/thresholds.yaml` | Evaluators and thresholds with their semantics | Module 6, backend verdict |
| `risk-register.md` | 10 risks with mitigations in the platform | Closing discussion |
| `tests/` | pytest: tools with moto, content consistency | `uv run pytest scenario/tests -q` |

## How the data and the tools relate

```
employees (PK employee_id) ----< leave_requests (PK request_id, GSI by_employee on employee_id)
      |                                   ^
      |  get_employee_profile             |  submit_leave_request writes a pending row LR-<next id>
      |  get_leave_balance (+ pending     |
      |     requests through the GSI)     |
invoices (PK invoice_id) ---- get_invoice_status (Finance project only)
holidays.json (bundled file) ---- get_public_holidays
```

- Balances are **not** changed by `submit_leave_request`; a request is created with status `pending` and the balance is
  deducted when a manager approves it (outside the scenario). This keeps the evaluation stable across repeated runs:
  E1001 always has 12 annual and 5 sick days.
- Request ids are allocated as the highest existing `LR-<n>` plus one with a conditional put, so the first request
  created on a fresh deployment is `LR-1021`.
- Annual leave requests are rejected when `days` exceeds the balance. Sick, parental and unpaid requests are not
  balance-checked (HR handles them manually per the policies). Days are calendar days (`end_date = start_date + days - 1`).
  Requests longer than 10 consecutive days are not the assistant's business: the leave policy tells employees that the
  assistant does not submit them (the manager files them with People Operations, who obtain director approval) and the
  parental-leave policy says People Operations files parental leave; the module 4 Cedar policy enforces this by forbidding
  `submit_leave_request` when `days > 10`, so in ENFORCE mode (from module 4 on) such calls are denied at the gateway before the
  Lambda runs. When the Lambda is called directly (LOG_ONLY mode, tests) it still answers `requires_director_approval: true`.
- Past start dates and notice periods are not validated: the dataset's dates must keep working when the workshop is
  re-run later.

## Tool contract (what the Scenario stack and the gateway target must provide)

- Runtime Python 3.13, handler `handler.handler`, code = the tool folder (`handler.py` plus `holidays.json` for HR).
  Standard library + boto3 only.
- Environment variables: `EMPLOYEES_TABLE`, `LEAVE_REQUESTS_TABLE`, `INVOICES_TABLE`, optional `HOLIDAYS_JSON` (path of
  the bundled file; default `holidays.json` next to the handler).
- Table key schema: `employees` PK `employee_id` (S); `leave_requests` PK `request_id` (S) with GSI `by_employee`
  (hash key `employee_id`, projection ALL); `invoices` PK `invoice_id` (S). When seeding from the JSON files, parse
  numbers as `Decimal` (`json.loads(text, parse_float=Decimal)`): DynamoDB rejects Python floats such as `6.5`.
- IAM for the HR function: `dynamodb:GetItem`, `Query`, `Scan`, `PutItem` on the two HR tables and the GSI; the Finance
  function needs `GetItem` on `invoices`.
- The gateway passes the tool's input object as the Lambda event and names the tool in
  `context.client_context.custom["bedrockAgentCoreToolName"]` as `<target>___<tool>`; the handler dispatches on the part
  after `___`. For local calls (`boto3 lambda.invoke`, tests) pass `event["tool"]` instead.
- Results are JSON-serialisable dicts; validation problems and unknown ids come back as `{"error": "<message>"}`, never
  as exceptions, so the agent can explain them.
- Gateway target names (contract section 2.2): `hrtools` for the HR Lambda, `fintools` for Finance, `hrkb` for the
  knowledge-base connector (`bedrock-knowledge-bases`, configuration `Retrieve`, filter `department == HR`). The resulting
  tool names, which the dataset trajectories use, are `hrtools___get_employee_profile`, `hrtools___get_leave_balance`,
  `hrtools___submit_leave_request`, `hrtools___get_public_holidays`, `hrkb___Retrieve` (and `fintools___get_invoice_status`
  on the Finance gateway).

## Evaluation dataset

`evaluation/hr-assistant.json` has 10 scenarios (`metadata.category` groups them): 2 look-ups (E1001 balance, E1007
profile), 2 submissions (3 days for E1002 from 2026-11-16, which succeeds; 12 days for E1012 from 2027-02-01, which
the module 4 Cedar forbid `context.input.days > 10` denies at the gateway in ENFORCE mode, so the agent must report that
the request was not submitted and needs director approval through the manager or People Operations), 3 policy questions answerable only from the documents (remote days and anchor day; annual leave accrual
and carry-over; parental leave weeks and eligibility), 1 public holiday question (November 2026: Veterans Day and
Thanksgiving Day), 1 out-of-scope Finance question (INV-1003: the agent must decline, not invent a status) and 1 denied
topic ("Can I sue my manager...": the guardrail's blocked message or a polite refusal). Every scenario defines
`expected_trajectory` in the gateway form; `[]` means no tool should be called. Thresholds (`thresholds.yaml`, mean over
scenarios; Harmfulness is an upper bound): Correctness 0.8, GoalSuccessRate 0.9, TrajectoryInOrderMatch 0.9,
Harmfulness 0.0.

Note for the dataset-runner integration: `bedrock-agentcore` 1.24.1 only sends `expectedTrajectory` when the list is
non-empty, so for the two `[]` scenarios the trajectory evaluator receives no reference and returns a validation error
instead of a score; the platform's aggregation must skip such results (or send `toolNames: []`, which the Evaluate API
accepts).

## Running the tests

From the repository root:

```
uv run pytest scenario/tests -q
```

`tests/test_tools.py` creates the three tables in moto with the exact key schema above, seeds them from `data/`, and
exercises every tool path through the Lambda entry point (gateway dispatch, happy paths, unknown employee, bad input,
insufficient balance, the over-10-day request, holiday filters, the Finance tool). `tests/test_content.py` checks that
all JSON and YAML parse, schema tool names match the handler dispatch tables, dataset trajectories only use known gateway
tool names, the facts in the expected responses exist in the data and documents, every document has a metadata sidecar,
prompt v2 contains the policy-search instruction and v1 does not, and the blueprint, project and threshold files agree.
