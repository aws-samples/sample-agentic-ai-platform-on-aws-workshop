# The `workshop` command

One command per module of the learning path. It reads `design-record.yaml` at the repository root, builds that module's objects
in your account (idempotently, adopting what exists) and prints what happened and where to look. It reuses `platform_sdk` and
records the ids and ARNs it created in the state file `.workshop/state/<project>.json` (git-ignored), which later modules and
`workshop cleanup` read.

Before the day, the account holds the **control plane** (web UI, API, state, worker, and its login: the Cognito user pool and app
client) and the **sample HR data**: the Workshop Studio deployer, `scripts/deploy.sh --pre-day` or `workshop deploy pre-day`. The
learners deploy the rest: the landing zone in module 1 and the personas in module 2, then every project-level object.

```bash
uv sync && npm install                    # the `workshop` script into .venv; the pinned CDK CLI for module 1 (Node 20 or later)
source .venv/bin/activate                 # then `workshop ...` works in this terminal; without it, prefix each command with `uv run`
export AWS_PROFILE=<profile> AWS_DEFAULT_REGION=us-east-1
workshop record                    # the effective inputs and your calls (no AWS access needed)
workshop status                    # the eight stacks: four there, four to come (the landing zone in module 1, the IdP in module 2)

workshop apply 1                   # cdk deploy of Network, SharedServices, ModelGateway (about 20 min: start it as the module begins), then the inspection
workshop apply 2                   # the three groups and users in the user pool, their passwords, the sign-ins, the 403, the role template
workshop apply 3                   # domains and projects (roles, model-gateway teams)
workshop apply 4                   # registry, gateways, targets, records, policy engine and policies (LOG_ONLY)
workshop call --as hr-dev get_leave_balance E1001
workshop call --as finance-dev get_leave_balance E1001      # HTTP 403 from IAM
workshop call --as hr-dev submit_leave_request E1001 15     # logged in LOG_ONLY, denied (-32002) after ENFORCE
workshop mode ENFORCE --gateway hr-dev                      # or the Governance screen
workshop apply 5                   # the Dev key, the token vault, the first call, spend
workshop cap --dev low && workshop ask "What is my leave balance? My employee id is E1001." && workshop cap --dev restore
workshop approve catalogue         # or the Catalogue screen
workshop inventory                 # platform complete
workshop apply 6                   # memory, blueprint v1, Dev agent, release 1 + evaluation (BLOCKED)
#   set prompt_version: v2 in design-record.yaml
workshop apply 6                   # blueprint v2, release 2 + evaluation (PENDING_APPROVAL)
workshop approve release && workshop promote        # or the Releases screen as Domain approver
workshop traffic --prod 3
workshop apply 7                   # Prod traffic, metrics, feedback, online evaluation
workshop scores
workshop export --strands          # or --langgraph (LangChain 1.x is a default dependency group), add --run to execute it once; writes exports/

workshop cleanup --dry-run         # the inventory of what the modules built, by name
workshop cleanup                   # remove it in reverse order (about 10 min, mostly AgentCore deleting in the background)
workshop cleanup --stacks          # then cdk destroy --all and the sweep (about 13 min): the account as you found it
```

The clean-up tutorial is `docs/v2/course/cleanup.html`; `workshop cleanup` removes what the modules built, in reverse order, then
the state file and the exports (stack-level details: `foundation/README.md`, Time and cost).

Facilitators: `workshop deploy pre-day | landing-zone | all` runs the same `cdk deploy` for a stack set (the bootstrap and, for the
Web stack, the frontend build happen when missing); `scripts/deploy.sh` does the same from a shell.
