# Workshop model options

The normal bootstrap deploys these model aliases through the existing model
gateway configuration. **Claude Sonnet 5 remains the HR reference default.** Availability
still depends on the AWS account, Region and provider access; a configured alias
is not proof that the model can be invoked.

| Model | Alias | Bedrock inference profile |
| --- | --- | --- |
| Claude Opus 4.8 | `claude-opus-4-8` | `us.anthropic.claude-opus-4-8` |
| Claude Opus 5 | `claude-opus-5` | `us.anthropic.claude-opus-5` |
| Claude Sonnet 5 **(default)** | `claude-sonnet-5` | `us.anthropic.claude-sonnet-5` |
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
models: [claude-sonnet-5, gpt-6-1-sol]
model: claude-sonnet-5
```

Use the existing `workshop apply 3`, `workshop apply 4` and `workshop apply 5`
steps. They set project permissions, publish the selected model's catalogue
record and verify its Dev access. Approve that record before releasing an agent.
Changing the design record does not change existing deployed agents or releases.

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
