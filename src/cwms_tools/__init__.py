"""cwms-tools — agent-friendly tools for the USACE CWMS Data API."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cwms-tools")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"

#: Where a defect in cwms-tools itself is reported. The same URL is declared as
#: ``[project.urls] Issues`` in ``pyproject.toml`` (and derived from there into
#: ``manifest.json``); ``tests/test_issues_url.py`` pins the copies together.
ISSUES_URL = "https://github.com/briandconnelly/cwms-tools/issues"

__all__ = ["ISSUES_URL", "__version__"]
