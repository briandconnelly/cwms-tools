"""CLI ↔ MCP parity tests (#56).

The CLI and MCP surfaces wrap the *same* `core/` producers and share the
`summary`/`full` `detail` toggle via `core.shaping`. These tests are the guard
that the two surfaces cannot silently diverge again (the #45 / #55 class): for
each tool, the same mocked producer payload is driven through both surfaces and
the emitted data must agree field-for-field, in both detail modes.

Method: each producer is monkeypatched to return a model-complete payload built
by round-tripping a minimal input through the MCP response model's own
`model_dump` — which null-strips non-semantic Nones and keeps the semantic
(`_keep_null`) ones. That payload is therefore exactly what MCP re-emits as data,
and the CLI emits it verbatim, so the two data dicts compare for *exact*
equality (after only peeling the MCP `ok`/`source` envelope and the CLI
`value get` batch wrapper — the deliberate per-surface envelopes). No null
normalization, so the guard cannot mask a field-presence divergence.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from typing import Any

import cwms
import pytest
from typer.testing import CliRunner

from cwms_tools.cli.app import app
from cwms_tools.core import models as M
from cwms_tools.core import places, publishers_index, session, values
from cwms_tools.core.cache import Cache, set_cache
from cwms_tools.core.models import SourceMeta
from cwms_tools.mcp.server import build_server

API_ROOT = "https://example.test/cwms-data/"
runner = CliRunner()


@pytest.fixture
def configured(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CWMS_TOOLS_API_ROOT", API_ROOT)
    session._state["config"] = None
    cwms.init_session(api_root=API_ROOT, pool_connections=4)
    session.configure_session()
    cache = Cache(directory=tmp_path / "cache")
    set_cache(cache)
    yield
    cache.close()
    set_cache(None)
    session._state["config"] = None


_DUMMY_SOURCE = SourceMeta(fingerprint="0" * 64)


def _producer_payload(model_cls: type, **fields: Any) -> dict[str, Any]:
    """A model-complete, null-stripped producer payload (no `ok`/`source`).

    Round-tripping through the response model realizes every default so the MCP
    re-validation adds nothing the CLI raw-dict would lack.
    """
    dumped = model_cls(source=_DUMMY_SOURCE, **fields).model_dump(mode="json")
    dumped.pop("ok", None)
    dumped.pop("source", None)
    return dumped


def _branch(structured: dict | None) -> dict:
    if structured is None:
        return {}
    return structured.get("result", structured)


def _mcp_data(name: str, args: dict[str, Any]) -> dict[str, Any]:
    server = build_server()
    result = asyncio.run(server.call_tool(name, arguments=args))
    payload = _branch(result.structured_content)
    payload.pop("ok", None)
    payload.pop("source", None)
    return payload


# --- Per-tool parity cases -------------------------------------------------
#
# Each case carries: the response model + fields used to build the shared
# producer payload, the producer to monkeypatch, the CLI argv and MCP tool/args,
# an extractor for the CLI data dict (identity, except the batch-wrapped
# `value get`), and `has_internal` — does this payload still carry the field the
# summary shaper strips? (the direct #55-class assertion).

SEARCH = {
    "model": M.SearchPlacesResponse,
    "fields": {"query": "FOSS", "results": [{"office_id": "SWT", "name": "FOSS", "raw": {"b": 1}}]},
    "producer": (places, "search_places"),
    "cli": ["place", "search", "FOSS", "--office", "SWT"],
    "mcp": ("cwms_search_places", {"query": "FOSS", "office": "SWT"}),
    "cli_extract": lambda d: d,
    "has_internal": lambda d: "raw" in d["results"][0],
}
DESCRIBE = {
    "model": M.DescribePlaceResponse,
    "fields": {
        "office_id": "SWT",
        "name": "FOSS",
        "location": {"office-id": "SWT", "name": "FOSS", "description": "verbose"},
        "partial": False,
        "partial_reasons": [],
        "parameters": ["Elev"],
        "parameter_count": 1,
        "publishers": [{"publisher": "Ccp-Rev", "rank": 9, "ts_count": 1, "parameters": ["Elev"]}],
        "ts_ids": ["FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"],
    },
    "producer": (places, "describe_place"),
    "cli": ["place", "describe", "SWT/FOSS"],
    "mcp": ("cwms_describe_place", {"office": "SWT", "name": "FOSS"}),
    "cli_extract": lambda d: d,
    "has_internal": lambda d: "description" in d["location"],
}
VALUE = {
    "model": M.ValueWithContextResponse,
    "fields": {
        "ts_id": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        "office_id": "SWT",
        "location": "FOSS",
        "parameter": "Elev",
        "unit": "ft",
        "value": 1.0,
        "timestamp": "2026-01-01T00:00:00Z",
        "status_class": "nominal",
        "thresholds_active": [
            {
                "specified_level_id": "Top",
                "value": 1.0,
                "unit": "ft",
                "relation": "above",
                "level_id": "LID",
                "source_workaround": "wa",
            }
        ],
    },
    "producer": (values, "get_value"),
    "cli": ["value", "get", "SWT/FOSS/Elev"],
    "mcp": ("cwms_get_value", {"office": "SWT", "name": "FOSS", "parameter": "Elev"}),
    "cli_extract": lambda d: d["results"][0]["data"],
    "has_internal": lambda d: "level_id" in d["thresholds_active"][0],
}
HISTORY = {
    "model": M.HistoryResponse,
    "fields": {
        "ts_id": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        "office_id": "SWT",
        "location": "FOSS",
        "parameter": "Elev",
        "unit": "ft",
        "begin": "2026-01-01T00:00:00Z",
        "end": "2026-01-02T00:00:00Z",
        "rollup": "raw",
        "summary": None,
        "values": [{"timestamp": "2026-01-01T00:00:00Z", "value": 1.0, "quality": 3}],
        "value_count": 1,
    },
    "producer": (values, "get_history"),
    "cli": [
        "value",
        "history",
        "SWT/FOSS/Elev",
        "--begin",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-02T00:00:00Z",
    ],
    "mcp": (
        "cwms_get_history",
        {
            "office": "SWT",
            "name": "FOSS",
            "parameter": "Elev",
            "begin_iso": "2026-01-01T00:00:00Z",
            "end_iso": "2026-01-02T00:00:00Z",
        },
    ),
    "cli_extract": lambda d: d,
    "has_internal": lambda d: "quality" in d["values"][0],
}
PROFILE = {
    "model": M.ProfileResponse,
    "fields": {
        "office_id": "NWDP",
        "name": "GWLW_S1",
        "parameter": "Temp-Water",
        "unit": "degC",
        "sensor_count": 1,
        "profile": [
            {
                "name": "S1",
                "depth": {"value": 3.0, "unit": "ft"},
                "value": 20.0,
                "timestamp": "2026-01-01T00:00:00Z",
                "ts_id": "TSID",
            }
        ],
    },
    "producer": (values, "get_profile"),
    "cli": ["value", "profile", "NWDP/GWLW_S1/Temp-Water"],
    "mcp": ("cwms_get_profile", {"office": "NWDP", "name": "GWLW_S1", "parameter": "Temp-Water"}),
    "cli_extract": lambda d: d,
    "has_internal": lambda d: "ts_id" in d["profile"][0],
}
PUBLISHERS = {
    "model": M.PublishersForParameterResponse,
    "fields": {
        "parameter": "Elev",
        "publishers": [{"publisher": "Best-MRBWM", "rank": 9, "locations_known": 3}],
        "publisher_count": 1,
        "ts_count": 3,
        "coverage": {
            "offices_requested": ["NWDM"],
            "offices_indexed": ["NWDM"],
            "complete": True,
        },
        "_observed_publishers_by_office": {"NWDM": ["Best-MRBWM"]},
    },
    "producer": (publishers_index, "publishers_for_parameter"),
    "cli": ["publisher", "for-parameter", "Elev", "--office", "NWDM"],
    "mcp": ("cwms_publishers_for_parameter", {"parameter": "Elev", "offices": ["NWDM"]}),
    "cli_extract": lambda d: d,
    "has_internal": lambda d: "_observed_publishers_by_office" in d,
}

CASES = [SEARCH, DESCRIBE, VALUE, HISTORY, PROFILE, PUBLISHERS]
CASE_IDS = ["search", "describe", "value", "history", "profile", "publishers"]


def _cli_data(case: dict, argv: list[str]) -> dict[str, Any]:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    return case["cli_extract"](json.loads(result.stdout))


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
@pytest.mark.parametrize("detail", ["summary", "full"])
def test_cli_mcp_parity(case: dict, detail: str, configured, monkeypatch) -> None:
    raw = _producer_payload(case["model"], **case["fields"])
    module, attr = case["producer"]
    monkeypatch.setattr(module, attr, lambda *a, **k: copy.deepcopy(raw))

    cli_argv = [*case["cli"], "--detail", detail]
    mcp_name, mcp_args = case["mcp"]

    cli_data = _cli_data(case, cli_argv)
    mcp_data = _mcp_data(mcp_name, {**mcp_args, "detail": detail})

    # 1. Exact field-for-field parity between the two surfaces — including
    #    semantic-null (`_keep_null`) fields. No normalization, so the guard
    #    cannot mask a divergence where one surface omits a field the other
    #    emits (e.g. `history.summary: null`). This holds because the producer
    #    payload is built via the response model's own `model_dump` (which
    #    null-strips non-semantic Nones and keeps semantic ones), so it equals
    #    exactly what MCP re-emits, and the CLI emits it verbatim.
    assert cli_data == mcp_data

    # 2. The summary shaper actually fired on BOTH surfaces (the #55-class guard):
    #    the internal field is present only in `full`, on each surface independently.
    want_internal: bool = detail == "full"
    check: Callable[[dict], bool] = case["has_internal"]
    assert check(cli_data) is want_internal
    assert check(mcp_data) is want_internal
