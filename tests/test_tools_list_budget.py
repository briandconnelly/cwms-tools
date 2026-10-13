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

#142: the per-component ceilings were never enforced until then. Under
FastMCP 4, `model_dump()` without `by_alias=True` emits snake_case keys
(`input_schema`/`output_schema`), so the camelCase lookups found nothing and
every schema counted as `{}`. Both tests now dump with `by_alias=True` (the
camelCase keys a client actually receives on the wire), and the component
test asserts each tool has non-empty schemas so a key mismatch fails loudly.
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
#:
#: 0.9.0 (milestone 2, 2026-10-12) deliberately grew the agent-facing prose:
#: #163 repair guidance on `office`/`name`, #170 `data_at`, #162 the
#: `StatusClass`/`zone` vocabulary. Measured 63,258 chars with #162 and #163
#: landed; bumped again to keep ~4% headroom. Still well under the ~81.4k
#: regression line.
#:
#: #187 (2026-10-13) added `cwms_search_places`'s structured `scope_note`,
#: which no prose trim could absorb (even a bare string field measured
#: 66,160). Measured 66,339 with it; this 67,000 is borrowed headroom until
#: #181 dedupes the per-tool error schema (~16.5k chars), after which the
#: budget should come back down.
#:
#: #188 (2026-10-13) added `cwms_search_places`'s `state` input/echo and the
#: `coverage` block on `cwms_list_offices`' output (structure, not prose: the
#: field descriptions are already one line). Measured 67,691; borrowed again
#: against #181.
#:
#: #177 (2026-10-13) typed the success-side repairs as the shared `RepairHint`
#: (search's `repair_hint`, publishers' `repair`, formerly an untyped object)
#: and gave it an optional `message`, which the error branch of every tool
#: also lists. Field descriptions moved to code comments to limit the growth.
#: Measured 68,775; borrowed against #181.
BUDGET_CHARS = 69_500


#: Per-component ceilings, set deliberately in #142 (2026-10-11) when these
#: were first actually measured: input=9,034, output=45,298, description=2,937
#: chars. Each leaves ~6-36% headroom; output schemas dominate and sit closest.
#: Output measured 48,003 after #162's `zone` field and #170's `data_at`
#: landed (2026-10-12); ceiling raised to keep ~6% headroom.
INPUT_CEILING = 11_000
#: #188's `coverage` block and `state` echo measured 51,442 (2026-10-13).
#: #177's typed repairs measured 52,521 (2026-10-13).
OUTPUT_CEILING = 53_000
DESCRIPTION_CEILING = 4_000

#: Floors: a subtotal below this means the schema lookup is broken (#142),
#: not that the surface shrank.
SCHEMA_FLOOR = 1_000


def _dump_tool(tool) -> dict:
    # by_alias=True yields the wire's camelCase keys (`inputSchema`,
    # `outputSchema`); without it FastMCP 4 emits snake_case (#142).
    return tool.to_mcp_tool().model_dump(mode="json", exclude_none=True, by_alias=True)


def _serialize_tool(tool) -> str:
    return json.dumps(_dump_tool(tool))


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
        dumped = _dump_tool(tool)
        input_schema = dumped.get("inputSchema")
        output_schema = dumped.get("outputSchema")
        assert input_schema, f"{tool.name}: no inputSchema in dump keys {sorted(dumped)}"
        assert output_schema, f"{tool.name}: no outputSchema in dump keys {sorted(dumped)}"
        input_total += len(json.dumps(input_schema))
        output_total += len(json.dumps(output_schema))
        desc_total += len(dumped.get("description") or "")
    print(
        f"tools/list breakdown: input={input_total} output={output_total} description={desc_total}"
    )
    # Loose per-component ceilings so a single runaway tool's schema or
    # docstring is caught even while the total stays under BUDGET_CHARS.
    assert input_total > SCHEMA_FLOOR
    assert output_total > SCHEMA_FLOOR
    assert input_total <= INPUT_CEILING
    assert output_total <= OUTPUT_CEILING
    assert desc_total <= DESCRIPTION_CEILING
