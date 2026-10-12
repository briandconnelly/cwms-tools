"""Live-CDA smoke tests (#138).

Skipped by default; opt in with `uv run pytest --run-integration -m integration`
(or `CWMS_TOOLS_RUN_INTEGRATION=1`). They hit the public CWMS Data API, so they
assert response *shapes* and invariants, never live counts or values — those
drift with upstream data. Endpoints known to be unreliable (locks: sporadic
500s and empty lists) are deliberately left out.

SWT/FOSS is the anchor: a district-published flood-control reservoir that has
carried a `Ccp-Rev` pool elevation for as long as this project has existed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import places, session, values
from cwms_tools.core.offices import REGION_HINTS
from cwms_tools.core.publishers import parse_ts_id
from cwms_tools.mcp.server import build_server

if TYPE_CHECKING:
    from cwms_tools.core.cache import Cache

pytestmark = pytest.mark.integration

OFFICE = "SWT"
NAME = "FOSS"
PARAMETER = "Elev"
#: Generous so a reporting gap upstream doesn't turn into a spurious failure.
WINDOW_HOURS = 7 * 24

runner = CliRunner()


@pytest.fixture(autouse=True)
def live_session(isolated_cache: Cache, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point cwms-python at the real CDA with a throwaway cache.

    Mocked tests re-init the process-global cwms-python session at a fake
    root, so reconfigure from defaults rather than trusting whatever ran
    last. `isolated_cache` keeps the user's real cache out of it.
    """
    for key in ("CWMS_TOOLS_API_ROOT", "_CWMS_TOOLS_NO_CACHE", "_CWMS_TOOLS_ISOLATED"):
        monkeypatch.delenv(key, raising=False)
    cfg = session.configure_session()
    assert cfg.api_root == session.DEFAULT_API_ROOT
    try:
        yield
    finally:
        # The cwms-python session stays at the real root; mocked tests re-init
        # it in their own fixtures, so clearing our cached config is enough.
        session._state["config"] = None


def _mcp_payload(result: Any) -> dict[str, Any]:
    structured = result.structured_content or {}
    return structured.get("result", structured)


# --------------------------------------------------------------------------
# core producers
# --------------------------------------------------------------------------


def test_describe_place_live_shape() -> None:
    payload = places.describe_place(OFFICE, NAME)
    assert payload["office_id"] == OFFICE
    assert payload["name"] == NAME
    loc = payload["location"]
    assert loc["name"] == NAME
    assert loc["location-kind"] == "PROJECT"
    assert isinstance(loc["latitude"], float)
    assert isinstance(loc["longitude"], float)
    assert loc.get("horizontal-datum")
    assert isinstance(payload["project"], dict)
    assert isinstance(payload["parameter_count"], int) and payload["parameter_count"] > 0
    assert payload["publishers"], "FOSS should have at least one publisher"
    assert all(parse_ts_id(ts) is not None for ts in payload["ts_ids"])


def test_list_parameters_live_shape() -> None:
    payload = places.list_parameters(OFFICE, NAME)
    assert payload["office_id"] == OFFICE
    assert payload["ts_count"] > 0
    assert payload["data_at"] is None  # data-bearing: no sibling repair needed
    assert PARAMETER in payload["all_parameters"]
    for group in payload["by_publisher"]:
        assert isinstance(group["publisher"], str) and group["publisher"]
        assert isinstance(group["rank"], int)
        assert isinstance(group["ts_count"], int) and group["ts_count"] > 0
        assert group["parameters"] == sorted(group["parameters"])


_REGION_HINT_EXAMPLES = [
    (hint["office"], example) for hint in REGION_HINTS for example in hint["examples"]
]


@pytest.mark.parametrize(("office", "example"), _REGION_HINT_EXAMPLES)
def test_region_hints_examples_are_data_bearing_live(office: str, example: str) -> None:
    """Every `REGION_HINTS` example id publishes data (#171): it is advertised as a
    catalog id an agent can hand straight to `cwms_get_value`."""
    payload = places.list_parameters(office, example)
    assert len(payload["all_parameters"]) > 0, f"{office}/{example}: no parameters"
    assert payload["ts_count"] > 0, f"{office}/{example}: no timeseries"


def test_get_value_live_shape() -> None:
    payload = values.get_value(OFFICE, NAME, PARAMETER, window=timedelta(hours=WINDOW_HOURS))
    parts = parse_ts_id(payload["ts_id"])
    assert parts is not None
    assert payload["parameter"] == PARAMETER
    assert payload["publisher"] == payload["ts_id"].rsplit(".", 1)[1]
    assert isinstance(payload["value"], float)
    assert payload["unit"] == "ft"
    assert payload["timestamp"].endswith("Z")
    assert payload["level_lookup_status"] == "skipped"


# --------------------------------------------------------------------------
# surfaces: MCP tool + CLI command, and their parity on live data
# --------------------------------------------------------------------------


async def test_mcp_get_value_live() -> None:
    server = build_server()
    result = await server.call_tool(
        "cwms_get_value",
        arguments={
            "office": OFFICE,
            "name": NAME,
            "parameter": PARAMETER,
            "window_hours": WINDOW_HOURS,
        },
    )
    payload = _mcp_payload(result)
    assert payload.get("ok", True) is not False, payload
    assert parse_ts_id(payload["ts_id"]) is not None
    assert isinstance(payload["value"], float)
    assert len(payload["source"]["fingerprint"]) == 64


async def test_list_parameters_cli_mcp_parity_live() -> None:
    """CLI `place parameters` and MCP `cwms_list_parameters` agree on live data.

    The first call populates the isolated cache, so both surfaces shape the
    same upstream snapshot. The comparison is exact: since #143 both surfaces
    serialize through the same response model, so nulls must match too.
    """
    server = build_server()
    mcp_payload = _mcp_payload(
        await server.call_tool("cwms_list_parameters", arguments={"office": OFFICE, "name": NAME})
    )
    cli = runner.invoke(app, ["place", "parameters", f"{OFFICE}/{NAME}"])
    assert cli.exit_code == 0, cli.stdout
    cli_payload = json.loads(cli.stdout)

    mcp_payload.pop("source", None)
    mcp_payload.pop("ok", None)
    assert cli_payload == mcp_payload
