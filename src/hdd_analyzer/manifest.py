"""Manifest stage: the salvage list to copy off a drive before it is wiped.

Read-only over an existing run's results.jsonl (via report.load_results,
which already collapses to the latest row per dedupe key) and inventory.jsonl
is never touched here. This module never writes results.jsonl or
inventory.jsonl; it only reads them and writes into its own manifest output
directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hdd_analyzer.paths import pure_path
from hdd_analyzer.report import NOUL_CATEGORIES, _format_size, collapse_siblings, load_results, verified_label

MANIFEST_DEFAULT_MIN_VALUE = 2.0
MANIFEST_DEFAULT_MIN_PROB = 0.7
CREDENTIALS_WARN_THRESHOLD = 0.6
TOP_DIRS = 30

# A verified read (actual content, or an OCR excerpt) is trusted on its own;
# a name-only judgment is only trusted for categories where a suggestive
# filename is itself real signal (passwords.xlsx, drivers_license.jpg). A
# name-only hit in, say, "irreplaceable" (a game save) is too weak to act on
# without ever having read the file.
NAME_ONLY_ELIGIBLE_CATEGORIES = ("credentials", "financial_legal", "personal")
# original_work is deliberately absent: authored code is usually also in a
# git remote, and Jev already scores such files low on `irreplaceable`
# (measured mean 0.20 on 450 content-verified original_work hits). Letting
# original_work qualify on its own turned an authored-code tree into 4,355
# "notable" rows on a 50k-file drive; without it the same drive has 820.
VERIFIED_ELIGIBLE_CATEGORIES = ("credentials", "financial_legal", "personal", "irreplaceable")
_VERIFIED_LABELS = ("content", "ocr")


def is_manifest_included(row: dict[str, Any], min_value: float, min_prob: float) -> bool:
    """True if `row` belongs in the salvage manifest.

    Excludes error rows and the "generated" category outright (in practice
    "generated" rows never reach results.jsonl at all, since scan never
    sends them to Jev, but the check is explicit here rather than assumed).
    Verified rows qualify on value_score or on a probability >= min_prob in
    any category except original_work; name-only (and legacy "unknown")
    rows qualify only through a probability >= min_prob in one of the three
    name-trustworthy categories.
    """
    if row.get("error"):
        return False
    if row.get("category") == "generated":
        return False

    probabilities = row.get("probabilities") or {}
    categories = VERIFIED_ELIGIBLE_CATEGORIES if verified_label(row) in _VERIFIED_LABELS else NAME_ONLY_ELIGIBLE_CATEGORIES

    if verified_label(row) in _VERIFIED_LABELS:
        value_score = row.get("value_score")
        if isinstance(value_score, (int, float)) and value_score >= min_value:
            return True

    return any(
        isinstance(probabilities.get(category), (int, float)) and probabilities[category] >= min_prob
        for category in categories
    )


def select_manifest_rows(results: list[dict[str, Any]], min_value: float, min_prob: float) -> list[dict[str, Any]]:
    return [row for row in results if is_manifest_included(row, min_value, min_prob)]


def split_verified_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (verified, name_only), where verified means content or ocr."""
    verified = [r for r in rows if verified_label(r) in _VERIFIED_LABELS]
    name_only = [r for r in rows if verified_label(r) not in _VERIFIED_LABELS]
    return verified, name_only


def _sort_key(row: dict[str, Any]) -> tuple[str, str]:
    path = pure_path(row["path"])
    return str(path.parent).lower(), path.name.lower()


def sorted_by_directory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort rows by (parent directory, filename) so a directory's files stay contiguous.

    Robocopy and rsync both benefit from a copy list grouped by source
    directory rather than scattered in arbitrary order.
    """
    return sorted(rows, key=_sort_key)


def _write_path_list(rows: list[dict[str, Any]], out_path: Path) -> None:
    ordered = sorted_by_directory(rows)
    text = "\n".join(row["path"] for row in ordered)
    out_path.write_text(text + "\n" if ordered else "", encoding="utf-8")


def directory_rollup(rows: list[dict[str, Any]], top_n: int = TOP_DIRS) -> list[tuple[str, int]]:
    """Top `top_n` directories by count of `rows` they contain, most first."""
    counts: dict[str, int] = {}
    for row in rows:
        directory = str(pure_path(row["path"]).parent)
        counts[directory] = counts.get(directory, 0) + 1
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[:top_n]


def _credentials_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in results:
        if row.get("error"):
            continue
        probability = (row.get("probabilities") or {}).get("credentials")
        if isinstance(probability, (int, float)) and probability >= CREDENTIALS_WARN_THRESHOLD:
            rows.append(row)
    rows.sort(key=lambda r: float(r["probabilities"]["credentials"]), reverse=True)
    return rows


def render_credentials_md(results: list[dict[str, Any]]) -> str:
    rows = _credentials_rows(results)
    lines = [
        "# Credential exposure list",
        "",
        "Every file below scored at or above a 0.6 probability of containing "
        "credentials (passwords, API keys, private keys, tokens, or similar). "
        "Treat every one of these as compromised if this drive ever left your "
        "custody, and rotate any credential that is still valid. This list is "
        "paths only; no excerpts or file content are included here.",
        "",
        "| probability | verified | size | path |",
        "|---|---|---|---|",
    ]
    if not rows:
        lines.append("| - | - | - | _none above threshold_ |")
        return "\n".join(lines) + "\n"
    for row in rows:
        probability = float(row["probabilities"]["credentials"])
        lines.append(f"| {probability:.2f} | {verified_label(row)} | {_format_size(row['size'])} | {row['path']} |")
    return "\n".join(lines) + "\n"


def _category_matches(rows: list[dict[str, Any]], category: str, min_prob: float) -> list[dict[str, Any]]:
    matches = [
        r for r in rows if isinstance((r.get("probabilities") or {}).get(category), (int, float)) and r["probabilities"][category] >= min_prob
    ]
    matches.sort(key=lambda r: float(r["probabilities"][category]), reverse=True)
    return matches


def _render_category_table(rows: list[dict[str, Any]], category: str) -> str:
    if not rows:
        return "_none_"
    lines = ["| rank | probability | verified | size | path |", "|---|---|---|---|---|"]
    rank = 0
    for row in collapse_siblings(rows):
        if row.get("collapsed"):
            lines.append(f"| - | - | - | - | ... and {row['count']} more in {row['dir']} |")
            continue
        rank += 1
        probability = float(row["probabilities"][category])
        lines.append(f"| {rank} | {probability:.2f} | {verified_label(row)} | {_format_size(row['size'])} | {row['path']} |")
    return "\n".join(lines)


def render_by_category_md(included_rows: list[dict[str, Any]], min_prob: float) -> str:
    sections = ["# Manifest by category", ""]
    for category in NOUL_CATEGORIES:
        matches = _category_matches(included_rows, category, min_prob)
        sections.append(f"## {category} (probability >= {min_prob})")
        sections.append("")
        sections.append(_render_category_table(matches, category))
        sections.append("")

    sections.append(f"## Top {TOP_DIRS} directories by included file count")
    sections.append("")
    rollup = directory_rollup(included_rows)
    if not rollup:
        sections.append("_none_")
    else:
        sections.append("| count | directory |")
        sections.append("|---|---|")
        for directory, count in rollup:
            sections.append(f"| {count} | {directory} |")
    sections.append("")
    return "\n".join(sections)


_ROBOCOPY_EXAMPLE = """Example: driving robocopy from copy-list.txt (PowerShell)

Robocopy copies from one source directory to one destination directory at a
time, so group the flat copy list by source directory first, then invoke
robocopy once per directory with the file names as trailing file-filter
arguments:

    Get-Content copy-list.txt | Group-Object { Split-Path $_ } | ForEach-Object {
        $sourceDir = $_.Name
        $destDir = Join-Path "D:\\salvage" ($sourceDir -replace "^[A-Za-z]:\\\\|^\\\\\\\\", "")
        $files = $_.Group | ForEach-Object { Split-Path $_ -Leaf }
        robocopy $sourceDir $destDir $files /R:1 /W:1
    }

UNC source paths (\\\\wsl.localhost\\...) work the same way; robocopy accepts
them directly as a source or destination, unchanged.
"""


def render_summary(included_rows: list[dict[str, Any]], min_prob: float, out_dir: Path) -> str:
    verified_rows, name_only_rows = split_verified_rows(included_rows)
    total_bytes = sum(row.get("size", 0) for row in included_rows)

    lines = [
        "hdd-analyzer manifest summary",
        "",
        f"total included: {len(included_rows)}",
        f"content/ocr verified: {len(verified_rows)}",
        f"name-only (unverified): {len(name_only_rows)}",
        "",
        f"by category (probability >= {min_prob}):",
    ]
    for category in NOUL_CATEGORIES:
        count = len(_category_matches(included_rows, category, min_prob))
        lines.append(f"  {category}: {count}")

    lines.extend(
        [
            "",
            f"total bytes to copy: {_format_size(total_bytes)} ({total_bytes:,} bytes)",
            "",
            "copy list files:",
            f"  {out_dir / 'copy-list.txt'} (everything included)",
            f"  {out_dir / 'copy-list-verified.txt'} (content/ocr verified only)",
            f"  {out_dir / 'copy-list-name-only.txt'} (unverified, judged by filename)",
            "",
            _ROBOCOPY_EXAMPLE,
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class ManifestOutcome:
    total: int
    verified: int
    name_only: int
    total_bytes: int
    out_dir: Path


def generate_manifest(
    run_dir: Path,
    out_dir: Path | None = None,
    min_value: float = MANIFEST_DEFAULT_MIN_VALUE,
    min_prob: float = MANIFEST_DEFAULT_MIN_PROB,
) -> ManifestOutcome:
    """Build the salvage manifest for `run_dir` into `out_dir` (default runs/NAME/manifest).

    Read-only over results.jsonl; never writes results.jsonl or
    inventory.jsonl.
    """
    results = load_results(run_dir)
    included = select_manifest_rows(results, min_value, min_prob)
    verified_rows, name_only_rows = split_verified_rows(included)

    target_dir = out_dir if out_dir is not None else run_dir / "manifest"
    target_dir.mkdir(parents=True, exist_ok=True)

    _write_path_list(included, target_dir / "copy-list.txt")
    _write_path_list(verified_rows, target_dir / "copy-list-verified.txt")
    _write_path_list(name_only_rows, target_dir / "copy-list-name-only.txt")
    (target_dir / "credentials.md").write_text(render_credentials_md(results), encoding="utf-8")
    (target_dir / "by-category.md").write_text(render_by_category_md(included, min_prob), encoding="utf-8")
    (target_dir / "summary.txt").write_text(render_summary(included, min_prob, target_dir), encoding="utf-8")

    return ManifestOutcome(
        total=len(included),
        verified=len(verified_rows),
        name_only=len(name_only_rows),
        total_bytes=sum(row.get("size", 0) for row in included),
        out_dir=target_dir,
    )
