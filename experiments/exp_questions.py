"""How much of the per-call cost is the question schema, and can it be slimmer?

Same sample and excerpts as exp_excerpt_cap, three schemas:
  full   - production QUESTIONS (5 nouls with criteria + value score)
  bare   - the same 5 nouls with criteria removed + the same score
  lean   - one Choice over the categories + the same score
Compares tokens per call and whether the cheaper schemas reproduce the
production notable decisions and category hits.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from common import classify_many, file_state, load_inventory, load_run, mean, notable, out_path, read_jsonl, spearman, stratified_sample, write_jsonl

from hdd_analyzer.extract import extract_excerpt
from hdd_analyzer.jev import NOULS, QUESTIONS, VALUE_SCORE
from hdd_analyzer.report import NOUL_CATEGORIES
from typesafe_sdk import Choice, Noul

RUN = "f-users"
PER_BUCKET = 300

BARE = {name: Noul(instructions=noul.instructions) for name, noul in NOULS.items()} | VALUE_SCORE
LEAN = {
    "kind": Choice(
        instructions="Which single category best describes this file?",
        criteria={
            "credentials": "Passwords, API keys, private keys, seed phrases, wallets, or auth tokens.",
            "personal": "Correspondence, journals, original personal writing, or sentimental content.",
            "financial_legal": "Tax, banking, contracts, insurance, medical, or identity documents.",
            "original_work": "Source code or creative work the drive owner authored themselves.",
            "other": "Downloaded, installed, generated, third-party, or routine content.",
        },
    ),
    **VALUE_SCORE,
}
SCHEMAS = {"full": QUESTIONS, "bare": BARE, "lean": LEAN}


def collect() -> None:
    inventory = {r["dedupe_key"]: r for r in load_inventory(RUN)}
    rows = [r for r in load_run(RUN) if r.get("extraction_status") == "ok" and r.get("category") in ("text", "code", "doc")]
    sample = stratified_sample(rows, PER_BUCKET, lambda r: r["value_score"])
    extracted = []
    for row in sample:
        record = inventory[row["dedupe_key"]]
        excerpt, _, status = extract_excerpt(Path(record["path"]), record["category"], record["ext"], record["size"])
        if status == "ok" and excerpt:
            extracted.append((record, excerpt))
    print(f"sample: {len(extracted)} files")

    out = {record["dedupe_key"]: {"dedupe_key": record["dedupe_key"], "path": record["path"], "schemas": {}} for record, _ in extracted}
    for name, questions in SCHEMAS.items():
        items = [(record["dedupe_key"], file_state(record, excerpt, False)) for record, excerpt in extracted]
        answers = asyncio.run(classify_many(items, questions))
        for key, answer in answers.items():
            out[key]["schemas"][name] = answer
    write_jsonl(out_path("questions.jsonl"), list(out.values()))


def analyze() -> None:
    rows = [r for r in read_jsonl(out_path("questions.jsonl")) if all("error" not in s for s in r["schemas"].values())]
    n = len(rows)
    full = [r["schemas"]["full"] for r in rows]
    print(f"\nrows analyzed: {n}")
    print(f"{'schema':7} {'tokens':>7} {'saving':>7} {'value spearman':>15} {'notable agree':>14} {'cred recall':>12} {'cred precision':>15}")
    full_tokens = mean([a["input_tokens"] or 0 for a in full])
    full_notable = [notable({"value_score": a["value_score"], "probabilities": a["probabilities"], "extraction_status": "ok"}) for a in full]
    full_cred = [a["probabilities"].get("credentials", 0) >= 0.6 for a in full]
    for name in SCHEMAS:
        alts = [r["schemas"][name] for r in rows]
        tokens = mean([a["input_tokens"] or 0 for a in alts])
        rho = spearman([a["value_score"] for a in full], [a["value_score"] for a in alts])
        if name == "lean":
            alt_notable = [
                a["value_score"] >= 2.0 or (a["choice"]["choice"] != "other" and max(a["choice"]["probabilities"].values()) >= 0.6)
                for a in alts
            ]
            alt_cred = [a["choice"]["probabilities"].get("credentials", 0) >= 0.5 for a in alts]
        else:
            alt_notable = [notable({"value_score": a["value_score"], "probabilities": a["probabilities"], "extraction_status": "ok"}) for a in alts]
            alt_cred = [a["probabilities"].get("credentials", 0) >= 0.6 for a in alts]
        agree = sum(1 for x, y in zip(full_notable, alt_notable) if x == y) / n
        tp = sum(1 for x, y in zip(full_cred, alt_cred) if x and y)
        recall = tp / max(sum(full_cred), 1)
        precision = tp / max(sum(alt_cred), 1)
        print(f"{name:7} {tokens:7.0f} {1 - tokens / full_tokens:7.1%} {rho:15.3f} {agree:14.1%} {recall:12.1%} {precision:15.1%}")

    print("\nbare vs full per-category: mean |dp| / fraction crossing 0.6")
    for c in NOUL_CATEGORIES:
        devs = [abs(r["schemas"]["full"]["probabilities"].get(c, 0) - r["schemas"]["bare"]["probabilities"].get(c, 0)) for r in rows]
        cross = sum(1 for r in rows if (r["schemas"]["full"]["probabilities"].get(c, 0) >= 0.6) != (r["schemas"]["bare"]["probabilities"].get(c, 0) >= 0.6))
        print(f"  {c:16} {mean(devs):.3f} / {cross / n:.1%}")

    print("\nlean choice vs full argmax-noul (rows where some full noul >= 0.6):")
    agree = total = 0
    for r in rows:
        probs = r["schemas"]["full"]["probabilities"]
        best = max(NOUL_CATEGORIES, key=lambda c: probs.get(c, 0))
        if probs.get(best, 0) < 0.6:
            continue
        total += 1
        agree += r["schemas"]["lean"]["choice"]["choice"] == best
    print(f"  {agree}/{total} = {agree / max(total, 1):.1%}")


if __name__ == "__main__":
    if "--analyze" not in sys.argv:
        collect()
    analyze()
