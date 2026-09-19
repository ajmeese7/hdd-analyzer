"""Deterministic, free directory walk producing an inventory of files."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from hdd_analyzer.config import DEDUPE_SAMPLE_BYTES, SIZE_FLOOR_BYTES, categorize, is_skip_dir, is_var_child_skip


@dataclass(frozen=True)
class InventoryRecord:
    path: str
    size: int
    mtime: float
    ext: str
    category: str
    dedupe_key: str
    dup_of: str | None = None


@dataclass
class WalkSummary:
    scanned: int = 0
    skipped_errors: int = 0
    skipped_dirs: int = 0
    duplicates: int = 0
    bytes_by_category: Counter[str] = field(default_factory=Counter)
    count_by_category: Counter[str] = field(default_factory=Counter)


def _dedupe_key(path: Path, size: int) -> str | None:
    """blake2b-128 of first DEDUPE_SAMPLE_BYTES plus size. None on read error."""
    hasher = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as handle:
            hasher.update(handle.read(DEDUPE_SAMPLE_BYTES))
    except OSError:
        return None
    hasher.update(str(size).encode("ascii"))
    return hasher.hexdigest()


def _iter_dirs_and_files(root: Path, error_log: list[str]) -> Iterator[tuple[Path, bool]]:
    """Yield (path, is_dir) for every non-skipped entry under root, DFS."""
    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        current, parent_parts = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            error_log.append(f"scandir failed: {current} ({exc})")
            continue

        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                error_log.append(f"stat failed: {entry.path} ({exc})")
                continue

            entry_path = Path(entry.path)
            if is_dir:
                if is_skip_dir(entry.name, parent_parts) or is_var_child_skip(entry.name, parent_parts):
                    continue
                yield entry_path, True
                stack.append((entry_path, parent_parts + (entry.name.lower(),)))
            else:
                yield entry_path, False


def walk(root: Path, run_dir: Path) -> WalkSummary:
    """Walk `root`, writing inventory.jsonl and walk-errors.log into run_dir."""
    return walk_many([root], run_dir)


def walk_many(roots: list[Path], run_dir: Path) -> WalkSummary:
    """Walk each of `roots`, writing a single combined inventory.jsonl.

    Dedupe keys are shared across all roots, so a file reachable via two
    `--include` subpaths is recorded once with the rest marked as duplicates.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    inventory_path = run_dir / "inventory.jsonl"
    errors_path = run_dir / "walk-errors.log"

    summary = WalkSummary()
    error_log: list[str] = []
    seen_keys: dict[str, str] = {}

    with open(inventory_path, "w", encoding="utf-8") as out:
        for root in roots:
            for path, is_dir in _iter_dirs_and_files(root, error_log):
                if is_dir:
                    continue

                try:
                    stat = path.stat()
                    size = stat.st_size
                    mtime = stat.st_mtime
                except OSError as exc:
                    error_log.append(f"stat failed: {path} ({exc})")
                    summary.skipped_errors += 1
                    continue

                if size < SIZE_FLOOR_BYTES:
                    continue

                ext = path.suffix.lstrip(".").lower()
                category = categorize(ext)
                key = _dedupe_key(path, size)
                if key is None:
                    error_log.append(f"read failed for dedupe hash: {path}")
                    summary.skipped_errors += 1
                    continue

                dup_of = None
                if key in seen_keys:
                    dup_of = seen_keys[key]
                    summary.duplicates += 1
                else:
                    seen_keys[key] = str(path)

                record = InventoryRecord(
                    path=str(path),
                    size=size,
                    mtime=mtime,
                    ext=ext,
                    category=category,
                    dedupe_key=key,
                    dup_of=dup_of,
                )
                out.write(json.dumps(record.__dict__) + "\n")
                summary.scanned += 1
                summary.bytes_by_category[category] += size
                summary.count_by_category[category] += 1

    with open(errors_path, "w", encoding="utf-8") as err_out:
        err_out.write("\n".join(error_log))
        if error_log:
            err_out.write("\n")

    return summary
