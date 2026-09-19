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

Per file record: path, size, mtime, ext, category, dedupe key. Streams JSONL; errors (permission, long path) are counted and logged to `runs/NAME/walk-errors.log`, never fatal. Skips symlinks/reparse points. Dedupe: first path wins, duplicates recorded with `dup_of`.

Progress: every 5000 files scanned, prints a running count and the current directory to stderr. At the end of the walk, prints a per-top-level-directory file count breakdown to stderr, so rabbit holes (a dependency cache tree that slipped past the skip rules, an AppData subtree with far more files than expected) are visible without re-running.

### Skip dirs

Real-drive walks showed most inventoried files come from dependency/build cache directories rather than user content, so skip rules are split into three layers:

**Unconditional (name match, case-insensitive, skipped everywhere):** Windows, Program Files, Program Files (x86), ProgramData, $Recycle.Bin, System Volume Information, $WinREAgent, Recovery, PerfLogs, Intel, XboxGames, node_modules, .git, __pycache__, .venv, venv, .cache, .nuget, .gradle, .m2, .cargo, .rustup, .npm, .pnpm-store, .yarn, site-packages, .tox, .mypy_cache, .pytest_cache, .ruff_cache, Cache/cache/Caches, CachedData, Code Cache, GPUCache, ShaderCache, .vs, .idea, .vscode-server, Temp/tmp, .Trash-1000, .thumbnails, plus Linux system dirs when walking a rootfs: proc, sys, dev, run, usr, lib, lib64, bin, sbin, boot, snap, var (except var/mail), lost+found, etc (keep: small, can hold hand-edited configs), cdrom, media, mnt, opt, srv (keep).

**Marker-based (only skipped when a sibling entry in the same directory identifies the project type; the sibling set comes for free from the scandir listing already used to walk the parent):**
- Library, Temp, Logs, obj -> skipped when a sibling ProjectSettings or Assets exists (Unity project)
- target -> skipped when a sibling Cargo.toml or pom.xml exists (Rust/Maven)
- bin, obj -> skipped when any sibling *.csproj or *.sln file exists (.NET)
- build -> skipped when a sibling gradlew, CMakeLists.txt, or package.json exists
- dist, .next, .nuxt, coverage -> skipped when a sibling package.json exists
- vendor -> skipped when a sibling composer.json or go.mod exists (not unconditional: "vendor" is a common legitimate directory name otherwise)

**AppData policy (Windows):** AppData\Local and AppData\LocalLow are skipped as whole subtrees (never descended into) since they're machine-local install/cache trees. AppData\Roaming is left walkable, since real user configs and credentials live there (FileZilla, Thunderbird, etc.); the unconditional cache-name skips above still apply inside Roaming.

Categories by extension: text (txt, md, csv, log, json, xml, yaml, ini, cfg, conf, eml, htm(l)), code (py, js, ts, c, cpp, h, java, rs, go, sh, ps1, bat, sql, rb, php, pl), doc (docx, doc, rtf, odt, xlsx, pdf), image (jpg, png, heic, gif, raw, cr2, tiff), archive (zip, 7z, rar, tar, gz), av (mp3, mp4, mov, avi, mkv, wav), binary/other. Size floor 32 bytes, ceiling for extraction sampling only (no file too big to inventory).

### Dedupe key: hash vs metadata

Hashing the first 256 KiB of every file dominates IO cost on spinning disks, and most of that cost was being spent on categories where a duplicate hit is rare or low value (images, archives, media, unclassified binaries) or on oversized files where a partial hash's dedupe accuracy is not worth a big read.

Content hashing (blake2b-128 of first 256 KiB + size) is applied only to files whose category is text, code, or doc (which covers pdf) AND whose size is at most 50 MB. Every other file, including oversized text/code/doc files, gets a metadata-only dedupe key of the form `meta:<size>:<lowercased filename>`, computed without opening the file.

Tradeoff: two differently-named copies of the same non-text file (or of an oversized text/code/doc file) will not be recognized as duplicates under the metadata key, since the name differs. This is a false-negative dedup (both copies kept, neither wrongly discarded), never a false positive, and is the accepted cost of skipping a full-file read on the categories that rarely need it.

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
