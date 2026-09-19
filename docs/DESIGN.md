# hdd-analyzer design (v1)

Purpose: triage old hard drives for semantically valuable files using TypeSafe's Jev (System One model). Pipeline: free deterministic walk -> cost estimate -> Jev classification pass under a hard budget cap -> ranked report for human review.

## Test drives

- Old Windows system drive mounted at `F:\` (value lives mostly under `F:\Users`)
- Old Linux root filesystem, WSL-mounted, reachable from Windows at `\wsl.localhost\Ubuntu\mnt\wsl\PHYSICALDRIVE4p2` (value lives mostly under `home/`)

## CLI (uv run hdd-analyzer ...)

- `walk ROOT --run NAME [--include SUBPATH ...]` -> writes `runs/NAME/inventory.jsonl` + prints summary (counts/bytes by category, skipped counts). No network, no API.
- `estimate --run NAME` -> token + dollar estimate for a scan of the inventory. No network.
- `scan --run NAME [--cap USD] [--limit N] [--min-prob P] [--yes] [--dry-run]` -> extraction + Jev calls, appends `runs/NAME/results.jsonl` incrementally (resumable: skips file hashes already present). Prints running spend. Hard-stops at cap (default $5). Prompts with the estimate before spending unless `--yes`. `--dry-run` does extraction only, zero API calls.
- `report --run NAME [--top N]` -> `runs/NAME/report.md` + `report.csv`, ranked per category and overall.

## Walk stage (deterministic, free)

Per file record: path, size, mtime, ext, category, blake2b-128 of first 256 KiB + size (dedupe key). Streams JSONL; errors (permission, long path) are counted and logged to `runs/NAME/walk-errors.log`, never fatal. Skips symlinks/reparse points. Dedupe: first path wins, duplicates recorded with `dup_of`.

Skip dirs (case-insensitive, name match): Windows, Program Files, Program Files (x86), ProgramData, $Recycle.Bin, System Volume Information, $WinREAgent, Recovery, PerfLogs, Intel, XboxGames, AppData/Local/Temp-like caches, node_modules, .git, __pycache__, .venv, venv, .cache, plus Linux system dirs when walking a rootfs: proc, sys, dev, run, usr, lib, lib64, bin, sbin, boot, snap, var (except var/mail), lost+found, etc (keep etc: small and can hold hand-edited configs -> keep), cdrom, media, mnt, opt (keep opt? skip), srv (keep).

Categories by extension: text (txt, md, csv, log, json, xml, yaml, ini, cfg, conf, eml, htm(l)), code (py, js, ts, c, cpp, h, java, rs, go, sh, ps1, bat, sql, rb, php, pl), doc (docx, doc, rtf, odt, xlsx, pdf), image (jpg, png, heic, gif, raw, cr2, tiff), archive (zip, 7z, rar, tar, gz), av (mp3, mp4, mov, avi, mkv, wav), binary/other. Size floor 32 bytes, ceiling for extraction sampling only (no file too big to inventory).

## Extraction (scan stage, local)

- text/code: read first 16 KiB, decode via chardet, strip NUL-heavy content (binary masquerading).
- pdf: pypdf, first 3 pages of extractable text.
- docx/xlsx: read as zip, pull word/document.xml / shared strings, tag-strip, first 16 KiB. Plain .doc: latin-1 strings-style salvage of printable runs.
- image/av/archive/binary: no content; judged on metadata only (path, name, size, mtime), marked `metadata_only`.
- Excerpt hard cap ~6000 chars (~1500 tokens) per file.

## Jev questions (one system_one call per file, 6 parallel questions)

State: JSON object {path, name, ext, size, modified, excerpt|null, metadata_only: bool}.
Nouls (each with explicit true/false criteria):
- credentials: passwords, API keys, private keys, seed phrases, wallets, tokens
- personal: personal correspondence, journals, original writing, sentimental content
- financial_legal: tax, banking, contracts, insurance, medical, identity documents
- original_work: authored source code or creative work (vs downloaded/installed/third-party)
- irreplaceable: unlikely to be re-downloadable or regenerable from the internet
Score `value` 0-3: [worthless/system noise, routine, notable, high-value irreplaceable].
Result row: dedupe key, path, all probabilities, value score + confidence, usage.input_tokens, error (if any).

## Budget + safety

- Price constant: $0.042 per 1M input tokens, output free. Live spend from response.usage.input_tokens (fallback: len(prompt)/4).
- Hard cap enforced BEFORE each request batch using actual spend so far + worst-case estimate of in-flight batch; abort cleanly, results already on disk remain.
- Concurrency 8 via AsyncTypeSafeClient; tenacity-style retry is built into SDK RetryPolicy (429/5xx).
- Drives are opened read-only by convention: tool never writes outside the repo `runs/` dir.

## SDK surface (verified against typesafe-sdk 0.7.0 installed in .venv)

- `from typesafe_sdk import AsyncTypeSafeClient, Noul, Score, TypeSafeAPIError`
- `AsyncTypeSafeClient(api_key=..., model="jev-latest")`; picks up TYPESAFE_API_KEY env var if api_key omitted.
- `await client.system_one(state: JSONContent, questions: Mapping[str, Noul|Score|Choice]) -> SystemOneResponse`
- `Noul(instructions=str, criteria={"true": str, "false": str})`
- `Score(instructions=str, criteria=[level0, level1, level2, level3])` -> ScoreAnswer(score: float, confidence: float, probabilities: dict[int,float])
- `NoulAnswer.noul: float` (P(yes), 0..1)
- `SystemOneResponse.answers: dict[str, Answer]`, `.usage.input_tokens: int|None`
- Errors: TypeSafeAPIError (has .status via subclass), TypeSafeRateLimitError, TypeSafeAuthenticationError.

## Layout

hdd_analyzer/{__init__.py, cli.py (argparse), config.py, walker.py, extract.py, jev.py, scan.py, report.py}
tests/ for pure logic only (skip rules, categorization, excerpt sanitization, budget accounting). No mocks, no network in tests.
Console script: `hdd-analyzer = hdd_analyzer.cli:main` in pyproject.
