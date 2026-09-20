"""Report stage: rank results and render report.md + report.csv (report.html lives in report_html)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from hdd_analyzer.budget import tokens_to_cost
from hdd_analyzer.paths import pure_path

NOUL_CATEGORIES = ("credentials", "personal", "financial_legal", "original_work", "irreplaceable")
DEFAULT_TOP_N = 25
MIN_PROB_DEFAULT = 0.6
# Value floor for a row to count as "notable" in report.html; matches the
# manifest's default so the tree shows what `manifest` would salvage.
MIN_VALUE_DEFAULT = 2.0
MAX_PER_DIR = 3


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    """Latest row per dedupe key, except that an error row never supersedes a successful judgment.

    A targeted rescan that hits a 429 storm appends error rows for keys that
    already carry a valid (if name-only) result; those rows must not erase
    the earlier judgment from the report, nor from name-only rescan selection.
    """
    results_path = run_dir / "results.jsonl"
    if not results_path.exists():
        raise FileNotFoundError(f"no results found at {results_path}; run `scan` first")
    latest: dict[str, dict[str, Any]] = {}
    with open(results_path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            previous = latest.get(record["dedupe_key"])
            if record.get("error") and previous is not None and not previous.get("error"):
                continue
            latest[record["dedupe_key"]] = record
    return list(latest.values())


def load_dedupe_savings(run_dir: Path) -> int:
    inventory_path = run_dir / "inventory.jsonl"
    if not inventory_path.exists():
        return 0
    count = 0
    with open(inventory_path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("dup_of") is not None:
                count += 1
    return count


def _top_category(probabilities: dict[str, Any]) -> str:
    best_name, best_prob = "-", -1.0
    for name in NOUL_CATEGORIES:
        value = probabilities.get(name)
        if isinstance(value, (int, float)) and value > best_prob:
            best_name, best_prob = name, float(value)
    return best_name


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.0f}TB"


def _valid_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in results if not r.get("error") and r.get("value_score") is not None]


def verified_label(row: dict[str, Any]) -> str:
    """"content" if extraction actually read the file, "name-only" if Jev only saw the name.

    "ocr" covers rows classified from a `scan --from-ocr` excerpt (see
    scan.EXTRACTION_STATUS_OCR), so a Tesseract-recovered read is told apart
    from both a direct content read and a bare filename guess. "unknown"
    covers legacy result rows written before extraction_status was tracked,
    so an old run's report does not silently claim verification it never
    performed.
    """
    status = row.get("extraction_status")
    if status is None:
        return "unknown"
    if status == "ocr":
        return "ocr"
    return "content" if status == "ok" else "name-only"


def split_by_verified(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows into (content-verified, everything else), preserving order.

    "everything else" bundles name-only and unknown rows together, since
    neither is confirmed by actual file content and both should be kept out
    of the headline ranking.
    """
    verified, unverified = [], []
    for row in rows:
        (verified if verified_label(row) == "content" else unverified).append(row)
    return verified, unverified


def _parent_dir(path: str) -> str:
    return str(pure_path(path).parent)


def collapse_siblings(rows: list[dict[str, Any]], max_per_dir: int = MAX_PER_DIR) -> list[dict[str, Any]]:
    """Collapse runs of >max_per_dir rows sharing a parent directory.

    `rows` must already be in the order they will be displayed (rank order).
    For each parent directory with more than `max_per_dir` entries, the first
    (highest-ranked) `max_per_dir` rows are kept as-is and the rest collapse
    into a single synthetic marker row: {"collapsed": True, "count": n, "dir": dir}.
    This keeps a directory of 50 game saves from flooding a table.
    """
    counts: dict[str, int] = {}
    for row in rows:
        d = _parent_dir(row["path"])
        counts[d] = counts.get(d, 0) + 1

    seen: dict[str, int] = {}
    collapsed_dirs: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        d = _parent_dir(row["path"])
        seen[d] = seen.get(d, 0) + 1
        if counts[d] <= max_per_dir:
            result.append(row)
            continue
        if seen[d] <= max_per_dir:
            result.append(row)
        elif d not in collapsed_dirs:
            result.append({"collapsed": True, "count": counts[d] - max_per_dir, "dir": d})
            collapsed_dirs.add(d)
    return result


def _overall_row_line(rank: int, row: dict[str, Any]) -> str:
    if row.get("collapsed"):
        return f"| - | - | - | - | ... and {row['count']} more in {row['dir']} | - |"
    return (
        f"| {rank} | {row['value_score']:.2f} | {row.get('value_confidence') or 0:.2f} | "
        f"{_top_category(row.get('probabilities', {}))} | {_format_size(row['size'])} | {row['path']} | "
        f"{verified_label(row)} |"
    )


def _render_overall_group(rows: list[dict[str, Any]], ranks: list[int]) -> str:
    collapsed = collapse_siblings(rows)
    rank_iter = iter(ranks)
    lines = []
    for row in collapsed:
        rank = next(rank_iter) if not row.get("collapsed") else 0
        lines.append(_overall_row_line(rank, row))
    return "\n".join(lines)


def _split_ranked_by_verified(ranked: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int], list[dict[str, Any]], list[int]]:
    """Like split_by_verified, but also returns each row's 1-based rank in `ranked`."""
    verified_rows, verified_ranks, unverified_rows, unverified_ranks = [], [], [], []
    for rank, row in enumerate(ranked, start=1):
        if verified_label(row) == "content":
            verified_rows.append(row)
            verified_ranks.append(rank)
        else:
            unverified_rows.append(row)
            unverified_ranks.append(rank)
    return verified_rows, verified_ranks, unverified_rows, unverified_ranks


def _overall_table(rows: list[dict[str, Any]], top_n: int) -> str:
    ranked = sorted(rows, key=lambda r: r["value_score"], reverse=True)[:top_n]
    header = ["| rank | value | confidence | top category | size | path | verified |", "|---|---|---|---|---|---|---|"]
    if not ranked:
        return "\n".join(header + ["| - | - | - | - | - | _no results_ | - |"])

    verified_rows, verified_ranks, unverified_rows, unverified_ranks = _split_ranked_by_verified(ranked)

    sections = list(header)
    if verified_rows:
        sections.append(_render_overall_group(verified_rows, verified_ranks))
    if unverified_rows:
        sections.append("| | | | | **Name-only matches (unverified)** | |")
        sections.append(_render_overall_group(unverified_rows, unverified_ranks))
    return "\n".join(sections)


def _category_row_line(rank: int, row: dict[str, Any], category: str) -> str:
    if row.get("collapsed"):
        return f"| - | - | - | ... and {row['count']} more in {row['dir']} | - |"
    probability = float(row["probabilities"][category])
    return f"| {rank} | {probability:.2f} | {_format_size(row['size'])} | {row['path']} | {verified_label(row)} |"


def _render_category_group(rows: list[dict[str, Any]], ranks: list[int], category: str) -> str:
    collapsed = collapse_siblings(rows)
    rank_iter = iter(ranks)
    lines = []
    for row in collapsed:
        rank = next(rank_iter) if not row.get("collapsed") else 0
        lines.append(_category_row_line(rank, row, category))
    return "\n".join(lines)


def _category_table(rows: list[dict[str, Any]], category: str, min_prob: float, top_n: int = 20) -> str:
    matches = [r for r in rows if float(r.get("probabilities", {}).get(category, 0) or 0) >= min_prob]
    matches.sort(key=lambda r: float(r["probabilities"][category]), reverse=True)
    matches = matches[:top_n]
    if not matches:
        return "_none above threshold_"

    verified_rows, verified_ranks, unverified_rows, unverified_ranks = _split_ranked_by_verified(matches)

    sections = ["| rank | probability | size | path | verified |", "|---|---|---|---|---|"]
    if verified_rows:
        sections.append(_render_category_group(verified_rows, verified_ranks, category))
    if unverified_rows:
        sections.append("| | | **Name-only matches (unverified)** | | |")
        sections.append(_render_category_group(unverified_rows, unverified_ranks, category))
    return "\n".join(sections)


def render_report_md(
    results: list[dict[str, Any]], dedupe_savings: int, spend_usd: float, top_n: int, min_prob: float
) -> str:
    rows = _valid_rows(results)
    errors = sum(1 for r in results if r.get("error"))

    sections = [
        "# hdd-analyzer report",
        "",
        f"- Files scanned: {len(results)}",
        f"- Files with errors: {errors}",
        f"- Estimated spend: ${spend_usd:.4f}",
        f"- Duplicate files skipped (dedupe savings): {dedupe_savings}",
        "",
        f"## Overall top {top_n}",
        "",
        _overall_table(rows, top_n),
        "",
    ]
    for category in NOUL_CATEGORIES:
        sections.append(f"## {category} (top 20, probability >= {min_prob})")
        sections.append("")
        sections.append(_category_table(rows, category, min_prob))
        sections.append("")
    return "\n".join(sections)


def write_report_csv(results: list[dict[str, Any]], csv_path: Path) -> None:
    fieldnames = [
        "path",
        "size",
        "category",
        "value_score",
        "value_confidence",
        "metadata_only",
        "extraction_status",
        "verified",
        "rubric_version",
        "error",
        "input_tokens",
        "dedupe_key",
        *NOUL_CATEGORIES,
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            flat = dict(row)
            flat["verified"] = verified_label(row)
            probabilities = row.get("probabilities") or {}
            for category in NOUL_CATEGORIES:
                flat[category] = probabilities.get(category, "")
            writer.writerow(flat)


def generate_report(run_dir: Path, top_n: int = DEFAULT_TOP_N, min_prob: float = MIN_PROB_DEFAULT) -> tuple[Path, Path]:
    """Write report.md and report.csv. report.html is report_html.write_report_html."""
    results = load_results(run_dir)
    dedupe_savings = load_dedupe_savings(run_dir)
    spend_usd = estimated_spend(results)

    md_path = run_dir / "report.md"
    csv_path = run_dir / "report.csv"
    md_path.write_text(render_report_md(results, dedupe_savings, spend_usd, top_n, min_prob), encoding="utf-8")
    write_report_csv(results, csv_path)
    return md_path, csv_path


def estimated_spend(results: list[dict[str, Any]]) -> float:
    return sum(_estimated_row_cost(r) for r in results)


def _estimated_row_cost(row: dict[str, Any]) -> float:
    tokens = row.get("input_tokens")
    return tokens_to_cost(tokens) if isinstance(tokens, int) else 0.0
