"""The committed ``manifest.json`` must stay in lockstep with its generator and
with the live server, so a stale bundle manifest fails CI rather than shipping.

Mirrors the three places ``gen_manifest.py --check`` is wired (prek, CI, here):
this is the pytest layer of that drift guard.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "manifest.json"


def _load_gen_manifest():
    """Import ``scripts/gen_manifest.py`` (not an installed package)."""
    spec = importlib.util.spec_from_file_location(
        "gen_manifest", REPO_ROOT / "scripts" / "gen_manifest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("gen_manifest", module)
    spec.loader.exec_module(module)
    return module


gen_manifest = _load_gen_manifest()


def test_manifest_in_sync() -> None:
    """The committed file equals the generator's output (run gen_manifest.py)."""
    assert MANIFEST.exists(), "manifest.json missing; run: uv run python scripts/gen_manifest.py"
    expected = gen_manifest.render(gen_manifest.build_manifest())
    assert MANIFEST.read_text(encoding="utf-8") == expected, (
        "manifest.json is stale; regenerate with: uv run python scripts/gen_manifest.py"
    )


async def test_manifest_tools_match_server() -> None:
    """The manifest's tool names match exactly what the server registers."""
    from cwms_tools.mcp.server import build_server

    mcp = build_server()
    server_tools = {t.to_mcp_tool().name for t in await mcp.list_tools()}

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_tools = {entry["name"] for entry in manifest["tools"]}

    assert manifest_tools == server_tools


def test_manifest_version_matches_package() -> None:
    """The manifest version tracks the installed package version."""
    from cwms_tools import __version__

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == __version__


def test_manifest_required_fields() -> None:
    """Sanity-check the MCPB-required surface so a malformed manifest fails here."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("manifest_version", "name", "version", "description", "server"):
        assert manifest.get(key), f"manifest.json missing required key: {key}"
    assert manifest["server"]["mcp_config"]["command"] == "uv"
