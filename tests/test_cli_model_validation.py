"""A response-model validation failure on the CLI is a structured error, not a traceback.

`surface_payload` (#143) runs every tool payload through its MCP response model.
When a producer hands the CLI something the model rejects, that is a cwms-tools
bug; it must still reach the agent as the `{ok: false, error: {...}}` envelope
on stderr (code `wrapper_bug`, exit 11) with stdout left success-only.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.cli.exit_codes import WRAPPER_BUG
from cwms_tools.core import places, values

runner = CliRunner()

_SECRET_KEY = "unexpected_secret_key_9f3a"


def _bad_browse(**_: object) -> dict:
    # `office` is required by BrowseRegionResponse; omitting it fails validation.
    return {"results": [], _SECRET_KEY: "shh"}


def test_region_browse_validation_failure_is_wrapper_bug_envelope(monkeypatch) -> None:
    monkeypatch.setattr(places, "browse_region", _bad_browse)
    result = runner.invoke(app, ["region", "browse", "--office", "SWT"])
    assert result.exit_code == WRAPPER_BUG, result.output
    assert result.stdout == ""
    envelope = json.loads(result.stderr)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "wrapper_bug"
    assert "BrowseRegionResponse" in envelope["error"]["message"]
    # The message names the model, never the payload's own keys or values.
    assert _SECRET_KEY not in result.stderr
    assert "shh" not in result.stderr


def test_value_get_batch_reports_validation_failure_inline_and_continues(monkeypatch) -> None:
    good = {
        "ts_id": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        "office_id": "SWT",
        "location": "FOSS",
        "parameter": "Elev",
        "unit": "ft",
        "publisher": "Ccp-Rev",
        "value": 1.5,
        "timestamp": "2026-01-01T00:00:00Z",
        "status_class": "unknown",
        "thresholds_active": [],
        "level_lookup_status": "skipped",
    }

    def fake_get_value(office, name, parameter, **_):
        if name == "BAD":
            return {"value": "not-a-number", _SECRET_KEY: 1}
        return dict(good)

    monkeypatch.setattr(values, "get_value", fake_get_value)
    result = runner.invoke(app, ["value", "get", "SWT/BAD/Elev", "SWT/FOSS/Elev"])
    batch = json.loads(result.stdout)
    assert [item["ok"] for item in batch["results"]] == [False, True]
    assert batch["results"][0]["error"]["code"] == "wrapper_bug"
    assert batch["results"][1]["data"]["value"] == pytest.approx(1.5)
    assert _SECRET_KEY not in result.stdout
