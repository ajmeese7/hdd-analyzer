"""Does a shorter excerpt change Jev's answers? Also measures run-to-run determinism.

Sample content-verified rubric-v2 rows, re-extract each file, classify the
same excerpt at several character caps (6000 is the production cap, sent
twice as a determinism control), and compare answers against the 6000 run.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from common import (
    RUNS,
    classify_many,
    file_state,
    load_inventory,
    load_run,
    mean,
    notable,
    out_path,
    read_jsonl,
    spearman,
    stratified_sample,
    write_jsonl,
)

from hdd_analyzer.extract import extract_excerpt
from hdd_analyzer.report import NOUL_CATEGORIES

RUN = "f-users"
PER_BUCKET = 300
CAPS = [6000, 3000, 1500, 500]
CONTROL = "cap6000-b"


def collect() -> None:
    inventory = {r["dedupe_key"]: r for r in load_inventory(RUN)}
    rows = [
        r for r in load_run(RUN)
        if r.get("extraction_status") == "ok" and r.get("category") in ("text", "code", "doc")
    ]
    sample = stratified_sample(rows, PER_BUCKET, lambda r: r["value_score"])
    print(f"sample: {len(sample)} of {len(rows)} eligible rows")

    extracted = []
    for row in sample:
        record = inventory[row["dedupe_key"]]
        excerpt, metadata_only, status = extract_excerpt(Path(record["path"]), record["category"], record["ext"], record["size"])
        if status != "ok" or not excerpt:
            continue
        extracted.append({"record": record, "stored": row, "excerpt": excerpt})
    print(f"re-extracted ok: {len(extracted)}")

    variants = [f"cap{cap}" for cap in CAPS] + [CONTROL]
    items = []
    for entry in extracted:
        for cap in CAPS:
            items.append((f"{entry['record']['dedupe_key']}|cap{cap}", file_state(entry["record"], entry["excerpt"][:cap], False)))
        items.append((f"{entry['record']['dedupe_key']}|{CONTROL}", file_state(entry["record"], entry["excerpt"][:6000], False)))

    answers = asyncio.run(classify_many(items))
    out = []
    for entry in extracted:
        key = entry["record"]["dedupe_key"]
        out.append({
            "dedupe_key": key,
            "path": entry["record"]["path"],
            "excerpt_chars": len(entry["excerpt"]),
            "stored": {k: entry["stored"].get(k) for k in ("value_score", "value_confidence", "probabilities", "input_tokens")},
            "variants": {v: answers[f"{key}|{v}"] for v in variants},
        })
    write_jsonl(out_path("excerpt_cap.jsonl"), out)


def analyze() -> None:
    rows = [r for r in read_jsonl(out_path("excerpt_cap.jsonl")) if all("error" not in v for v in r["variants"].values())]
    base = "cap6000"
    print(f"\nrows analyzed: {len(rows)}; excerpt length: mean {mean([r['excerpt_chars'] for r in rows]):.0f}, "
          f"{sum(1 for r in rows if r['excerpt_chars'] >= 6000)} at the 6000 cap")
    print(f"{'variant':12} {'tokens':>7} {'dvalue':>7} {'|dv|>.25':>8} {'notable flip':>12} {'spearman':>9} " + " ".join(f"{c[:6]:>8}" for c in NOUL_CATEGORIES))
    for variant in [CONTROL, "cap3000", "cap1500", "cap500"]:
        tokens, dv, big, flips, xs, ys = [], [], 0, 0, [], []
        cat_dev = {c: [] for c in NOUL_CATEGORIES}
        cat_cross = {c: 0 for c in NOUL_CATEGORIES}
        for r in rows:
            a = r["variants"][base]
            b = r["stored"] if variant == "stored" else r["variants"][variant]
            if variant != "stored":
                tokens.append(b["input_tokens"] or 0)
            d = abs(a["value_score"] - b["value_score"])
            dv.append(d)
            big += d > 0.25
            xs.append(a["value_score"]); ys.append(b["value_score"])
            fa = {"value_score": a["value_score"], "probabilities": a["probabilities"], "extraction_status": "ok"}
            fb = {"value_score": b["value_score"], "probabilities": b["probabilities"], "extraction_status": "ok"}
            flips += notable(fa) != notable(fb)
            for c in NOUL_CATEGORIES:
                pa, pb = a["probabilities"].get(c, 0), b["probabilities"].get(c, 0)
                cat_dev[c].append(abs(pa - pb))
                cat_cross[c] += (pa >= 0.6) != (pb >= 0.6)
        n = len(rows)
        print(f"{variant:12} {mean(tokens):7.0f} {mean(dv):7.3f} {big / n:8.1%} {flips / n:12.1%} {spearman(xs, ys):9.3f} "
              + " ".join(f"{mean(cat_dev[c]):.3f}/{cat_cross[c] / n:.0%}" for c in NOUL_CATEGORIES))
    base_tokens = mean([r["variants"][base]["input_tokens"] or 0 for r in rows])
    print(f"\n(base cap6000 tokens: {base_tokens:.0f}; columns per category are mean |dp| / fraction crossing the 0.6 threshold)")


if __name__ == "__main__":
    if "--analyze" not in sys.argv:
        collect()
    analyze()
