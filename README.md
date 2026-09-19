# hdd-analyzer

Use Jev (TypeSafe's System One model) to triage old hard drives for
semantically valuable files. It walks a drive for free, estimates the token
and dollar budget for classifying every file, then runs a capped LLM scan
and produces a ranked report so a human can decide what's worth keeping.

## Pipeline

1. `walk` - deterministic, free directory walk. Writes `runs/NAME/inventory.jsonl`
   (path, size, mtime, extension, category, dedupe key) and a
   `runs/NAME/walk-errors.log` for anything unreadable. No network calls.
2. `estimate` - token and dollar estimate for scanning the inventory. No network.
3. `scan` - local extraction (first bytes of text/code, first pages of PDFs,
   document.xml/sharedStrings.xml for docx/xlsx) followed by one Jev call per
   file, under a hard spend cap. Appends to `runs/NAME/results.jsonl` and is
   resumable: re-running skips files whose dedupe key is already recorded.
4. `report` - ranks the results into `runs/NAME/report.md` (overall top-N plus
   per-category top-20 tables) and `runs/NAME/report.csv` (all rows, flattened).

## Setup

Copy `.env.example` to `.env` and fill in `TYPESAFE_API_KEY`:

```
copy .env.example .env
```

Dependencies are managed with `uv`. Install with:

```
uv sync
```

## Usage

Run everything through `uv run hdd-analyzer <command>`.

### Test drive 1: old Windows system drive

Value lives mostly under `F:\Users`.

```
uv run hdd-analyzer walk F:\ --run winbox --include Users
uv run hdd-analyzer estimate --run winbox
uv run hdd-analyzer scan --run winbox --cap 5
uv run hdd-analyzer report --run winbox
```

### Test drive 2: old Linux root filesystem (WSL-mounted)

Value lives mostly under `home/`.

```
uv run hdd-analyzer walk \\wsl.localhost\Ubuntu\mnt\wsl\PHYSICALDRIVE4p2 --run linuxbox --include home
uv run hdd-analyzer estimate --run linuxbox
uv run hdd-analyzer scan --run linuxbox --cap 5
uv run hdd-analyzer report --run linuxbox
```

## Safety

- Drives are opened read-only by convention. The tool never writes outside
  the repo's `runs/` directory.
- `scan` prints the candidate file count, estimated tokens, estimated cost,
  and the hard cap, then asks for interactive `y` confirmation before
  spending anything (skip with `--yes`).
- `scan --dry-run` runs extraction only, with zero API calls.
- The default hard cap is $5 and is enforced before dispatching each batch,
  using actual spend so far plus a worst-case estimate of the in-flight
  batch. Results already written to `results.jsonl` are never truncated or
  discarded when the cap is hit.
