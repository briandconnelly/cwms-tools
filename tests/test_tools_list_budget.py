"""CI budget guard over the serialized `tools/list` payload (#67).

Every preloading MCP client pays the full serialized size of `tools/list`
before its first useful call. #67 found this at 65,439 chars (~16.4k tokens)
across 9 tools — output schemas ~74% of it, with the `ErrorEnvelope` schema
inlined in full on every tool and long param prose repeated verbatim (the
office-discovery sentence alone 7x).

The fix has two parts: `mcp.output_schema.iserror_output_schema()` replaces
the auto-derived, per-tool-duplicated `ErrorEnvelope` schema with one shared
compact shape (`COMPACT_ERROR_SCHEMA`), and tool docstrings / param prose /
response-field descriptions were tightened throughout `mcp/tools.py`,
`mcp/server.py`, and `core/models.py`.

By the time this landed the tool count had grown 9 -> 10 (two more
discovery-fallback tools), so the original "<=10k tokens" target — set
against 9 tools — no longer has the same headroom: hitting it today would
mean cutting real repair/selection content the issue explicitly said to
keep (including the finite `ErrorCode` enum and the `RepairHint`/`SourceInfo`
nested field shapes resource-blind clients need inline — restored after #67
review feedback flagged their removal/loosening). The achieved number
instead: ~54.1k chars (~13.5k tokens), down from ~81.4k chars (~20.4k
tokens) for the current 10-tool surface — a 34% reduction. `BUDGET_CHARS`
below locks that in with headroom for organic growth;
`test_tools_list_component_budget` reports subtotals so a future
regression is easy to localize to input schemas, output schemas, or
docstrings.

#76 later grew the error envelope's own wire shape (`details`, richer
`repair`, `rate_limit_remaining`), moving the measured size to ~58.4k
chars — see `BUDGET_CHARS` below for that adjustment.
"""

from __future__ import annotations

import json

from cwms_tools.mcp.server import build_server

#: #76 grew the error envelope wire shape (`details`, `repair.next_step`/
#: `repair.alternative`, `rate_limit_remaining`) inlined once per tool via
#: `COMPACT_ERROR_SCHEMA`, moving the measured size from ~54.1k to ~58.4k
#: chars. Budget bumped to keep headroom for organic growth before the
#: guard fires, while still catching a regression back toward the pre-#67
#: ~81.4k.
BUDGET_CHARS = 62_000


def _serialize_tool(tool) -> str:
    return json.dumps(tool.to_mcp_tool().model_dump(mode="json", exclude_none=True))


async def _tools():
    mcp = build_server()
    return await mcp.list_tools()


async def test_tools_list_total_size_under_budget() -> None:
    tools = await _tools()
    sizes = {t.name: len(_serialize_tool(t)) for t in tools}
    total = sum(sizes.values())
    assert total <= BUDGET_CHARS, (
        f"tools/list is {total} chars (budget {BUDGET_CHARS}); "
        f"per-tool sizes: {sorted(sizes.items(), key=lambda kv: -kv[1])}"
    )


async def test_tools_list_component_budget() -> None:
    """Enforce looser per-component ceilings, and print the breakdown.

    These ceilings are individually looser than the combined total the
    test above enforces, but still real, failing gates — the printed
    breakdown is what makes a future regression's culprit surface visible
    in CI output immediately, instead of requiring a follow-up bisect.
    """
    tools = await _tools()
    input_total = output_total = desc_total = 0
    for tool in tools:
        dumped = tool.to_mcp_tool().model_dump(mode="json", exclude_none=True)
        input_total += len(json.dumps(dumped.get("inputSchema", {})))
        output_total += len(json.dumps(dumped.get("outputSchema", {})))
        desc_total += len(dumped.get("description") or "")
    print(
        f"tools/list breakdown: input={input_total} output={output_total} description={desc_total}"
    )
    # Loose per-component ceilings so a single runaway tool's schema or
    # docstring is caught even while the total stays under BUDGET_CHARS.
    assert input_total <= 15_000
    assert output_total <= 45_000
    assert desc_total <= 5_000
