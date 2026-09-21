"""Directory prefilter: one Jev call per directory listing, before any per-file call.

A directory listing (names, extension histogram, child directory names) is
usually enough to tell `node_modules`-adjacent junk from a Documents tree,
and one listing call costs about the same as one file call. Measured in
September 2026 against the per-file results of two real drives
(docs/EXPERIMENTS-2026-09.md): asking only about directories with at least
PREFILTER_MIN_FILES files and skipping those below PREFILTER_SKIP_BELOW
halved the calls on a junk-heavy profile drive while keeping 214 of 218
content-verified credential hits (every one valued 2.0 or higher; the four
misses were extension package.json false positives), every file valued 2.5
or higher, and 97.7% of notable rows. On a curated archive it skipped
nothing and cost under 1% in listing calls. A threshold of 0.10 saved 62%
but dropped a fifth of the credential hits (VS Code local-history
snapshots of edited source), which is not a trade this tool makes.
Directories under the floor are always scanned per-file: asking costs more
than answering.

Decisions are appended to `runs/NAME/prefilter.jsonl` so a re-run reuses
them and the human can audit what was skipped. A failed directory call
never skips anything.
"""

from __future__ import annotations

import asyncio
import collections
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul

from hdd_analyzer.budget import BudgetTracker, estimate_worst_case_tokens, tokens_to_cost
from hdd_analyzer.config import PER_CALL_OVERHEAD_TOKENS
from hdd_analyzer.paths import pure_path

PREFILTER_MIN_FILES = 10
PREFILTER_SKIP_BELOW = 0.05
PREFILTER_FILE = "prefilter.jsonl"
_MAX_NAMES = 40
_MAX_SUBDIRS = 30
_MAX_EXTENSIONS = 15

QUESTIONS: dict[str, Any] = {
    "worth_scanning": Noul(
        instructions=(
            "Judging only from this directory listing, is it likely to contain files worth reading individually "
            "because they may hold credentials, personal content, financial or legal documents, original work the "
            "drive owner authored, or other irreplaceable content?"
        ),
        criteria={
            "true": (
                "User-created or user-received content: documents, spreadsheets, correspondence, photos, notes, "
                "source code or creative projects the owner wrote, configs likely to hold secrets, personal data exports."
            ),
            "false": (
                "Installed software, dependency or build caches, libraries, vendored third-party code, game assets and "
                "engine files, application state or caches, temp or generated files, OS or driver files."
            ),
        },
    ),
    "kind": Choice(
        instructions="What best describes this directory?",
        criteria={
            "user_content": "Documents, media, notes, correspondence, or data the owner created or collected.",
            "owner_code": "A software project the owner appears to have authored.",
            "third_party": "Installed software, libraries, vendored code, SDKs, game data, or downloads of others' work.",
            "app_state": "Application configs, caches, databases, logs, or generated state.",
            "system": "OS, driver, or hardware vendor files.",
        },
    ),
}


def parent_of(path: str) -> str:
    return str(pure_path(path).parent)


@dataclass(frozen=True)
class DirectoryListing:
    path: str
    file_count: int
    total_bytes: int
    extensions: dict[str, int]
    names_sample: list[str]
    subdirectories: list[str]

    def to_state(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "extensions": self.extensions,
            "file_names_sample": self.names_sample,
            "subdirectory_names": self.subdirectories[:_MAX_SUBDIRS],
            "subdirectory_count": len(self.subdirectories),
        }


def _sample_names(names: list[str]) -> list[str]:
    """Up to _MAX_NAMES names spread evenly through the sorted listing, so a
    directory of 1,000 icons still shows its range rather than its first page."""
    ordered = sorted(names, key=str.lower)
    if len(ordered) <= _MAX_NAMES:
        return ordered
    step = len(ordered) / _MAX_NAMES
    return [ordered[int(i * step)] for i in range(_MAX_NAMES)]


def build_listings(
    inventory: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    min_files: int = PREFILTER_MIN_FILES,
) -> dict[str, DirectoryListing]:
    """Listings for every directory holding a candidate and at least `min_files` inventory files.

    The listing counts every inventory row in the directory, duplicates
    included, because that is what a human would see; only the candidate
    set decides which directories are worth asking about.
    """
    names: dict[str, list[str]] = collections.defaultdict(list)
    extensions: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    total_bytes: collections.Counter = collections.Counter()
    children: dict[str, set[str]] = collections.defaultdict(set)
    for record in inventory:
        path = pure_path(record["path"])
        parent = str(path.parent)
        names[parent].append(path.name)
        extensions[parent][record["ext"] or "(none)"] += 1
        total_bytes[parent] += record["size"]
        node = path.parent
        while node.parent != node:
            children[str(node.parent)].add(node.name)
            node = node.parent

    listings: dict[str, DirectoryListing] = {}
    for directory in {parent_of(record["path"]) for record in candidates}:
        if len(names[directory]) < min_files:
            continue
        listings[directory] = DirectoryListing(
            path=directory,
            file_count=len(names[directory]),
            total_bytes=total_bytes[directory],
            extensions=dict(extensions[directory].most_common(_MAX_EXTENSIONS)),
            names_sample=_sample_names(names[directory]),
            subdirectories=sorted(children.get(directory, ())),
        )
    return listings


def load_decisions(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Latest successful decision per directory from prefilter.jsonl; failed calls are retried."""
    path = run_dir / PREFILTER_FILE
    if not path.exists():
        return {}
    decisions: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("error") is None:
                decisions[record["path"]] = record
    return decisions


def estimated_cost(listings: dict[str, DirectoryListing]) -> float:
    """Worst-case cost of asking about every listing, for the pre-dispatch cap check."""
    tokens = sum(
        PER_CALL_OVERHEAD_TOKENS + estimate_worst_case_tokens(len(json.dumps(listing.to_state())))
        for listing in listings.values()
    )
    return tokens_to_cost(tokens)


async def _decide_one(client: AsyncTypeSafeClient, listing: DirectoryListing) -> dict[str, Any]:
    state = listing.to_state()
    try:
        response = await client.system_one(state=state, questions=QUESTIONS)
    except Exception as exc:  # noqa: BLE001 - a failed directory call keeps the directory, never skips it
        return {"path": listing.path, "file_count": listing.file_count, "error": str(exc), "state_chars": len(json.dumps(state))}
    usage = getattr(response, "usage", None)
    return {
        "path": listing.path,
        "file_count": listing.file_count,
        "worth_scanning": response.answers["worth_scanning"].noul,
        "kind": response.answers["kind"].choice,
        "input_tokens": usage.input_tokens if usage is not None else None,
        "error": None,
        "state_chars": len(json.dumps(state)),
    }


async def decide(
    client: AsyncTypeSafeClient,
    listings: dict[str, DirectoryListing],
    run_dir: Path,
    tracker: BudgetTracker,
    semaphore: asyncio.Semaphore,
    pacer: Any,
) -> dict[str, dict[str, Any]]:
    """Ask Jev about every listing without a stored decision; append answers to prefilter.jsonl.

    Returns every decision, stored and new. `pacer` is scan.Pacer, passed in
    rather than imported to keep this module free of scan's dependencies.
    """
    decisions = load_decisions(run_dir)
    pending = [listing for path, listing in listings.items() if path not in decisions]
    if not pending:
        return decisions

    async def one(listing: DirectoryListing) -> dict[str, Any]:
        async with semaphore:
            await pacer.wait()
            return await _decide_one(client, listing)

    with open(run_dir / PREFILTER_FILE, "a", encoding="utf-8") as out:
        for record in await asyncio.gather(*(one(listing) for listing in pending)):
            state_chars = record.pop("state_chars")
            if record["error"] is None:
                tracker.record_tokens(record["input_tokens"], state_chars)
                decisions[record["path"]] = record
            out.write(json.dumps(record) + "\n")
    return decisions


@dataclass(frozen=True)
class PrefilterOutcome:
    kept: list[dict[str, Any]]
    asked: int
    skipped_directories: int
    skipped_files: int
    skipped_paths: list[str] = field(default_factory=list)


def apply(
    candidates: list[dict[str, Any]],
    listings: dict[str, DirectoryListing],
    decisions: dict[str, dict[str, Any]],
    skip_below: float = PREFILTER_SKIP_BELOW,
) -> PrefilterOutcome:
    """Drop candidates whose directory was asked about and scored below `skip_below`.

    A directory without a successful decision (below the floor, or its call
    failed) keeps every file.
    """
    skipped = {
        path
        for path in listings
        if path in decisions and decisions[path]["worth_scanning"] < skip_below
    }
    kept = [record for record in candidates if parent_of(record["path"]) not in skipped]
    return PrefilterOutcome(
        kept=kept,
        asked=sum(1 for path in listings if path in decisions),
        skipped_directories=len(skipped),
        skipped_files=len(candidates) - len(kept),
        skipped_paths=sorted(skipped),
    )
