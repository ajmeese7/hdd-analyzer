"""Scan stage: extraction + Jev classification under a hard budget cap."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from hdd_analyzer.budget import BudgetTracker, estimate_tokens, estimate_worst_case_tokens, tokens_to_cost
from hdd_analyzer.config import DEFAULT_CAP_USD, DEFAULT_CONCURRENCY, PER_CALL_OVERHEAD_TOKENS, categorize
from hdd_analyzer.extract import _extraction_eligible, extract_excerpt, extract_excerpt_async
from hdd_analyzer.jev import RUBRIC_VERSION, build_state, classify_file
from hdd_analyzer.jev_provider import resolve_async_provider

SYSTEMIC_STATUS_CODES = frozenset({401, 402, 403})
CONSECUTIVE_SYSTEMIC_LIMIT = 5
CANARY_STATE = {"path": "canary.txt", "excerpt": "hello"}

# The SDK default (2 retries, 0.5s to 5s backoff) turns a shared gateway's
# 429 burst into a wall of error rows. Jev calls are cheap and fast, so
# waiting out a burst is always better than recording a failure.
RETRY_POLICY = RetryPolicy(max_retries=6, backoff_initial=1.0, backoff_max=20.0, timeout=120.0)


class Pacer:
    """Space request starts evenly at `rpm` per minute across every worker.

    Vercel AI Gateway's free tier caps a model at 30 requests/minute, and a
    scan that ignores that spends its time in 429 backoff rather than
    classifying. `rpm=None` disables pacing.
    """

    def __init__(self, rpm: float | None) -> None:
        self.interval = 60.0 / rpm if rpm else 0.0
        self._next_start = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_start - now)
            self._next_start = max(now, self._next_start) + self.interval
        if delay:
            await asyncio.sleep(delay)

# extraction_status for a candidate whose excerpt came from `scan --from-ocr`
# (see excerpt_override below) rather than from extract.py.
EXTRACTION_STATUS_OCR = "ocr"


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
    extraction_status: str


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


def load_ocr_classified_keys(run_dir: Path) -> set[str]:
    """Dedupe keys whose latest result was already classified on OCR evidence."""
    from hdd_analyzer.report import load_results

    if not (run_dir / "results.jsonl").exists():
        return set()
    return {r["dedupe_key"] for r in load_results(run_dir) if r.get("extraction_status") == EXTRACTION_STATUS_OCR}


def load_outdated_rubric_keys(run_dir: Path) -> set[str]:
    """Dedupe keys whose latest valid result was scored under an older rubric.

    Scores from different rubric versions are not comparable (see
    jev.RUBRIC_VERSION), so once a rubric changes, every row still carrying
    the old version is worth re-scoring; `scan --outdated-rubric` does that,
    name-only rows included, since their old scores are just as stale.
    """
    from hdd_analyzer.report import load_results

    if not (run_dir / "results.jsonl").exists():
        return set()
    return {
        row["dedupe_key"]
        for row in load_results(run_dir)
        if not row.get("error") and row.get("rubric_version") != RUBRIC_VERSION
    }


def load_name_only_keys(run_dir: Path) -> set[str]:
    """Dedupe keys worth a targeted `scan --only-name-only` re-classification.

    A key qualifies only if both hold: the stored result was judged from
    filename alone (`was_name_only`), and today's extraction would actually
    be attempted for it (`extract._extraction_eligible`, using today's
    `categorize(ext)` since the categorizer may have changed since the
    original scan). This is a pure selection decision based on inventory
    metadata only, no file IO; a row that's still name-only after actual
    extraction just gets rewritten with its unchanged status, same as today.
    """
    from hdd_analyzer.report import load_results

    if not (run_dir / "results.jsonl").exists():
        return set()
    name_only_keys = {row["dedupe_key"] for row in load_results(run_dir) if not row.get("error") and was_name_only(row)}
    if not name_only_keys:
        return name_only_keys

    inventory_by_key = {r["dedupe_key"]: r for r in load_inventory(run_dir)}
    return {key for key in name_only_keys if _extraction_eligible_today(inventory_by_key.get(key))}


def _extraction_eligible_today(inventory_record: dict[str, Any] | None) -> bool:
    if inventory_record is None:
        return False
    ext = inventory_record["ext"]
    category_today = categorize(ext)
    eligible, _sniff = _extraction_eligible(category_today, ext, inventory_record["size"])
    return eligible


# Categories the original (rubric v1) scan attempted content extraction for.
_LEGACY_EXTRACTED_CATEGORIES = frozenset({"text", "code", "doc"})


def was_name_only(record: dict[str, Any]) -> bool:
    """True if the classifier judged this result row from filename and metadata alone.

    Rows written before extraction_status existed are inferred from their
    stored category: the v1 scan only ever read text, code, and doc files.
    """
    status = record.get("extraction_status")
    if status is not None:
        return status != "ok"
    return record.get("category") not in _LEGACY_EXTRACTED_CATEGORIES


# Engine/tool-generated metadata (Unity .meta sidecars, project artifacts):
# no independent value, never worth a Jev call. Excluded from candidates the
# same way a duplicate is, regardless of dup_of.
_EXCLUDED_CATEGORIES = frozenset({"generated"})


def eligible_records(
    records: list[dict[str, Any]],
    resumed_keys: set[str],
    limit: int | None = None,
    only_keys: set[str] | None = None,
    exclude_exts: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter inventory records to non-duplicate, unresolved candidates, honoring `limit`.

    `only_keys`, when given, further restricts candidates to that set of
    dedupe keys (used by `scan --only-name-only` for a cheap targeted
    re-scan of previously name-only-judged rows). `exclude_exts`, when
    given, drops any record whose extension is in the set (`--exclude-ext`).
    """
    eligible: list[dict[str, Any]] = []
    for record in records:
        if record.get("dup_of") is not None:
            continue
        if record.get("category") in _EXCLUDED_CATEGORIES:
            continue
        if exclude_exts is not None and record["ext"].lower() in exclude_exts:
            continue
        key = record["dedupe_key"]
        if only_keys is not None:
            # only_keys already encodes "must re-scan this" (extraction_status
            # != ok), so a prior successful-but-name-only result must not be
            # skipped by the normal resume check here.
            if key not in only_keys:
                continue
        elif key in resumed_keys:
            continue
        if limit is not None and len(eligible) >= limit:
            break
        eligible.append(record)
    return eligible


def _candidate_from_record(record: dict[str, Any], excerpt: str | None, metadata_only: bool, extraction_status: str) -> ScanCandidate:
    return ScanCandidate(
        path=record["path"],
        size=record["size"],
        mtime=record["mtime"],
        ext=record["ext"],
        category=record["category"],
        dedupe_key=record["dedupe_key"],
        excerpt=excerpt,
        metadata_only=metadata_only,
        extraction_status=extraction_status,
    )


def build_candidates(
    records: list[dict[str, Any]],
    resumed_keys: set[str],
    limit: int | None = None,
    only_keys: set[str] | None = None,
    exclude_exts: set[str] | None = None,
    excerpt_override: dict[str, str] | None = None,
) -> list[ScanCandidate]:
    """Filter to non-duplicate, unresolved records and run local extraction, sequentially.

    Used by `estimate_run`, where a simple sequential pass is fine since no
    API calls are in flight to overlap with. `run_scan` uses the pipelined
    producer/consumer path instead so extraction IO overlaps API latency.
    `excerpt_override` (see `scan --from-ocr`) short-circuits extraction
    entirely for any dedupe key present in it, using the given text as the
    excerpt with `extraction_status` "ocr" instead.
    """
    candidates: list[ScanCandidate] = []
    for record in eligible_records(records, resumed_keys, limit, only_keys, exclude_exts):
        override = excerpt_override.get(record["dedupe_key"]) if excerpt_override else None
        if override is not None:
            candidates.append(_candidate_from_record(record, override, False, EXTRACTION_STATUS_OCR))
            continue
        path = Path(record["path"])
        excerpt, metadata_only, extraction_status = extract_excerpt(path, record["category"], record["ext"], record["size"])
        candidates.append(_candidate_from_record(record, excerpt, metadata_only, extraction_status))
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
    pacer: Pacer,
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
        await pacer.wait()
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
        "metadata_only": candidate.metadata_only,
        "extraction_status": candidate.extraction_status,
        "rubric_version": RUBRIC_VERSION,
        "error": None,
    }, None


@dataclass(frozen=True)
class Estimate:
    candidate_count: int
    estimated_tokens: int
    estimated_cost_usd: float


def estimate_run(
    run_dir: Path,
    limit: int | None = None,
    only_keys: set[str] | None = None,
    exclude_exts: set[str] | None = None,
    excerpt_override: dict[str, str] | None = None,
) -> Estimate:
    """Extraction-only, zero-network estimate of a scan's token/dollar cost."""
    records = load_inventory(run_dir)
    resumed_keys = load_resumed_keys(run_dir)
    candidates = build_candidates(records, resumed_keys, limit, only_keys, exclude_exts, excerpt_override)

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


async def _extract_worker(
    input_queue: asyncio.Queue,
    out_queue: asyncio.Queue,
    excerpt_override: dict[str, str] | None = None,
    skip_metadata_only: bool = False,
) -> None:
    """Pull records off `input_queue`, extract, push finished candidates to `out_queue`.

    With `skip_metadata_only`, candidates whose content still cannot be read
    are dropped instead of dispatched: a name-only re-scan of a row that is
    still name-only would only reproduce the verdict already on disk.
    """
    while True:
        try:
            record = input_queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        override = excerpt_override.get(record["dedupe_key"]) if excerpt_override else None
        if override is not None:
            await out_queue.put(_candidate_from_record(record, override, False, EXTRACTION_STATUS_OCR))
            continue
        path = Path(record["path"])
        excerpt, metadata_only, extraction_status = await extract_excerpt_async(
            path, record["category"], record["ext"], record["size"]
        )
        if skip_metadata_only and metadata_only:
            continue
        await out_queue.put(_candidate_from_record(record, excerpt, metadata_only, extraction_status))


async def _run_extraction_pipeline(
    records: list[dict[str, Any]],
    out_queue: asyncio.Queue,
    concurrency: int,
    excerpt_override: dict[str, str] | None = None,
    skip_metadata_only: bool = False,
) -> None:
    """Extract every record concurrently, then signal completion with a sentinel.

    Runs as its own task alongside the classification consumer, so IO for
    the next batch overlaps API latency for the current one instead of
    running entirely upfront. `excerpt_override` is forwarded to each worker
    (see `scan --from-ocr`).
    """
    input_queue: asyncio.Queue = asyncio.Queue()
    for record in records:
        input_queue.put_nowait(record)

    workers = [
        asyncio.create_task(_extract_worker(input_queue, out_queue, excerpt_override, skip_metadata_only))
        for _ in range(concurrency)
    ]
    await asyncio.gather(*workers)
    await out_queue.put(None)


async def _next_batch(out_queue: asyncio.Queue, batch_size: int) -> tuple[list[ScanCandidate], bool]:
    """Collect up to `batch_size` candidates from `out_queue`.

    Returns (batch, extraction_done). extraction_done is True once the
    producer's sentinel has been consumed, whether or not the batch is full.
    """
    batch: list[ScanCandidate] = []
    while len(batch) < batch_size:
        item = await out_queue.get()
        if item is None:
            return batch, True
        batch.append(item)
    return batch, False


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
    only_keys: set[str] | None = None,
    exclude_exts: set[str] | None = None,
    excerpt_override: dict[str, str] | None = None,
    rpm: float | None = None,
    skip_metadata_only: bool | None = None,
) -> ScanOutcome:
    """Dispatch classification calls for unresolved candidates, batch by batch.

    Extraction and classification are pipelined: a producer task extracts
    records concurrently and feeds finished candidates onto a queue while the
    consumer loop below classifies whatever batch is ready, so file IO for
    the next batch overlaps API latency for the current one. `only_keys`
    restricts candidates to a specific dedupe-key set (see `--only-name-only`).
    `exclude_exts` drops extensions on top of that (see `--exclude-ext`).
    `excerpt_override` short-circuits extraction for the given dedupe keys
    (see `--from-ocr`). `rpm` caps classification requests per minute (see
    `--rpm`). `skip_metadata_only` drops candidates whose content still
    cannot be read; it defaults to on for a targeted `only_keys` rescan,
    since re-sending a name-only row reproduces the verdict already on
    disk, and `--outdated-rubric` turns it off because a stale score is
    stale whether or not the file was readable.
    """
    if skip_metadata_only is None:
        skip_metadata_only = only_keys is not None
    records = load_inventory(run_dir)
    resumed_keys = load_resumed_keys(run_dir)
    to_extract = eligible_records(records, resumed_keys, limit, only_keys, exclude_exts)

    tracker = BudgetTracker(cap_usd=cap_usd)
    outcome = ScanOutcome()
    results_path = run_dir / "results.jsonl"
    semaphore = asyncio.Semaphore(concurrency)
    pacer = Pacer(rpm)
    out_queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 4)

    provider = resolve_async_provider(api_key)
    print(f"jev provider: {provider.name} (model={provider.model})")

    tracker_state = SystemicErrorTracker()
    extraction_task = asyncio.create_task(
        _run_extraction_pipeline(to_extract, out_queue, concurrency, excerpt_override, skip_metadata_only=skip_metadata_only)
    )

    async with AsyncTypeSafeClient(
        api_key=api_key, model=provider.model, retry=RETRY_POLICY, **provider.client_kwargs
    ) as client:
        canary_exc = await _run_canary(client, tracker)
        if canary_exc is not None and is_systemic_error(canary_exc):
            outcome.aborted_reason = tracker_state.canary_abort_reason(canary_exc)
            print(outcome.aborted_reason, file=sys.stderr)
            await _cancel_and_wait(extraction_task)
            outcome.spent_usd = tracker.spent_usd
            return outcome

        with open(results_path, "a", encoding="utf-8") as out:
            extraction_done = False
            while not extraction_done:
                batch, extraction_done = await _next_batch(out_queue, concurrency)
                if not batch:
                    break

                worst_case = sum(tokens_to_cost(estimate_candidate_worst_case_tokens(c)) for c in batch)
                if tracker.would_exceed_cap(worst_case):
                    outcome.stopped_at_cap = True
                    break

                pairs = await asyncio.gather(*(_classify_one(client, semaphore, pacer, c) for c in batch))
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

    await _cancel_and_wait(extraction_task)
    outcome.spent_usd = tracker.spent_usd
    return outcome


async def _cancel_and_wait(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
