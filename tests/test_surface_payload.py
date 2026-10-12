"""`core._compact.surface_payload` — the CLI's half of the shared serialization step (#143)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cwms_tools.core import models as M
from cwms_tools.core._compact import surface_payload


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
            "repair": {"tool": "x", "args": {"office": None}},
            "_observed_publishers_by_office": {"NWDM": []},
        },
    )
    assert out["_observed_publishers_by_office"] == {"NWDM": []}
    # `repair` is a free-form dict passthrough: nulls inside it are data.
    assert out["repair"] == {"tool": "x", "args": {"office": None}}


def test_model_without_source_field() -> None:
    out = surface_payload(M.SensorDepth, {"value": 3.0, "unit": "ft"})
    assert out == {"value": 3.0, "unit": "ft"}


def test_invalid_payload_raises_like_mcp() -> None:
    with pytest.raises(ValidationError):
        surface_payload(M.BrowseRegionResponse, {"office": "SWT", "results": [], "bogus": 1})
