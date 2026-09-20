"""Argparse CLI: walk, estimate, scan, report."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from hdd_analyzer import annotate as annotate_mod
from hdd_analyzer import manifest as manifest_mod
from hdd_analyzer import ocr as ocr_mod
from hdd_analyzer import report as report_mod
from hdd_analyzer import report_html as report_html_mod
from hdd_analyzer import scan as scan_mod
from hdd_analyzer import walker
from hdd_analyzer.config import DEFAULT_CAP_USD, DEFAULT_CONCURRENCY
from hdd_analyzer.manifest import MANIFEST_DEFAULT_MIN_PROB, MANIFEST_DEFAULT_MIN_VALUE
from hdd_analyzer.ocr import OCR_DEFAULT_MIN_VALUE, OCR_DEFAULT_TOP_N
from hdd_analyzer.report import DEFAULT_TOP_N, MIN_PROB_DEFAULT, MIN_VALUE_DEFAULT

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


def _parse_exclude_ext(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {ext.strip().lstrip(".").lower() for ext in value.split(",") if ext.strip()}


def _cmd_scan(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    only_keys = scan_mod.load_name_only_keys(run_dir) if args.only_name_only else None
    exclude_exts = _parse_exclude_ext(args.exclude_ext)

    excerpt_override = None
    if args.from_ocr:
        excerpt_override = ocr_mod.load_ocr_excerpts(run_dir)
        ocr_keys = set(excerpt_override.keys()) - scan_mod.load_ocr_classified_keys(run_dir)
        only_keys = ocr_keys if only_keys is None else only_keys & ocr_keys

    estimate = scan_mod.estimate_run(
        run_dir, limit=args.limit, only_keys=only_keys, exclude_exts=exclude_exts, excerpt_override=excerpt_override
    )

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
            only_keys=only_keys,
            exclude_exts=exclude_exts,
            excerpt_override=excerpt_override,
        )
    )

    print(f"processed: {outcome.processed}, errors: {outcome.errors}, spent: ${outcome.spent_usd:.4f}")
    if outcome.stopped_at_cap:
        print("stopped: hard cap reached; re-run scan to resume")
    if outcome.aborted_reason:
        return 1
    return 0


def _cmd_annotate(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    outcome = annotate_mod.annotate_run(run_dir, all_rows=args.all, top_n=args.top, min_prob=args.min_prob)
    print(f"considered: {outcome.considered}, updated: {outcome.updated}")
    if outcome.became_name_only:
        print(f"revealed as name-only ({len(outcome.became_name_only)}):")
        for path in outcome.became_name_only:
            print(f"  {path}")
    if outcome.rescan_candidates:
        print(f"candidates for a cheap targeted re-scan ({len(outcome.rescan_candidates)}):")
        print("  scan-time judgment was name-only, but current extraction now succeeds")
        print("  re-classify with: scan --run <name> --only-name-only")
        for path in outcome.rescan_candidates:
            print(f"  {path}")
    return 0


def _cmd_ocr(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)

    try:
        ocr_mod.resolve_tesseract()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        results = report_mod.load_results(run_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    candidates = ocr_mod.select_ocr_candidates(
        results, top_n=args.top, take_all=args.all, min_value=args.min_value, limit=args.limit
    )
    print(f"ocr candidates: {len(candidates)}")
    if not candidates:
        print("nothing to OCR")
        return 0

    outcome = ocr_mod.run_ocr(run_dir, candidates)
    print(
        f"ocr done: ok={outcome.ok} no_text={outcome.no_text} timeout={outcome.timeout} "
        f"error={outcome.error} unsupported={outcome.unsupported} elapsed={outcome.elapsed_seconds:.1f}s"
    )
    print(f"wrote {run_dir / 'ocr.jsonl'}")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    out_dir = Path(args.out) if args.out else run_dir / "manifest"

    try:
        outcome = manifest_mod.generate_manifest(
            run_dir, out_dir=out_dir, min_value=args.min_value, min_prob=args.min_prob
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"included: {outcome.total} (verified: {outcome.verified}, name-only: {outcome.name_only})")
    print(f"total bytes to copy: {outcome.total_bytes:,}")
    print(f"wrote {outcome.out_dir}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    run_dir = _run_dir(args.run)
    try:
        md_path, csv_path = report_mod.generate_report(run_dir, top_n=args.top, min_prob=args.min_prob)
        html_path = report_html_mod.write_report_html(run_dir, min_value=args.min_value, min_prob=args.min_prob)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in (md_path, csv_path, html_path):
        print(f"wrote {path}")
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
    scan_parser.add_argument(
        "--only-name-only",
        action="store_true",
        help="Restrict candidates to name-only-judged rows that today's extraction would actually retry.",
    )
    scan_parser.add_argument(
        "--exclude-ext",
        default=None,
        help="Comma-separated extensions to exclude from candidates (e.g. jpg,png), applied on top of selection.",
    )
    scan_parser.add_argument(
        "--from-ocr",
        action="store_true",
        help="Restrict candidates to dedupe keys with an ok OCR excerpt in runs/NAME/ocr.jsonl, "
        "and classify using that excerpt instead of running extraction (see `ocr` subcommand).",
    )
    scan_parser.set_defaults(func=_cmd_scan)

    annotate_parser = subparsers.add_parser(
        "annotate", help="Backfill extraction_status/metadata_only onto existing results. Zero API calls."
    )
    annotate_parser.add_argument("--run", required=True, help="Run name.")
    annotate_parser.add_argument("--all", action="store_true", help="Annotate every result row, not just report-surfaced ones.")
    annotate_parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Overall top-N used to select surfaced rows.")
    annotate_parser.add_argument(
        "--min-prob", type=float, default=MIN_PROB_DEFAULT, help="Per-category probability threshold used to select surfaced rows."
    )
    annotate_parser.set_defaults(func=_cmd_annotate)

    ocr_parser = subparsers.add_parser(
        "ocr", help="OCR name-only image/no-text-PDF rows with Tesseract. Local, free, no API calls."
    )
    ocr_parser.add_argument("--run", required=True, help="Run name.")
    ocr_parser.add_argument(
        "--top", type=int, default=OCR_DEFAULT_TOP_N, help="Top N eligible rows by value_score to OCR."
    )
    ocr_parser.add_argument("--all", action="store_true", help="OCR every eligible row, ignoring --top/--min-value.")
    ocr_parser.add_argument(
        "--min-value", type=float, default=OCR_DEFAULT_MIN_VALUE, help="Minimum value_score to be OCR'd."
    )
    ocr_parser.add_argument("--limit", type=int, default=None, help="Cap the total number of files OCR'd.")
    ocr_parser.set_defaults(func=_cmd_ocr)

    report_parser = subparsers.add_parser(
        "report", help="Render report.md, report.csv, and report.html from scan results."
    )
    report_parser.add_argument("--run", required=True, help="Run name.")
    report_parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Overall top-N table size (Markdown).")
    report_parser.add_argument(
        "--min-prob", type=float, default=MIN_PROB_DEFAULT, help="Per-category probability threshold."
    )
    report_parser.add_argument(
        "--min-value",
        type=float,
        default=MIN_VALUE_DEFAULT,
        help="Minimum value_score for a file to be embedded in report.html as notable.",
    )
    report_parser.set_defaults(func=_cmd_report)

    manifest_parser = subparsers.add_parser(
        "manifest", help="Build a salvage copy list and summaries. Read-only over results.jsonl/inventory.jsonl."
    )
    manifest_parser.add_argument("--run", required=True, help="Run name.")
    manifest_parser.add_argument(
        "--min-value", type=float, default=MANIFEST_DEFAULT_MIN_VALUE, help="Minimum value_score for inclusion."
    )
    manifest_parser.add_argument(
        "--min-prob", type=float, default=MANIFEST_DEFAULT_MIN_PROB, help="Minimum category probability for inclusion."
    )
    manifest_parser.add_argument(
        "--out", default=None, help="Output directory. Defaults to runs/NAME/manifest."
    )
    manifest_parser.set_defaults(func=_cmd_manifest)

    return parser


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
