"""Scan stage: extraction + Jev classification under a hard budget cap."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient

from hdd_analyzer.budget import BudgetTracker, estimate_tokens, estimate_worst_case_tokens, tokens_to_cost
from hdd_analyzer.config import DEFAULT_CAP_USD, DEFAULT_CONCURRENCY, PER_CALL_OVERHEAD_TOKENS
from hdd_analyzer.extract import extract_excerpt
from hdd_analyzer.jev import build_state, classify_file
from hdd_analyzer.jev_provider import resolve_async_provider

SYSTEMIC_STATUS_CODES = frozenset({401, 402, 403})
CONSECUTIVE_SYSTEMIC_LIMIT = 5
CANARY_STATE = {"path": "canary.txt", "excerpt": "hello"}


def is_systemic_error(exc: BaseException) -> bool:
    """A systemic error means the whole run is doomed (bad key, no credits, no access).

    Per-file errors (validation, timeouts, transient 5xx) should not trip this;
    only auth/billing/permission failures do.
    """
    status = getattr(exc, "status", None)
    if status is not None:
        return status in SYSTEMIC_STATUS_CODES
    return False


class SystemicErrorTracker:
    """Counts consecutive systemic errors; any success resets the count."""

    def __init__(self, limit: int = CONSECUTIVE_SYSTEMIC_LIMIT) -> None:
        self._limit = limit
        self._consecutive = 0
        self.first_error: BaseException | None = None

    def record_success(self) -> None:
        self._consecutive = 0
        self.first_error = None

    def record_error(self, exc: BaseException) -> bool:
        """Record an error; return True if the abort threshold has now been reached."""
        if not is_systemic_error(exc):
            return False
        if self._consecutive == 0:
            self.first_error = exc
        self._consecutive += 1
        return self._consecutive >= self._limit

    def abort_reason(self) -> str:
        status = getattr(self.first_error, "status", "?")
        return f"aborting: {self._limit} consecutive auth/billing errors (HTTP {status}): {self.first_error}"

    def canary_abort_reason(self, exc: BaseException) -> str:
        status = getattr(exc, "status", "?")
        return f"aborting: preflight canary check failed (HTTP {status}): {exc}"


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


def build_candidates(records: list[dict[str, Any]], resumed_keys: set[str], limit: int | None = None) -> list[ScanCandidate]:
    """Filter to non-duplicate, unresolved records and run local extraction."""
    candidates: list[ScanCandidate] = []
    for record in records:
        if record.get("dup_of") is not None:
            continue
        key = record["dedupe_key"]
        if key in resumed_keys:
            continue
        if limit is not None and len(candidates) >= limit:
            break
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
    return len(candidate.excerpt) if candidate.excerpt else 0


def estimate_candidate_tokens(candidate: ScanCandidate) -> int:
    return PER_CALL_OVERHEAD_TOKENS + estimate_tokens(_candidate_char_count(candidate))


def estimate_candidate_worst_case_tokens(candidate: ScanCandidate) -> int:
    return PER_CALL_OVERHEAD_TOKENS + estimate_worst_case_tokens(_candidate_char_count(candidate))


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
) -> tuple[dict[str, Any], BaseException | None]:
    """Classify one candidate. Returns (result, raised_exception_or_none).

    The raised exception is surfaced alongside the error result row so the
    caller can distinguish systemic errors (auth/billing) from per-file ones
    without re-parsing error strings.
    """
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
        except Exception as exc:  # noqa: BLE001 - per-file errors must never abort the run here
            return {
                "dedupe_key": candidate.dedupe_key,
                "path": candidate.path,
                "error": str(exc),
            }, exc

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
    }, None


@dataclass(frozen=True)
class Estimate:
    candidate_count: int
    estimated_tokens: int
    estimated_cost_usd: float


def estimate_run(run_dir: Path, limit: int | None = None) -> Estimate:
    """Extraction-only, zero-network estimate of a scan's token/dollar cost."""
    records = load_inventory(run_dir)
    resumed_keys = load_resumed_keys(run_dir)
    candidates = build_candidates(records, resumed_keys, limit)

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
    aborted_reason: str | None = None


async def _run_canary(client: AsyncTypeSafeClient, tracker: BudgetTracker) -> BaseException | None:
    """One real call before dispatching candidates; catches a dead key/no credits early."""
    try:
        response = await classify_file(client, CANARY_STATE)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller for classification
        return exc
    usage = getattr(response, "usage", None)
    input_tokens = usage.input_tokens if usage is not None else None
    tracker.record_tokens(input_tokens, len(CANARY_STATE["excerpt"]))
    return None


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
    candidates = build_candidates(records, resumed_keys, limit)

    tracker = BudgetTracker(cap_usd=cap_usd)
    outcome = ScanOutcome()
    results_path = run_dir / "results.jsonl"
    semaphore = asyncio.Semaphore(concurrency)

    provider = resolve_async_provider(api_key)
    print(f"jev provider: {provider.name} (model={provider.model})")

    tracker_state = SystemicErrorTracker()

    async with AsyncTypeSafeClient(api_key=api_key, model=provider.model, **provider.client_kwargs) as client:
        canary_exc = await _run_canary(client, tracker)
        if canary_exc is not None and is_systemic_error(canary_exc):
            outcome.aborted_reason = tracker_state.canary_abort_reason(canary_exc)
            print(outcome.aborted_reason, file=sys.stderr)
            outcome.spent_usd = tracker.spent_usd
            return outcome

        with open(results_path, "a", encoding="utf-8") as out:
            batch_start = 0
            while batch_start < len(candidates):
                batch = candidates[batch_start : batch_start + concurrency]
                worst_case = sum(tokens_to_cost(estimate_candidate_worst_case_tokens(c)) for c in batch)
                if tracker.would_exceed_cap(worst_case):
                    outcome.stopped_at_cap = True
                    break

                pairs = await asyncio.gather(*(_classify_one(client, semaphore, c) for c in batch))
                aborted = False
                for result, exc in pairs:
                    if exc is not None:
                        if tracker_state.record_error(exc):
                            outcome.aborted_reason = tracker_state.abort_reason()
                            print(outcome.aborted_reason, file=sys.stderr)
                            aborted = True
                            break
                        if is_systemic_error(exc):
                            continue  # noise; do not persist, but keep counting

                    tracker_state.record_success()
                    fallback_chars = len(next(c.excerpt or "" for c in batch if c.dedupe_key == result["dedupe_key"]))
                    result = bill_result(tracker, result, fallback_chars)
                    out.write(json.dumps(result) + "\n")
                    out.flush()
                    outcome.processed += 1
                    if result.get("error"):
                        outcome.errors += 1

                outcome.spent_usd = tracker.spent_usd
                if aborted:
                    break
                batch_start += concurrency

    outcome.spent_usd = tracker.spent_usd
    return outcome
