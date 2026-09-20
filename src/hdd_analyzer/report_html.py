"""HTML report: a navigable directory tree plus a filterable ranked file table.

The Markdown report is a flat ranked list, and on a real drive that is
thousands of rows. The question a human actually has is "where on this drive
should I go look?", so this module aggregates scan results into a directory
tree with subtree rollups and embeds it, with the notable file rows, into one
self-contained HTML page (inline CSS/JS, no network, no external assets).

Only rows that pass the manifest inclusion test (manifest.is_manifest_included)
are embedded as files; every scanned row still contributes to per-directory
"scanned" counts so the tree shows how much noise surrounded each hit. Rows
carry paths, sizes, and scores, never excerpts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from typing import Any

from hdd_analyzer.manifest import directory_rollup, is_manifest_included
from hdd_analyzer.paths import pure_path
from hdd_analyzer.report import (
    NOUL_CATEGORIES,
    _valid_rows,
    estimated_spend,
    load_dedupe_savings,
    load_results,
    verified_label,
)

TOP_DIRS = 15
_DATA_MARKER = "/*__HDD_ANALYZER_DATA__*/"


@dataclass
class _Dir:
    """Mutable build-time node; finalized into a plain dict for the payload."""

    path: str
    scanned: int = 0
    files: list[int] = field(default_factory=list)
    children: dict[str, _Dir] = field(default_factory=dict)


def _file_entry(row: dict[str, Any], dir_index: int) -> dict[str, Any]:
    """One embedded file. `dir` indexes the payload's `dirs` list; the page joins dir + name.

    Paths are the bulk of the payload on a big run, so the directory prefix
    is stored once per directory instead of once per file.
    """
    probabilities = row.get("probabilities") or {}
    return {
        "dir": dir_index,
        "name": pure_path(row["path"]).name,
        "size": int(row.get("size") or 0),
        "value": round(float(row["value_score"]), 2),
        "confidence": round(float(row.get("value_confidence") or 0), 2),
        "verified": verified_label(row),
        "probs": {
            name: round(float(probabilities[name]), 2)
            for name in NOUL_CATEGORIES
            if isinstance(probabilities.get(name), (int, float))
        },
    }


def _insert(roots: dict[str, _Dir], row_path: str) -> _Dir:
    """Walk (creating as needed) the directory chain for `row_path`; return the leaf dir."""
    ancestors = list(reversed(pure_path(row_path).parents))
    anchor = ancestors[0]
    node = roots.setdefault(str(anchor), _Dir(path=str(anchor)))
    for ancestor in ancestors[1:]:
        node = node.children.setdefault(ancestor.name, _Dir(path=str(ancestor)))
    return node


def _separator(path: str) -> str:
    return "\\" if isinstance(pure_path(path), PureWindowsPath) else "/"


def _join(prefix: str, name: str, separator: str) -> str:
    """Append `name` to `prefix`; anchors like `F:\\` already end in a separator."""
    return prefix + name if prefix.endswith(separator) else prefix + separator + name


def _finalize(
    node: _Dir, name: str, entries: list[dict[str, Any]], min_prob: float
) -> tuple[dict[str, Any] | None, int]:
    """Post-order: roll up counts, prune subtrees with no notable files, collapse chains.

    Returns (payload node or None if pruned, scanned_subtree). The scanned
    count is returned separately so a pruned subtree's noise still counts
    toward its surviving ancestor.
    """
    children: list[dict[str, Any]] = []
    scanned_subtree = node.scanned
    for child_name, child in node.children.items():
        finalized, child_scanned = _finalize(child, child_name, entries, min_prob)
        scanned_subtree += child_scanned
        if finalized is not None:
            children.append(finalized)

    notable_subtree = len(node.files) + sum(c["notable_subtree"] for c in children)
    if notable_subtree == 0:
        return None, scanned_subtree

    if not node.files and len(children) == 1:
        only = children[0]
        joined = _join(name, only["name"], _separator(only["path"]))
        collapsed = {**only, "name": joined, "scanned_subtree": scanned_subtree}
        return collapsed, scanned_subtree

    direct = [entries[i] for i in node.files]
    cats = {category: 0 for category in NOUL_CATEGORIES}
    for entry in direct:
        for category, prob in entry["probs"].items():
            if prob >= min_prob:
                cats[category] += 1
    for child in children:
        for category, count in child["cats"].items():
            cats[category] += count

    max_value = max([e["value"] for e in direct] + [c["max_value"] for c in children])
    bytes_subtree = sum(e["size"] for e in direct) + sum(c["bytes_subtree"] for c in children)
    children.sort(key=lambda c: (-c["notable_subtree"], c["name"].lower()))
    return {
        "name": name,
        "path": node.path,
        "scanned": node.scanned,
        "scanned_subtree": scanned_subtree,
        "notable": len(node.files),
        "notable_subtree": notable_subtree,
        "max_value": max_value,
        "bytes_subtree": bytes_subtree,
        "cats": cats,
        "files": sorted(node.files, key=lambda i: -entries[i]["value"]),
        "children": children,
    }, scanned_subtree


@dataclass(frozen=True)
class Tree:
    roots: list[dict[str, Any]]
    files: list[dict[str, Any]]
    dirs: list[str]


def build_tree(rows: list[dict[str, Any]], notable: list[dict[str, Any]], min_prob: float) -> Tree:
    """Aggregate `rows` into a directory tree, embedding only `notable` rows as files."""
    notable_keys = {row["dedupe_key"] for row in notable}
    roots: dict[str, _Dir] = {}
    entries: list[dict[str, Any]] = []
    dir_index: dict[str, int] = {}
    for row in rows:
        leaf = _insert(roots, row["path"])
        leaf.scanned += 1
        if row["dedupe_key"] in notable_keys:
            leaf.files.append(len(entries))
            entries.append(_file_entry(row, dir_index.setdefault(leaf.path, len(dir_index))))

    tree = []
    for anchor, node in roots.items():
        finalized, _ = _finalize(node, anchor, entries, min_prob)
        if finalized is not None:
            tree.append(finalized)
    tree.sort(key=lambda n: -n["notable_subtree"])
    return Tree(roots=tree, files=entries, dirs=[_join(d, "", _separator(d)) for d in dir_index])


def build_payload(
    results: list[dict[str, Any]],
    *,
    run_name: str,
    spend_usd: float,
    dedupe_savings: int,
    min_value: float,
    min_prob: float,
) -> dict[str, Any]:
    rows = _valid_rows(results)
    notable = [row for row in rows if is_manifest_included(row, min_value, min_prob)]
    tree = build_tree(rows, notable, min_prob)

    verified_counts = {label: 0 for label in ("content", "ocr", "name-only", "unknown")}
    for entry in tree.files:
        verified_counts[entry["verified"]] += 1
    credential_hits = sum(1 for e in tree.files if e["probs"].get("credentials", 0) >= min_prob)

    return {
        "run": run_name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thresholds": {"min_value": min_value, "min_prob": min_prob},
        "categories": list(NOUL_CATEGORIES),
        "summary": {
            "scanned": len(results),
            "errors": sum(1 for r in results if r.get("error")),
            "spend_usd": round(spend_usd, 4),
            "dedupe_savings": dedupe_savings,
            "notable": len(tree.files),
            "verified": verified_counts,
            "credential_hits": credential_hits,
        },
        "top_dirs": [{"path": path, "count": count} for path, count in directory_rollup(notable, TOP_DIRS)],
        "tree": tree.roots,
        "dirs": tree.dirs,
        "files": tree.files,
    }


def render_report_html(payload: dict[str, Any]) -> str:
    template = files("hdd_analyzer").joinpath("templates/report.html").read_text(encoding="utf-8")
    if _DATA_MARKER not in template:
        raise RuntimeError("report template is missing its data marker")
    # ensure_ascii keeps lone surrogates from odd filenames encodable; the
    # `</` escape keeps a filename like `</script>` from ending the data block.
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    return template.replace(_DATA_MARKER, data, 1)


def write_report_html(run_dir: Path, *, min_value: float, min_prob: float) -> Path:
    results = load_results(run_dir)
    payload = build_payload(
        results,
        run_name=run_dir.name,
        spend_usd=estimated_spend(results),
        dedupe_savings=load_dedupe_savings(run_dir),
        min_value=min_value,
        min_prob=min_prob,
    )
    html_path = run_dir / "report.html"
    html_path.write_text(render_report_html(payload), encoding="utf-8")
    return html_path
