#!/usr/bin/env python3
"""CLI for the Lesson 0012 HITL policy lab.

Try:
    python3 hitl_cli.py demo
    python3 hitl_cli.py policy
    python3 hitl_cli.py submit --tool restart_service \
        --args '{"service":"order-service","env":"prod"}' \
        --requested-by agent/repomedic --task INC-42
    python3 hitl_cli.py inbox
    python3 hitl_cli.py approve --id APR-0001 --approver bob
    python3 hitl_cli.py execute --id APR-0001
    python3 hitl_cli.py metrics
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from gateway import ApprovalGateway, SimulatedCrash
from models import Action, DENY, FakeClock, PolicyDecision, TIER_LABELS
from policy import Policy
from store import Store


DEFAULT_STATE = Path(".lab/state.json")


def load_gateway(state_path: Path, policy: Optional[Policy] = None, clock=None) -> ApprovalGateway:
    store = Store(state_path)
    return ApprovalGateway(policy=policy, store=store, clock=clock)


def money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return "%.1f s" % value


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def print_request(request) -> None:
    print("request:   %s" % request.request_id)
    print("tool:      %s" % request.tool)
    print("tier:      %s (%s)" % (TIER_LABELS[request.tier], request.rule_id))
    print("digest:    %s..." % request.digest[:16])
    print("why:       %s" % request.reason)
    print("requested: %s · task %s" % (request.requested_by, request.task_id or "-"))
    print("approvals: %d/%d %s" % (request.approval_count(), request.required_approvals,
                                   [a.get("approver") for a in request.approvals]))
    print("expires:   %s (status %s)" % (request.expires_at, request.status))
    print("args:")
    print_json(request.action.get("args", {}))


def print_submission(submission) -> None:
    print("status: %s" % submission.status)
    print("policy: %s · %s" % (TIER_LABELS[submission.decision.tier], submission.decision.rule_id))
    print("reason: %s" % submission.decision.reason)
    if submission.request:
        print()
        print_request(submission.request)
    if submission.execution:
        print()
        print("effect: %s" % submission.execution.status)
        print("  %s" % submission.execution.message)
        if submission.execution.result:
            print("  idempotency_key: %s" % submission.execution.idempotency_key)
            print_json(submission.execution.result)


def cmd_policy(args: argparse.Namespace) -> None:
    policy = Policy()
    print("policy version: %s" % policy.version)
    print()
    print("%-18s %-48s %-14s %s" % ("rule", "match", "tier", "note"))
    print("-" * 108)
    for row in policy.explain():
        print("%-18s %-48s %-14s %s" % (row["rule"], row["match"], row["tier"], row["note"]))


def cmd_submit(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    try:
        raw = json.loads(args.args or "{}")
    except ValueError as exc:
        print("invalid --args JSON: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(raw, dict):
        print("--args must be a JSON object", file=sys.stderr)
        raise SystemExit(2)
    action = Action(
        tool=args.tool,
        args=raw,
        requested_by=args.requested_by,
        task_id=args.task,
    )
    print_submission(gateway.submit(action))


def cmd_inbox(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    requests = gateway.inbox()
    if not requests:
        print("inbox is empty")
        return
    for request in requests:
        print(
            "%s  %-20s %-12s %d/%d  %s"
            % (
                request.request_id,
                request.tool,
                TIER_LABELS[request.tier],
                request.approval_count(),
                request.required_approvals,
                request.status,
            )
        )


def cmd_show(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    request = gateway.store.get_request(args.id)
    if request is None:
        print("unknown request %s" % args.id, file=sys.stderr)
        raise SystemExit(2)
    print_json(request.to_dict())


def cmd_approve(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    result = gateway.approve(args.id, args.approver, args.note)
    print("%s: %s" % (result.status, result.message))
    if result.request:
        print()
        print_request(result.request)


def cmd_reject(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    result = gateway.reject(args.id, args.approver, args.note)
    print("%s: %s" % (result.status, result.message))
    if result.request:
        print()
        print_request(result.request)


def cmd_execute(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    request = gateway.store.get_request(args.id)
    if request is None:
        print("unknown request %s" % args.id, file=sys.stderr)
        raise SystemExit(2)

    action = None
    if args.args:
        try:
            raw = json.loads(args.args)
        except ValueError as exc:
            print("invalid --args JSON: %s" % exc, file=sys.stderr)
            raise SystemExit(2)
        base = request.to_action()
        action = Action(base.tool, raw, base.requested_by, base.task_id)

    try:
        result = gateway.execute(args.id, action=action, crash_after_effect=args.crash_after_effect)
    except SimulatedCrash as exc:
        print("process crashed: %s" % exc)
        print("the effect is already in the ledger; rerun execute to see the duplicate path")
        raise SystemExit(3)

    print("%s: %s" % (result.status, result.message))
    if result.result:
        print("idempotency_key: %s" % result.idempotency_key)
        print_json(result.result)


def cmd_expire(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    expired = gateway.expire(args.id)
    if not expired:
        print("nothing expired")
        return
    for request in expired:
        print("%s expired (%s)" % (request.request_id, request.tool))


def cmd_metrics(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    metrics = gateway.metrics(rubber_stamp_seconds=args.rubber_stamp_seconds)
    for key, value in metrics.items():
        if isinstance(value, float):
            print("%-32s %.3f" % (key, value))
        else:
            print("%-32s %s" % (key, value))


def cmd_audit(args: argparse.Namespace) -> None:
    gateway = load_gateway(Path(args.state))
    events = gateway.audit()
    if args.limit:
        events = events[-args.limit:]
    for event in events:
        detail = {k: v for k, v in event.items() if k not in ("at", "event")}
        print("%s  %-30s %s" % (event.get("at"), event.get("event"), json.dumps(detail, ensure_ascii=False)))


def cmd_reset(args: argparse.Namespace) -> None:
    store = Store(Path(args.state))
    store.reset()
    print("removed %s" % args.state)


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def cmd_demo(args: argparse.Namespace) -> None:
    """A deterministic tour of the policy, approval and replay paths."""
    state = Path(args.state)
    store = Store(state)
    store.reset()
    clock = FakeClock(datetime(2026, 11, 14, 2, 0, 0, tzinfo=timezone.utc))
    gateway = ApprovalGateway(store=store, clock=clock)

    section("1. T0 auto: reading evidence needs no human")
    print_submission(
        gateway.submit(
            Action("query_logs", {"service": "order-service", "limit": 3}, "agent/repomedic", "INC-42")
        )
    )

    section("2. T1 notify: a dev restart is reversible; execute and tell on-call")
    print_submission(
        gateway.submit(
            Action("restart_service", {"service": "order-service", "env": "dev"}, "agent/repomedic", "INC-43")
        )
    )

    section("3. T2 approval: production restart is parked")
    sub = gateway.submit(
        Action("restart_service", {"service": "order-service", "env": "prod"}, "agent/repomedic", "INC-44")
    )
    print_submission(sub)
    request_id = sub.request.request_id

    print("\n-- execution before approval --")
    print_json(gateway.execute(request_id).to_dict())

    clock.advance(180)
    print("\n-- bob approves after 3 minutes --")
    print(gateway.approve(request_id, "bob", "checked pool metrics").message)

    print("\n-- execution after approval --")
    first = gateway.execute(request_id)
    print_json(first.to_dict())

    print("\n-- replay the same approval --")
    print_json(gateway.execute(request_id).to_dict())

    section("4. T3 four-eyes: payment-service needs two distinct senior approvers")
    sub = gateway.submit(
        Action("restart_service", {"service": "payment-service", "env": "prod"}, "agent/repomedic", "INC-45")
    )
    print_submission(sub)
    payment_id = sub.request.request_id

    clock.advance(60)
    print("\n-- alice (senior) approves: still pending --")
    print(gateway.approve(payment_id, "alice", "first pair of eyes").message)

    print("\n-- alice tries again: duplicate blocked --")
    print(gateway.approve(payment_id, "alice").message)

    print("\n-- carol (on-call, not senior) tries: not authorized --")
    print(gateway.approve(payment_id, "carol").message)

    clock.advance(60)
    print("\n-- bob (senior, distinct) approves: fully approved --")
    print(gateway.approve(payment_id, "bob", "second pair of eyes").message)

    print("\n-- execution --")
    print_json(gateway.execute(payment_id).to_dict())

    section("5. separation of duties: the requester cannot approve their own request")
    sub = gateway.submit(
        Action("restart_service", {"service": "inventory-service", "env": "prod"}, "carol", "INC-46")
    )
    print_submission(sub)
    own_id = sub.request.request_id
    print("\n-- carol tries to approve her own request --")
    print(gateway.approve(own_id, "carol").message)
    print("\n-- alice approves it instead --")
    print(gateway.approve(own_id, "alice", "independent reviewer").message)

    section("6. binding: an approval for order-service cannot execute payment-service")
    sub = gateway.submit(
        Action("restart_service", {"service": "order-service", "env": "prod"}, "agent/repomedic", "INC-47")
    )
    print_submission(sub)
    bound_id = sub.request.request_id
    clock.advance(30)
    print(gateway.approve(bound_id, "alice", "approved order-service only").message)

    swapped = Action(
        "restart_service",
        {"service": "payment-service", "env": "prod"},
        "agent/repomedic",
        "INC-47",
    )
    print("\n-- execute with swapped arguments --")
    print_json(gateway.execute(bound_id, action=swapped).to_dict())

    print("\n-- execute with the approved arguments --")
    print_json(gateway.execute(bound_id).to_dict())

    section("7. expiry: no answer is not approval")
    sub = gateway.submit(
        Action("restart_service", {"service": "catalog-service", "env": "prod"}, "agent/repomedic", "INC-48")
    )
    print_submission(sub)
    expired_id = sub.request.request_id
    clock.advance(1000)
    print("\n-- 16+ minutes later, nobody answered --")
    print_json(gateway.execute(expired_id).to_dict())

    section("8. T4 deny: some actions never go through this gateway")
    print_submission(
        gateway.submit(
            Action("modify_firewall", {"rule": "allow 0.0.0.0/0:22"}, "agent/repomedic", "INC-49")
        )
    )

    section("9. policy change: an old approval cannot outrun a tightened policy")
    sub = gateway.submit(
        Action("restart_service", {"service": "report-service", "env": "prod"}, "agent/repomedic", "INC-50")
    )
    print_submission(sub)
    changed_id = sub.request.request_id
    clock.advance(30)
    print(gateway.approve(changed_id, "bob", "approved under the old policy").message)

    strict = Policy(
        overrides={
            "restart_service": PolicyDecision(
                DENY, "freeze", "production restart freeze during peak hours"
            )
        }
    )
    strict_gateway = ApprovalGateway(policy=strict, store=store, clock=clock)
    print("\n-- policy tightened to deny restarts; execute the old approval --")
    print_json(strict_gateway.execute(changed_id).to_dict())

    section("10. metrics: is the gate doing real work?")
    metrics = gateway.metrics(rubber_stamp_seconds=5)
    for key, value in metrics.items():
        print("%-32s %s" % (key, value))

    section("audit trail (last 12 events)")
    for event in gateway.audit()[-12:]:
        detail = {k: v for k, v in event.items() if k not in ("at", "event")}
        print("%s  %-30s %s" % (event.get("at"), event.get("event"), json.dumps(detail, ensure_ascii=False)))

    print()
    print("state file:", state)
    print("try: python3 hitl_cli.py metrics --state %s" % state)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lesson 0012 HITL policy gateway")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="JSON state file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("policy", help="show the risk rule table")
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("submit", help="evaluate and submit an action")
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}")
    p.add_argument("--requested-by", default="agent/repomedic")
    p.add_argument("--task", default="INC-42")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("inbox", help="list pending approvals")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("show", help="show one approval request")
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="approve a request")
    p.add_argument("--id", required=True)
    p.add_argument("--approver", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="reject a request")
    p.add_argument("--id", required=True)
    p.add_argument("--approver", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("execute", help="execute an approved request")
    p.add_argument("--id", required=True)
    p.add_argument("--args", default="", help="optional different args to test digest binding")
    p.add_argument("--crash-after-effect", action="store_true")
    p.set_defaults(func=cmd_execute)

    p = sub.add_parser("expire", help="expire overdue requests")
    p.add_argument("--id", default="")
    p.set_defaults(func=cmd_expire)

    p = sub.add_parser("metrics", help="show approval quality metrics")
    p.add_argument("--rubber-stamp-seconds", type=float, default=5.0)
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("audit", help="show the audit trail")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("reset", help="delete the lab state")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("demo", help="run a deterministic end-to-end tour")
    p.set_defaults(func=cmd_demo)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
