"""Argparse CLI: walk, estimate, scan, report."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from hdd_analyzer import report as report_mod
from hdd_analyzer import scan as scan_mod
from hdd_analyzer import walker
from hdd_analyzer.config import DEFAULT_CAP_USD, DEFAULT_CONCURRENCY
from hdd_analyzer.report import DEFAULT_TOP_N, MIN_PROB_DEFAULT

RUNS_DIR = Path("runs")


def _run_dir(name: str) -> Path:
    return RUNS_DIR / name


def _cmd_walk(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if not root.exists():
        print(f"error: root does not exist: {root}", file=sys.stderr)
        return 1

    run_dir = _run_dir(args.run)
    roots = [root / sub for sub in args.include] if args.include else [root]
    summary = walker.walk_many(roots, run_dir)

    print(f"scanned: {summary.scanned} files, {summary.duplicates} duplicates, {summary.skipped_errors} errors")
    for category, count in sorted(summary.count_by_category.items()):
        size = summary.bytes_by_category[category]
        print(f"  {category}: {count} files, {size:,} bytes")
    print(f"wrote {run_dir / 'inventory.jsonl'}")
    return 0


def _cmd_estimate(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    estimate = scan_mod.estimate_run(run_dir)
    print(f"candidate files: {estimate.candidate_count}")
    print(f"estimated tokens: {estimate.estimated_tokens:,}")
    print(f"estimated cost: ${estimate.estimated_cost_usd:.4f}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    estimate = scan_mod.estimate_run(run_dir, limit=args.limit)

    print(f"candidate files: {estimate.candidate_count}")
    print(f"estimated tokens: {estimate.estimated_tokens:,}")
    print(f"estimated cost: ${estimate.estimated_cost_usd:.4f}")
    print(f"hard cap: ${args.cap:.2f}")

    run_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        print("dry run: extraction only, zero API calls")
        return 0

    if estimate.candidate_count == 0:
        print("nothing to scan")
        return 0

    if not args.yes:
        answer = input("proceed with API spend? [y/N] ").strip().lower()
        if answer != "y":
            print("aborted")
            return 1

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("error: TYPESAFE_API_KEY not set (check .env)", file=sys.stderr)
        return 1

    outcome = asyncio.run(
        scan_mod.run_scan(
            run_dir,
            api_key=api_key,
            cap_usd=args.cap,
            limit=args.limit,
            concurrency=DEFAULT_CONCURRENCY,
        )
    )

    print(f"processed: {outcome.processed}, errors: {outcome.errors}, spent: ${outcome.spent_usd:.4f}")
    if outcome.stopped_at_cap:
        print("stopped: hard cap reached; re-run scan to resume")
    if outcome.aborted_reason:
        return 1
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    md_path, csv_path = report_mod.generate_report(run_dir, top_n=args.top, min_prob=args.min_prob)
    print(f"wrote {md_path}")
    print(f"wrote {csv_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hdd-analyzer", description="Triage old hard drives with Jev.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    walk_parser = subparsers.add_parser("walk", help="Deterministic, free inventory walk of a drive/directory.")
    walk_parser.add_argument("root", help="Root directory to walk.")
    walk_parser.add_argument("--run", required=True, help="Run name; output goes to runs/NAME/.")
    walk_parser.add_argument("--include", action="append", help="Restrict walk to this subpath (repeatable).")
    walk_parser.set_defaults(func=_cmd_walk)

    estimate_parser = subparsers.add_parser("estimate", help="Token/dollar estimate for a scan. No network.")
    estimate_parser.add_argument("--run", required=True, help="Run name.")
    estimate_parser.set_defaults(func=_cmd_estimate)

    scan_parser = subparsers.add_parser("scan", help="Extraction + Jev classification under a hard budget cap.")
    scan_parser.add_argument("--run", required=True, help="Run name.")
    scan_parser.add_argument("--cap", type=float, default=DEFAULT_CAP_USD, help="Hard spend cap in USD.")
    scan_parser.add_argument("--limit", type=int, default=None, help="Limit number of files scanned.")
    scan_parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    scan_parser.add_argument("--dry-run", action="store_true", help="Extraction only; zero API calls.")
    scan_parser.set_defaults(func=_cmd_scan)

    report_parser = subparsers.add_parser("report", help="Render report.md and report.csv from scan results.")
    report_parser.add_argument("--run", required=True, help="Run name.")
    report_parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Overall top-N table size.")
    report_parser.add_argument(
        "--min-prob", type=float, default=MIN_PROB_DEFAULT, help="Per-category probability threshold."
    )
    report_parser.set_defaults(func=_cmd_report)

    return parser


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
