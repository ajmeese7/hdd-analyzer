# Releasing

Releases are published to PyPI automatically by `.github/workflows/publish.yml` using PyPI Trusted Publishing (OIDC), so no API token is stored anywhere.

## One-time setup

1. On PyPI, add a trusted publisher for the project (Your projects, then Publishing, or the pending-publisher form for a project that does not exist yet): owner `ajmeese7`, repository `hdd-analyzer`, workflow `publish.yml`, environment `pypi`.
2. On GitHub, create an environment named `pypi` under the repository settings. Optionally restrict it to protected tags and require your own review, which adds a manual approval gate before anything reaches PyPI.

## Each release

1. Bump `version` in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. Commit and tag:

```
git tag -a vX.Y.Z -m "hdd-analyzer X.Y.Z"
git push origin master
git push origin vX.Y.Z
```

3. Create the GitHub release; publishing it triggers the workflow, which runs the tests, builds the wheel and sdist, and uploads them to PyPI:

```
gh release create vX.Y.Z --title "hdd-analyzer X.Y.Z" --notes-file CHANGELOG.md
```

Watch the run with `gh run watch`. The release page does not need the artifacts attached by hand; PyPI is the distribution channel.

For 1.0.0 the tag already exists locally, so only the one-time setup and step 3 remain.
