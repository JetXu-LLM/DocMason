"""CLI entrypoints for the DocMason operator surface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from .commands import (
    doctor_workspace,
    emit_report,
    prepare_workspace,
    retrieve_knowledge,
    run_workflow,
    status_workspace,
    sync_adapters,
    sync_workspace,
    trace_knowledge,
    validate_knowledge_base,
)
from .project import SUPPORTED_DOCUMENT_TYPES


def build_parser() -> argparse.ArgumentParser:
    """Build the stable command-line interface."""
    parser = argparse.ArgumentParser(prog="docmason", description="DocMason CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="Bootstrap the local workspace.")
    prepare_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    prepare_parser.add_argument(
        "--yes",
        action="store_true",
        help="Automatically approve supported dependency-install attempts during prepare.",
    )

    doctor_parser = subparsers.add_parser("doctor", help="Inspect workspace readiness.")
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    status_parser = subparsers.add_parser("status", help="Show the current workspace stage.")
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    sync_kb_parser = subparsers.add_parser("sync", help="Build or refresh the knowledge base.")
    sync_kb_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    retrieve_parser = subparsers.add_parser(
        "retrieve",
        help="Run retrieval over the published knowledge base.",
    )
    retrieve_parser.add_argument("query", help="Retrieval query text.")
    retrieve_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    retrieve_parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Maximum number of source bundles to return.",
    )
    retrieve_parser.add_argument(
        "--graph-hops",
        type=int,
        default=1,
        help="Maximum number of graph-expansion hops to explore.",
    )
    retrieve_parser.add_argument(
        "--document-type",
        action="append",
        choices=SUPPORTED_DOCUMENT_TYPES,
        default=[],
        help="Restrict retrieval to a document type. Repeat for multiple types.",
    )
    retrieve_parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Restrict retrieval to one or more source IDs.",
    )
    retrieve_parser.add_argument(
        "--include-renders",
        action="store_true",
        help="Include render references in the result payload.",
    )

    trace_parser = subparsers.add_parser(
        "trace",
        help="Trace knowledge objects or answer text back to source evidence.",
    )
    trace_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    trace_parser.add_argument(
        "--top",
        type=int,
        default=3,
        help="Maximum number of supporting results per answer segment.",
    )
    trace_group = trace_parser.add_mutually_exclusive_group(required=True)
    trace_group.add_argument(
        "--source-id",
        help="Trace a published source ID back to its evidence and relations.",
    )
    trace_group.add_argument(
        "--answer-file",
        help="Trace an answer file back to supporting evidence.",
    )
    trace_group.add_argument(
        "--session-id",
        help="Reuse a prior answer session for provenance tracing.",
    )
    trace_parser.add_argument(
        "--unit-id",
        help="When tracing a source ID, also inspect one evidence unit in detail.",
    )

    validate_parser = subparsers.add_parser(
        "validate-kb",
        help="Validate the staged or published knowledge base.",
    )
    validate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    validate_parser.add_argument(
        "--target",
        choices=("staging", "current"),
        default=None,
        help="Validation target. Defaults to staging when present, otherwise current.",
    )

    sync_parser = subparsers.add_parser("sync-adapters", help="Generate supported agent adapters.")
    sync_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    sync_parser.add_argument(
        "--target",
        default="claude",
        help="Adapter target to generate. DocMason currently supports `claude` only.",
    )

    workflow_parser = subparsers.add_parser(
        "workflow",
        help="Execute a supported advanced workflow surface.",
    )
    workflow_parser.add_argument("workflow_id", help="Workflow ID to execute.")
    workflow_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    # Hidden hook subcommand — called by .claude/hooks/*.sh scripts.
    hook_parser = subparsers.add_parser("_hook")
    hook_parser.add_argument(
        "event_name",
        help="Hook event name (session, prompt-submit, post-tool-use, stop).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested DocMason subcommand and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "prepare":
        return emit_report(prepare_workspace(assume_yes=args.yes), as_json=args.json)
    if args.command == "doctor":
        return emit_report(doctor_workspace(), as_json=args.json)
    if args.command == "status":
        return emit_report(status_workspace(), as_json=args.json)
    if args.command == "sync":
        return emit_report(sync_workspace(), as_json=args.json)
    if args.command == "retrieve":
        return emit_report(
            retrieve_knowledge(
                query=args.query,
                top=args.top,
                graph_hops=args.graph_hops,
                document_types=args.document_type,
                source_ids=args.source_id,
                include_renders=args.include_renders,
            ),
            as_json=args.json,
        )
    if args.command == "trace":
        return emit_report(
            trace_knowledge(
                source_id=args.source_id,
                unit_id=args.unit_id,
                answer_file=args.answer_file,
                session_id=args.session_id,
                top=args.top,
            ),
            as_json=args.json,
        )
    if args.command == "validate-kb":
        return emit_report(validate_knowledge_base(target=args.target), as_json=args.json)
    if args.command == "sync-adapters":
        return emit_report(sync_adapters(target=args.target), as_json=args.json)
    if args.command == "workflow":
        return emit_report(run_workflow(args.workflow_id), as_json=args.json)
    if args.command == "_hook":
        from .hooks import run_hook_cli

        return run_hook_cli(args.event_name)

    parser.error(f"Unsupported command: {args.command}")
    return 1
