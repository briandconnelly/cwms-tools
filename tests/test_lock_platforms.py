"""``uv.lock`` must keep Intel Macs on a cryptography release that still ships
x86_64 macOS wheels, while every other platform tracks current releases.

cryptography 49+ ships only arm64 macOS wheels, and the ``.mcpb`` bundle runs
``uv run --frozen`` against this lock, so a lock that sends Intel Macs to 49+
forces a Rust source build there. These tests fail if a lock bump (e.g. from
Dependabot) collapses the per-platform fork declared in ``pyproject.toml``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK = REPO_ROOT / "uv.lock"

INTEL_MAC = "platform_machine == 'x86_64' and sys_platform == 'darwin'"
NOT_INTEL_MAC = "platform_machine != 'x86_64' or sys_platform != 'darwin'"


def _packages() -> list[dict[str, Any]]:
    return tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]


def _cryptography_edges() -> dict[str | None, str | None]:
    """Map each cryptography version cwms-tools depends on to its edge marker."""
    root = next(p for p in _packages() if p["name"] == "cwms-tools")
    return {
        dep.get("version"): dep.get("marker")
        for dep in root["dependencies"]
        if dep["name"] == "cryptography"
    }


def _only_version_for(marker: str) -> str:
    edges = _cryptography_edges()
    versions = [v for v, m in edges.items() if m == marker and v is not None]
    assert len(versions) == 1, (
        f"uv.lock has no single cryptography version for {marker!r}: {edges}; "
        "the per-platform fork in pyproject.toml has collapsed"
    )
    return versions[0]


def _major(version: str) -> int:
    return int(version.split(".", maxsplit=1)[0])


def test_intel_macs_lock_a_release_with_x86_64_wheels() -> None:
    version = _only_version_for(INTEL_MAC)
    assert _major(version) < 49, f"Intel Macs locked to cryptography {version}"

    locked = next(p for p in _packages() if p["name"] == "cryptography" and p["version"] == version)
    wheels = [w["url"].rsplit("/", 1)[-1] for w in locked.get("wheels", [])]
    assert any("macosx" in w and ("x86_64" in w or "universal2" in w) for w in wheels), (
        f"cryptography {version} has no x86_64-capable macOS wheel: {wheels}"
    )


def test_other_platforms_lock_current_cryptography() -> None:
    version = _only_version_for(NOT_INTEL_MAC)
    assert _major(version) >= 50, f"non-Intel platforms locked to cryptography {version}"
