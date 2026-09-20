# Changelog

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
