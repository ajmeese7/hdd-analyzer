"""Shared plumbing for the free-Jev-window experiments (September 2026).

These scripts are one-off measurements, not product code: they read existing
runs under runs/, call Jev through whatever provider TYPESAFE_API_KEY
resolves to, and write JSONL under runs/experiments/. Nothing here touches a
run's results.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy  # noqa: E402

from hdd_analyzer.jev import QUESTIONS, build_state  # noqa: E402
from hdd_analyzer.jev_provider import resolve_async_provider  # noqa: E402
from hdd_analyzer.manifest import is_manifest_included  # noqa: E402
from hdd_analyzer.report import load_results  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

RUNS = Path(__file__).resolve().parents[1] / "runs"
OUT = RUNS / "experiments"
CONCURRENCY = int(os.environ.get("JEV_CONCURRENCY", "4"))
# Vercel AI Gateway's free tier allows 30 requests/minute per model; stay under it
# rather than burning the budget on 429 retries. Override with JEV_RPM.
RPM = float(os.environ.get("JEV_RPM", "28"))
RETRY = RetryPolicy(max_retries=8, backoff_initial=1.0, backoff_max=30.0, timeout=240.0)
SEED = 20260920


class Pacer:
    """Spaces request starts evenly at `rpm` per minute across all tasks."""

    def __init__(self, rpm: float) -> None:
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self.next_start = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self.interval:
            return
        async with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.interval
        if delay:
            await asyncio.sleep(delay)


def out_path(name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT / name


def load_inventory(run: str) -> list[dict[str, Any]]:
    with open(RUNS / run / "inventory.jsonl", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_run(run: str) -> list[dict[str, Any]]:
    return load_results(RUNS / run)


def notable(row: dict[str, Any], min_value: float = 2.0, min_prob: float = 0.6) -> bool:
    return is_manifest_included(row, min_value, min_prob)


def stratified_sample(rows: list[dict[str, Any]], per_bucket: int, key: Callable[[dict[str, Any]], float]) -> list[dict[str, Any]]:
    """Up to `per_bucket` rows from each of the value buckets [0,1), [1,2), [2,3]."""
    rng = random.Random(SEED)
    buckets: dict[int, list[dict[str, Any]]] = {0: [], 1: [], 2: []}
    for row in rows:
        buckets[min(2, int(key(row)))].append(row)
    picked = []
    for bucket in buckets.values():
        rng.shuffle(bucket)
        picked.extend(bucket[:per_bucket])
    return picked


def flatten_answers(response: Any) -> dict[str, Any]:
    """Mirror scan._classify_one's result shape from a SystemOneResponse."""
    probabilities: dict[str, Any] = {}
    value_score = value_confidence = None
    choice = None
    for name, answer in response.answers.items():
        if answer.type == "noul":
            probabilities[name] = answer.noul
        elif answer.type == "score":
            value_score, value_confidence = answer.score, answer.confidence
            probabilities[name] = answer.probabilities
        elif answer.type == "choice":
            choice = {"choice": answer.choice, "probabilities": answer.probabilities}
    usage = getattr(response, "usage", None)
    return {
        "probabilities": probabilities,
        "value_score": value_score,
        "value_confidence": value_confidence,
        "choice": choice,
        "input_tokens": usage.input_tokens if usage is not None else None,
    }


async def classify_many(
    items: list[tuple[str, dict[str, Any]]],
    questions: dict[str, Any] = QUESTIONS,
    concurrency: int = CONCURRENCY,
    progress_every: int = 200,
) -> dict[str, dict[str, Any]]:
    """Classify `(id, state)` pairs; returns id -> flattened answers (or {"error": ...})."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    provider = resolve_async_provider(api_key)
    print(f"provider: {provider.name} model={provider.model} items={len(items)}", flush=True)
    semaphore = asyncio.Semaphore(concurrency)
    pacer = Pacer(RPM)
    results: dict[str, dict[str, Any]] = {}
    done = 0
    started = time.monotonic()

    async with AsyncTypeSafeClient(api_key=api_key, model=provider.model, retry=RETRY, **provider.client_kwargs) as client:

        async def one(item_id: str, state: dict[str, Any]) -> None:
            nonlocal done
            async with semaphore:
                await pacer.wait()
                try:
                    response = await client.system_one(state=state, questions=questions)
                    results[item_id] = flatten_answers(response)
                except Exception as exc:  # noqa: BLE001 - record and continue, this is measurement
                    results[item_id] = {"error": f"{type(exc).__name__}: {exc}"}
            done += 1
            if done % progress_every == 0:
                elapsed = time.monotonic() - started
                print(f"  {done}/{len(items)} ({done / elapsed * 60:.0f}/min, eta {(len(items) - done) * elapsed / done / 60:.0f} min)", flush=True)

        await asyncio.gather(*(one(item_id, state) for item_id, state in items))
    errors = sum(1 for r in results.values() if "error" in r)
    print(f"done: {len(results)} results, {errors} errors", flush=True)
    return results


def file_state(record: dict[str, Any], excerpt: str | None, metadata_only: bool) -> dict[str, Any]:
    return build_state(
        path=record["path"],
        name=Path(record["path"]).name,
        ext=record["ext"],
        size=record["size"],
        modified=record["mtime"],
        excerpt=excerpt,
        metadata_only=metadata_only,
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = rank
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
