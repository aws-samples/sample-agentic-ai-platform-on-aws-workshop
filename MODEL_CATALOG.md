# Workshop model options

The normal bootstrap deploys these model aliases through the existing model
gateway configuration. **Claude Opus 5 is the HR reference default.** Availability
still depends on the AWS account, Region and provider access; a configured alias
is not proof that the model can be invoked.

| Model | Alias | Bedrock inference profile |
| --- | --- | --- |
| Claude Opus 4.8 | `claude-opus-4-8` | `us.anthropic.claude-opus-4-8` |
| Claude Opus 5 **(default)** | `claude-opus-5` | `us.anthropic.claude-opus-5` |
| Claude Sonnet 5 | `claude-sonnet-5` | `us.anthropic.claude-sonnet-5` |
| Claude Fable 5 | `claude-fable-5` | `us.anthropic.claude-fable-5` |
| Claude Fable 5.1 | `claude-fable-5-1` | `us.anthropic.claude-fable-5-1` |
| Claude Opus 5.5 | `claude-opus-5-5` | `us.anthropic.claude-opus-5-5` |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` | `us.anthropic.claude-sonnet-5-5` |
| Claude Haiku 5.5 | `claude-haiku-5-5` | `us.anthropic.claude-haiku-5-5` |
| GPT-6.1 Sol | `gpt-6-1-sol` | `us.openai.gpt-6.1-sol` |
| Nova 2 Lite | `nova-2-lite` | `us.amazon.nova-2-lite-v1:0` |

## Select a model

Before Modules 3–4, edit `module_3.inputs` in `design-record.yaml`. Include your
chosen alias in `models` and set `model` to that alias. For example:

```yaml
models: [claude-opus-5, gpt-6-1-sol]
model: claude-opus-5
```

When the record or these fields are omitted, the CLI falls back to the same
selection: `models: [claude-opus-5, gpt-6-1-sol]` and `model: claude-opus-5`.
To select a different model, include its configured alias in `models` and set
`model` to that alias.
A record that already names its model keeps that selection.

Use the existing `workshop apply 3`, `workshop apply 4` and `workshop apply 5`
steps. They set project permissions, publish the selected model's catalogue
record and verify its Dev access. Approve that record before releasing an agent.
Changing the design record does not change existing deployed agents or releases.

## Switch an existing deployment

An event that already ran with another model keeps working
while you switch. Nothing is deleted automatically:

1. Set `models` and `model` in `design-record.yaml`, then run `workshop record`.
2. `workshop apply 3` adds the new alias to the project's models. Aliases
   already enabled stay enabled.
3. `workshop apply 4` publishes the new model's catalogue record. A platform
   admin approves it in **Catalogue** (fallback: `workshop approve catalogue`).
4. `workshop apply 5` creates the Dev key for the new model and stores it in the
   token vault. Existing keys and credential providers stay active and keep
   counting against the project budget.
5. `workshop apply 6` stores a new blueprint version and starts a release. Prod
   keeps serving the promoted release until an approver approves and promotes
   the new one, which creates the Prod key for the new model (fallbacks:
   `workshop approve release`, then `workshop promote`).

Remove the previous model from the project, or suspend its keys, only after no
deployed agent or release still uses it. `workshop cleanup` removes all of the
project's keys at teardown.

## Add a model before bootstrapping an event

An event owner adds a model in the two existing configuration files:

1. Add its alias, Bedrock model/inference-profile ID, description and USD-per-token
   prices to `MODELS` in [foundation/foundation/config.py](foundation/foundation/config.py).
2. Add the matching `model_name` and Bedrock Converse entry in
   [foundation/litellm/config.yaml.tpl](foundation/litellm/config.yaml.tpl).
   Copy an existing entry's Region placeholders and baseline guardrail binding.
   Keep the model ID and both prices identical to the Python configuration.
3. Run `uv run pytest foundation/tests/test_synth.py -q`, then use the normal
   authorized bootstrap/deployment process with this source revision.

These tests check alias, model ID, price and guardrail consistency. Use a real
model ID available in your event Region; never add credentials to either file.
An existing running event must receive a configuration deployment before it can
use newly added aliases. Its backend also needs a restart or Platform redeployment
to refresh the model configuration cached by running processes.
Updating source alone does not update event packages.
Keep aliases used by existing keys, blueprints or agents until those consumers
have been migrated.
