# GitHub Release Checklist — MVP 1.0

## Release scope and privacy

- [x] Keep the existing Git history and verify `main` against the remote before release.
- [x] Publish current source, migrations, synthetic tests/fixtures, launchers, and portable documentation.
- [x] Keep local databases, Chroma persistence, credentials, model/runtime binaries, raw host outputs, and coverage artifacts out of the release snapshot.
- [x] Replace historical machine-specific release prose with a sanitized MVP summary; keep local historical originals.
- [x] Do not rewrite the already-published initial commit. Older history can still contain material that is no longer in the release snapshot.
- [x] Do not run real model acceptance as part of this release task.

## Verification

- [x] Python import and package version check.
- [x] CLI help check.
- [x] Project configuration check.
- [x] Alembic migration-head check (offline; no migration applied to user database).
- [x] Focused offline tests.
- [x] Full offline regression test (344 run, 336 pass, 0 fail/error, 8 skipped; skips listed in machine results).
- [x] Review staged file list, staged diff, ignore/exclusion result, and secret/path scan (276 final tree files; 73 added/modified; 75 removed; zero confirmed sensitive matches).
- [ ] Confirm release commit and `v1.0.0-mvp` tag.
- [ ] Push `main` and the release tag; verify remote refs.

Execution facts and command exit codes are in `GITHUB_RELEASE_RESULTS.json`.
