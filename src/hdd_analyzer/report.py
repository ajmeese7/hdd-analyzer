"""Report stage: rank results and render report.md + report.csv."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from hdd_analyzer.budget import tokens_to_cost

NOUL_CATEGORIES = ("credentials", "personal", "financial_legal", "original_work", "irreplaceable")
DEFAULT_TOP_N = 25
CATEGORY_MIN_PROB = 0.6


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    results_path = run_dir / "results.jsonl"
    if not results_path.exists():
        raise FileNotFoundError(f"no results found at {results_path}; run `scan` first")
    with open(results_path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


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


def _overall_table(rows: list[dict[str, Any]], top_n: int) -> str:
    ranked = sorted(rows, key=lambda r: r["value_score"], reverse=True)[:top_n]
    lines = ["| rank | value | confidence | top category | size | path |", "|---|---|---|---|---|---|"]
    for rank, row in enumerate(ranked, start=1):
        lines.append(
            f"| {rank} | {row['value_score']:.2f} | {row.get('value_confidence') or 0:.2f} | "
            f"{_top_category(row.get('probabilities', {}))} | {_format_size(row['size'])} | {row['path']} |"
        )
    return "\n".join(lines)


def _category_table(rows: list[dict[str, Any]], category: str, top_n: int = 20) -> str:
    matches = [r for r in rows if float(r.get("probabilities", {}).get(category, 0) or 0) >= CATEGORY_MIN_PROB]
    matches.sort(key=lambda r: float(r["probabilities"][category]), reverse=True)
    matches = matches[:top_n]
    if not matches:
        return "_none above threshold_"
    lines = ["| rank | probability | size | path |", "|---|---|---|---|"]
    for rank, row in enumerate(matches, start=1):
        probability = float(row["probabilities"][category])
        lines.append(f"| {rank} | {probability:.2f} | {_format_size(row['size'])} | {row['path']} |")
    return "\n".join(lines)


def render_report_md(results: list[dict[str, Any]], dedupe_savings: int, spend_usd: float, top_n: int) -> str:
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
        sections.append(f"## {category} (top 20, probability >= {CATEGORY_MIN_PROB})")
        sections.append("")
        sections.append(_category_table(rows, category))
        sections.append("")
    return "\n".join(sections)


def write_report_csv(results: list[dict[str, Any]], csv_path: Path) -> None:
    fieldnames = [
        "path",
        "size",
        "category",
        "value_score",
        "value_confidence",
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
            probabilities = row.get("probabilities") or {}
            for category in NOUL_CATEGORIES:
                flat[category] = probabilities.get(category, "")
            writer.writerow(flat)


def generate_report(run_dir: Path, top_n: int = DEFAULT_TOP_N) -> tuple[Path, Path]:
    results = load_results(run_dir)
    dedupe_savings = load_dedupe_savings(run_dir)
    spend_usd = sum(_estimated_row_cost(r) for r in results)

    md_path = run_dir / "report.md"
    csv_path = run_dir / "report.csv"
    md_path.write_text(render_report_md(results, dedupe_savings, spend_usd, top_n), encoding="utf-8")
    write_report_csv(results, csv_path)
    return md_path, csv_path


def _estimated_row_cost(row: dict[str, Any]) -> float:
    tokens = row.get("input_tokens")
    return tokens_to_cost(tokens) if isinstance(tokens, int) else 0.0
