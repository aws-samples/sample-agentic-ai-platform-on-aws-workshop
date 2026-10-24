# LiteLLM proxy configuration for the workshop model gateway (build contract section 7).
# Rendered by foundation/foundation/model_gateway_stack.py: {{GUARDRAIL_ID}} and {{GUARDRAIL_VERSION}} are replaced with the
# baseline guardrail from the SharedServices stack and {{REGION}} with the deployment region, then the file is uploaded to the
# config bucket as config.yaml.
# Prices are explicit because v1.104.1's local price map predates these models (values: LiteLLM's own for the us. profiles).
# Every alias is a `us.` cross-Region inference profile reached through the Bedrock Converse API; three vendors, one gateway.
model_list:
  - model_name: claude-opus-5
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-opus-5
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 5.5e-06
      output_cost_per_token: 2.75e-05
  - model_name: claude-sonnet-5
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-sonnet-5
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 2.2e-06
      output_cost_per_token: 1.1e-05
  - model_name: gpt-6-1-sol
    litellm_params:
      model: bedrock/converse/us.openai.gpt-6.1-sol
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 2.2e-06
      output_cost_per_token: 1.1e-05
  - model_name: nova-2-lite
    litellm_params:
      model: bedrock/converse/us.amazon.nova-2-lite-v1:0
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 3.3e-07
      output_cost_per_token: 2.75e-06

  - model_name: claude-opus-4-8
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-opus-4-8
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 5.5e-06
      output_cost_per_token: 2.75e-05
  - model_name: claude-fable-5
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-fable-5
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 1.1e-05
      output_cost_per_token: 5.5e-05
  - model_name: claude-fable-5-1
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-fable-5-1
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 1.1e-05
      output_cost_per_token: 5.5e-05
  - model_name: claude-opus-5-5
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-opus-5-5
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 4.4e-06
      output_cost_per_token: 2.2e-05
  - model_name: claude-sonnet-5-5
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-sonnet-5-5
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 2.2e-06
      output_cost_per_token: 1.1e-05
  - model_name: claude-haiku-5-5
    litellm_params:
      model: bedrock/converse/us.anthropic.claude-haiku-5-5
      aws_region_name: {{REGION}}
      guardrailConfig:
        guardrailIdentifier: "{{GUARDRAIL_ID}}"
        guardrailVersion: "{{GUARDRAIL_VERSION}}"
        trace: enabled
      input_cost_per_token: 1.1e-07
      output_cost_per_token: 5.5e-07

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  store_model_in_db: false
  database_connection_pool_limit: 5
  proxy_batch_write_at: 10

litellm_settings:
  drop_params: true
