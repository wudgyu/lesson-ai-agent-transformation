"""Optional Postgres-backed CLI for lesson 0009.

This is the same incident graph as `graph_agent.py`; the only difference is the
checkpointer. It is optional because it needs a running Postgres.

Quick start:
    docker compose up -d
    pip install -r requirements-postgres.txt
    export DATABASE_URL=postgresql://repomedic:repomedic@localhost:5432/repomedic
    python3 postgres_demo.py init
    python3 postgres_demo.py start --thread pg-incident-1
    python3 postgres_demo.py resume --thread pg-incident-1 \
        --decision approve --approver alice --approval-id APR-7
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Tuple

try:
    import psycopg
    from psycopg.rows import dict_row
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.types import Command
except ImportError as exc:  # pragma: no cover - depends on optional extras
    raise SystemExit(
        "Postgres extras are not installed.\n"
        "Run: pip install -r requirements-postgres.txt"
    ) from exc

from effects import EffectStore
from graph_agent import (
    SimulatedCrash,
    build_graph,
    config_for,
    initial_state,
    print_result,
)

DEFAULT_URL = (
    os.environ.get("DATABASE_URL")
    or "postgresql://repomedic:repomedic@localhost:5432/repomedic"
)


@contextmanager
def open_postgres_graph(
    database_url: str,
    effects_path: Path,
    crash_after_effect: bool = False,
) -> Iterator[Tuple[Any, PostgresSaver]]:
    """Open one connection, ensure the schema, and compile the graph.

    `setup()` is idempotent, but in production it should run once as a
    migration rather than on every request.
    """
    with psycopg.Connection.connect(
        database_url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as conn:
        saver = PostgresSaver(conn)
        saver.setup()
        graph = build_graph(
            EffectStore(effects_path),
            saver,
            crash_after_effect=crash_after_effect,
        )
        yield graph, saver


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        default=DEFAULT_URL,
        help="Postgres connection string (default: $DATABASE_URL or local compose URL)",
    )
    parser.add_argument(
        "--effects",
        default=".lab/postgres-effects.json",
        help="external effect ledger (JSON, same as the SQLite lab)",
    )
    parser.add_argument("--thread", default="pg-incident-42", help="thread_id")


def cmd_init(args: argparse.Namespace) -> None:
    with open_postgres_graph(args.database_url, Path(args.effects)) as (_graph, saver):
        rows = saver.conn.execute(
            "select tablename from pg_tables where schemaname = 'public' order by 1"
        ).fetchall()
    names = [row["tablename"] for row in rows]
    print("schema ready:", ", ".join(names))


def cmd_start(args: argparse.Namespace) -> None:
    with open_postgres_graph(
        args.database_url, Path(args.effects)
    ) as (graph, _saver):
        config = config_for(args.thread)
        if graph.get_state(config).next:
            print("thread already has pending work; use resume/continue", file=sys.stderr)
            raise SystemExit(2)
        result = graph.invoke(
            initial_state(args.incident, args.question),
            config,
            durability="sync",
        )
        print_result(graph, config, result)


def cmd_resume(args: argparse.Namespace) -> None:
    with open_postgres_graph(
        args.database_url,
        Path(args.effects),
        crash_after_effect=args.crash_after_effect,
    ) as (graph, _saver):
        config = config_for(args.thread)
        approval_id = args.approval_id
        if args.decision == "approve" and not approval_id:
            approval_id = "APR-%d" % int(time.time())
            print("no --approval-id supplied; generated", approval_id)

        decision = {
            "approved": args.decision == "approve",
            "approver": args.approver,
            "approval_id": approval_id,
            "note": args.note,
        }
        try:
            result = graph.invoke(
                Command(resume=decision), config, durability="sync"
            )
        except SimulatedCrash as exc:
            print("process crashed:", exc)
            print("checkpoint still points at:", graph.get_state(config).next)
            print(
                "external effect ledger:",
                json.dumps(EffectStore(Path(args.effects)).items(), ensure_ascii=False),
            )
            raise SystemExit(3)
        print_result(graph, config, result)


def cmd_continue(args: argparse.Namespace) -> None:
    with open_postgres_graph(
        args.database_url, Path(args.effects)
    ) as (graph, _saver):
        config = config_for(args.thread)
        result = graph.invoke(None, config, durability="sync")
        print_result(graph, config, result)


def cmd_reset(args: argparse.Namespace) -> None:
    with open_postgres_graph(
        args.database_url, Path(args.effects)
    ) as (_graph, saver):
        saver.delete_thread(args.thread)
        print("deleted thread:", args.thread)


def cmd_stats(args: argparse.Namespace) -> None:
    with open_postgres_graph(
        args.database_url, Path(args.effects)
    ) as (graph, saver):
        config = config_for(args.thread)
        checkpoint_count = len(list(saver.list(config)))
        snapshot = graph.get_state(config)
        print("thread:", args.thread)
        print("checkpoints:", checkpoint_count)
        print("next:", snapshot.next)
        print("stop_reason:", snapshot.values.get("stop_reason", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Lesson 0009 Postgres checkpointer")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the LangGraph checkpoint tables")
    p.add_argument("--database-url", default=DEFAULT_URL)
    p.add_argument("--effects", default=".lab/postgres-effects.json")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("start", help="start a thread and pause for approval")
    add_common_args(p)
    p.add_argument("--incident", default="INC-PG-42")
    p.add_argument("--question", default="order-service 连接池耗尽，是否重启？")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("resume", help="resume with an approval decision")
    add_common_args(p)
    p.add_argument("--decision", choices=["approve", "reject"], required=True)
    p.add_argument("--approver", default="")
    p.add_argument("--approval-id", default="")
    p.add_argument("--note", default="")
    p.add_argument("--crash-after-effect", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("continue", help="resume after a crash with invoke(None)")
    add_common_args(p)
    p.set_defaults(func=cmd_continue)

    p = sub.add_parser("reset", help="delete all checkpoints for a thread")
    add_common_args(p)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("stats", help="show checkpoint count and latest state")
    add_common_args(p)
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
