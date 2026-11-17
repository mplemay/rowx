# Publishing

`rowx`, `rowx-core`, `rowx-postgres`, and `rowx-embedded` publish from repository `mplemay/rowx` through workflow
`release.yml` and one GitHub environment, `pypi`.

Each package is its own job. Publish order is `rowx-core`, then `rowx-postgres`, then `rowx-embedded`, then `rowx`. Each
job runs `uv publish`. PyPI accepts a second upload of a byte-identical file and rejects a same-name file with
different bytes, so re-running a failed publish job uploads only what is missing. If a later publish job was skipped
because an earlier one failed, re-run that skipped job after the failed one succeeds.

`rowx` already uses the `pypi` environment. The owner registers pending trusted publishers for
`rowx-core` and `rowx-postgres`.

`just release X.Y.Z` bumps the lockstep version, regenerates `docs/changelog.md` with git-cliff, and opens the release
pull request. After that pull request merges, a green CI push to main runs the tag release job. That job creates
`vX.Y.Z` on the merge commit and dispatches `release.yml` on the tag. A tag push from `GITHUB_TOKEN` does not start
`release.yml`. The release tag creation ruleset exempts `github-actions[bot]` so `create-tag` can run, and the
release tag integrity ruleset still blocks moving or deleting the tag.

A `workflow_dispatch` whose ref is a branch builds, checks, and rebuilds the artifacts. It does not publish.
Publishing runs when the ref is a `v*` tag, including a dispatch of that tag. If the tag exists and the dispatch
failed, run Release from the Actions tab with the ref set to that tag.

git-cliff builds `docs/changelog.md` from `feat`, `fix`, `perf`, and breaking commits that belong to a tag.
`v0.4.0` and `v0.4.1` are ignored, so those commits stay in 0.4.2. `just changelog` regenerates that released file.
`just release` gives the commits since the current tag the new version. The release commit's committer date is
12:00 UTC on the day of the cut, and that UTC day is the new section's heading date. CI and the tag workflow
regenerate the file and fail when it differs. After the packages publish, the workflow opens or updates the GitHub
Release from the latest section. A re-dispatch of the `v*` tag updates that release too.

The abi3 wheels do not include a free-threaded cp314t build. Free-threaded CPython 3.14t cannot install
them, so pip builds `rowx-postgres` from the source distribution.

## rowx-embedded

`rowx-embedded` releases in lockstep with the other packages: the same `v*` tag, the same `release.yml` workflow and
the same `pypi` environment, with the same version as `packages/rowx-embedded/pyproject.toml`, both of its `Cargo.toml`
files and its `Cargo.lock`. It ships three wheels (macOS arm64, manylinux x86_64, manylinux aarch64) and no sdist. The
owner registers a pending trusted publisher for `rowx-embedded` with workflow `release.yml` and environment `pypi`
first.

The embedded wheels block the release: `rowx` publishes only after `rowx-embedded` does, and `rowx-embedded` publishes
after `rowx-postgres` because it pins it. A manual run on a branch builds, checks (40 MB budget per wheel) and smoke
tests every wheel and does not publish. A run whose ref is a `v*` tag publishes. The old `embedded-v*` tags and
`release-embedded.yml` are gone.
