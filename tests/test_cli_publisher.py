"""Tests for the `cwms-tools publisher for-parameter` command.

Regression coverage for #55: the CLI must not leak the internal
`_observed_publishers_by_office` field in its default (summary) output, matching
the MCP `cwms_publishers_for_parameter` summary behavior. A `--detail full`
toggle preserves it, mirroring the MCP surface and the CLI `--detail` convention.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.cli.commands import publisher as publisher_cmd

runner = CliRunner()


_PAYLOAD = {
    "parameter": "Elev",
    "publishers": [{"publisher": "Best-MRBWM", "rank": 9, "locations_known": 3}],
    "publisher_count": 1,
    "ts_count": 3,
    "coverage": {"offices_requested": ["NWDM"], "complete": True},
    "repair": None,
    "_observed_publishers_by_office": {"NWDM": ["Best-MRBWM"]},
}


@pytest.fixture
def stub_producer(monkeypatch: pytest.MonkeyPatch):
    def _fake(parameter, *, offices=None):
        return dict(_PAYLOAD)

    monkeypatch.setattr(publisher_cmd.publishers_index, "publishers_for_parameter", _fake)


def test_for_parameter_summary_strips_internal_field(stub_producer) -> None:
    result = runner.invoke(app, ["publisher", "for-parameter", "Elev"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "_observed_publishers_by_office" not in payload
    # The rest of the contract is intact.
    assert payload["parameter"] == "Elev"
    assert payload["publisher_count"] == 1


def test_for_parameter_full_preserves_internal_field(stub_producer) -> None:
    result = runner.invoke(app, ["publisher", "for-parameter", "Elev", "--detail", "full"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["_observed_publishers_by_office"] == {"NWDM": ["Best-MRBWM"]}
