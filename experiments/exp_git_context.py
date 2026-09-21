"""Does knowing a file is version-controlled or cloud-synced change Jev's irreplaceable/value answers?

The f-users report is dominated by authored source files that almost
certainly also live in a git remote. Detect a `.git` ancestor on the live
drive and a OneDrive path segment, then classify each sampled file three ways:
  control  - production state
  enriched - state plus `version_controlled` / `cloud_synced` booleans
  rubric   - enriched state plus an irreplaceable criterion that names those copies
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

from common import SEED, classify_many, file_state, load_inventory, load_run, mean, notable, out_path, read_jsonl, write_jsonl

from hdd_analyzer.extract import extract_excerpt
from hdd_analyzer.jev import NOULS, QUESTIONS, VALUE_SCORE
from typesafe_sdk import Noul

RUN = "f-users"
SAMPLE = 450

RUBRIC = {
    **NOULS,
    "irreplaceable": Noul(
        instructions=NOULS["irreplaceable"].instructions,
        criteria={
            "true": NOULS["irreplaceable"].criteria["true"],
            "false": (
                NOULS["irreplaceable"].criteria["false"]
                + " Also false when the state says the file is version_controlled (a git remote likely holds it) "
                "or cloud_synced (a cloud copy exists), unless the content itself is clearly a unique personal record."
            ),
        },
    ),
    **VALUE_SCORE,
}


def git_ancestor(path: Path) -> bool:
    for ancestor in path.parents:
        if (ancestor / ".git").exists():
            return True
    return False


def collect() -> None:
    inventory = {r["dedupe_key"]: r for r in load_inventory(RUN)}
    rows = [
        r for r in load_run(RUN)
        if r.get("extraction_status") == "ok" and (r.get("probabilities") or {}).get("original_work", 0) >= 0.6
    ]
    random.Random(SEED).shuffle(rows)
    picked = []
    for row in rows:
        record = inventory[row["dedupe_key"]]
        path = Path(record["path"])
        excerpt, _, status = extract_excerpt(path, record["category"], record["ext"], record["size"])
        if status != "ok" or not excerpt:
            continue
        picked.append((record, excerpt, git_ancestor(path), "\\OneDrive\\" in record["path"]))
        if len(picked) >= SAMPLE:
            break
    print(f"sample: {len(picked)}; in git: {sum(1 for p in picked if p[2])}; onedrive: {sum(1 for p in picked if p[3])}")

    out = {}
    for record, excerpt, in_git, synced in picked:
        base = file_state(record, excerpt, False)
        enriched = {**base, "version_controlled": in_git, "cloud_synced": synced}
        out[record["dedupe_key"]] = {"dedupe_key": record["dedupe_key"], "path": record["path"], "in_git": in_git, "synced": synced, "variants": {}}
        out[record["dedupe_key"]]["_states"] = {"control": base, "enriched": enriched}
    for variant, state_name, questions in (("control", "control", QUESTIONS), ("enriched", "enriched", QUESTIONS), ("rubric", "enriched", RUBRIC)):
        items = [(key, entry["_states"][state_name]) for key, entry in out.items()]
        answers = asyncio.run(classify_many(items, questions))
        for key, answer in answers.items():
            out[key]["variants"][variant] = answer
    for entry in out.values():
        del entry["_states"]
    write_jsonl(out_path("git_context.jsonl"), list(out.values()))


def analyze() -> None:
    rows = [r for r in read_jsonl(out_path("git_context.jsonl")) if all("error" not in v for v in r["variants"].values())]
    print(f"\nrows analyzed: {len(rows)}")
    for label, subset in (("in git", [r for r in rows if r["in_git"]]), ("not in git", [r for r in rows if not r["in_git"]]),
                          ("onedrive", [r for r in rows if r["synced"]])):
        if not subset:
            continue
        print(f"\n{label} (n={len(subset)})")
        print(f"{'variant':9} {'irreplaceable':>14} {'>=0.6':>6} {'value':>6} {'notable':>8} {'orig_work':>10}")
        for variant in ("control", "enriched", "rubric"):
            vs = [r["variants"][variant] for r in subset]
            irr = [v["probabilities"]["irreplaceable"] for v in vs]
            val = [v["value_score"] for v in vs]
            nb = [notable({"value_score": v["value_score"], "probabilities": v["probabilities"], "extraction_status": "ok"}) for v in vs]
            ow = [v["probabilities"]["original_work"] for v in vs]
            print(f"{variant:9} {mean(irr):14.3f} {sum(1 for p in irr if p >= 0.6) / len(vs):6.0%} {mean(val):6.2f} {sum(nb) / len(vs):8.0%} {mean(ow):10.3f}")


if __name__ == "__main__":
    if "--analyze" not in sys.argv:
        collect()
    analyze()
