# Releasing

1. Bump `version` in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. Commit, then tag the commit with the version and build the artifacts:

```
git tag -a vX.Y.Z -m "hdd-analyzer X.Y.Z"
uv build
```

3. Push the branch and the tag, then create the GitHub release with the wheel and sdist attached, using the changelog entry as the notes:

```
git push origin master
git push origin vX.Y.Z
gh release create vX.Y.Z dist/hdd_analyzer-X.Y.Z-py3-none-any.whl dist/hdd_analyzer-X.Y.Z.tar.gz --title "hdd-analyzer X.Y.Z" --notes-file CHANGELOG.md
```

For 1.0.0 the tag already exists locally and `dist/` already holds the built artifacts, so only step 3 remains.
