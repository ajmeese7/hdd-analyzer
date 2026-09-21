# hdd-analyzer design (v1)

Purpose: triage old hard drives for semantically valuable files using TypeSafe's Jev (System One model). Pipeline: free deterministic walk -> cost estimate -> Jev classification pass under a hard budget cap -> ranked report for human review.

Target environments: a locally attached or externally mounted Windows drive (e.g. `D:\`), or a Linux/macOS rootfs mounted read-only (including a WSL-mounted rootfs reachable from Windows over UNC, e.g. `\\wsl.localhost\Ubuntu\...`). The walker's skip rules and the manifest's UNC path handling both exist to support this cross-platform, cross-filesystem case.

## CLI (uv run hdd-analyzer ...)

- `walk ROOT --run NAME [--include SUBPATH ...]` -> writes `runs/NAME/inventory.jsonl` + prints summary (counts/bytes by category, skipped counts). No network, no API.
- `estimate --run NAME` -> token + dollar estimate for a scan of the inventory. No network.
- `scan --run NAME [--cap USD] [--limit N] [--yes] [--dry-run] [--only-name-only] [--exclude-ext EXT[,EXT...]] [--from-ocr]` -> extraction + Jev calls, appends `runs/NAME/results.jsonl` incrementally (resumable: skips file hashes already present). Prints running spend. Hard-stops at cap (default $5). Prompts with the estimate before spending unless `--yes`. `--dry-run` does extraction only, zero API calls. `--only-name-only` restricts candidates to rows that were judged from filename alone (`was_name_only`) AND that today's extraction would actually attempt (`extract._extraction_eligible` against today's `categorize(ext)`, no file IO at selection time), for a cheap targeted re-scan after an extraction/categorization fix; this bypasses the normal resume skip for those specific keys, everything else keeps normal resume semantics. `--exclude-ext` drops specific extensions from candidates on top of whatever selection is in effect. `--from-ocr` restricts candidates to dedupe keys with an `ok` row in `runs/NAME/ocr.jsonl` (see the `ocr` stage below) and classifies each using its OCR excerpt instead of running extraction, recording `extraction_status: "ocr"`. The filtered count is what prints as "candidate files" before the confirmation prompt.
- `ocr --run NAME [--top N] [--all] [--min-value V] [--limit N]` -> local, free, zero-API-call OCR pass over an existing run's `results.jsonl`. Selects name-only image rows and name-only PDF rows whose extraction found no text, runs Tesseract on a thread pool, and writes `runs/NAME/ocr.jsonl`. See the OCR stage section below.
- `report --run NAME [--top N] [--min-prob P] [--min-value V]` -> `runs/NAME/report.md` + `report.csv` (ranked per category and overall) + `report.html` (interactive directory tree, see HTML report below).
- `annotate --run NAME [--all] [--top N] [--min-prob P]` -> backfills `metadata_only`/`extraction_status` on `results.jsonl` rows that predate the fields entirely (zero API calls). Never overwrites a row that already has a stored scan-time status, since the report's "verified" column must reflect what Jev actually saw, not what extraction can do today; instead it lists such rows separately as re-scan candidates when today's extraction would now succeed. By default only report-surfaced rows are checked; `--all` covers every row.
- `manifest --run NAME [--min-value V] [--min-prob P] [--out DIR]` -> the salvage deliverable: read-only over `results.jsonl`/`inventory.jsonl` (never writes either), writes `runs/NAME/manifest/` (or `--out`) with copy lists and summaries. See the Manifest stage section below.

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

Categories by extension: text (txt, md, csv, log, json, xml, yaml, ini, cfg, conf, eml, htm(l), svg, plus credential/config formats: pem, key, crt, cer, csr, pub, env, toml, properties, tfvars, netrc, npmrc, pgpass, rtf, plus mail/structured formats: emlx, emlxpart, mbox, ics, vcf, plist, wifi), code (py, js, ts, c, cpp, h, java, rs, go, sh, ps1, bat, sql, rb, php, pl, cs, pyi, pyx, css, scss, less, vue, svelte, kt, swift, m, mm, dart, lua, r, jl, ex, exs, erl, hs, scala, groovy, gradle, cmake, mk, dockerfile, tf, hcl, nix, zsh, fish, psm1, psd1, vbs, ahk), doc (docx, doc, odt, xlsx, pdf), image (jpg, png, heic, gif, raw, cr2, tiff, webp, avif, bmp, ico), archive (zip, 7z, rar, tar, gz), av (mp3, mp4, mov, avi, mkv, wav), generated (meta, asset, mat, prefab, unity, anim, controller, cubemap, physicmaterial), binary/other. Size floor 32 bytes, ceiling for extraction sampling only (no file too big to inventory).

`generated` covers engine/tool-generated metadata with no independent value
(Unity `.meta` sidecars and similar project artifacts): inventoried and
counted like any other category, but excluded from candidate building the
same way a duplicate is (never extracted, never content-hashed since it's
outside `_HASH_ELIGIBLE_CATEGORIES`, never sent to Jev).

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
- eml/emlx: parsed with `email.parser` (policy=`email.policy.default`); excerpt is From/To/Date/Subject headers plus the first text/plain part, or html tag-stripped if no plain-text part exists. `.emlx` (Apple Mail) files are a byte-count line, then that many bytes of RFC822 message, then a trailing plist; only the count line and the message bytes are read, the plist is never included.
- image/av/archive: no content; judged on metadata only (path, name, size, mtime), marked `metadata_only`.
- generated (Unity `.meta` and similar): no content, excluded from candidates entirely before extraction is even considered (see Walk stage).
- binary (no recognized extension): a content-sniffing fallback (see below) catches plain-text files hiding behind an unknown or missing extension before giving up on them.
- Excerpt hard cap 3000 chars (~750 tokens) per file; measured against 6000 in September 2026 as a 28% token cut at the rerun noise floor (see EXPERIMENTS-2026-09.md).

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

## OCR stage (separate command, local, free)

`scan` never runs OCR itself; images and no-text PDFs are extraction-`unsupported`/`no_text`
and get judged by Jev on filename alone. `ocr` (`hdd_analyzer/ocr.py`) is a
standalone, zero-API-call command that revisits an existing run's
`results.jsonl` and gives the highest-ranked of those name-only guesses a
chance to be confirmed or refuted by actual content, without re-spending on
a fresh Jev pass for files that were never going to be readable anyway.

Selection (`select_ocr_candidates`): a row is eligible when `was_name_only`
is true AND either its category is `image`, or its extension is `pdf` and
its stored `extraction_status` is `no_text` (a scanned document with no text
layer of its own; other non-`ok` PDF statuses, like `unsupported` or
`error`, are not retried here since OCR would not help). By default the
top `--top` (200) eligible rows by `value_score`, restricted to
`value_score >= --min-value` (2.0), are OCR'd; `--all` OCRs every eligible
row regardless of score or rank; `--limit` caps the total either way.

Tesseract resolution (`resolve_tesseract`): `TESSERACT_CMD` env var (`.env`,
unquoted or single-quoted -- python-dotenv unescapes `\t` inside
double-quoted values, which silently turns `\tesseract.exe` into a tab),
else `tesseract` on PATH, else the winget default install path
(`C:\Program Files\Tesseract-OCR\tesseract.exe`) if it exists, else a
`RuntimeError` pointing at the README's OCR setup section.

Images (`OCR_IMAGE_EXTS`: jpg, jpeg, png, tif, tiff, bmp, webp, gif) are
OCR'd directly via `tesseract <file> stdout -l eng --psm 3` over stdin/stdout,
using the extended-length path helper from `paths.py` and, on Windows,
`CREATE_NO_WINDOW` so no console flashes per file. HEIC is a valid `image`
category extension but has no Tesseract decoder, so it is reported
`unsupported` without an attempt. PDFs are rasterized with `pypdfium2` at
~150dpi, first 3 pages (`OCR_PDF_MAX_PAGES`), each page OCR'd the same way
via a temporary PNG cleaned up in a `finally` block; a corrupt or encrypted
PDF that pypdfium2 cannot even open, or cannot rasterize a single page from,
comes back `no_text` rather than raising.

Runs on a 4-worker thread pool (subprocess/IO bound), printing progress
every 25 files to stderr. Every attempt appends one row to
`runs/NAME/ocr.jsonl`: `{dedupe_key, path, ocr_status, excerpt_chars,
excerpt}`, `ocr_status` one of `ok`, `no_text`, `timeout`, `error`,
`unsupported`. The excerpt is capped at the same `EXCERPT_CHAR_CAP` used by
`extract.py`. **`ocr.jsonl` holds real file content excerpts (potentially
credentials or other PII pulled straight off a scanned ID or document) and
must never be reported or printed verbatim; it lives under `runs/`, which is
gitignored, same as everything else in a run.**

`scan --from-ocr` re-classifies rows OCR recovered content for: it restricts
candidates to dedupe keys with an `ok` row in `ocr.jsonl` (`only_keys`,
reusing the same resume-bypass mechanism as `--only-name-only`) and, via an
`excerpt_override` mapping threaded through `build_candidates` and the
extraction pipeline, uses the OCR excerpt directly instead of calling
`extract_excerpt`/`extract_excerpt_async`. Candidates built this way get
`metadata_only: false` and `extraction_status: "ocr"`, a state distinct from
both `ok` (direct extraction) and name-only, and `verified_label` renders it
as `ocr` in the report rather than folding it into `content` or `name-only`.

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

## Provider resolution (native TypeSafe, OpenRouter, or Vercel AI Gateway)

`TYPESAFE_API_KEY` may hold a native TypeSafe key, an OpenRouter key (prefix
`sk-or-`), or a Vercel AI Gateway key (prefix `vck_`).
`hdd_analyzer.jev_provider.resolve_provider` picks the provider from the
prefix, native TypeSafe when nothing matches, with
`JEV_PROVIDER=typesafe|openrouter|vercel` overriding the sniff.

Vercel AI Gateway exposes a TypeSafe-compatible API at
`https://ai-gateway.vercel.sh/typesafe` (`POST /v1/systemone`, TypeSafe's own
request and response shapes, Bearer auth with the gateway key), so it needs
only a different `base_url` and the model id `typesafe-ai/jev`; no transport
wrapper. The gateway adds a `provider_metadata` field to responses, which the
SDK's response models ignore (`extra="ignore"`).

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

## HTML report

`report_html.py` renders `report.html`, a single self-contained page (inline
CSS/JS, no external assets) from a JSON payload embedded in a
`<script type="application/json">` block. `build_tree` aggregates every valid
result row into a directory trie keyed by `paths.pure_path(...).parents`, so
Windows and POSIX paths both split correctly on any host. Every row bumps its
directory's `scanned` count; only rows passing `manifest.is_manifest_included`
(the same notable test the manifest uses, with `report --min-value` and
`--min-prob`) are embedded as file entries. `_finalize` post-order rolls up
`scanned_subtree`, `notable_subtree`, per-category counts at or above
`min_prob`, max value, and notable bytes, prunes subtrees with no notable
files (their scanned counts still roll into the surviving ancestor), and
collapses single-child chains with no direct files (`F:\Users\ajmee` becomes
one node). Children sort by `notable_subtree` desc.

Payload size is dominated by paths, so file entries carry a `dir` index into
a flat `dirs` list (each with a trailing native separator) plus `name`, and
the page joins them. The JSON is `ensure_ascii` (odd filenames may carry lone
surrogates) with `</` escaped so a filename cannot terminate the script
block. Entries never include excerpts. The page renders tree children lazily
on expand, caps rendered children per node with a "more" row, pages the
ranked table, and auto-selects the directory with the most direct notable
files.

## Report format

Every table (overall top-N and each category's top-20) carries a `verified`
column: `content` when `extraction_status == "ok"`, `ocr` when
`extraction_status == "ocr"` (see the OCR stage), `name-only` for every other
status, `unknown` for legacy rows written before extraction_status existed.
Within each table, content-verified rows are listed first, preserving their
rank among the full ranked set; a clearly separated "Name-only matches
(unverified)" subsection follows with the remaining rows (including `ocr`
rows), rather than interleaving a name-only guess between two
content-verified hits. Sibling collapsing then applies within each
subsection: once a parent directory contributes more than 3 rows to a table,
the highest-ranked row is kept and the rest collapse into one `... and N
more in <dir>` line, so a directory of 50 game saves cannot flood a table.

## Manifest stage (salvage list, read-only)

The final deliverable for a drive is "salvage list to copy off, then wipe."
`manifest.py` only reads `results.jsonl` (via `report.load_results`, so it
never duplicates the latest-row-per-key logic) and `inventory.jsonl`; it
never writes either.

Selection (`is_manifest_included`, defaults `--min-value 2.0`, `--min-prob
0.7`): a row is excluded outright if it has an `error` or its category is
`generated`. A content-verified or OCR-verified row (`verified_label` in
`content`/`ocr`) is included if its `value_score >= min-value` OR its
probability for `credentials`, `financial_legal`, `personal`, or
`irreplaceable` is `>= min-prob`. `original_work` never qualifies a row on
its own: authored code usually also lives in a git remote, Jev scores it
low on `irreplaceable` anyway, and letting it through turned one drive's
authored-code tree into thousands of "notable" rows (EXPERIMENTS-2026-09.md).
A name-only (or legacy `unknown`) row is
trusted far less: it is included only if its probability for `credentials`,
`financial_legal`, or `personal` is `>= min-prob`, never through
`value_score` alone and never through the other two categories
(`original_work`, `irreplaceable`), since a suggestive filename is real
signal for "this might be a password file" but not for "this might be a
unique file," e.g. a name-only-judged game save should not make the cut.

Output, written to `runs/NAME/manifest/` (or `--out`):

- `copy-list.txt`, `copy-list-verified.txt`, `copy-list-name-only.txt`: one
  absolute source path per line, plain (not extended-length) form, sorted by
  (parent directory, filename) so a copy tool's batching stays sane. UNC
  paths (`\\wsl.localhost\...`) pass through unchanged; robocopy accepts
  them directly.
- `credentials.md`: every row (from the full result set, not just the
  included manifest) scoring `>= 0.6` on `credentials`, sorted descending,
  columns probability/verified/size/path, paths only, with a header warning
  to treat these as compromised if the drive ever left custody and to
  rotate anything still valid.
- `by-category.md`: a table per Noul category of included rows scoring
  `>= min-prob` in that category, using `report.collapse_siblings` so a
  single flooded directory doesn't dominate a table, followed by a top-30
  directory rollup (by count of included files) so a whole folder worth
  copying wholesale (an Obsidian vault, a Documents tree) is obvious without
  reading every row.
- `summary.txt`: total/verified/name-only counts, per-category counts, total
  bytes to copy (human-readable), and a documented robocopy invocation shape
  for driving `copy-list.txt` (grouping by source directory, since robocopy
  copies one source directory to one destination directory per invocation).

## Layout

hdd_analyzer/{__init__.py, cli.py (argparse), config.py, walker.py, extract.py, jev.py, jev_provider.py, scan.py, ocr.py, report.py, report_html.py, templates/report.html, annotate.py, manifest.py, paths.py}
tests/ for pure logic only (skip rules, categorization, excerpt sanitization, budget accounting, OCR selection/resolution, manifest selection/rollup). No mocks, no network, no tesseract invocation in tests.
Console script: `hdd-analyzer = hdd_analyzer:main` in pyproject (re-exported from `cli.main`).
