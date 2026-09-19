"""Scan stage: extraction + Jev classification under a hard budget cap."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient

from hdd_analyzer.budget import BudgetTracker, estimate_tokens, estimate_worst_case_tokens, tokens_to_cost
from hdd_analyzer.config import DEFAULT_CAP_USD, DEFAULT_CONCURRENCY
from hdd_analyzer.extract import extract_excerpt
from hdd_analyzer.jev import build_state, classify_file
from hdd_analyzer.jev_provider import resolve_async_provider


@dataclass(frozen=True)
class ScanCandidate:
    path: str
    size: int
    mtime: float
    ext: str
    category: str
    dedupe_key: str
    excerpt: str | None
    metadata_only: bool


def load_inventory(run_dir: Path) -> list[dict[str, Any]]:
    inventory_path = run_dir / "inventory.jsonl"
    if not inventory_path.exists():
        raise FileNotFoundError(f"no inventory found at {inventory_path}; run `walk` first")
    with open(inventory_path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_resumed_keys(run_dir: Path) -> set[str]:
    results_path = run_dir / "results.jsonl"
    if not results_path.exists():
        return set()
    keys: set[str] = set()
    with open(results_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("error") is not None:
                continue
            key = record.get("dedupe_key")
            if key:
                keys.add(key)
    return keys


def build_candidates(records: list[dict[str, Any]], resumed_keys: set[str]) -> list[ScanCandidate]:
    """Filter to non-duplicate, unresolved records and run local extraction."""
    candidates: list[ScanCandidate] = []
    for record in records:
        if record.get("dup_of") is not None:
            continue
        key = record["dedupe_key"]
        if key in resumed_keys:
            continue
        path = Path(record["path"])
        excerpt, metadata_only = extract_excerpt(path, record["category"], record["ext"])
        candidates.append(
            ScanCandidate(
                path=record["path"],
                size=record["size"],
                mtime=record["mtime"],
                ext=record["ext"],
                category=record["category"],
                dedupe_key=key,
                excerpt=excerpt,
                metadata_only=metadata_only,
            )
        )
    return candidates


def _candidate_char_count(candidate: ScanCandidate) -> int:
    return (len(candidate.excerpt) if candidate.excerpt else 0) + 200  # + fixed overhead for questions/schema


def estimate_candidate_tokens(candidate: ScanCandidate) -> int:
    return estimate_tokens(_candidate_char_count(candidate))


def estimate_candidate_worst_case_tokens(candidate: ScanCandidate) -> int:
    return estimate_worst_case_tokens(_candidate_char_count(candidate))


def bill_result(tracker: BudgetTracker, result: dict[str, Any], fallback_chars: int) -> dict[str, Any]:
    """Bill a completed classification result against `tracker`.

    Error results are never billed (nothing was actually sent/received), and
    are recorded with 0 input tokens. Successful results are billed on the
    real `input_tokens` from the API response when present, falling back to
    the char-based estimate only when a successful response lacks usage
    data. Returns `result` with `input_tokens` normalized.
    """
    if result.get("error"):
        return {**result, "input_tokens": 0}
    tracker.record_tokens(result.get("input_tokens"), fallback_chars)
    return result


async def _classify_one(
    client: AsyncTypeSafeClient,
    semaphore: asyncio.Semaphore,
    candidate: ScanCandidate,
) -> dict[str, Any]:
    state = build_state(
        path=candidate.path,
        name=Path(candidate.path).name,
        ext=candidate.ext,
        size=candidate.size,
        modified=candidate.mtime,
        excerpt=candidate.excerpt,
        metadata_only=candidate.metadata_only,
    )
    async with semaphore:
        try:
            response = await classify_file(client, state)
        except Exception as exc:  # noqa: BLE001 - per-file errors must never abort the run
            return {
                "dedupe_key": candidate.dedupe_key,
                "path": candidate.path,
                "error": str(exc),
            }

    probabilities: dict[str, float] = {}
    value_score = None
    value_confidence = None
    for name, answer in response.answers.items():
        if answer.type == "noul":
            probabilities[name] = answer.noul
        elif answer.type == "score":
            value_score = answer.score
            value_confidence = answer.confidence
            probabilities[name] = answer.probabilities

    usage = getattr(response, "usage", None)
    input_tokens = usage.input_tokens if usage is not None else None

    return {
        "dedupe_key": candidate.dedupe_key,
        "path": candidate.path,
        "size": candidate.size,
        "category": candidate.category,
        "probabilities": probabilities,
        "value_score": value_score,
        "value_confidence": value_confidence,
        "input_tokens": input_tokens,
        "error": None,
    }


@dataclass(frozen=True)
class Estimate:
    candidate_count: int
    estimated_tokens: int
    estimated_cost_usd: float


def estimate_run(run_dir: Path, limit: int | None = None) -> Estimate:
    """Extraction-only, zero-network estimate of a scan's token/dollar cost."""
    records = load_inventory(run_dir)
    resumed_keys = load_resumed_keys(run_dir)
    candidates = build_candidates(records, resumed_keys)
    if limit is not None:
        candidates = candidates[:limit]

    total_tokens = sum(estimate_candidate_tokens(c) for c in candidates)
    return Estimate(
        candidate_count=len(candidates),
        estimated_tokens=total_tokens,
        estimated_cost_usd=tokens_to_cost(total_tokens),
    )


@dataclass
class ScanOutcome:
    processed: int = 0
    spent_usd: float = 0.0
    stopped_at_cap: bool = False
    errors: int = 0


async def run_scan(
    run_dir: Path,
    api_key: str | None,
    cap_usd: float = DEFAULT_CAP_USD,
    limit: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> ScanOutcome:
    """Dispatch classification calls for unresolved candidates, batch by batch."""
    records = load_inventory(run_dir)
    resumed_keys = load_resumed_keys(run_dir)
    candidates = build_candidates(records, resumed_keys)
    if limit is not None:
        candidates = candidates[:limit]

    tracker = BudgetTracker(cap_usd=cap_usd)
    outcome = ScanOutcome()
    results_path = run_dir / "results.jsonl"
    semaphore = asyncio.Semaphore(concurrency)

    provider = resolve_async_provider(api_key)
    print(f"jev provider: {provider.name} (model={provider.model})")

    async with AsyncTypeSafeClient(api_key=api_key, model=provider.model, **provider.client_kwargs) as client:
        with open(results_path, "a", encoding="utf-8") as out:
            batch_start = 0
            while batch_start < len(candidates):
                batch = candidates[batch_start : batch_start + concurrency]
                worst_case = sum(tokens_to_cost(estimate_candidate_worst_case_tokens(c)) for c in batch)
                if tracker.would_exceed_cap(worst_case):
                    outcome.stopped_at_cap = True
                    break

                results = await asyncio.gather(*(_classify_one(client, semaphore, c) for c in batch))
                for result in results:
                    fallback_chars = len(next(c.excerpt or "" for c in batch if c.dedupe_key == result["dedupe_key"]))
                    result = bill_result(tracker, result, fallback_chars)
                    out.write(json.dumps(result) + "\n")
                    out.flush()
                    outcome.processed += 1
                    if result.get("error"):
                        outcome.errors += 1

                outcome.spent_usd = tracker.spent_usd
                batch_start += concurrency

    outcome.spent_usd = tracker.spent_usd
    return outcome
