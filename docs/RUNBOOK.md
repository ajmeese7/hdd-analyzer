# Runbook: scanning the two test drives

One-time setup: copy `.env.example` to `.env` and paste your TypeSafe API key.

Small supervised pilot (about 500 files, roughly $0.01, prints estimate and asks y/n first):

```
uv run hdd-analyzer scan --run f-users --limit 500 --cap 0.25
uv run hdd-analyzer report --run f-users
```

Full scans (estimates: f-users $0.38, wsl-home $0.81, both capped at $5):

```
uv run hdd-analyzer scan --run f-users
uv run hdd-analyzer scan --run wsl-home
uv run hdd-analyzer report --run f-users
uv run hdd-analyzer report --run wsl-home
```

Scans are resumable: rerun the same command to continue after a stop or cap hit. Failed files retry automatically on rerun. Reports land in `runs/<name>/report.md` and `report.csv`.

Fresh inventory after drive changes:

```
uv run hdd-analyzer walk F:/Users/ajmee --run f-users
uv run hdd-analyzer walk //wsl.localhost/Ubuntu/mnt/wsl/PHYSICALDRIVE4p2/home/ajmeese7 --run wsl-home
uv run hdd-analyzer estimate --run f-users
```

Re-classify only the rows that were judged by filename alone (cheap, targeted):

```
uv run hdd-analyzer scan --run f-users --only-name-only --cap 0.25
uv run hdd-analyzer scan --run wsl-home --only-name-only --cap 0.25
uv run hdd-analyzer report --run f-users
uv run hdd-analyzer report --run wsl-home
```
