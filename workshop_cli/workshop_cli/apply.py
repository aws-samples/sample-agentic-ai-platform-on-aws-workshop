"""`workshop apply N`: dispatch to the module builders. Every builder is idempotent: it adopts what exists."""

from __future__ import annotations

import time

from .context import Context, head, say

TITLES = {
    1: "Landing zone: deploy the network, the shared services and the model gateway",
    2: "Identity: the three personas, the corporate IdP (Keycloak) and the on-behalf-of credential providers",
    3: "Control plane and governance: onboard the domains and projects",
    4: "Tool plane: gateways, targets, catalogue records, policies; the on-behalf-of gateway",
    5: "Model gateway: the Dev key, the token vault, spend",
    6: "AgentOps: memory, blueprint, Dev agent and its on-behalf-of twin, release and its evaluation",
    7: "Operate: Prod traffic, metrics, online evaluation, feedback",
}


def run(ctx: Context, module: int, skip_evaluation: bool = False, run_doctor: bool = True, deploy: bool = True) -> int:
    from . import apply_3, apply_4, apply_5, apply_6, apply_7, apply_foundation

    started = time.monotonic()
    head(f"workshop apply {module}: {TITLES[module]}")
    say(f"record {ctx.record_path}")
    if module == 1:
        apply_foundation.landing_zone(ctx, run_doctor=run_doctor, deploy=deploy)
    elif module == 2:
        apply_foundation.identity(ctx, deploy=deploy)
    elif module == 3:
        apply_3.run(ctx)
    elif module == 4:
        apply_4.run(ctx)
    elif module == 5:
        apply_5.run(ctx)
    elif module == 6:
        apply_6.run(ctx, skip_evaluation=skip_evaluation)
    elif module == 7:
        apply_7.run(ctx)
    minutes = (time.monotonic() - started) / 60
    ctx.mark(f"apply_{module}", minutes=round(minutes, 1))
    say(f"\nworkshop apply {module} done in {minutes:.1f} min")
    return 0
