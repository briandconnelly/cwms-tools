# Discovery-Critical Content Fallback Tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close #65 — give resource-blind MCP clients a working tool fallback for
office-code discovery and the overview-section index, and fix the non-callable
`not_found` repair hint on `cwms_get_overview_section`.

**Architecture:** `mcp/server.py` already registers one resource-fallback tool
(`cwms_get_overview_section`) directly (not via `mcp/tools.py`'s `register_*`
helpers) because its schema is fixed to the overview document, not a CDA task.
This plan extends that same pattern: make `section_id` optional so the tool can
also serve the overview index, add a sibling `cwms_list_offices` fallback tool
that mirrors the `cwms://offices` resource payload exactly (same
`offices_payload()` core call the resource and the CLI `offices` command
already share), and fix the two `not_found` repair hints that currently emit a
non-callable placeholder arg.

**Tech Stack:** FastMCP 3, pydantic, existing `core/offices.py` /
`core/overview.py` / `mcp/resources.py` helpers. No new dependencies.

## Global Constraints

- Conventional commit messages (`fix:`, `feat:`, etc.), per AGENTS.md.
- Branch prefix `fix/` (this is a bug/gap fix), per AGENTS.md.
- CLI ↔ MCP parity is out of scope for this change: `cwms_list_offices` is a
  new *tool* mirroring an *existing* resource/CLI payload byte-for-byte (same
  `offices_payload()` call) — there is no CLI-side gap to close.
- Every registered MCP tool must be `@iserror_aware` with a
  `SomeResponse | ErrorRef` return annotation (existing invariant, pinned by
  `tests/test_mcp_tool_handlers.py::test_every_inventoried_tool_is_iserror_aware_and_wrap_flagged`).
  Follow it even though `cwms_list_offices` has no live error path today
  (`offices_payload()` never raises — it degrades to a fallback slice
  instead).
- Adding a tool changes `TOOL_INVENTORY`, which changes
  `canonical_fingerprint()`. That is expected and does not need a workaround.
- Add a `CHANGELOG.md` entry under `## [Unreleased]` → `### Fixed`, closing #65.

---

### Task 1: Make `section_id` optional on `cwms_get_overview_section`; omitted → index

**Files:**
- Modify: `src/cwms_tools/mcp/server.py` (imports, new response models, tool signature/body)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `OverviewIndexResponse` (pydantic model in `mcp/server.py`) — fields
  `document_sha256: str`, `sections: list[OverviewIndexEntry]` where
  `OverviewIndexEntry` has `section_id, title, summary, size_bytes, sha256,
  chunk_count`. Shape matches `overview_index_payload()` in `mcp/resources.py`
  exactly (already imported in `server.py`).
- Consumes: `overview_index_payload()` (already imported), `overview.section_ids()`
  (new import: `from cwms_tools.core import overview`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
def test_overview_tool_without_section_id_returns_the_index(server) -> None:
    """#65: the only fallback tool for resource-blind clients must itself be
    discoverable without first reading the `cwms://overview` resource."""

    async def go():
        return await server.call_tool("cwms_get_overview_section", arguments={})

    result = asyncio.run(go())
    sc = result.structured_content
    assert sc is not None
    branch = sc.get("result", sc)
    assert "sections" in branch
    assert "document_sha256" in branch
    section_ids = {s["section_id"] for s in branch["sections"]}
    assert section_ids == set(overview.section_ids())
    assert "body" not in branch["sections"][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py::test_overview_tool_without_section_id_returns_the_index -v`
Expected: FAIL — `section_id` is currently a required argument, so the call
raises a protocol-level validation error before reaching the handler (no
`structured_content` branch shaped like the index).

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/server.py`, add the import:

```python
from cwms_tools.core import overview
```

(place it alphabetically among the existing `from cwms_tools...` imports,
right before the `cwms_tools.core.concurrency` import).

Add two new models directly below `OverviewSectionResponse`:

```python
class OverviewIndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str
    summary: str
    size_bytes: int
    sha256: str
    chunk_count: int


class OverviewIndexResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_sha256: str
    sections: list[OverviewIndexEntry]
```

Change the tool signature and body. `section_id` becomes optional; when it is
`None`, return the index (the same payload `overview_index_payload()` builds
for the `cwms://overview` resource). `chunk_id` with no `section_id` is a
usage error (a chunk is meaningless without a section):

```python
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "idempotentHint": True,
            "title": "Get overview section",
        }
    )
    @iserror_aware
    async def cwms_get_overview_section(
        section_id: Annotated[
            str | None,
            "Stable slug from the `cwms://overview` index (e.g. 'orientation', "
            "'core-entities', 'gotchas'). Omit to get the index itself — the "
            "same content as the `cwms://overview` resource.",
        ] = None,
        detail: Annotated[
            Detail,
            "`summary` returns metadata and the chunk list; `full` returns the "
            "section body (or its first chunk when chunked). Ignored when "
            "`section_id` is omitted.",
        ] = Detail.SUMMARY,
        chunk_id: Annotated[
            str | None,
            "When set, returns just that chunk's body. Chunk ids come from the "
            "`chunks` list on a prior section read. Requires `section_id`.",
        ] = None,
    ) -> OverviewSectionResponse | OverviewIndexResponse | ErrorRef:
        """Read the bundled CWMS orientation document, or one section of it.

        Call with no arguments to get the index of section ids (the same
        content as the `cwms://overview` resource) — the primary escape
        hatch for clients that cannot browse MCP resources. Pass `section_id`
        to read one section; without `chunk_id` the response matches the
        `cwms://overview/{section_id}` resource, with `chunk_id` it returns
        that single chunk's body (with `title`/`summary` empty since they
        apply to the section, not the chunk). On a missing section/chunk it
        returns the standard `{ok: false, error: {...}}` envelope (code
        `not_found`), the same shape every task tool uses.
        """
        if section_id is None:
            if chunk_id is not None:
                return error_ref(
                    CwmsToolsError.of(
                        ErrorCode.USAGE_ERROR,
                        "chunk_id requires section_id.",
                        field="chunk_id",
                        offending_value=chunk_id,
                        hint="Pass section_id along with chunk_id, or omit both to get the index.",
                    )
                )
            return OverviewIndexResponse.model_validate(overview_index_payload())

        if chunk_id is not None:
            chunk = overview_chunk_payload(section_id, chunk_id)
            if chunk is None:
                return error_ref(
                    CwmsToolsError.of(
                        ErrorCode.NOT_FOUND,
                        f"No chunk {chunk_id!r} in section {section_id!r}.",
                        field="chunk_id",
                        offending_value=chunk_id,
                        repair=RepairHint(
                            tool="cwms_get_overview_section",
                            args={"section_id": section_id, "detail": "summary"},
                        ),
                    )
                )
            return OverviewSectionResponse(
                section_id=chunk["section_id"],
                title="",  # chunks don't carry a separate title
                summary="",
                size_bytes=chunk["byte_range"][1] - chunk["byte_range"][0],
                sha256=chunk["sha256"],
                chunks=[
                    OverviewChunkRef(
                        chunk_id=chunk["chunk_id"],
                        byte_range=chunk["byte_range"],
                        sha256=chunk["sha256"],
                        has_more=chunk["has_more"],
                    )
                ],
                body=chunk["body"],
                next_chunk_id=None,
            )

        payload = overview_section_payload(section_id, detail=detail.value)
        if payload is None:
            return error_ref(
                CwmsToolsError.of(
                    ErrorCode.NOT_FOUND,
                    f"No overview section {section_id!r}. Valid sections: "
                    f"{', '.join(overview.section_ids())}.",
                    field="section_id",
                    offending_value=section_id,
                    repair=RepairHint(tool="cwms_get_overview_section", args={}),
                )
            )
        return OverviewSectionResponse.model_validate(payload)
```

This replaces the entire existing `cwms_get_overview_section` function body
in-place (same decorators, same registration position).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k overview -v`
Expected: PASS, including the new test and all pre-existing overview tests
(`test_overview_section_tool_returns_section_for_good_slug`,
`test_overview_section_tool_returns_not_found_payload_for_bad_slug`, etc.).

- [ ] **Step 5: Update the hardcoded output-schema task-tool set**

In `tests/test_mcp_server.py::test_every_task_tool_publishes_a_real_output_schema`,
the schema is still non-hollow for a 3-way Union, but re-run the full file to
confirm nothing else broke:

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS (all tests green).

- [ ] **Step 6: Commit**

```bash
git add src/cwms_tools/mcp/server.py tests/test_mcp_server.py
git commit -m "fix: cwms_get_overview_section returns the index when section_id is omitted"
```

---

### Task 2: Enumerate real slugs / fix the non-callable resource-side repair hint

**Files:**
- Modify: `src/cwms_tools/mcp/server.py` (`_overview_section` resource handler)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `overview.section_ids()` (imported in Task 1).

Task 1 already fixed the *tool*-side `not_found` repair hint (now
`args={}`, callable, with the message enumerating real slugs). The
*resource*-side `_overview_section` handler (`cwms://overview/{section_id}`)
raises the same class of error via `_raise_resource_not_found` and has an
identical placeholder — fix it the same way for carrier parity (#64's "one
error, one shape, regardless of carrier" principle, restated in the
`_raise_resource_not_found` docstring).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`, near
`test_overview_section_resource_miss_raises_structured_jsonrpc_error`:

```python
def test_overview_section_resource_miss_repair_hint_is_callable(server) -> None:
    """#65 F2: the not_found repair hint must be a real, callable arg set —
    not a placeholder like `{"section_id": "<one of the listed slugs>"}` — and
    the message must enumerate the actual valid slugs (small, static set)."""
    from mcp import McpError

    async def go():
        return await server.read_resource("cwms://overview/does-not-exist")

    with pytest.raises(McpError) as ex:
        asyncio.run(go())
    data = ex.value.error.data
    assert data["repair"]["args"] == {}
    for sid in overview.section_ids():
        assert sid in data["human_message"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py::test_overview_section_resource_miss_repair_hint_is_callable -v`
Expected: FAIL — current `repair.args` is
`{"section_id": "<one of the listed slugs>"}`, and the message doesn't list
slugs.

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/server.py`, in `_overview_section`, change:

```python
        payload = overview_section_payload(section_id, detail=detail)
        if payload is None:
            _raise_resource_not_found(
                field="section_id",
                offending_value=section_id,
                message=f"No overview section {section_id!r}; read cwms://overview for slugs.",
                repair=RepairHint(
                    tool="cwms_get_overview_section",
                    args={"section_id": "<one of the listed slugs>"},
                ),
            )
        return payload
```

to:

```python
        payload = overview_section_payload(section_id, detail=detail)
        if payload is None:
            _raise_resource_not_found(
                field="section_id",
                offending_value=section_id,
                message=(
                    f"No overview section {section_id!r}. Valid sections: "
                    f"{', '.join(overview.section_ids())}."
                ),
                repair=RepairHint(tool="cwms_get_overview_section", args={}),
            )
        return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS (all tests green, including the pre-existing
`test_overview_section_resource_miss_raises_structured_jsonrpc_error`, which
only asserts `data["repair"]["tool"]`, not `args`, so it is unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/mcp/server.py tests/test_mcp_server.py
git commit -m "fix: overview resource not_found repair hint is now callable and lists valid slugs"
```

---

### Task 3: Add `cwms_list_offices` MCP tool

**Files:**
- Modify: `src/cwms_tools/mcp/server.py` (new response models, new tool registration)
- Modify: `src/cwms_tools/mcp/resources.py` (`TOOL_INVENTORY`, `TOOL_ERROR_CODES`, `TOOL_LATENCY`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `offices_payload()` (already imported in `server.py`), `run_sync`
  (already imported).
- Produces: `OfficesResponse` (pydantic model in `mcp/server.py`) — fields
  `count: int`, `offices: list[OfficeRecord]`, `guidance: OfficesGuidance`,
  `partial: bool`. Field-for-field identical to `offices_payload()`'s dict
  shape (same shape the `cwms://offices` resource and the CLI `offices`
  command already return).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
def test_list_offices_tool_is_registered_and_matches_resource(server) -> None:
    """#65 F2: office-code discovery needs a tool fallback for clients that
    cannot browse MCP resources — mirrors `cwms://offices` field-for-field."""

    async def go_tools():
        tools = await server.list_tools()
        return {t.name: t for t in tools}

    async def go_call():
        return await server.call_tool("cwms_list_offices", arguments={})

    tools = asyncio.run(go_tools())
    assert "cwms_list_offices" in tools
    tool = tools["cwms_list_offices"]
    assert tool.annotations.readOnlyHint is True
    assert tool.output_schema is not None

    result = asyncio.run(go_call())
    sc = result.structured_content
    assert sc is not None
    branch = sc.get("result", sc)
    resource_payload = _read_json(server, "cwms://offices")
    assert branch["count"] == resource_payload["count"]
    assert branch["guidance"]["nw_regional_rollup"] == resource_payload["guidance"]["nw_regional_rollup"]
    assert {o["name"] for o in branch["offices"]} == {
        o["name"] for o in resource_payload["offices"]
    }


def test_capabilities_advertise_list_offices_tool(server) -> None:
    payload = _read_json(server, "cwms://capabilities")
    assert "cwms_list_offices" in payload["tools"]
    assert payload["tool_error_codes"]["cwms_list_offices"] == []
    assert payload["tool_latency"]["cwms_list_offices"] == "cached"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k list_offices -v`
Expected: FAIL — `cwms_list_offices` is not a registered tool.

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/server.py`, add three models below `OverviewIndexResponse`:

```python
class OfficeRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    long_name: str | None = None
    type: str | None = None
    type_label: str | None = None
    reports_to: str | None = None


class OfficesGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nw_regional_rollup: str
    nw_district_stubs: list[str]
    nw_rollup_targets: dict[str, str]


class OfficesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    count: int
    offices: list[OfficeRecord]
    guidance: OfficesGuidance
    partial: bool
```

Register the tool right after `cwms_get_overview_section`, before the
"Task tools — registered via per-milestone helpers" comment block:

```python
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "List USACE offices",
        }
    )
    @iserror_aware
    async def cwms_list_offices() -> OfficesResponse | ErrorRef:
        """List USACE office codes for the `office` argument, with NW regional-rollup guidance.

        Fallback for clients that cannot browse MCP resources — returns the
        same content as the `cwms://offices` resource (every office's name,
        long name, type, reporting parent, plus the NW regional-rollup
        guidance: query NWDM/NWDP, not the NWO/NWK/NWS/NWP/NWW district
        stubs). Network-backed (cached 7 days); degrades to a documented
        fallback slice with `partial: true` when upstream is unreachable on
        a cold start.
        """
        payload = await run_sync(offices_payload)
        return OfficesResponse.model_validate(payload)
```

In `src/cwms_tools/mcp/resources.py`:

- Add `"cwms_list_offices"` to `TOOL_INVENTORY` (after `"cwms_publishers_for_parameter"`,
  before `"cwms_get_overview_section"` — keep it grouped with the other
  discovery-adjacent entries, order doesn't matter functionally).
- Add `"cwms_list_offices": []` to `TOOL_ERROR_CODES` (no live error path —
  `offices_payload()` never raises).
- Add `"cwms_list_offices": "cached"` to `TOOL_LATENCY` (7-day cache TTL;
  the first "cached" latency class used — the enum in the module docstring
  already names it).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS (all tests green).

- [ ] **Step 5: Run the full suite to catch cross-file invariants**

Run: `uv run pytest -q`
Expected: PASS. Two tests are *expected* to need a mechanical update because
they assert exact tool-name sets:
- `tests/test_mcp_tool_handlers.py::test_every_inventoried_tool_is_iserror_aware_and_wrap_flagged`
  — should already pass since `cwms_list_offices` is `@iserror_aware` with a
  wrap-flagged Union return; if it fails, the failure message will name the
  missing invariant directly.
- `tests/test_capability_fingerprint_snapshot.py` — no hardcoded tool *set*
  there (only `@pytest.mark.parametrize` membership checks), should pass
  unchanged.

If any test fails on a hardcoded tool-name set not anticipated here, add
`cwms_list_offices` to that set — do not weaken the assertion.

- [ ] **Step 6: Commit**

```bash
git add src/cwms_tools/mcp/server.py src/cwms_tools/mcp/resources.py tests/test_mcp_server.py
git commit -m "feat: add cwms_list_offices tool fallback for resource-blind clients"
```

---

### Task 4: Reword the `$defs/ErrorEnvelope` capability-summary nit (F14)

**Files:**
- Modify: `src/cwms_tools/mcp/resources.py`
- Test: `tests/test_mcp_server.py` (or a new assertion in an existing capabilities test)

**Interfaces:** None (pure string change in `capabilities_payload()`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
def test_capabilities_error_handling_text_does_not_promise_defs_path(server) -> None:
    """#65 F14: deployed schemas inline all definitions — there is no literal
    `$defs/ErrorEnvelope` path in an agent's actual outputSchema, so the
    capability summary must not claim one exists."""
    payload = _read_json(server, "cwms://capabilities")
    assert "$defs" not in payload["error_handling"]["tools"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py::test_capabilities_error_handling_text_does_not_promise_defs_path -v`
Expected: FAIL — current text contains `($defs/ErrorEnvelope)`.

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/resources.py`, in `capabilities_payload()`, change:

```python
                "errors) return the in-band "
                "envelope {ok: false, error: {...}} in structuredContent AND set "
                "protocol isError:true. Discriminate on the `ok` field — that is the "
                "stable, branchable contract across MCP clients; isError is an additive "
                "signal layered on top, not the discriminator. Malformed-argument "
                "errors rejected by the protocol/schema layer BEFORE a handler runs "
                "(wrong type, missing required arg, out-of-enum value) surface as plain "
                "protocol errors without this envelope. The error object's full field "
                "set is documented in each tool's outputSchema ($defs/ErrorEnvelope); "
                "key repair fields: code, message, field, offending_value, hint, "
                "repair, retryable, retry_after_ms, request_id, protocol_request_id, "
                "source."
```

to:

```python
                "errors) return the in-band "
                "envelope {ok: false, error: {...}} in structuredContent AND set "
                "protocol isError:true. Discriminate on the `ok` field — that is the "
                "stable, branchable contract across MCP clients; isError is an additive "
                "signal layered on top, not the discriminator. Malformed-argument "
                "errors rejected by the protocol/schema layer BEFORE a handler runs "
                "(wrong type, missing required arg, out-of-enum value) surface as plain "
                "protocol errors without this envelope. The error object's full field "
                "set is documented in the error branch of each tool's outputSchema; "
                "key repair fields: code, message, field, offending_value, hint, "
                "repair, retryable, retry_after_ms, request_id, protocol_request_id, "
                "source."
```

(Only the one sentence changes — "documented in each tool's outputSchema
($defs/ErrorEnvelope)" → "documented in the error branch of each tool's
outputSchema". Everything else in the string is byte-identical.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/mcp/resources.py tests/test_mcp_server.py
git commit -m "fix: capability summary no longer claims a \$defs/ErrorEnvelope schema path"
```

---

### Task 5: CHANGELOG, full verification, PR

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add a CHANGELOG entry**

Under `## [Unreleased]` → `### Fixed` (create the `### Fixed` heading under
`Unreleased` if the top entry is currently only `### Changed`; check current
file state first), add:

```markdown
- `cwms_get_overview_section` no longer requires `section_id` — omitted, it
  returns the same index as the `cwms://overview` resource, so resource-blind
  clients have a real discovery path instead of hitting a tool that requires
  a slug only the resource could teach them. Added `cwms_list_offices`, the
  matching tool fallback for `cwms://offices` office-code discovery. The
  `not_found` repair hint on missing overview sections is now an actually
  callable `{}` args set (was a non-callable placeholder string) and the
  error message enumerates the real section slugs. The capability summary no
  longer claims a `$defs/ErrorEnvelope` schema path that deployed schemas
  don't have (they inline all definitions). Closes #65.
```

- [ ] **Step 2: Run the full test suite, lint, and type check**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run ty check`
Expected: All green.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for #65 discovery fallback tools"
```

- [ ] **Step 4: Push and open the PR**

```bash
git push -u origin <branch-name>
gh pr create --title "fix: discovery-critical content fallback tools (#65)" --body "..."
```

Before opening the PR, run a `codex_review_changes` pass over the branch diff
and address (or consciously accept, with a note in the PR body) any findings
it raises.
