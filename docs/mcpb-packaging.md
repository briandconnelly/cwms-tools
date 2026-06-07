# MCPB packaging — the standard flow for uv-based Python MCP servers

This is the repeatable recipe for shipping a `uv`-managed Python MCP server as
an installable **`.mcpb` bundle** (one-click install in Claude Desktop and
other MCPB-aware hosts), alongside the existing PyPI and Claude Code plugin
channels. It is implemented in this repo and in `mcp-server-tempest`; copy it
into any new uv MCP server.

## Design principle: one source of truth, three drift guards

`manifest.json` is **generated, never hand-edited**. Its inputs:

- **Identity** (name, version, description, author, keywords, python
  requirement) — read from `pyproject.toml`.
- **Tool list** (name + one-line description) — read from the **live server**
  (`build_server()` → `list_tools()`), so it can't drift from what agents see.
- **Bundle surface** (display name, docs URL, launch command, `user_config`) —
  a small constants block in `scripts/gen_manifest.py`, the only per-server
  thing you edit.

The generated `manifest.json` **is committed**, and three layers fail loudly if
it goes stale (run `gen_manifest.py --check` everywhere):

1. **prek** — local pre-commit hook, scoped to `pyproject.toml`,
   `manifest.json`, the generator, and `src/<pkg>/mcp/**`.
2. **CI** — the release pipeline's `verify` job runs `--check` before publish.
3. **pytest** — `tests/test_manifest.py` asserts committed == generated, tools
   match the server, and version matches the package.

## The files (the reusable kit)

| File | Role |
|------|------|
| `scripts/gen_manifest.py` | Generator. Reads `pyproject.toml` + live server; emits deterministic `manifest.json`. `--check` exits 1 if stale. |
| `manifest.json` | **Generated, committed.** What the bundle is packed from. |
| `.mcpbignore` | Keeps the archive minimal & secret-free. Ships only `manifest.json`, `pyproject.toml`, `uv.lock`, `src/`, `README`, `LICENSE`, `.python-version`. |
| `tests/test_manifest.py` | The pytest drift guard. |
| `Makefile` | `make manifest` regenerates; `make mcpb` validates + packs to `dist/`. |
| `.github/workflows/mcpb.yml` | Reusable (`workflow_call` + `workflow_dispatch`) bundle job. |
| `ci.yml` `bundle` job | Calls `mcpb.yml` after the GitHub release exists. |

## The launch mechanism

The manifest declares `server.type: "uv"` and runs the server reproducibly from
the bundle's own lockfile:

```
uv run --directory ${__dirname} --frozen --no-dev <stdio entry point>
```

- `--frozen` → resolve exactly from the bundled `uv.lock` (reproducible).
- `--no-dev` → runtime deps only.
- **The end user needs `uv` on PATH.** This is the pragmatic middle ground: a
  tiny, reproducible bundle that does *not* vendor a Python runtime. If you need
  a truly zero-dependency install for non-technical users, that's a different
  (heavier) bundling strategy — revisit per server.

The `<stdio entry point>` is per-server:
- **tempest**: `python -m mcp_server_tempest`
- **cwms-tools**: `cwms-tools mcp serve --transport stdio` — reuses the existing
  console subcommand, which already installs the stdout guard required for a
  correct stdio MCP server (stdout reserved for JSON-RPC).

## `user_config` — the honest-config rule

`user_config` keys map to environment variables the server reads. **Gotcha:**
most hosts substitute an *empty string* for an optional field the user leaves
blank — they don't omit the env var. So:

> Only expose a knob if the server behaves correctly when it receives that
> field's `default` (or an empty string). Give optional knobs a sane `default`,
> or make sure the server treats empty as unset.

cwms-tools exposes `api_root` (optional, with the real default so blank is
always valid) and `operator_email` (optional contact). It deliberately does
*not* surface internal knobs (`CWMS_TOOLS_WORKERS`, cache dir) that would
misbehave if injected empty. The CWMS Data API is public, so there is **no API
token** — contrast tempest, whose `user_config.api_token` is `required: true`
and `sensitive: true`.

## Release wiring

`ci.yml` runs `verify → build → publish-pypi → release → bundle` on a `v*` tag.
The `bundle` job is `uses: ./.github/workflows/mcpb.yml` with
`tag: ${{ github.ref_name }}`. It is a **direct call**, not a `release:
published` trigger, because a release created with `GITHUB_TOKEN` does not
trigger other workflows.

`mcpb.yml` also runs `gen_manifest.py --check`, validates the manifest, packs
the bundle, and runs a **forbidden-paths assertion** (`unzip -Z1 | grep -E …`)
— belt-and-suspenders proof that `.env`, `.git/`, `tests/`, etc. never landed
in the archive even if `.mcpbignore` were wrong.

## Adapting this to a new uv MCP server

1. Copy `scripts/gen_manifest.py`, `.mcpbignore`, `tests/test_manifest.py`,
   `Makefile`, `.github/workflows/mcpb.yml`.
2. In `gen_manifest.py`, edit the constants block: `DISPLAY_NAME`,
   `DOCUMENTATION`, `LAUNCH_ARGS` (your stdio entry point), `USER_CONFIG`, and
   `ENV_FROM_CONFIG`. Point `_tools()` at your server's `build_server()`.
3. In `.mcpbignore` and the `mcpb.yml` forbidden-paths regex, swap any
   repo-specific paths (e.g. `cwms-overview.md`).
4. Add the `gen-manifest` prek hook and the `verify` manifest-check + `bundle`
   job to your release workflow.
5. Add an `icon.png` (optional but improves the host install UI — tempest has
   one; cwms-tools does not yet).
6. Run `make mcpb` locally and confirm the forbidden-paths assertion passes.
