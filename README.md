# hdd-analyzer

<p align="center">
  <img src="https://raw.githubusercontent.com/ajmeese7/hdd-analyzer/master/promo/hdd-analyzer.gif" alt="hdd-analyzer walking a drive, scanning files with an LLM, and ranking the keepers">
</p>

Old hard drives pile up faster than anyone can manually sort them, and most of what is on them is "dark data": junk, caches, and installers with a handful of genuinely irreplaceable files buried inside. hdd-analyzer walks a drive for free, estimates the token and dollar budget to have [Jev](https://typesafe.ai) (TypeSafe's [System One](https://docs.typesafe.ai/concepts/system-one) decision model, also served through [OpenRouter](https://openrouter.ai/typesafe/jev-1.13)) read every file, then runs a capped classification pass and produces a ranked report so a human can decide what is worth keeping before the drive gets wiped. Cheap, fast inference has made this kind of exhaustive per-file triage practical in a way it was not a few years ago.

## What is Jev?

Jev is [TypeSafe](https://typesafe.ai)'s System One decision model: instead of free-text generation, it answers a fixed set of yes/no questions (nouls), scored questions, and multiple-choice questions against a piece of state, returning calibrated probabilities instead of prose, which makes it well suited to structured classification like this tool's per-file rubric. hdd-analyzer talks to Jev either directly through TypeSafe's API or through [OpenRouter's Decisions endpoint](https://openrouter.ai/typesafe/jev-1.13), auto-detected from your API key.

## Install

```
pip install hdd-analyzer
```

Or as an isolated tool with `uv`:

```
uv tool install hdd-analyzer
```

Requires Python 3.13+. To run the latest unreleased code instead, install from GitHub with `pip install git+https://github.com/ajmeese7/hdd-analyzer.git`.

## Configuration

Create a `.env` file in the working directory you'll run `hdd-analyzer` from; it is loaded automatically.

| Variable | Required | Description |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | yes | Either a native TypeSafe key or an OpenRouter key (prefix `sk-or-`). The provider is auto-detected from the prefix. |
| `JEV_PROVIDER` | no | `openrouter` or `typesafe`, overrides the key-prefix auto-detection. |
| `TESSERACT_CMD` | no | Full path to `tesseract.exe` if it is not on PATH. Needed only for the optional `ocr` command. |

```
TYPESAFE_API_KEY=your-key-here
```

Do not double-quote `TESSERACT_CMD` in `.env`. `python-dotenv` treats a double-quoted value as an escaped string, so `"\tesseract.exe"` becomes a literal tab followed by `esseract.exe`. Leave it unquoted or single-quoted.

## Quickstart

Everything below assumes an old drive mounted at `D:\` (or `/mnt/olddrive` on Linux/macOS) with the interesting content under `Users`.

```
hdd-analyzer walk D:\ --run olddrive --include Users
hdd-analyzer estimate --run olddrive
hdd-analyzer scan --run olddrive --cap 5
hdd-analyzer report --run olddrive
hdd-analyzer manifest --run olddrive
```

`walk` inventories the drive for free. `estimate` prices out a full scan with no network calls. `scan` extracts local content and sends one classification call per file to Jev, prompting for confirmation and stopping at the cap. `report` renders `runs/olddrive/report.md` and `report.csv`, ranked overall and per category. `manifest` turns the results into a salvage copy list in `runs/olddrive/manifest/`, ready to hand to `robocopy` or `rsync` before the drive is wiped.

## How it stays cheap and safe

- `walk` is a pure filesystem pass: no network calls, no API spend.
- Duplicate files are deduped by content hash (text/code/doc) or by size and filename (everything else), so Jev never classifies the same file twice.
- Dependency caches, build output, and other junk directories (`node_modules`, `.git`, `AppData\Local`, and dozens more) are skipped before they ever hit the candidate list.
- `estimate` and `scan` print the candidate count, estimated tokens, and estimated dollar cost, and `scan` asks for interactive `y` confirmation before spending anything (skip with `--yes`).
- `scan` enforces a hard spend cap (`--cap`, default $5) before dispatching each batch, using actual spend so far plus a worst-case estimate of the in-flight batch.
- A preflight canary call and a circuit breaker abort the scan immediately on authentication or billing errors (HTTP 401/403/402), instead of burning through the candidate list on a broken key.
- `scan` is resumable: re-running it skips files whose dedupe key is already in `results.jsonl`, and failed files retry automatically.
- Every result is labeled `content` (Jev read the actual file), `ocr` (Jev read a Tesseract transcription), or `name-only` (Jev only saw the file name and metadata), and reports list content-verified hits first so a suggestive filename like `passport.pdf` with no extractable text can't be mistaken for a verified hit.
- The tool never writes outside its own `runs/` directory, and drives are only ever opened read-only.

## Optional: OCR for images and scanned PDFs

`scan` never runs OCR itself; images and no-text PDFs get judged by Jev on filename alone. The separate `ocr` command is local, free, and makes zero API calls: it revisits an existing run's results, OCRs the highest-ranked name-only images and no-text PDFs with Tesseract, and writes `runs/NAME/ocr.jsonl`. Then `scan --from-ocr` re-classifies those rows using the OCR excerpt instead of re-running extraction.

Install Tesseract first:

```
winget install UB-Mannheim.TesseractOCR
```

On Linux or macOS:

```
sudo apt install tesseract-ocr
brew install tesseract
```

Then:

```
hdd-analyzer ocr --run olddrive
hdd-analyzer scan --run olddrive --from-ocr --cap 0.25
hdd-analyzer report --run olddrive
```

## Commands

- `walk ROOT --run NAME [--include SUBPATH ...]` - free, deterministic inventory walk. Writes `runs/NAME/inventory.jsonl`.
- `estimate --run NAME` - token and dollar estimate for a scan. No network calls.
- `scan --run NAME [--cap USD] [--limit N] [--yes] [--dry-run] [--only-name-only] [--exclude-ext EXT[,EXT...]] [--from-ocr]` - local extraction plus one Jev call per file, under a hard spend cap. Appends to `runs/NAME/results.jsonl` and is resumable.
- `annotate --run NAME [--all] [--top N] [--min-prob P]` - backfills `metadata_only`/`extraction_status` onto existing results by re-running local extraction only, zero API calls.
- `ocr --run NAME [--top N] [--all] [--min-value V] [--limit N]` - local, free OCR pass over an existing run's name-only image and no-text-PDF rows.
- `report --run NAME [--top N] [--min-prob P]` - renders `runs/NAME/report.md` and `report.csv`, ranked overall and per category.
- `manifest --run NAME [--min-value V] [--min-prob P] [--out DIR]` - the salvage deliverable: read-only over `results.jsonl`/`inventory.jsonl`, writes copy lists and summaries to `runs/NAME/manifest/`.

## Output files

- `runs/NAME/inventory.jsonl` - one row per walked file: path, size, mtime, extension, category, dedupe key.
- `runs/NAME/walk-errors.log` - files the walker could not read (permissions, long paths).
- `runs/NAME/results.jsonl` - one row per scanned file: all Jev probabilities, value score, extraction status, tokens used.
- `runs/NAME/ocr.jsonl` - OCR excerpts for name-only images and no-text PDFs; contains real file content, keep it out of version control.
- `runs/NAME/report.md` / `report.csv` - the ranked report, overall and per category.
- `runs/NAME/manifest/copy-list.txt`, `copy-list-verified.txt`, `copy-list-name-only.txt` - one absolute source path per line, ready for `robocopy` or `rsync`.
- `runs/NAME/manifest/credentials.md` - every row that scored high on credentials, paths only, with a reminder to rotate anything still valid.
- `runs/NAME/manifest/by-category.md` - a table per category plus a top-30 directory rollup.
- `runs/NAME/manifest/summary.txt` - counts, total bytes, and a documented copy-tool invocation.

## Privacy

`scan` sends excerpts of file content to whichever API provider you configure (TypeSafe or OpenRouter) so Jev can classify them. `runs/ocr.jsonl` and `runs/results.jsonl` hold those excerpts locally, which can include credentials or other personal information pulled straight from your files; `runs/` is gitignored for this reason and should never be committed or shared as-is.

## Development

```
uv sync
uv run pytest
```

## License

BSD 3-Clause. See [LICENSE](LICENSE).
