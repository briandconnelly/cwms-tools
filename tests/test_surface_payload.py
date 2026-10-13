"""`core._compact.surface_payload` — the CLI's half of the shared serialization step (#143)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cwms_tools.core import models as M
from cwms_tools.core._compact import surface_payload
from cwms_tools.core.errors import CwmsToolsError, ErrorCode


def test_drops_envelope_and_non_semantic_nulls_keeps_semantic_ones() -> None:
    out = surface_payload(
        M.HistoryResponse,
        {
            "ts_id": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
            "office_id": "SWT",
            "location": "FOSS",
            "parameter": "Elev",
            "unit": "ft",
            "begin": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
            "rollup": "raw",
            "summary": None,
            "publisher": None,
            "values": [{"timestamp": "2026-01-01T00:00:00Z", "value": None, "quality": None}],
            "value_count": 1,
        },
    )
    assert "ok" not in out
    assert "source" not in out
    assert "publisher" not in out  # non-semantic null: dropped
    assert out["summary"] is None  # HistoryResponse._keep_null
    assert out["values"] == [{"timestamp": "2026-01-01T00:00:00Z", "value": None}]


def test_keeps_wire_alias_and_passthrough_nulls() -> None:
    out = surface_payload(
        M.PublishersForParameterResponse,
        {
            "parameter": "Elev",
            "publishers": [],
            "publisher_count": 0,
            "ts_count": 0,
            "coverage": {"offices_requested": ["NWDM"], "offices_indexed": [], "complete": False},
            "repair": {"next_step": "s", "tool": "x", "arguments": {"office": None}},
            "_observed_publishers_by_office": {"NWDM": []},
        },
    )
    assert out["_observed_publishers_by_office"] == {"NWDM": []}
    # `repair.arguments` is a free-form dict passthrough: nulls inside it are data.
    assert out["repair"] == {"next_step": "s", "tool": "x", "arguments": {"office": None}}


def test_model_without_source_field() -> None:
    out = surface_payload(M.SensorDepth, {"value": 3.0, "unit": "ft"})
    assert out == {"value": 3.0, "unit": "ft"}


def test_invalid_payload_raises_wrapper_bug_without_echoing_payload() -> None:
    """A payload the model rejects is a cwms-tools bug: surface it as the
    `wrapper_bug` envelope so the CLI boundary can emit it structured, and
    keep the payload's own keys and values out of the message."""
    with pytest.raises(CwmsToolsError) as info:
        surface_payload(
            M.BrowseRegionResponse, {"results": [], "bogus_secret_key": "bogus_secret_value"}
        )
    envelope = info.value.envelope
    assert envelope.code is ErrorCode.WRAPPER_BUG
    assert "BrowseRegionResponse" in envelope.message
    assert "bogus_secret_key" not in envelope.message
    assert "bogus_secret_value" not in envelope.message
    assert isinstance(info.value.__cause__, ValidationError)
