"""`workshop`: the command behind the hands-on steps of the learning path.

workshop record                      show the effective inputs and your calls (no AWS access needed)
workshop apply 1 .. 7                build one module from design-record.yaml (idempotent; re-run any time)
                                     1 deploys the landing zone (about 20 min: start it as the module begins)
                                     2 also deploys the corporate IdP, Keycloak (about 8 min: start it as the module begins)
workshop obo ["prompt"]              the on-behalf-of chain as far as it exists: module 2 the exchange, 4 the gateway, 6 the harness
workshop call --as hr-dev get_leave_balance E1001
workshop cap --dev low | restore | 0.5
workshop ask --env dev "What is my leave balance? My employee id is E1001."
workshop mode ENFORCE [--gateway hr-dev]
workshop approve catalogue | release [--release-id ID]      fallbacks for the UI steps
workshop promote [--release-id ID]
workshop register-agents             the promoted Prod agents as AGENT records of the catalogue (fallback for the Catalogue button)
workshop inventory                   every platform object, by name ("platform complete")
workshop traffic --prod 3
workshop scores                      online evaluation results so far
workshop export --strands | --langgraph [--run]
workshop deploy pre-day | landing-zone | idp | all           facilitators: cdk deploy of a stack set
workshop status                      the eight stacks and their CloudFormation status
workshop cleanup [--dry-run] [--yes] [--stacks]              remove what the modules built; --stacks destroys the platform too
"""

from __future__ import annotations

import argparse
import sys
import traceback

from . import __version__
from .context import WorkshopError, build_context, print_exception_hint


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="workshop", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--record", help="path of design-record.yaml (default: the repository root)")
    parser.add_argument("--region", help="override the platform region (default: the configuration's)")
    parser.add_argument("--version", action="version", version=f"workshop {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("record", help="print the effective inputs and the recorded calls")

    p = sub.add_parser("apply", help="build one module from the record")
    p.add_argument("module", type=int, choices=range(1, 8), metavar="MODULE", help="1 to 7")
    p.add_argument(
        "--skip-evaluation", action="store_true", help="module 6: create the release without running the dataset"
    )
    p.add_argument("--no-doctor", action="store_true", help="module 1: skip scripts/doctor.py")
    p.add_argument("--no-deploy", action="store_true", help="modules 1 and 2: only inspect, do not run cdk deploy")

    p = sub.add_parser(
        "obo", help="the on-behalf-of chain as far as it is built: employee token -> (harness ->) gateway -> HR API"
    )
    p.add_argument(
        "prompt", nargs="?", default=None, help="what the employee asks (default: the leave balance question)"
    )
    p.add_argument(
        "--skip-manual", action="store_true", help="do not perform the first exchange by hand before the real call"
    )

    p = sub.add_parser("call", help="call a gateway tool as an agent role (SigV4), like the agent would")
    p.add_argument(
        "tool",
        help="get_leave_balance | get_employee_profile | get_public_holidays | submit_leave_request | retrieve | get_invoice_status | tools",
    )
    p.add_argument("args", nargs="*", help="tool arguments, for example E1001 or 'E1001 15'")
    p.add_argument("--as", dest="caller", default="hr-dev", help="hr-dev | hr-prod | finance-dev | me (default hr-dev)")
    p.add_argument("--gateway", default=None, help="hr-dev | hr-prod | finance-dev (default: the caller's own gateway)")

    p = sub.add_parser("cap", help="change the Dev key's cap as the Platform admin")
    p.add_argument(
        "--dev", required=True, help="low (half the spend so far) | restore (the record's cap) | an amount in USD"
    )

    p = sub.add_parser(
        "ask", help="one conversation with the agent through the platform (POST /invoke, the Playground path)"
    )
    p.add_argument("prompt")
    p.add_argument("--env", default="dev", choices=("dev", "prod"))
    p.add_argument("--session", default=None, help="continue a session id")

    p = sub.add_parser("mode", help="set the policy mode of a gateway (fallback for the Governance toggle)")
    p.add_argument("mode", choices=("LOG_ONLY", "ENFORCE"))
    p.add_argument("--gateway", default="all", help="hr-dev | hr-prod | finance-dev | all (default all)")

    p = sub.add_parser("approve", help="fallbacks for the UI approvals")
    p.add_argument("what", choices=("catalogue", "release"))
    p.add_argument("--release-id", default=None, help="release to approve (default: the newest PENDING_APPROVAL)")

    p = sub.add_parser("promote", help="promote an APPROVED release to Prod (fallback for the UI button)")
    p.add_argument("--release-id", default=None)

    sub.add_parser(
        "register-agents",
        help="register every promoted Prod agent in the catalogue (fallback for the Catalogue button; catch-up for agents "
        "promoted before the promote job wrote AGENT records)",
    )

    sub.add_parser("inventory", help="list every platform object by name")

    p = sub.add_parser("traffic", help="send conversations to Prod")
    p.add_argument("--prod", type=int, default=3, metavar="N")

    sub.add_parser("scores", help="online evaluation results for Prod")

    p = sub.add_parser("export", help="export the Dev harness to a code skeleton")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--strands", action="store_true")
    group.add_argument("--langgraph", action="store_true")
    p.add_argument("--run", action="store_true", help="run the generated module once with a temporary key")
    p.add_argument("--out", default="exports", help="output directory, relative to the repository (default: exports/)")

    p = sub.add_parser("deploy", help="cdk deploy of a stack set (facilitators; module 1 uses `apply 1`)")
    p.add_argument("scope", choices=("pre-day", "landing-zone", "idp", "all"))

    sub.add_parser("status", help="the eight stacks and their CloudFormation status")

    p = sub.add_parser(
        "cleanup", help="remove what the modules built (reverse order), optionally the stacks and the sweep"
    )
    p.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--stacks", action="store_true", help="then cdk destroy --all and scripts/cleanup.py --yes")
    p.add_argument("--keep-shared", action="store_true", help="keep the policy engine and the registry")
    p.add_argument("--keep-state", action="store_true", help="keep .workshop/state and exports/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ctx = build_context(args.record, args.region)
        if args.command == "record":
            from . import record as record_module

            print(f"record: {ctx.record_path}" + ("" if ctx.record_path.exists() else " (missing: defaults apply)"))
            print(record_module.describe(ctx.record))
            return 0
        if args.command in ("deploy", "status"):
            from . import cdk

            if args.command == "deploy":
                cdk.deploy(ctx.root, args.scope, region=ctx.region_override)
            else:
                _, region = cdk.identity(ctx.region_override)
                print(f"account stacks in {region}:")
                cdk.print_statuses(region)
            return 0
        if args.command == "cleanup":
            from . import cleanup

            return cleanup.run(
                ctx,
                dry_run=args.dry_run,
                yes=args.yes,
                stacks=args.stacks,
                keep_shared=args.keep_shared,
                keep_state=args.keep_state,
            )
        if args.command == "apply":
            from . import apply

            return apply.run(
                ctx,
                args.module,
                skip_evaluation=args.skip_evaluation,
                run_doctor=not args.no_doctor,
                deploy=not args.no_deploy,
            )
        from . import tools

        if args.command == "call":
            return tools.call(ctx, args.tool, args.args, caller=args.caller, gateway=args.gateway)
        if args.command == "cap":
            return tools.cap(ctx, args.dev)
        if args.command == "ask":
            return tools.ask(ctx, args.prompt, env=args.env, session_id=args.session)
        if args.command == "obo":
            from . import obo

            return obo.run(ctx, prompt=args.prompt, manual=not args.skip_manual)
        if args.command == "mode":
            return tools.mode(ctx, args.mode, gateway=args.gateway)
        if args.command == "approve":
            return tools.approve(ctx, args.what, release_id=args.release_id)
        if args.command == "promote":
            return tools.promote(ctx, release_id=args.release_id)
        if args.command == "register-agents":
            return tools.register_agents(ctx)
        if args.command == "inventory":
            return tools.inventory(ctx)
        if args.command == "traffic":
            return tools.traffic(ctx, count=args.prod)
        if args.command == "scores":
            return tools.scores(ctx)
        if args.command == "export":
            from . import export_cmd

            flavour = "langgraph" if args.langgraph else "strands" if args.strands else None
            return export_cmd.run(ctx, flavour=flavour, run=args.run, out_dir=args.out)
        raise WorkshopError(f"unknown command {args.command}")
    except WorkshopError as exc:
        print(f"\nERROR {exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # anything else: the traceback helps the facilitator
        traceback.print_exc()
        print_exception_hint(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
