# Build an Agentic AI Platform with Amazon Bedrock AgentCore

**[UNDER ACTIVE DEVELOPMENT]**

A one-day, hands-on workshop. 

Attendees will learn to design and architect an enterprise agentic AI platform using Amazon Bedrock AgentCore. 

## Overview

This workshop will guide you to build a functional platform (MVP) on a pre-deployed control plane with UI, set up governed model and tool access, manage asset registry, and build an HR assistant using a blueprint and promote from development to production with quality gates, human-in-the-loop approval, and spending caps.

Start with the **[learning path](docs/v2/course/index.html)** (open it in a browser) and walk through the guided learning modules covering key decisions and hands-on steps.

## Quick start

**(1) Workshop Studio path**   
Attend the instructor-led workshop in person. This path uses an AWS Workshop Studio account with the control plane (platform UI) and the sample data already deployed.

The learning contents in [Get started](docs/v2/course/index.html#start) will guide attendees through setting up and deploying the rest, module by module, while placing weights on learning the design decisions, options and trade-offs.


```bash
git clone <repository URL>                              

cd workshop-agentic-ai-platform
uv sync                                                 

npm install                                             
source .venv/bin/activate.   
# Windows PowerShell: .venv\Scripts\Activate.ps1 (or prefix commands with `uv run`)

# set the AWS region to "us-east-1"
export AWS_DEFAULT_REGION=us-east-1
workshop status              # eight stacks: four there (control plane, sample data), four to come (landing zone in module 1, IdP in module 2)

workshop apply 1             # Module 1: the landing zone

workshop apply 2             # Module 2: the three personas

...

# continue onto workshop apply 7
```

**(2) Self-paced path**    


```bash
git clone <repository> && cd workshop-agentic-ai-platform

# Build platform UI stack (frontend/dist)
uv sync && npm install
(cd frontend && npm ci && npm run build)

# Run once per account and region
export AWS_PROFILE=<your profile> 
export AWS_DEFAULT_REGION=us-east-1
(cd foundation && npx cdk bootstrap)      

# Setup all eight stacks at once (~20 minutes)
scripts/deploy.sh                         

# Reveals the three persona account login once every stack is deployed; 
# Sign in once, then Switch persona in the user menu
uv run python scripts/doctor.py --show-passwords    
```

**(3) Workshop deployer (CloudFormation)**

Alternatively, you may deploy the same foundation as one CloudFormation stack with the Workshop Studio deployer (`workshop-studio/deployer.yaml`).

CodeBuild runs the CDK deployment inside the account: about 20 minutes in a fresh account. 


## Repository layout

| Folder | Description |
| --- | --- |
| `foundation/` | CDK application (Python), eight stacks: Network, SharedServices, ModelGateway, Web, Auth, Platform, Scenario and Idp (Keycloak, module 2) |
| `platform_sdk/` | Shared Python package: configuration, names, platform API client, blueprint renderer, AgentCore, LiteLLM, evaluation, metrics |
| `backend/` | Platform API and worker Lambda code (bundled into `build/backend` by `scripts/build_backend.py`) |
| `frontend/` | Platform control plane web UI (Vite + React + TypeScript);`frontend/docs/screens` holds the UI screenshots |
| `scenario/` | HR assistant content: data, tools, knowledge documents, blueprint and prompts, evaluation dataset, risk register |
| `scripts/` | `deploy.sh`, `doctor.py/.sh`, `cleanup.py/.sh`, `build_backend.py`, `package_for_workshop_studio.py`, lifecycle and dry-run tests |
| `workshop-studio/` | `deployer.yaml`, the attendee IAM policy, and the event instructions |
| `docs/` | `docs/v2/course/`: the course (HTML), module pages, architecture diagrams, cleanup; `workshop-guide.html` (attendee overview) and `facilitator-guide.html` (facilitator checklist) |


## Documentation

| Document | For | Content |
| --- | --- | --- |
| [docs/v2/course/index.html](docs/v2/course/index.html) | everyone | An overview of the workshop;<br/>[Get started](docs/v2/course/index.html#start) has the prerequisites, the learning path, the checks and setup troubleshooting |
| [docs/v2/course/module-*-rev.html](docs/v2/course/module-1-rev.html) | attendees | One page per module: explain, decisions with options and trade-offs, hands-on with `workshop apply N`, the platform after the module |
| [docs/v2/course/cleanup.html](docs/v2/course/cleanup.html) | attendees | `workshop cleanup`, the stacks, what remains, cost |
| [docs/workshop-guide.html](docs/workshop-guide.html) | attendees | Workshop learning objectives, architecture diagram, AWS services, hands-on, agenda and glossary |
| [docs/facilitator-guide.html](docs/facilitator-guide.html)| facilitator | The facilitator run-through and day-of runbook |
| [foundation/README.md](foundation/README.md)<br/>[frontend/README.md](frontend/README.md)<br/>[platform_sdk/README.md](platform_sdk/README.md)<br/>[scenario/README.md](scenario/README.md)<br/>[workshop-studio/README.md](workshop-studio/README.md) | engineers | Per-component details |

## Development checks

```bash
uv run pytest -q                 

uv run ruff check . && uv run ruff format --check .

(cd frontend && npm test && npm run typecheck && npm run build)
(cd foundation && npx cdk synth)
```
