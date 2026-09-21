"""Can one Jev call per directory predict which directories hold notable files?

If so, a future two-stage scan (directory triage, then per-file only inside
kept directories) is dramatically cheaper. Ground truth is the existing
per-file results: a directory is "hot" if any of its direct files is notable
under the manifest inclusion test.
"""

from __future__ import annotations

import asyncio
import collections
import sys
from typing import Any

from common import classify_many, load_inventory, load_run, notable, out_path, read_jsonl, write_jsonl

from hdd_analyzer.paths import pure_path
from typesafe_sdk import Choice, Noul

RUNS = ["f-users", "wsl-home"]
MAX_NAMES = 40
MAX_SUBDIRS = 30

QUESTIONS = {
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


def build_directories(run: str) -> dict[str, dict[str, Any]]:
    """Per directory with direct files: names, extension histogram, bytes, and child directory names."""
    dirs: dict[str, dict[str, Any]] = {}
    for record in load_inventory(run):
        path = pure_path(record["path"])
        entry = dirs.setdefault(str(path.parent), {"names": [], "exts": collections.Counter(), "bytes": 0, "count": 0})
        entry["names"].append(path.name)
        entry["exts"][record["ext"] or "(none)"] += 1
        entry["bytes"] += record["size"]
        entry["count"] += 1

    # Child directory names, including intermediate directories that hold no files themselves.
    children: dict[str, set[str]] = collections.defaultdict(set)
    for d in dirs:
        node = pure_path(d)
        while node.parent != node:
            children[str(node.parent)].add(node.name)
            node = node.parent
    for d, entry in dirs.items():
        entry["subdirs"] = sorted(children.get(d, ()))
    return dirs


def dir_state(path: str, entry: dict[str, Any]) -> dict[str, Any]:
    names = sorted(entry["names"], key=str.lower)
    if len(names) > MAX_NAMES:
        step = len(names) / MAX_NAMES
        names = [names[int(i * step)] for i in range(MAX_NAMES)]
    return {
        "path": path,
        "file_count": entry["count"],
        "total_bytes": entry["bytes"],
        "extensions": dict(entry["exts"].most_common(15)),
        "file_names_sample": names,
        "subdirectory_names": entry["subdirs"][:MAX_SUBDIRS],
        "subdirectory_count": len(entry["subdirs"]),
    }


def collect(run: str) -> None:
    dirs = build_directories(run)
    print(f"{run}: {len(dirs)} directories with files")
    items = [(path, dir_state(path, entry)) for path, entry in dirs.items()]
    answers = asyncio.run(classify_many(items, QUESTIONS, progress_every=500))
    write_jsonl(out_path(f"dir_prefilter.{run}.jsonl"), [{"path": p, "state": s, "answer": answers[p]} for p, s in items])


def analyze(run: str) -> None:
    rows = read_jsonl(out_path(f"dir_prefilter.{run}.jsonl"))
    answers = {r["path"]: r["answer"] for r in rows if "error" not in r["answer"]}
    results = [r for r in load_run(run) if not r.get("error") and r.get("value_score") is not None]
    by_dir: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in results:
        by_dir[str(pure_path(r["path"]).parent)].append(r)

    total_files = len(results)
    notable_rows = [r for r in results if notable(r)]
    cred_rows = [r for r in results if (r.get("probabilities") or {}).get("credentials", 0) >= 0.6]
    verified_notable = [r for r in notable_rows if r.get("extraction_status") in ("ok", "ocr")]
    top100 = sorted(results, key=lambda r: -r["value_score"])[:100]
    dir_calls = len(answers)
    tokens = sum(a["input_tokens"] or 0 for a in answers.values())
    errors = len(rows) - len(answers)

    def p_of(row: dict[str, Any]) -> float:
        a = answers.get(str(pure_path(row["path"]).parent))
        return a["probabilities"]["worth_scanning"] if a else 1.0

    print(f"\n== {run}: {dir_calls} directory calls ({errors} errors), {tokens:,} tokens "
          f"(~{tokens / max(dir_calls, 1):.0f}/call); {total_files:,} scanned files, {len(notable_rows):,} notable, "
          f"{len(cred_rows):,} credential hits, {len(verified_notable):,} content-verified notable")
    print(f"{'thresh':>6} {'files kept':>10} {'net calls':>9} {'saving':>7} {'notable':>8} {'cred':>6} {'verified':>8} {'top100':>7}")
    for t in (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        kept = sum(1 for r in results if p_of(r) >= t)
        net = kept + dir_calls
        rec = lambda rows: sum(1 for r in rows if p_of(r) >= t) / max(len(rows), 1)  # noqa: E731
        print(f"{t:6.2f} {kept:10,} {net:9,} {1 - net / total_files:7.1%} {rec(notable_rows):8.1%} {rec(cred_rows):6.1%} "
              f"{rec(verified_notable):8.1%} {rec(top100):7.1%}")

    kinds = collections.Counter(a["choice"]["choice"] for a in answers.values() if a.get("choice"))
    print("kind distribution:", dict(kinds))
    print("\nhot directories the prefilter would drop at 0.3 (most notable files first):")
    dropped = [(d, rs) for d, rs in by_dir.items() if d in answers and answers[d]["probabilities"]["worth_scanning"] < 0.3]
    dropped.sort(key=lambda item: -sum(1 for r in item[1] if notable(r)))
    for d, rs in dropped[:12]:
        n = sum(1 for r in rs if notable(r))
        if n == 0:
            break
        print(f"  p={answers[d]['probabilities']['worth_scanning']:.2f} kind={answers[d]['choice']['choice']:13} notable={n:4} files={len(rs):5}  {d}")


if __name__ == "__main__":
    runs = [a for a in sys.argv[1:] if not a.startswith("--")] or RUNS
    for run in runs:
        if "--analyze" not in sys.argv:
            collect(run)
        analyze(run)
