# Trace Hunter code-only snapshot

This GitHub `dev` branch contains a source snapshot for project discussion.
It was taken from the committed enterprise `dev` revision
`c509ab657b016428cd385e1df0439051fa66381e`; it does not carry that
repository's Git history or uncommitted local changes.

Included: the frontend, API, shared library, contracts, database migrations,
deployment templates, plugins, scripts, skills, tests, dependency lock files,
and technical documentation.

Excluded: imported traces, example traces, evaluation corpora, test fixtures,
research and benchmark results, operational deployment notes, secrets,
databases, build output, and local configuration. Tests that depend on
excluded fixtures are not expected to run from this snapshot alone.

The GitHub `main` branch remains the separate existing Trace Infra project.
