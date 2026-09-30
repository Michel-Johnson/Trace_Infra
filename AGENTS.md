# Repository rules

- User branch policy: all current work goes to `dev` first. Push development changes to `dev`; do not merge or enable automatic merging into `main` unless the user explicitly requests it later.
- Version policy: every new commit entering `dev` must have a unique version in both its subject and `Dev-Version` trailer; follow `docs/versioning.md`. Do not renumber existing history.
- User requirement: install only dependency versions and artifacts published at least seven days before the release being prepared. Apply this to direct, transitive and optional packages.
- Current release cutoff: `2026-09-03T00:00:00Z`. Keep npm `before`, uv `exclude-newer` and `scripts/verify_dependencies.py` consistent when deliberately advancing it.
- Use exact lock files, verify official publication dates and integrity hashes, disable npm lifecycle scripts and Python source builds. Do not bypass the age rule because an internal mirror lacks timestamps; use previously verified artifacts offline.
- See `docs/security/dependencies.md` for installation and verification commands. Never commit credentials, raw source captures, database files, build output or local environment directories.
- Platform changes belong in `apps/web`, `apps/api`, shared `src/trace_hunter`, and `contracts`. Keep imported trace payloads and digests immutable. Browsing and importing must not trigger analysis.
- The remote deployment is documented in `docs/deployment.md`. Do not reinitialize persistent databases or overwrite newer data during a code rollback.
