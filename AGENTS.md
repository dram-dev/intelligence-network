# AGENTS.md

Project instructions for coding agents live in [CLAUDE.md](CLAUDE.md) — the
design rules, the data model, the schedule, and current status. Keep the two
in sync by editing CLAUDE.md only.

Quick facts: Python ≥3.12, `uv`; package `intelnet` (`src/`), CLI `intelnet`;
`uv run pytest` is hermetic (no network); `digest-core` is an editable path
dependency from `../pc-insurance-digest/packages/digest-core`.
