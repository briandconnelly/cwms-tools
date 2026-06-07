#!/usr/bin/env python
"""Generate ``manifest.json`` (the MCPB bundle manifest) from a single source
of truth.

Identity (name/version/description/author/keywords/python-requirement) comes
from ``pyproject.toml``; the tool list is derived from the *live* FastMCP
server so the manifest cannot drift from what the server actually exposes.
Only the bundle-specific surface (display name, docs URL, launch command, and
the ``user_config`` knobs) lives here as constants.

``tests/test_manifest.py`` asserts the committed ``manifest.json`` matches this
generator's output, so regenerate and commit whenever the version or tool
surface changes.

Usage:
    uv run python scripts/gen_manifest.py            # write manifest.json
    uv run python scripts/gen_manifest.py --check     # exit 1 if stale
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
MANIFEST = REPO_ROOT / "manifest.json"

# --- Bundle-specific surface (the only per-server knobs to edit) ------------

DISPLAY_NAME = "CWMS Tools"
DOCUMENTATION = "https://github.com/briandconnelly/cwms-tools#readme"

# How the installed bundle launches the server. The CWMS Data API's read
# endpoints are public, so there is no API token; we launch the same guarded
# stdio entry point the CLI exposes (`cwms-tools mcp serve`), reproducibly from
# the bundle's own uv.lock. The end user only needs `uv` on PATH.
LAUNCH_ARGS = [
    "run",
    "--directory",
    "${__dirname}",
    "--frozen",
    "--no-dev",
    "cwms-tools",
    "mcp",
    "serve",
    "--transport",
    "stdio",
]

# Operator-facing configuration surfaced in the host's install UI. Each key maps
# to a CWMS_TOOLS_* env var below. Keep this honest: only expose knobs that are
# safe when left at their default (see api_root's default).
USER_CONFIG = {
    "api_root": {
        "type": "string",
        "title": "CWMS Data API root URL",
        "description": "Base URL for the USACE CWMS Data API. Override only to "
        "target a mirror or test endpoint.",
        "default": "https://cwms-data.usace.army.mil/cwms-data/",
        "required": False,
    },
    "operator_email": {
        "type": "string",
        "title": "Operator contact email",
        "description": "Optional contact address advertised in the API "
        "User-Agent so the data provider can reach you. Leave blank to omit.",
        "required": False,
    },
}

# user_config key -> env var the server reads.
ENV_FROM_CONFIG = {
    "CWMS_TOOLS_API_ROOT": "${user_config.api_root}",
    "CWMS_TOOLS_OPERATOR_EMAIL": "${user_config.operator_email}",
}

# ---------------------------------------------------------------------------


def _tools() -> list[dict[str, str]]:
    """Manifest ``tools`` entries, derived from the live server so the
    name/description pairs stay in lockstep with what agents actually see."""
    from cwms_tools.mcp.server import build_server  # noqa: PLC0415

    async def _extract() -> list[dict[str, str]]:
        mcp = build_server()
        out: list[dict[str, str]] = []
        for tool in await mcp.list_tools():
            mcp_tool = tool.to_mcp_tool()
            out.append(
                {
                    "name": mcp_tool.name,
                    "description": (mcp_tool.description or "").strip().split("\n", 1)[0],
                }
            )
        return out

    return asyncio.run(_extract())


def build_manifest() -> dict:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    author = project["authors"][0]
    urls = project.get("urls", {})
    repo_url = urls.get("Repository") or urls.get("Homepage", "")

    return {
        "manifest_version": "0.4",
        "name": project["name"],
        "display_name": DISPLAY_NAME,
        "version": project["version"],
        "description": project["description"],
        "author": {"name": author["name"], "email": author["email"]},
        "repository": {"type": "git", "url": repo_url},
        "homepage": urls.get("Homepage", repo_url),
        "documentation": DOCUMENTATION,
        "support": urls.get("Issues", f"{repo_url}/issues"),
        "license": project.get("license", "MIT"),
        "keywords": project.get("keywords", []),
        "server": {
            "type": "uv",
            "entry_point": "src/cwms_tools/cli/commands/mcp.py",
            "mcp_config": {
                "command": "uv",
                "args": LAUNCH_ARGS,
                "env": dict(ENV_FROM_CONFIG),
            },
        },
        "compatibility": {
            "platforms": ["darwin", "linux", "win32"],
            "runtimes": {"python": project["requires-python"]},
        },
        "user_config": USER_CONFIG,
        "tools": _tools(),
    }


def render(manifest: dict) -> str:
    """Deterministic JSON rendering used for both writing and drift comparison."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if manifest.json is missing or stale (do not write).",
    )
    args = parser.parse_args()

    rendered = render(build_manifest())

    if args.check:
        current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
        if current != rendered:
            print("manifest.json is out of date; run: uv run python scripts/gen_manifest.py")
            return 1
        print("manifest.json is up to date.")
        return 0

    MANIFEST.write_text(rendered, encoding="utf-8")
    print(f"Wrote {MANIFEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
