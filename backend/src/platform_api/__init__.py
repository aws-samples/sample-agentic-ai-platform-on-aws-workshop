"""Platform API package (build contract section 4).

    app.handler  ->  router.dispatch(event)  ->  auth.require(principal, route.roles)  ->  handlers.<resource>.<fn>(request)

Modules: `router` (route table + matching + JSON plumbing), `auth` (Principal from the Cognito claims), `errors` (ApiError and the
mapping of upstream exceptions), `db` (DynamoDB single table), `settings` (defaults merged with the SETTINGS item), `validation`
(body/query helpers), `jobs` (JOB items + worker dispatcher), `worker_jobs` (deploy / promote / invoke handlers), `handlers/` (one module per
resource) and `services/` (LiteLLM provisioning, project roles, Agent Registry, governance, spend, harness lifecycle, metrics).
"""

__version__ = "0.6.0"
