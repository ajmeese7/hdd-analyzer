"""Deterministic, free directory walk producing an inventory of files."""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from hdd_analyzer.config import (
    DEDUPE_SAMPLE_BYTES,
    SIZE_FLOOR_BYTES,
    categorize,
    is_marker_skip,
    is_skip_dir,
    is_var_child_skip,
    should_hash_content,
)

PROGRESS_INTERVAL = 5000


def is_reparse_point(entry: os.DirEntry) -> bool:
    """True if `entry` is a Windows reparse point (junction, symlink, OneDrive placeholder).

    `st_file_attributes` only exists on Windows `os.stat_result`; on other
    platforms this is always False and entry.is_symlink() covers cycles.
    """
    try:
        entry_stat = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    attributes = getattr(entry_stat, "st_file_attributes", None)
    if attributes is None:
        return False
    return bool(attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT)


def dir_identity(path: Path) -> tuple[int, int] | None:
    """Return (st_dev, st_ino) for `path`, or None if it cannot be stat'd."""
    try:
        result = os.stat(path)
    except OSError:
        return None
    return result.st_dev, result.st_ino


def is_cycle(identity: tuple[int, int] | None, visited: set[tuple[int, int]]) -> bool:
    """True if `identity` has already been visited (and is not None)."""
    return identity is not None and identity in visited


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
    count_by_top_level: Counter[str] = field(default_factory=Counter)


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


def _metadata_dedupe_key(size: int, name: str) -> str:
    """Cheap dedupe key for files we deliberately don't hash: size + name.

    Two differently-named copies of the same non-text binary (e.g. a photo
    renamed on export) will not collide here and will both be kept, which is
    a false-negative dedup, not a false positive. That's the accepted
    tradeoff for skipping the IO cost of hashing archives/media/binaries.
    """
    return f"meta:{size}:{name.lower()}"


def _top_level_part(path: Path, root: Path) -> str:
    """First path component of `path` relative to `root`, for breakdown stats."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return str(root)
    return relative.parts[0] if relative.parts else str(root)


def _iter_dirs_and_files(root: Path, error_log: list[str]) -> Iterator[tuple[Path, bool]]:
    """Yield (path, is_dir) for every non-skipped entry under root, DFS.

    Guards against directory cycles two ways: skipping any reparse point
    (Windows junctions, symlinks, OneDrive placeholders) and tracking
    (st_dev, st_ino) of every directory already descended into, which also
    catches cycles introduced by non-Windows bind/UNC mounts.
    """
    visited: set[tuple[int, int]] = set()
    root_identity = dir_identity(root)
    if root_identity is not None:
        visited.add(root_identity)

    stack: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while stack:
        current, parent_parts = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            error_log.append(f"scandir failed: {current} ({exc})")
            continue

        siblings = frozenset(entry.name.lower() for entry in entries)

        for entry in entries:
            try:
                if entry.is_symlink() or is_reparse_point(entry):
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                error_log.append(f"stat failed: {entry.path} ({exc})")
                continue

            entry_path = Path(entry.path)
            if is_dir:
                if (
                    is_skip_dir(entry.name, parent_parts)
                    or is_var_child_skip(entry.name, parent_parts)
                    or is_marker_skip(entry.name, siblings)
                ):
                    continue
                identity = dir_identity(entry_path)
                if is_cycle(identity, visited):
                    continue
                if identity is not None:
                    visited.add(identity)
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

                if should_hash_content(category, size):
                    key = _dedupe_key(path, size)
                    if key is None:
                        error_log.append(f"read failed for dedupe hash: {path}")
                        summary.skipped_errors += 1
                        continue
                else:
                    key = _metadata_dedupe_key(size, path.name)

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
                summary.count_by_top_level[_top_level_part(path, root)] += 1

                if summary.scanned % PROGRESS_INTERVAL == 0:
                    print(f"...{summary.scanned} files scanned, in {path.parent}", file=sys.stderr)

    with open(errors_path, "w", encoding="utf-8") as err_out:
        err_out.write("\n".join(error_log))
        if error_log:
            err_out.write("\n")

    print("files by top-level directory:", file=sys.stderr)
    for name, count in summary.count_by_top_level.most_common():
        print(f"  {name}: {count}", file=sys.stderr)

    return summary
