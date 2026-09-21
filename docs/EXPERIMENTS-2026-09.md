# Free-Jev-window experiments, September 2026

Vercel AI Gateway served `typesafe-ai/jev` at $0 through 2026-09-25. These are the measurements taken in that window, using the two existing runs (`f-users`, a Windows user profile with 50,498 unique scanned files, and `wsl-home`, a curated Documents-heavy Linux home with 92,674) as ground truth. Scripts live in `experiments/`; raw outputs are under `runs/experiments/` (gitignored). All numbers below came from those scripts on 2026-09-20 and 2026-09-21.

## Gateway behaviour

- The free tier rate-limits each model to 30 requests per minute (`x-ratelimit-limit-requests: 30`, `retry-after` up to 60s). The first rescan attempt hit this and, with the SDK's default retry (2 attempts, 0.5s to 5s backoff), appended 1,567 error rows in about a minute. Three fixes followed: `scan` retries with a longer backoff, `load_results` never lets an error row supersede a successful judgment, and `scan --rpm` paces requests.
- Buying any amount of gateway credits moves the team to the paid tier, which removes the gateway limit entirely. Measured throughput afterwards: 17 calls/s at concurrency 4, 33 at 8, 62 at 16, zero errors. TypeSafe's own limit was not reached.
- Latency is 0.2 to 0.3s per call. Occasional 504s (30s) come from upstream and are retried.

## Determinism

Sending the same 6000-char excerpt twice for 561 content-verified files: mean |dvalue| 0.023, no file moved more than 0.25, one notable flip (0.2%), Spearman 0.994, under 1% of files crossed the 0.6 threshold in any category. Jev is close to deterministic, so differences of 0.1 in a threshold are real signal and not noise.

## Excerpt cap

Same 561 files, same excerpt truncated to different lengths (250 of them hit the 6000 cap; mean length 3,595 chars). Compared against the 6000-char answers:

| cap | tokens/call | mean dvalue | notable flips | Spearman | worst category threshold crossing |
| --- | --- | --- | --- | --- | --- |
| 6000 (control) | 2570 | 0.023 | 0.2% | 0.994 | 1% (original_work) |
| 3000 | 1861 (-28%) | 0.036 | 0.7% | 0.991 | 1% (irreplaceable) |
| 1500 | 1428 (-44%) | 0.045 | 3.2% | 0.989 | 1% |
| 500 | 1099 (-57%) | 0.076 | 5.0% | 0.981 | 3% (original_work) |

3000 is essentially free: a 28% token cut on content-bearing calls at the control's own noise floor. 1500 starts to cost real notable decisions.

## Question schema

Same files, three schemas:

| schema | tokens/call | value Spearman vs full | notable agreement | credential recall | credential precision |
| --- | --- | --- | --- | --- | --- |
| full (production) | 2570 | 1.000 | 100% | 100% | 100% |
| bare (nouls without criteria) | 2291 (-11%) | 0.994 | 98.0% | 100% | 100% |
| lean (one Choice + score) | 2311 (-10%) | 0.995 | 97.1% | 79.2% | 100% |

The schema is only about 11% of a content call, and removing criteria destabilises the fuzzy questions (`irreplaceable` mean shift 0.091, 2.5% crossings; `original_work` 3.4%). The single-choice schema cannot express a file that is both credentials and authored code, so it drops a fifth of credential hits. Keep the schema as is.

## Version-control and cloud-sync context

450 content-verified files with `original_work >= 0.6` (275 under a `.git` ancestor, 307 under OneDrive). Adding `version_controlled` / `cloud_synced` booleans to the state changed nothing on its own; adding them to the `irreplaceable` false-criterion dropped mean `irreplaceable` from 0.205 to 0.059 on git files. But `irreplaceable` was already low: only 1% of these files scored 0.6 or more under the production rubric. They flood the report because `manifest.is_manifest_included` treats `original_work >= 0.6` alone as notable, not because Jev misjudges them. The fix is in the inclusion rule, not the rubric.

Simulating an inclusion rule that drops `original_work` from the any-category test (value, credentials, personal, financial_legal, irreplaceable still qualify): `f-users` notable 1,612 to 689; every dropped row has value under 2.0 and all but 25 have `irreplaceable` under 0.4 (CLAW `.aspx.cs`, a purchased Unity asset's shaders, spicetify themes). `wsl-home` 20,261 to 19,052.

## Directory prefilter

One call per directory with a listing (file count, bytes, extension histogram, up to 40 sampled names, child directory names; about 750 to 800 tokens), asking `worth_scanning` (noul) and `kind` (choice). Evaluated against the per-file results: a per-file call is "saved" if its directory scores below the threshold, and recall is the fraction of notable, credential, and top-100 files whose directory survives. Call counts include the directory calls themselves.

Only directories with at least 10 files get a call; smaller ones are always scanned per-file (it costs less than asking).

| run | threshold | dir calls | saving | notable recall (proposed rule) | content-verified credential recall | value >= 2.5 recall |
| --- | --- | --- | --- | --- | --- | --- |
| f-users | 0.05 | 887 | 48% | 97.5% | 100% | 100% |
| f-users | 0.10 | 887 | 64% | 90.3% | 100% | 100% |
| f-users | 0.20 | 887 | 68% | 84.7% | 96.7% | 99% |
| wsl-home | 0.05 | 1,121 | 0% | 100% | 100% | 100% |
| wsl-home | 0.10 | 1,121 | 2% | 100% | 100% | 100% |
| wsl-home | 0.20 | 1,121 | 10% | 99.7% | 99.5% | 99.8% |

The prefilter pays for itself on junk-heavy drives and is neutral on curated ones; with the 10-file floor it was never net-negative. Without the floor, `wsl-home` (32,621 directories for 92,674 files, 89% judged user content) is a 34% net loss. The files it drops on `f-users` at 0.10 are dominated by per-file false positives: Unity asset shaders scored as original work, VS Code history snapshots, ROM folders. The `kind` choice is a usable directory-level classifier on its own (`f-users`: 2,909 app_state, 2,529 third_party, 1,358 owner_code, 252 user_content).

### Production validation

`scan --prefilter` was then run for real over the `f-users` inventory into a fresh run and compared with the full rubric-v2 baseline. The simulation above scored credential recall against the 60 verified hits that existed before the rubric rescan; against the 218 that exist now, 0.10 kept only 174 (79.8%), the misses being 37 VS Code local-history snapshots of edited source files (`AppData\Roaming\Code\User\History\<hash>\XXXX.js`, directory scored 0.07 as app_state) plus extension `package.json` false positives. At 0.05 the same decisions keep 214 of 218 (98.2%), including all 84 valued 2.0 or higher; the four misses are extension `package.json` files. Final production numbers at 0.05 and a 10-file floor: 887 directory calls, 24,366 per-file calls instead of 50,498 (50.0% fewer), 90 of 90 files valued 2.5 or higher, 801 of 820 notable rows. The shipped default is 0.05.

| threshold | saving | verified credential recall | of those valued 2.0+ | notable recall | value 2.5+ recall |
| --- | --- | --- | --- | --- | --- |
| 0.03 | 21.0% | 100% | 100% | 99.3% | 100% |
| 0.05 | 50.0% | 98.2% | 100% | 97.7% | 100% |
| 0.075 | 56.7% | 91.3% | 100% | 95.0% | 100% |
| 0.10 | 62.1% | 79.8% | 100% | 90.1% | 100% |

## Name-only rescans

`scan --only-name-only` on both runs (7,171 and 745 calls) upgraded 8,139 rows from name-only to content-verified, found zero new credential hits, and added 175 notable rows, all css/svg qualifying through `original_work`. Low value; the original name-only judgments were right about the junk.

## Rubric v2 rescan

`annotate --all` backfilled `extraction_status` on 29,886 (`f-users`) and 30,000-odd (`wsl-home`) legacy rows, which exposed a second problem: those rows were scored under rubric v1, whose `irreplaceable` question happily gave 0.8 to editor logs at value 0.1. Previously hidden as "unknown" verification, they became content-verified notable files overnight. `scan --outdated-rubric` was added and both runs were brought entirely onto rubric v2 (29,990 and 30,609 calls, zero errors, roughly $3.90 at list price).

| run | rule | notable before | notable after | verified credential hits before / after |
| --- | --- | --- | --- | --- |
| f-users | current | 5,734 | 4,355 | 218 / 218 |
| f-users | proposed (no `original_work`) | 3,108 | 820 | 218 / 218 |
| wsl-home | current | 24,818 | 22,823 | 680 / 679 |
| wsl-home | proposed | 23,485 | 20,976 | 680 / 679 |

The 820 on `f-users` qualify as value (513), credentials (159), financial/legal (93), personal (43), irreplaceable (12), and cluster in Downloads, Screenshots, Quicken backups, an Obsidian vault, and VS Code history. On `wsl-home` the 20,976 are 13,678 mail messages, 3,706 PDFs, and 1,625 docx files qualifying mostly as personal or financial/legal: that drive is a curated archive and the count is real.

## Recommendations

1. Cut `EXCERPT_CHAR_CAP` to 3000. Measured cost of doing so is inside the determinism noise. Shipped.
2. Change the inclusion rule so `original_work` alone does not make a file notable. With both runs on rubric v2 this takes `f-users` from 4,355 notable rows to 820 without touching a single verified credential hit, and it is what the `irreplaceable` signal already says. Shipped.
3. Build the prefilter as `scan --prefilter` (directory calls for directories with 10 or more files, skip below 0.05, always scan the rest). Halves the calls on the kind of drive this tool exists for and cannot lose more than the directory-call overhead on the other kind. Shipped.
4. Leave the question schema alone.
5. Bump `RUBRIC_VERSION` whenever a question changes and run `scan --outdated-rubric` afterwards; mixed-version results are worse than either version alone.
