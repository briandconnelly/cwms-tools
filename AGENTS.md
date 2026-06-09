# AGENTS.md

Canonical instructions for both humans and AI agents working in this repo.
Per-tool files (`CLAUDE.md`, etc.) are thin pointers to this file — keep repo-wide
norms here, not duplicated elsewhere.

## What this project is

`cwms-tools` is an agent-friendly MCP server and CLI for querying the USACE CWMS
hydrologic data API.

- CWMS data orientation doc: @src/cwms_tools/data/cwms-overview.md
- Upstream Python client for CWMS data: https://github.com/HydrologicEngineeringCenter/cwms-python

## Toolchain

- **uv** manages the project and all dependencies — never pip, poetry, or conda.
  Add deps with `uv add <pkg>`; sync with `uv sync`; run with `uv run <cmd>`.
- **ruff** for formatting and linting (`uv run ruff format`, `uv run ruff check`).
- **ty** for type checking (`uv run ty check`).
- **prek** runs the pre-commit hooks; config in `prek.toml`. Run `prek run --all-files`
  before pushing.
- **FastMCP 3** for the MCP server; **Typer** for the CLI.

## Tests & coverage

- Run tests with `uv run pytest`.
- Aim for 95%+ coverage.

## Branching & commits

- Work on feature branches; never commit directly to `main`.
- Branch names use a type prefix: `feat/`, `fix/`, `chore/`, `docs/`, `ci/`, `build/`,
  `refactor/`, `test/`, `release/`.
- Commit messages follow Conventional Commits (`type(scope): summary`).
- Keep a CHANGELOG entry for every user-visible change (`CHANGELOG.md`,
  Keep-a-Changelog format under `## [Unreleased]`).

## Pull requests & review

- Open a PR into `main`; let CI run. The `CI success` check is the required gate.
- This is a single-maintainer repo (solo profile): there is no second human reviewer,
  so required PR reviews are set to 0. Merge protection is carried by the actor-independent
  gates — strict required status checks, required linear history, and blocked
  force-push/branch-deletion. Do not bypass them.
- An agent never force-pushes to or deletes `main`, and never merges a PR whose
  required checks are red.

## Releases

- Versions must agree across the git tag, `pyproject.toml`, and the top `CHANGELOG.md`
  entry — CI enforces this on tag pushes.
- When a release lands on `main`, create a matching `vX.Y.Z` tag; CI publishes to PyPI
  (OIDC trusted publishing, `pypi` environment), creates the GitHub release, and attaches
  the `.mcpb` bundle.

## Off-limits / generated

- Do not edit `uv.lock` by hand — it is managed by uv.
- Do not commit secrets; `.gitignore` covers `.env*`, `*.pem`, `*.key`.
