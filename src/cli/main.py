"""Command-line entrypoints (assignment Section 6: 'the exact command(s) to run the
agent on a goal, then replay the resulting artifact'). See README.md for the full
demo path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.agent.artifact_builder import build_artifact
from src.agent.discover import run_discovery
from src.artifacts import store
from src.artifacts.schema import ParamSpec, OutputSpec
from src.escalation.operator import run_operator_session
from src.replay.engine import replay as replay_capability

DEFAULT_BASE_URL = "http://127.0.0.1:5001"
CAPABILITY_ID = "open_member_subaccount"


def cmd_discover(args: argparse.Namespace) -> None:
    deposit_str = f"{args.deposit:.2f}"
    goal = (
        f"Look up member {args.member_id} in Meridian Core, open their member record, start "
        f"opening a new sub-account, and submit that form to reach the confirmation screen "
        f"before it is finalized. Use EXACTLY these values, typed/selected verbatim as shown: "
        f"account type = \"{args.account_type}\" (choose this exact option from the dropdown), "
        f"initial deposit = \"{deposit_str}\" (type exactly this text, no dollar sign, into the "
        f"deposit field). Stop as soon as you reach the confirmation screen that reviews the "
        f"account type and deposit -- do not click the final 'Confirm and Open Account' button; "
        f"reaching the confirmation screen is the whole goal."
    )
    print(f"Goal: {goal}\n")
    result = run_discovery(
        goal=goal,
        base_url=args.base_url,
        capability_name=CAPABILITY_ID,
        headless=not args.headed,
        allow_risky_finalize=False,
    )
    print(f"\nDiscovery run: {result.run_id}  (evidence: {result.logger_dir})")
    if not result.success:
        print("Discovery did NOT reach finish_success. See the run log for details.")
        sys.exit(1)

    print(f"Success. Summary: {result.summary}")
    print(f"Recorded {len(result.steps)} steps. Escalation used: {result.escalation_used}")

    known_params = [
        ParamSpec(name="member_id", type="string", required=True,
                   description="Meridian Core member ID to look up.", example=str(args.member_id)),
        ParamSpec(name="account_type", type="string", required=True,
                   description="Sub-account type: 'Checking' or 'Savings'.", example=args.account_type),
        ParamSpec(name="initial_deposit", type="number", required=True,
                   description="Initial deposit amount in USD (minimum 25.00).", example=deposit_str),
    ]
    outputs = [
        OutputSpec(name="account_number", type="string",
                   description="The newly created sub-account number (only present when confirm=True).",
                   extraction_pattern=r"New Account Number\s+([A-Za-z]{2,4}-[\w-]+)"),
    ]

    artifact = build_artifact(
        result=result,
        capability_id=CAPABILITY_ID,
        name="Open Member Sub-Account",
        description="Looks up a Meridian Core member, opens a new sub-account of a given type and "
                     "initial deposit, and reaches the confirmation screen. Finalizing the account "
                     "(the actual account-opening write) requires confirm=True.",
        app_name="meridian_core",
        known_params=known_params,
        outputs=outputs,
        risk_level="risky",
        requires_confirmation=True,
    )
    _warn_if_unparameterized(artifact, known_params)

    path = store.save(artifact)
    print(f"\nSaved capability artifact: {path}")


def _warn_if_unparameterized(artifact, known_params) -> None:
    templates = " ".join(s.value_template or "" for s in artifact.steps)
    for p in known_params:
        token = "{" + p.name + "}"
        if token not in templates:
            print(f"WARNING: param '{p.name}' (example={p.example!r}) never appeared verbatim in any "
                  f"recorded step's typed/selected value -- the artifact will replay with a hardcoded "
                  f"literal instead of this parameter. Inspect the artifact JSON before treating it as "
                  f"reusable for other {p.name} values.")


def _parse_kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--param must be key=value, got '{p}'")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_replay(args: argparse.Namespace) -> None:
    if args.artifact:
        artifact = store.load(Path(args.artifact))
    else:
        artifact = store.load_latest(args.capability_id or CAPABILITY_ID)

    params = _parse_kv(args.param)
    print(f"Replaying '{artifact.capability_id}' v{artifact.version} with params={params} confirm={args.confirm}")
    result = replay_capability(artifact, params, confirm=args.confirm, headless=not args.headed)
    print(json.dumps(result.to_dict(), indent=2))
    print(f"\nEvidence: {result.evidence_dir}")
    sys.exit(0 if result.status in ("success", "business_outcome") else 2)


def cmd_operator(args: argparse.Namespace) -> None:
    run_operator_session(args.run_dir)


def cmd_list(args: argparse.Namespace) -> None:
    for cap in store.list_capabilities():
        print(f"- {cap.capability_id} v{cap.version}: {cap.name}")
        print(f"    inputs: {[p.name + ':' + p.type for p in cap.input_params]}")
        print(f"    outputs: {[o.name for o in cap.outputs]}")
        print(f"    risky: {cap.risk_level == 'risky'} (confirm gate: {cap.confirmation_step_id})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="computeruse")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Run the LLM discovery agent and save a capability artifact.")
    d.add_argument("--base-url", default=DEFAULT_BASE_URL)
    d.add_argument("--member-id", default="10001")
    d.add_argument("--account-type", default="Savings", choices=["Checking", "Savings"])
    d.add_argument("--deposit", type=float, default=100.00)
    d.add_argument("--headed", action="store_true", help="Run with a visible browser window.")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="Deterministically replay a saved capability artifact.")
    r.add_argument("--capability-id", default=None)
    r.add_argument("--artifact", default=None, help="Explicit path to an artifact JSON file.")
    r.add_argument("--param", action="append", default=[], help="key=value, repeatable.")
    r.add_argument("--confirm", action="store_true", help="Execute the gated risky/irreversible final step.")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(func=cmd_replay)

    o = sub.add_parser("operator", help="Attach as a human operator to a paused, escalated run.")
    o.add_argument("--run-dir", required=True)
    o.set_defaults(func=cmd_operator)

    l = sub.add_parser("list", help="List saved capability artifacts (agent-facing catalog).")
    l.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
