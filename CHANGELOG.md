# Changelog

## Unreleased

- `EXCERPT_CHAR_CAP` 6000 to 3000: 28% fewer tokens on content calls; measured at the rerun noise floor for notable decisions (docs/EXPERIMENTS-2026-09.md).
- Inclusion rule (`manifest`, `report.html`): `original_work` alone no longer makes a verified row notable; value, credentials, personal, financial/legal, and irreplaceable still do. On a 50k-file profile drive this took the notable set from 4,355 rows to 820 without dropping a credential hit.
- `scan --outdated-rubric` re-scores every row still carrying an older rubric version, name-only rows included. Mixed-version results are not comparable, and `annotate --all` had just exposed thousands of rubric-v1 rows as content-verified with stale scores.
- `scan --rpm N` paces classification requests for rate-limited providers (Vercel AI Gateway's free tier allows 30 per minute), and `--concurrency N` exposes the worker count.
- `scan` retries 429s and 5xx with a longer backoff (6 attempts, 1s to 20s) instead of the SDK default that turned a gateway burst into error rows.
- `load_results` never lets an error row supersede a successful judgment, so a failed rescan cannot hide a prior result from the report or from `--only-name-only` selection.

## 1.1.0 - 2026-09-20

- `report`: also writes `report.html`, a self-contained interactive report with summary tiles, a "where to focus" list of the directories holding the most notable files, a collapsible directory tree with notable/scanned counts per subtree, and a searchable, filterable table of every notable file. New `--min-value` flag (default 2.0, matching `manifest`) sets the value floor for a file to count as notable.
- Provider resolution: Vercel AI Gateway keys (`vck_` prefix) route to the gateway's TypeSafe-compatible endpoint with model `typesafe-ai/jev`; `JEV_PROVIDER=vercel` forces it.
- `walk`: a relative root (`walk docs --run x`) now works on Windows; the extended-length `\\?\` prefix requires an absolute path, so it was silently listing nothing.
- pyproject: drop the deprecated license classifier (PEP 639); `license = "BSD-3-Clause"` already declares it.

## 1.0.1 - 2026-09-21

- README: link the demo GIF by absolute GitHub URL so it renders on PyPI.

## 1.0.0 - 2026-09-21

- `walk`: free, deterministic drive inventory with content and metadata dedupe, layered skip rules for dependency/build caches, and per-extension categorization.
- `estimate`: local token and dollar cost projection for a full scan, no network calls.
- `scan`: pipelined local extraction and Jev classification under a hard, enforced spend cap, with resumable dedupe-key-based skip on re-run.
- Provider resolution for Jev: auto-detects a native TypeSafe key or an OpenRouter key (`sk-or-` prefix) and routes to the corresponding API, overridable via `JEV_PROVIDER`.
- Preflight canary check and a systemic-error circuit breaker abort a scan immediately on authentication or billing failures instead of burning through every candidate.
- `annotate`: zero-API-call backfill of extraction status onto existing results after an extraction fix.
- `ocr`: local, free Tesseract OCR pass over name-only images and no-text PDFs, plus `scan --from-ocr` to re-classify using the recovered text.
- `report`: ranked Markdown and CSV reports that clearly separate content-verified, OCR-verified, and name-only-verified results.
- `manifest`: salvage-list generator producing copy lists, a credentials warning list, a per-category breakdown, and a summary, read-only over prior run output.
