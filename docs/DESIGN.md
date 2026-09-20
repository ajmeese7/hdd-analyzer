# hdd-analyzer design (v1)

Purpose: triage old hard drives for semantically valuable files using TypeSafe's Jev (System One model). Pipeline: free deterministic walk -> cost estimate -> Jev classification pass under a hard budget cap -> ranked report for human review.

## Test drives

- Old Windows system drive mounted at `F:\` (value lives mostly under `F:\Users`)
- Old Linux root filesystem, WSL-mounted, reachable from Windows at `\wsl.localhost\Ubuntu\mnt\wsl\PHYSICALDRIVE4p2` (value lives mostly under `home/`)

## CLI (uv run hdd-analyzer ...)

- `walk ROOT --run NAME [--include SUBPATH ...]` -> writes `runs/NAME/inventory.jsonl` + prints summary (counts/bytes by category, skipped counts). No network, no API.
- `estimate --run NAME` -> token + dollar estimate for a scan of the inventory. No network.
- `scan --run NAME [--cap USD] [--limit N] [--yes] [--dry-run] [--only-name-only]` -> extraction + Jev calls, appends `runs/NAME/results.jsonl` incrementally (resumable: skips file hashes already present). Prints running spend. Hard-stops at cap (default $5). Prompts with the estimate before spending unless `--yes`. `--dry-run` does extraction only, zero API calls. `--only-name-only` restricts candidates to dedupe keys whose existing result has `extraction_status != "ok"`, for a cheap targeted re-scan after an extraction/categorization fix (bypasses the normal resume skip for those specific keys; everything else keeps normal resume semantics).
- `report --run NAME [--top N] [--min-prob P]` -> `runs/NAME/report.md` + `report.csv`, ranked per category and overall.
- `annotate --run NAME [--all] [--top N] [--min-prob P]` -> backfills `metadata_only`/`extraction_status` on `results.jsonl` rows that predate the fields entirely (zero API calls). Never overwrites a row that already has a stored scan-time status, since the report's "verified" column must reflect what Jev actually saw, not what extraction can do today; instead it lists such rows separately as re-scan candidates when today's extraction would now succeed. By default only report-surfaced rows are checked; `--all` covers every row.

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

Categories by extension: text (txt, md, csv, log, json, xml, yaml, ini, cfg, conf, eml, htm(l), plus credential/config formats: pem, key, crt, cer, csr, pub, env, toml, properties, tfvars, netrc, npmrc, pgpass, rtf), code (py, js, ts, c, cpp, h, java, rs, go, sh, ps1, bat, sql, rb, php, pl), doc (docx, doc, odt, xlsx, pdf), image (jpg, png, heic, gif, raw, cr2, tiff), archive (zip, 7z, rar, tar, gz), av (mp3, mp4, mov, avi, mkv, wav), binary/other. Size floor 32 bytes, ceiling for extraction sampling only (no file too big to inventory).

The credential/config extensions were previously unmapped and fell into the
"binary" default, so extraction never even tried them; headline hits like
`private.pem`, `id_rsa`, and `API_keys.rtf` were judged by Jev on filename
alone. `rtf` moved out of the `doc` category into `text`, since RTF is
markup over plain text and gets a dedicated tag-stripping salvage
(`_extract_rtf`) rather than the office-document zip handling used for
docx/xlsx.

### Dedupe key: hash vs metadata

Hashing the first 256 KiB of every file dominates IO cost on spinning disks, and most of that cost was being spent on categories where a duplicate hit is rare or low value (images, archives, media, unclassified binaries) or on oversized files where a partial hash's dedupe accuracy is not worth a big read.

Content hashing (blake2b-128 of first 256 KiB + size) is applied only to files whose category is text, code, or doc (which covers pdf) AND whose size is at most 50 MB. Every other file, including oversized text/code/doc files, gets a metadata-only dedupe key of the form `meta:<size>:<lowercased filename>`, computed without opening the file.

Tradeoff: two differently-named copies of the same non-text file (or of an oversized text/code/doc file) will not be recognized as duplicates under the metadata key, since the name differs. This is a false-negative dedup (both copies kept, neither wrongly discarded), never a false positive, and is the accepted cost of skipping a full-file read on the categories that rarely need it.

## Extraction (scan stage, local)

- text/code: read first 16 KiB, decode via chardet, strip NUL-heavy content (binary masquerading).
- pdf: pypdf, first 3 pages of extractable text.
- docx/xlsx: read as zip, pull word/document.xml / shared strings, tag-strip, first 16 KiB. Plain .doc: latin-1 strings-style salvage of printable runs. rtf: strip RTF control words and braces directly (regex), since RTF's escaping is regular enough not to need the legacy-doc printable-run heuristic.
- image/av/archive: no content; judged on metadata only (path, name, size, mtime), marked `metadata_only`.
- binary (no recognized extension): a content-sniffing fallback (see below) catches plain-text files hiding behind an unknown or missing extension before giving up on them.
- Excerpt hard cap ~6000 chars (~1500 tokens) per file.

### Content sniffing fallback for unrecognized-extension binaries

A file with no extension or an extension outside the categorizer's map
falls into "binary" by default, but some of these (`id_rsa`, `Login Data`,
`myKeyStore`) are plain text with no clue in the name. `should_sniff_binary`
gates a 4 KB peek: eligible only when category is `binary`, size is at most
1 MB, and the extension is not in a denylist of unambiguously binary
formats (`exe`, `dll`, `sqlite`, `pfx`, `kdbx`, and similar) that are
excluded from sniffing even though they also default to `binary`. The peek
reuses the same NUL-heavy binary detection as `sanitize_excerpt`, inverted:
if the sample does not look binary, it is extracted normally with
`extraction_status "ok"`; if it does, the file is left `unsupported`, same
as before. This is purely an extraction-time decision; it does not change
the walker's hash-gating (`should_hash_content`), which still only
content-hashes the text/code/doc categories.

### Extraction transparency

Every candidate carries an `extraction_status`, one of: `ok` (content read
successfully), `no_text` (extraction ran but found nothing, e.g. an
image-only PDF or an empty decode), `timeout` (the per-file extraction
timeout fired), `unsupported` (the category is never sent to extraction at
all: images, audio/video, archives, other binaries), or `error` (extraction
raised, e.g. the file could not be opened). `metadata_only` is true for
every status except `ok`. Both fields are written into every result row, so
a high value_score driven entirely by the file name (Jev guessing from
`passport.pdf` with no readable content) is visible in results.jsonl and in
the report, instead of looking identical to a genuinely content-verified
hit. Extraction is pipelined with classification in `scan` (a producer task
extracts records on a bounded thread pool while the consumer classifies
whatever batch is ready), so file IO overlaps API latency rather than
running entirely upfront; `estimate` keeps the simpler sequential path
since no API calls are in flight to overlap with.

When `metadata_only` is true, the state sent to Jev also sets
`content_readable: false` and replaces `excerpt` with an explicit note
telling Jev the name is weak evidence, rather than silently sending
`excerpt: null` and leaving Jev to infer that on its own.

## Jev questions (one system_one call per file, 6 parallel questions)

State: JSON object {path, name, ext, size, modified, excerpt|null, metadata_only: bool}.
Nouls (each with explicit true/false criteria):
- credentials: passwords, API keys, private keys, seed phrases, wallets, tokens
- personal: personal correspondence, journals, original writing, sentimental content
- financial_legal: tax, banking, contracts, insurance, medical, identity documents
- original_work: authored source code or creative work (vs downloaded/installed/third-party)
- irreplaceable: unlikely to be re-downloadable or regenerable from the internet
Score `value` 0-3: [worthless/system noise, routine, notable, high-value irreplaceable].
Result row: dedupe key, path, all probabilities, value score + confidence, usage.input_tokens, metadata_only, extraction_status, rubric_version, error (if any).

### Rubric versioning

`RUBRIC_VERSION` (in `jev.py`) is bumped whenever a question's
instructions or criteria change meaning, and every result row records the
version it was scored under. Rubric changes only affect future scans;
scores from different rubric versions are not comparable and should not be
mixed in a single ranking. Version 2 tightened the `irreplaceable` rubric to
exclude application-regenerable state (game saves, application caches, a
mail client's local database file that merely indexes mail stored
elsewhere) and target genuinely unique human-created or human-received
content instead.

## Budget + safety

- Price constant: $0.042 per 1M input tokens, output free. Live spend from response.usage.input_tokens (fallback: len(prompt)/4).
- Hard cap enforced BEFORE each request batch using actual spend so far + worst-case estimate of in-flight batch; abort cleanly, results already on disk remain.
- Concurrency 8 via AsyncTypeSafeClient; tenacity-style retry is built into SDK RetryPolicy (429/5xx).
- Drives are opened read-only by convention: tool never writes outside the repo `runs/` dir.
- Systemic error circuit breaker: HTTP 401/403/402 (auth, permission, billing) are treated as run-fatal, not per-file. A preflight canary call runs before any candidate is dispatched; if it fails systemically, the scan aborts immediately with zero files touched. During the batch loop, 5 consecutive systemic errors abort the scan (any success resets the count). Systemic error rows are never written to results.jsonl; per-file errors (validation, timeouts, transient 5xx) are still written and retried on the next `scan` invocation. `ScanOutcome.aborted_reason` carries the message and the CLI exits non-zero when set.

## SDK surface (verified against typesafe-sdk 0.7.0 installed in .venv)

- `from typesafe_sdk import AsyncTypeSafeClient, Noul, Score, TypeSafeAPIError`
- `AsyncTypeSafeClient(api_key=..., model="jev-latest")`; picks up TYPESAFE_API_KEY env var if api_key omitted.
- `await client.system_one(state: JSONContent, questions: Mapping[str, Noul|Score|Choice]) -> SystemOneResponse`
- `Noul(instructions=str, criteria={"true": str, "false": str})`
- `Score(instructions=str, criteria=[level0, level1, level2, level3])` -> ScoreAnswer(score: float, confidence: float, probabilities: dict[int,float])
- `NoulAnswer.noul: float` (P(yes), 0..1)
- `SystemOneResponse.answers: dict[str, Answer]`, `.usage.input_tokens: int|None`
- Errors: TypeSafeAPIError (has .status via subclass), TypeSafeRateLimitError, TypeSafeAuthenticationError.

## Provider resolution (native TypeSafe or OpenRouter)

`TYPESAFE_API_KEY` may hold either a native TypeSafe key or an OpenRouter key
(prefix `sk-or-`). `hdd_analyzer.jev_provider.resolve_provider` picks the
provider: OpenRouter if the key starts with `sk-or-`, native TypeSafe
otherwise, with `JEV_PROVIDER=openrouter|typesafe` overriding the sniff.

OpenRouter serves Jev at its Decisions API, `POST
https://openrouter.ai/api/alpha/decisions`, model id `typesafe/jev-1.13`,
using the same wire protocol and Bearer auth as native TypeSafe. Since the
SDK always builds its request URL as `base_url + "/v1/systemone"`, OpenRouter
support is a path-rewriting httpx2 transport wrapped around the SDK's own
HTTP transport: `OpenRouterTransport` (sync) and `AsyncOpenRouterTransport`
(async) both rewrite `/v1/systemone` to `/api/alpha/decisions` via
`request.url.copy_with(path=...)` and delegate to `httpx2.HTTPTransport` /
`httpx2.AsyncHTTPTransport`. `scan` uses `AsyncTypeSafeClient`, so it wires
`AsyncOpenRouterTransport` via `resolve_async_provider`. `scan` prints the
resolved provider and model at startup.

## Report format

Every table (overall top-N and each category's top-20) carries a `verified`
column: `content` when `extraction_status == "ok"`, `name-only` otherwise,
`unknown` for legacy rows written before extraction_status existed. Within
each table, content-verified rows are listed first, preserving their rank
among the full ranked set; a clearly separated "Name-only matches
(unverified)" subsection follows with the remaining rows, rather than
interleaving a name-only guess between two content-verified hits. Sibling
collapsing then applies within each subsection: once a parent directory
contributes more than 3 rows to a table, the highest-ranked row is kept and
the rest collapse into one `... and N more in <dir>` line, so a directory of
50 game saves cannot flood a table.

## Layout

hdd_analyzer/{__init__.py, cli.py (argparse), config.py, walker.py, extract.py, jev.py, jev_provider.py, scan.py, report.py, annotate.py, paths.py}
tests/ for pure logic only (skip rules, categorization, excerpt sanitization, budget accounting). No mocks, no network in tests.
Console script: `hdd-analyzer = hdd_analyzer.cli:main` in pyproject.
