"""Backfill extraction_status/metadata_only onto existing results, zero API calls.

Older results.jsonl rows predate extraction_status tracking, so a report
generated from them cannot tell a content-verified hit from a name-only
guess. `annotate_run` re-runs local extraction (no network) for the rows
that currently surface in report.md, or every row with --all, and appends
corrected rows to results.jsonl. `report.load_results` already keeps only
the latest row per dedupe_key, so the corrected rows simply supersede the
originals on the next report render.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hdd_analyzer.extract import extract_excerpt
from hdd_analyzer.report import MIN_PROB_DEFAULT, NOUL_CATEGORIES, DEFAULT_TOP_N, load_results


def surfaced_dedupe_keys(results: list[dict[str, Any]], top_n: int, min_prob: float) -> set[str]:
    """Dedupe keys of every row that appears in the overall top-N or any category table."""
    valid = [r for r in results if not r.get("error") and r.get("value_score") is not None]
    keys: set[str] = set()

    overall = sorted(valid, key=lambda r: r["value_score"], reverse=True)[:top_n]
    keys.update(r["dedupe_key"] for r in overall)

    for category in NOUL_CATEGORIES:
        matches = [r for r in valid if float(r.get("probabilities", {}).get(category, 0) or 0) >= min_prob]
        matches.sort(key=lambda r: float(r["probabilities"][category]), reverse=True)
        keys.update(r["dedupe_key"] for r in matches[:20])

    return keys


@dataclass(frozen=True)
class AnnotateOutcome:
    considered: int
    updated: int
    became_name_only: list[str]
    rescan_candidates: list[str]


def annotate_run(run_dir: Path, all_rows: bool = False, top_n: int = DEFAULT_TOP_N, min_prob: float = MIN_PROB_DEFAULT) -> AnnotateOutcome:
    """Backfill missing extraction_status/metadata_only, without overwriting known scan-time judgments.

    The report's "verified" column must reflect what the classifier actually
    saw when it was scored, not what extraction can do today. A row that
    already carries an extraction_status is a real record of that scan-time
    judgment, so it is left untouched here even if a categorization or
    extraction fix means the same file would extract successfully today;
    retroactively relabeling it as content-verified would misrepresent what
    Jev actually received. Re-extraction only backfills rows that predate
    extraction_status tracking entirely (the field is absent).

    Rows with a stored non-ok status where today's extraction *would* now
    succeed are reported separately as `rescan_candidates`: good targets for
    a cheap, targeted `scan --only-name-only` re-classification.
    """
    inventory_path = run_dir / "inventory.jsonl"
    if not inventory_path.exists():
        raise FileNotFoundError(f"no inventory found at {inventory_path}; run `walk` first")

    by_key: dict[str, dict[str, Any]] = {}
    with open(inventory_path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            by_key[record["dedupe_key"]] = record

    results = load_results(run_dir)
    target_keys = None if all_rows else surfaced_dedupe_keys(results, top_n, min_prob)

    results_path = run_dir / "results.jsonl"
    considered = 0
    updated = 0
    became_name_only: list[str] = []
    rescan_candidates: list[str] = []

    with open(results_path, "a", encoding="utf-8") as out:
        for row in results:
            key = row.get("dedupe_key")
            if target_keys is not None and key not in target_keys:
                continue
            record = by_key.get(key)
            if record is None:
                continue

            considered += 1
            path = Path(record["path"])
            excerpt, metadata_only, extraction_status = extract_excerpt(
                path, record["category"], record["ext"], record["size"]
            )

            stored_status = row.get("extraction_status")
            if stored_status is not None:
                if stored_status != "ok" and extraction_status == "ok":
                    rescan_candidates.append(row["path"])
                continue

            new_row = {**row, "metadata_only": metadata_only, "extraction_status": extraction_status}
            out.write(json.dumps(new_row) + "\n")
            updated += 1

            if extraction_status != "ok":
                became_name_only.append(row["path"])

    return AnnotateOutcome(
        considered=considered, updated=updated, became_name_only=became_name_only, rescan_candidates=rescan_candidates
    )
