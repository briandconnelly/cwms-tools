# Agent-Friendliness Remediation (v0.3.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the eight agreed agent-friendliness findings (M-1, M-2, M-3, C-1, m-4..m-7) from the MCP/CLI review, shipped as one v0.3.0 release.

**Architecture:** Add opaque cursor pagination to the list tools and window-continuation to history (M-1); fold the CLI schema into the single capability fingerprint so `scope: "schema-contract"` is honest (M-2); structure the CLI `schema` output with real arg/flag/error/latency records (C-1); add honest tool annotations (m-4), resource-template completions (m-5), structured latency metadata (m-6), and an explicit `ok: true` success discriminator (m-7). M-3 is addressed by the explicit discriminator plus a tracked FastMCP limitation note.

**Tech Stack:** Python 3.10+, FastMCP 3.3.1, Typer, Pydantic v2, pytest, `uv`, `ruff`, `ty`.

**Key design decisions (confirmed with Codex):**
- **Pagination = opaque cursor**, not offset — results are sorted data-bearing-first, so an inserted row shifts every page. The cursor locks the searched office set and carries a request hash + the full `total_count`; any mismatch raises `invalid_cursor` with a restart-without-cursor repair hint.
- **History = window-continuation** via `next_begin = last-point-timestamp + 1ms` (CWMS points are millisecond-based, so +1ms avoids a duplicate/skipped seam point).
- **Fingerprint = fold CLI in.** `core/fingerprint.compute()` gains a `cli_contract` arg injected from `mcp/contract.canonical_fingerprint()` via lazy import, preserving `core/`'s no-upward-imports purity.
- **Release = one branch, one v0.3.0.** Commits 1-6 below are each independently testable; pagination is internal until the fingerprint scope fix (commit 6) lands.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/cwms_tools/core/pagination.py` | Cursor encode/decode/validate; request hashing | **Create** |
| `src/cwms_tools/core/errors.py` | Add `INVALID_CURSOR` code + exit mapping | Modify |
| `src/cwms_tools/core/models.py` | Cursor/`next_begin`/`ok:true` fields on response models | Modify |
| `src/cwms_tools/core/places.py` | Cursor pagination in `search_places` / `browse_region` | Modify |
| `src/cwms_tools/core/timeseries.py` | `next_begin` on `fetch_window` | Modify |
| `src/cwms_tools/core/values.py` | Pass `next_begin` through `get_history` | Modify |
| `src/cwms_tools/mcp/tools.py` | `cursor` param + annotations on tools; pass-through fields | Modify |
| `src/cwms_tools/mcp/server.py` | Annotations on overview tool (m-5 completions deferred — no change) | Modify |
| `src/cwms_tools/mcp/resources.py` | `invalid_cursor` in error catalog; latency metadata + completions-fallback note in capabilities | Modify |
| `src/cwms_tools/mcp/contract.py` | Inject CLI contract into the fingerprint | Modify |
| `src/cwms_tools/core/fingerprint.py` | Accept + hash `cli_contract` | Modify |
| `src/cwms_tools/cli/commands/place.py` | `--cursor` flag on `search` | Modify |
| `src/cwms_tools/cli/commands/region.py` | `--cursor` flag on `browse` | Modify |
| `src/cwms_tools/cli/commands/value.py` | Surface `next_begin` (no shape change beyond core) | Modify (verify only) |
| `src/cwms_tools/cli/commands/schema.py` | Structured args/flags/error-catalog/latency; `cli_contract_payload()` | Modify |
| `src/cwms_tools/cli/exit_codes.py` | `INVALID_CURSOR` exit constant | Modify |
| `tests/test_pagination.py` | Cursor unit tests | **Create** |
| `tests/test_places.py`, `test_values.py`, `test_mcp_*`, `test_cli_*`, `test_capability_fingerprint_snapshot.py`, `test_errors.py` | Behavior + snapshot updates | Modify |
| `CHANGELOG.md`, `pyproject.toml` | v0.3.0 entry + version bump | Modify |

---

## Task 0: Feature branch

**Files:** none (git only)

- [ ] **Step 1: Create the branch**

```bash
cd /Users/bdc/projects/cwms-tools
git checkout main && git pull --ff-only
git checkout -b feat/agent-friendliness-v0.3.0
```

- [ ] **Step 2: Confirm a clean baseline**

Run: `uv run pytest -q && uv run ty check`
Expected: all tests pass, ty reports no errors. (If the pre-existing tree has unrelated failures, stop and report — do not build on a red baseline.)

---

## Commit 1 — Shared models & error code

### Task 1: Add the `invalid_cursor` error code

**Files:**
- Modify: `src/cwms_tools/core/errors.py:20-53`
- Modify: `src/cwms_tools/cli/exit_codes.py:12-23,30-43`
- Test: `tests/test_errors.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_errors.py`:

```python
from cwms_tools.core.errors import ErrorCode, exit_code_for


def test_invalid_cursor_code_maps_to_usage_exit():
    assert ErrorCode.INVALID_CURSOR.value == "invalid_cursor"
    assert exit_code_for(ErrorCode.INVALID_CURSOR) == 2
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_errors.py::test_invalid_cursor_code_maps_to_usage_exit -v`
Expected: FAIL — `AttributeError: INVALID_CURSOR`.

- [ ] **Step 3: Add the enum member and exit mapping**

In `src/cwms_tools/core/errors.py`, add to `ErrorCode` (after `USAGE_ERROR`):

```python
    INVALID_CURSOR = "invalid_cursor"
```

In the same file, add to `_EXIT_CODE_MAP`:

```python
    ErrorCode.INVALID_CURSOR: 2,
```

- [ ] **Step 4: Mirror the CLI exit constant**

In `src/cwms_tools/cli/exit_codes.py`, add the constant (near `USAGE_ERROR = 2`):

```python
INVALID_CURSOR = 2
```

and add `"INVALID_CURSOR",` to `__all__`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_errors.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/cwms_tools/core/errors.py src/cwms_tools/cli/exit_codes.py tests/test_errors.py
git commit -m "feat(errors): add invalid_cursor error code for pagination"
```

### Task 2: Add cursor / next_begin / ok:true fields to response models

**Files:**
- Modify: `src/cwms_tools/core/models.py:198-237,298-337,361-407`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
from cwms_tools.core.models import (
    HistoryResponse,
    SearchPlacesResponse,
    BrowseRegionResponse,
)


def test_success_models_default_ok_true_and_carry_cursor_fields():
    src = {"fingerprint": "abc"}
    s = SearchPlacesResponse(query="x", results=[], source=src)
    assert s.ok is True
    assert s.has_more is False
    assert s.next_cursor is None

    b = BrowseRegionResponse(office="SWT", bbox=None, state=None, results=[], source=src)
    assert b.ok is True and b.has_more is False and b.next_cursor is None

    h = HistoryResponse(
        ts_id="t", office_id="o", location="l", parameter="p", publisher=None,
        unit="EN", begin="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
        values=[], value_count=0, source=src,
    )
    assert h.ok is True and h.next_begin is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_models.py::test_success_models_default_ok_true_and_carry_cursor_fields -v`
Expected: FAIL — `ok`/`has_more`/`next_cursor`/`next_begin` undefined.

- [ ] **Step 3: Add the fields**

In `src/cwms_tools/core/models.py`, add `from typing import Literal` if not already imported (it is). Add `ok: Literal[True] = True` to each success response model (`SearchPlacesResponse`, `DescribePlaceResponse`, `ListParametersResponse`, `BrowseRegionResponse`, `ValueWithContextResponse`, `HistoryResponse`, `PublishersForParameterResponse`) as the first field after `model_config`.

To `SearchPlacesResponse` add:

```python
    has_more: bool = Field(
        default=False,
        description="True when more results exist beyond this page; fetch with `next_cursor`.",
    )
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for the next page. Pass back as `cursor`. Null when has_more is false.",
    )
```

To `BrowseRegionResponse` add the same two fields.

To `HistoryResponse` add:

```python
    next_begin: str | None = Field(
        default=None,
        description=(
            "When `truncated` is true, the RFC3339 timestamp to use as `begin` on the "
            "next request to continue the window with no duplicate/skipped point. Null otherwise."
        ),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/core/models.py tests/test_models.py
git commit -m "feat(models): add ok:true, cursor (has_more/next_cursor), and history next_begin fields"
```

---

## Commit 2 — Core pagination & history continuation

### Task 3: Create the cursor module

**Files:**
- Create: `src/cwms_tools/core/pagination.py`
- Test: `tests/test_pagination.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pagination.py`:

```python
import pytest

from cwms_tools.core import pagination
from cwms_tools.core.errors import CwmsToolsError, ErrorCode


def test_roundtrip_encode_decode():
    payload = {"v": 1, "kind": "search_places", "off": 50, "req": "abc", "offices": ["NWDM"], "total": 120}
    token = pagination.encode_cursor(payload)
    assert isinstance(token, str) and "=" not in token
    assert pagination.decode_cursor(token) == payload


def test_request_hash_is_stable_and_order_independent():
    a = pagination.request_hash({"q": "peck", "parameter": "Elev"})
    b = pagination.request_hash({"parameter": "Elev", "q": "peck"})
    assert a == b
    assert a != pagination.request_hash({"q": "peck", "parameter": "Flow-In"})


def test_decode_rejects_garbage():
    with pytest.raises(CwmsToolsError) as exc:
        pagination.decode_cursor("!!!not-base64!!!")
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_validate_continuation_checks_kind_req_offset():
    cur = {"v": 1, "kind": "search_places", "off": 50, "req": "abc", "offices": ["NWDM"], "total": 120}
    # happy path returns the resume offset (kind+req+offset only — NOT total)
    assert pagination.validate_continuation(cur, kind="search_places", req="abc") == 50
    # changed query/filter
    with pytest.raises(CwmsToolsError) as e1:
        pagination.validate_continuation(cur, kind="search_places", req="DIFFERENT")
    assert e1.value.envelope.code is ErrorCode.INVALID_CURSOR
    # wrong operation
    with pytest.raises(CwmsToolsError):
        pagination.validate_continuation(cur, kind="browse_region", req="abc")
    # malformed offset
    bad = {**cur, "off": -1}
    with pytest.raises(CwmsToolsError):
        pagination.validate_continuation(bad, kind="search_places", req="abc")


def test_ensure_total_detects_catalog_shift():
    cur = {"v": 1, "kind": "search_places", "off": 50, "req": "abc", "total": 120}
    pagination.ensure_total(cur, total=120)  # ok, no raise
    with pytest.raises(CwmsToolsError) as exc:
        pagination.ensure_total(cur, total=121)
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR


def test_coerce_offices_rejects_malformed_payloads():
    assert pagination.coerce_offices({"offices": ["NWDM", "SWT"]}) == ["NWDM", "SWT"]
    for bad in ({"offices": "NWDM"}, {"offices": [1, 2]},
                {"offices": ["A"] * (pagination.MAX_CURSOR_OFFICES + 1)}, {}):
        with pytest.raises(CwmsToolsError) as exc:
            pagination.coerce_offices(bad)
        assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR
```

> Validation is deliberately split so the cheap checks (kind/req/offset) run **before** any upstream fan-out, and only the `total` check runs after the result set is assembled.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_pagination.py -v`
Expected: FAIL — `ModuleNotFoundError: cwms_tools.core.pagination`.

- [ ] **Step 3: Create the implementation**

Create `src/cwms_tools/core/pagination.py`:

```python
"""Opaque cursor encoding for paginated list tools (search_places, browse_region).

A cursor is a base64url(JSON) token carrying the next offset, a hash of the
normalized request, the locked office set, and the full-result total. On
continuation the producer recomputes the result set over the locked offices
and validates the hash + total; any mismatch (changed query/filter, or a
catalog that shifted under us) raises `invalid_cursor` so the agent restarts
without the cursor rather than silently skipping or duplicating rows.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint

CURSOR_VERSION = 1


def request_hash(parts: dict[str, Any]) -> str:
    """Stable, order-independent short hash of the normalized request."""
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> dict[str, Any]:
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad)
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise invalid_cursor(f"cursor is not a valid token: {exc}") from exc
    if not isinstance(data, dict) or data.get("v") != CURSOR_VERSION:
        raise invalid_cursor("cursor version is unsupported; restart without a cursor")
    return data


def invalid_cursor(message: str, *, repair: RepairHint | None = None) -> CwmsToolsError:
    return CwmsToolsError.of(
        ErrorCode.INVALID_CURSOR,
        message,
        field="cursor",
        hint="Re-issue the original call without `cursor` to restart pagination.",
        repair=repair,
    )


def validate_continuation(cursor: dict[str, Any], *, kind: str, req: str) -> int:
    """Cheap pre-fan-out checks: kind, request hash, offset shape. Returns offset.

    The `total` check is intentionally NOT here — it needs the assembled result
    set, so callers run `ensure_total` after gathering. This lets a mismatched
    cursor fail before any upstream fan-out.
    """
    if cursor.get("kind") != kind:
        raise invalid_cursor("cursor was issued for a different operation")
    if cursor.get("req") != req:
        raise invalid_cursor("cursor does not match the current query/filters")
    offset = cursor.get("off")
    if not isinstance(offset, int) or offset < 0:
        raise invalid_cursor("cursor offset is malformed")
    return offset


def ensure_total(cursor: dict[str, Any], *, total: int) -> None:
    """Post-assembly check: the full result set must match the cursor's snapshot."""
    if cursor.get("total") != total:
        raise invalid_cursor("result set changed since the cursor was issued (catalog shifted)")


#: A cursor's locked office set is bounded — a hand-crafted token must not be
#: able to drive an unbounded fan-out. (USACE has ~70 offices; 200 is generous.)
MAX_CURSOR_OFFICES = 200


def coerce_offices(cursor: dict[str, Any]) -> list[str]:
    """Validate + return the cursor's locked office set before any fan-out.

    Rejects a malformed `offices` payload (non-list, over-long, or non-string
    members) so a forged cursor cannot widen the search beyond its snapshot.
    """
    offices = cursor.get("offices")
    if (
        not isinstance(offices, list)
        or len(offices) > MAX_CURSOR_OFFICES
        or not all(isinstance(o, str) for o in offices)
    ):
        raise invalid_cursor("cursor office set is malformed")
    return list(offices)


__all__ = [
    "CURSOR_VERSION",
    "MAX_CURSOR_OFFICES",
    "coerce_offices",
    "decode_cursor",
    "encode_cursor",
    "ensure_total",
    "invalid_cursor",
    "request_hash",
    "validate_continuation",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_pagination.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/core/pagination.py tests/test_pagination.py
git commit -m "feat(core): add opaque cursor pagination primitives"
```

### Task 4: Cursor pagination in `search_places`

**Files:**
- Modify: `src/cwms_tools/core/places.py:49-139`
- Test: `tests/test_places.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_places.py` (reuse the module's existing fixtures/mock catalog pattern — if helpers like `_seed_office` exist, use them; otherwise mock `locations.search` to return >2 enriched rows for one cached office):

```python
from cwms_tools.core import places, pagination
from cwms_tools.core.errors import CwmsToolsError, ErrorCode


def test_search_places_paginates_with_cursor(monkeypatch):
    rows = [
        {"office_id": "NWDM", "name": f"L{i}", "parameter_count": 1, "parameters": ["Elev"],
         "publishers": [], "co_located": []}
        for i in range(5)
    ]
    monkeypatch.setattr(places, "_run_fanout", lambda req: (["NWDM"], [], []))
    monkeypatch.setattr(places, "_gather_enriched", lambda offices, q, use_cache: list(rows))

    page1 = places.search_places("L", office="NWDM", limit=2)
    assert len(page1["results"]) == 2
    assert page1["has_more"] is True
    assert page1["total_count"] == 5
    assert page1["next_cursor"]

    page2 = places.search_places("L", office="NWDM", limit=2, cursor=page1["next_cursor"])
    assert [r["name"] for r in page2["results"]] == ["L2", "L3"]
    assert page2["has_more"] is True

    page3 = places.search_places("L", office="NWDM", limit=2, cursor=page2["next_cursor"])
    assert [r["name"] for r in page3["results"]] == ["L4"]
    assert page3["has_more"] is False
    assert page3["next_cursor"] is None


def test_search_places_rejects_mismatched_cursor(monkeypatch):
    rows = [{"office_id": "NWDM", "name": f"L{i}", "parameter_count": 1, "parameters": [],
             "publishers": [], "co_located": []} for i in range(5)]
    monkeypatch.setattr(places, "_run_fanout", lambda req: (["NWDM"], [], []))
    monkeypatch.setattr(places, "_gather_enriched", lambda offices, q, use_cache: list(rows))
    page1 = places.search_places("L", office="NWDM", limit=2)
    with pytest.raises(CwmsToolsError) as exc:
        places.search_places("DIFFERENT", office="NWDM", limit=2, cursor=page1["next_cursor"])
    assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_places.py -k paginat -v`
Expected: FAIL — `search_places() got an unexpected keyword argument 'cursor'`.

- [ ] **Step 3: Implement the cursor path**

In `src/cwms_tools/core/places.py`, add the import near the top:

```python
from cwms_tools.core import pagination
```

Change the `search_places` signature to add `cursor`:

```python
def search_places(
    query: str,
    *,
    office: str | list[str] | None = None,
    parameter: str | None = None,
    limit: int | None = DEFAULT_SEARCH_LIMIT,
    cursor: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
```

Replace the existing body block — from the negative-limit guard (`if limit is not None and limit < 0:` at `places.py:87`) through the `response: dict[str, Any] = {...}` assignment — with the following. Note this **keeps** the single negative-limit guard (do not duplicate it elsewhere), **normalizes `limit == 0` to `None`** (no cap) so the cursor arithmetic can never spin on an empty page, validates the cursor's cheap fields **before** any fan-out, and checks `total` only after assembly:

```python
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer or None")
    if limit == 0:
        limit = None  # 0 means "no cap"; normalize so pagination math is well-defined

    req = pagination.request_hash({"q": query, "parameter": parameter})
    decoded: dict[str, Any] | None = None
    offset = 0
    if cursor is not None:
        decoded = pagination.decode_cursor(cursor)
        # Cheap checks BEFORE upstream fan-out: kind, request hash, offset shape,
        # and a bounded/typed office set (a forged cursor must not widen the fan-out).
        offset = pagination.validate_continuation(decoded, kind="search_places", req=req)
        offices_searched = pagination.coerce_offices(decoded)
        offices_skipped: list[str] = []
        partial_reasons: list[str] = []
    else:
        requested = _normalize_office_arg(office)
        if requested is None:
            requested = offices.cached_offices_for_locations()
        offices_searched, offices_skipped, partial_reasons = _run_fanout(requested)

    enriched, filtered_out = _apply_parameter_filter(
        _gather_enriched(offices_searched, query, use_cache=use_cache),
        parameter,
        use_cache=use_cache,
    )
    enriched.sort(key=lambda r: (-r["parameter_count"], r["office_id"], r["name"]))
    total_count = len(enriched)
    if decoded is not None:
        pagination.ensure_total(decoded, total=total_count)  # catalog-shift guard

    if limit is None:
        page = enriched[offset:]
        has_more = False
    else:
        page = enriched[offset : offset + limit]
        has_more = offset + limit < total_count
    next_cursor = (
        pagination.encode_cursor(
            {
                "v": pagination.CURSOR_VERSION,
                "kind": "search_places",
                "off": offset + limit,  # limit is guaranteed not-None here (has_more path)
                "req": req,
                "offices": offices_searched,
                "total": total_count,
            }
        )
        if has_more
        else None
    )
```

Then change the `results = [...]` comprehension to iterate over `page` instead of `enriched`, and replace the `response` dict's tail fields. The final `response` becomes:

```python
    response: dict[str, Any] = {
        "query": query,
        "office": office,
        "offices_searched": offices_searched,
        "offices_skipped_for_budget": offices_skipped,
        "results": results,
        "total_count": total_count,
        "truncated": has_more,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "limit": limit,
    }
```

(Keep the existing `if parameter is not None:` and `if partial_reasons:` blocks unchanged.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_places.py -k "paginat or cursor" -v`
Expected: PASS. Then run the whole file: `uv run pytest tests/test_places.py -v` — fix any existing assertions that now also see `has_more`/`next_cursor` keys (additive, should not break).

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/core/places.py tests/test_places.py
git commit -m "feat(core): cursor pagination for search_places with locked office set"
```

### Task 5: Cursor pagination in `browse_region`

**Files:**
- Modify: `src/cwms_tools/core/places.py:446-549`
- Test: `tests/test_places.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_places.py`:

```python
def test_browse_region_paginates_with_cursor(monkeypatch):
    rows = [{"office_id": "SWT", "name": f"B{i}", "parameter_count": 1, "parameters": [],
             "publishers": [], "co_located": []} for i in range(5)]
    monkeypatch.setattr(places.catalog, "enrich_locations", lambda office, use_cache=True: list(rows))
    p1 = places.browse_region(office="SWT", limit=2)
    assert p1["has_more"] is True and p1["total_count"] == 5 and p1["next_cursor"]
    p2 = places.browse_region(office="SWT", limit=2, cursor=p1["next_cursor"])
    assert [r["name"] for r in p2["results"]] == ["B2", "B3"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_places.py::test_browse_region_paginates_with_cursor -v`
Expected: FAIL — `browse_region() got an unexpected keyword argument 'cursor'`.

- [ ] **Step 3: Implement**

Add `cursor: str | None = None` to the `browse_region` keyword-only signature (after `limit`). Add the `limit == 0` normalization next to the existing negative-limit guard (`places.py:467`):

```python
    if limit is not None and limit < 0:
        raise ValueError("limit must be a non-negative integer or None")
    if limit == 0:
        limit = None  # 0 means "no cap"
```

The cursor here needs `total_count`, which `browse_region` computes after filtering — but the cheap kind/req/offset checks can still run up front (filtering an already-cached catalog is local, not a fan-out, so there is no expensive call to gate). Decode and run the cheap checks right after the guard above, then after `rows = sorted(...)` / `total_count = len(rows)` / `ghost_count = ...`, run `ensure_total` and slice. Replace the `truncated`/slice block with:

```python
    if decoded is not None:
        pagination.ensure_total(decoded, total=total_count)  # catalog-shift guard

    if limit is None:
        rows = rows[offset:]
        has_more = False
    else:
        rows = rows[offset : offset + limit]
        has_more = offset + limit < total_count
    next_cursor = (
        pagination.encode_cursor(
            {
                "v": pagination.CURSOR_VERSION,
                "kind": "browse_region",
                "off": offset + limit,  # limit not-None on the has_more path
                "req": req,
                "total": total_count,
            }
        )
        if has_more
        else None
    )
```

where `decoded`/`offset`/`req` were established right after the limit guard:

```python
    req = pagination.request_hash({"office": office, "bbox": _bbox_to_dict(bbox), "state": state})
    decoded: dict[str, Any] | None = None
    offset = 0
    if cursor is not None:
        decoded = pagination.decode_cursor(cursor)
        offset = pagination.validate_continuation(decoded, kind="browse_region", req=req)
```

In the `response` dict, change `"truncated": truncated,` to `"truncated": has_more,` and add `"has_more": has_more,` and `"next_cursor": next_cursor,`. Change the trailing `if truncated:` to `if has_more:` and update the hint text:

```python
    if has_more:
        response["truncation_hint"] = (
            f"returned {len(rows)} of {total_count}; fetch the next page with the "
            "`next_cursor`, or pass --limit 0 for all rows"
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_places.py -k browse -v`
Expected: PASS. Then `uv run pytest tests/test_places.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/core/places.py tests/test_places.py
git commit -m "feat(core): cursor pagination for browse_region"
```

### Task 6: `next_begin` continuation for history

**Files:**
- Modify: `src/cwms_tools/core/timeseries.py:38-76,128-149`
- Modify: `src/cwms_tools/core/values.py:97-123`
- Test: `tests/test_values.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_values.py`:

```python
from cwms_tools.core import timeseries


def test_fetch_window_emits_next_begin_when_truncated(monkeypatch):
    cap = timeseries._UPSTREAM_PAGE_SIZE_CAP
    last_ms = 1_700_000_000_000
    values = [[last_ms - (cap - 1 - i) * 60000, float(i), 0] for i in range(cap)]
    payload = {"values": values, "units": "ft"}

    class _Resp:
        json = payload

    from datetime import datetime, timezone
    monkeypatch.setattr(
        timeseries.ts_api, "get_timeseries", lambda **kw: _Resp()
    )
    out = timeseries.fetch_window(
        "t", office="NWDM",
        begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2099, 1, 1, tzinfo=timezone.utc),  # far future forces truncation
        unit="EN",
    )
    assert out["truncated"] is True
    # next_begin == last point + 1ms, RFC3339 Z
    expected = datetime.fromtimestamp((last_ms + 1) / 1000, tz=timezone.utc)
    assert out["next_begin"] == expected.isoformat().replace("+00:00", "Z")


def test_fetch_window_next_begin_none_when_not_truncated(monkeypatch):
    payload = {"values": [[1_700_000_000_000, 1.0, 0]], "units": "ft"}

    class _Resp:
        json = payload

    monkeypatch.setattr(timeseries.ts_api, "get_timeseries", lambda **kw: _Resp())
    from datetime import datetime, timezone
    out = timeseries.fetch_window(
        "t", office="NWDM",
        begin=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        unit="EN",
    )
    assert out["truncated"] is False
    assert out["next_begin"] is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_values.py -k next_begin -v`
Expected: FAIL — `KeyError: 'next_begin'`.

- [ ] **Step 3: Implement the helper and wire it**

In `src/cwms_tools/core/timeseries.py`, add a helper near `_detect_truncation`:

```python
def _next_begin(payload: dict[str, Any]) -> str | None:
    """RFC3339 timestamp one millisecond after the latest returned point.

    Used as the `begin` of the next slice when a window truncated at the page
    cap, so the continuation has no duplicate or skipped seam point (CWMS
    point timestamps are millisecond-resolution).
    """
    values = payload.get("values")
    if not isinstance(values, list):
        return None
    last_ms: int | None = None
    for row in reversed(values):
        if isinstance(row, list) and row and isinstance(row[0], (int, float)):
            last_ms = int(row[0])
            break
    if last_ms is None:
        return None
    return _ms_to_rfc3339(last_ms + 1)
```

In `fetch_window`, after computing `truncated = _detect_truncation(...)`, derive `next_begin` once and make the hint consistent with whether a continuation point was actually found (`_detect_truncation` can flag truncation even when the final timestamp is unparseable, in which case `_next_begin` returns `None` and the hint must NOT tell the agent to continue from a null `next_begin`):

```python
    next_begin = _next_begin(payload) if truncated else None
    if not truncated:
        truncation_hint = None
    elif next_begin is not None:
        truncation_hint = (
            "hit upstream page cap of 300000; continue the window from `next_begin`, "
            "or narrow --begin/--end"
        )
    else:
        truncation_hint = (
            "hit upstream page cap of 300000 but could not derive a continuation "
            "timestamp; narrow --begin/--end and re-request"
        )
    return {
        "ts_id": ts_id,
        "office_id": office,
        "unit": payload.get("units") or payload.get("unit") or unit,
        "begin": begin.isoformat(),
        "end": end.isoformat(),
        "values": _values_from_payload(payload),
        "truncated": truncated,
        "next_begin": next_begin,
        "truncation_hint": truncation_hint,
        "raw": payload,
    }
```

In `src/cwms_tools/core/values.py`, add `next_begin` to the `get_history` return dict (after `truncation_hint`):

```python
        "next_begin": series.get("next_begin"),
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_values.py -k next_begin -v`
Expected: PASS. Then `uv run pytest tests/test_values.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/core/timeseries.py src/cwms_tools/core/values.py tests/test_values.py
git commit -m "feat(core): history window-continuation via next_begin on truncation"
```

---

## Commit 3 — MCP tools wiring

### Task 7: Add `cursor` param to MCP list tools and pass through new fields

**Files:**
- Modify: `src/cwms_tools/mcp/tools.py:62-125,180-234,305-374`
- Test: `tests/test_mcp_tool_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_tool_handlers.py`. Match the repo's async-test convention — `tests/test_mcp_server.py` drives coroutines with a sync wrapper + `asyncio.run(go())` (do NOT use `@pytest.mark.asyncio`; `asyncio_mode = "auto"` is set but the established style is the sync wrapper):

```python
import asyncio
from cwms_tools.mcp.server import build_server


def test_search_places_tool_exposes_cursor_in_schema():
    async def go():
        mcp = build_server()
        return {t.name: t for t in await mcp.list_tools()}

    tools = asyncio.run(go())
    assert "cursor" in tools["cwms_search_places"].to_mcp_tool().inputSchema["properties"]
    assert "cursor" in tools["cwms_browse_region"].to_mcp_tool().inputSchema["properties"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_mcp_tool_handlers.py -k cursor -v`
Expected: FAIL — `cursor` not in properties.

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/tools.py`, add a `cursor` parameter to `cwms_search_places` (before `detail`):

```python
        cursor: Annotated[
            str | None,
            "Opaque pagination cursor from a prior call's `next_cursor`. Pass it "
            "back verbatim to fetch the next page; omit it for the first page. "
            "On a stale cursor (changed query/filters or a shifted catalog) the "
            "tool returns the `invalid_cursor` error — restart without `cursor`.",
        ] = None,
```

and thread it into the core call: `places.search_places(query, office=office, parameter=parameter, limit=effective_limit, cursor=cursor)`.

Do the same for `cwms_browse_region`: add the identical `cursor` annotated param before `detail`, and pass `cursor=cursor` into `places.browse_region(...)`.

The `next_begin` for history needs no handler change — `_shape_history_detail` keeps unknown keys; confirm `HistoryResponse` (Task 2) carries `next_begin` so `model_validate` preserves it.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_tool_handlers.py -k cursor -v`
Expected: PASS. Then `uv run pytest tests/test_mcp_tool_handlers.py tests/test_mcp_server.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/mcp/tools.py tests/test_mcp_tool_handlers.py
git commit -m "feat(mcp): cursor parameter on search/browse tools; history next_begin passthrough"
```

### Task 8: Register `invalid_cursor` in the per-tool error catalog

**Files:**
- Modify: `src/cwms_tools/mcp/resources.py:46-64`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
from cwms_tools.mcp.resources import TOOL_ERROR_CODES


def test_list_tools_declare_invalid_cursor():
    assert "invalid_cursor" in TOOL_ERROR_CODES["cwms_search_places"]
    assert "invalid_cursor" in TOOL_ERROR_CODES["cwms_browse_region"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k invalid_cursor -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/resources.py`, add `"invalid_cursor"` (keep lists sorted) to the `cwms_search_places` and `cwms_browse_region` entries of `TOOL_ERROR_CODES`:

```python
    "cwms_search_places": ["ghost_office", "invalid_cursor", "rate_limited", "upstream_error"],
    ...
    "cwms_browse_region": [
        "ghost_office", "invalid_cursor", "rate_limited", "upstream_error", "usage_error",
    ],
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k invalid_cursor -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/mcp/resources.py tests/test_mcp_server.py
git commit -m "feat(mcp): declare invalid_cursor in search/browse error catalogs"
```

---

## Commit 4 — CLI flags & structured schema (C-1)

### Task 9: `--cursor` flag on `place search` and `region browse`

**Files:**
- Modify: `src/cwms_tools/cli/commands/place.py:47-148`
- Modify: `src/cwms_tools/cli/commands/region.py:21-123`
- Test: `tests/test_cli_place.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_place.py` (follow the file's existing `CliRunner` + monkeypatch-of-`places` pattern):

```python
from typer.testing import CliRunner
from cwms_tools.cli.app import app
from cwms_tools.cli.commands import place as place_cmd

runner = CliRunner()


def test_place_search_accepts_cursor(monkeypatch):
    captured = {}

    def fake_search(query, *, office=None, parameter=None, limit=None, cursor=None, use_cache=True):
        captured["cursor"] = cursor
        return {"query": query, "results": [], "has_more": False, "next_cursor": None,
                "total_count": 0, "source": {"fingerprint": "x"}}

    monkeypatch.setattr(place_cmd.places, "search_places", fake_search)
    # CliRunner stdout is non-TTY, so machine mode auto-engages — no --machine needed.
    # (If you pass it explicitly it MUST precede the subcommand: it is a root-callback option.)
    result = runner.invoke(app, ["place", "search", "L", "-o", "NWDM", "--cursor", "TOKEN"])
    assert result.exit_code == 0
    assert captured["cursor"] == "TOKEN"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli_place.py -k cursor -v`
Expected: FAIL — no such option `--cursor`.

- [ ] **Step 3: Implement**

In `src/cwms_tools/cli/commands/place.py`, add a `cursor` option to `search` (before `detail`):

```python
    cursor: Annotated[
        str | None,
        typer.Option(
            "--cursor",
            help=(
                "Opaque pagination cursor from a prior call's `next_cursor`. "
                "Pass it back to fetch the next page; omit for the first page. "
                "A stale cursor returns the `invalid_cursor` error (exit 2) — "
                "re-run without --cursor to restart."
            ),
        ),
    ] = None,
```

and thread `cursor=cursor` into the `places.search_places(...)` call.

In `src/cwms_tools/cli/commands/region.py`, add the same `cursor` option to `browse` (before the function body's validation) and pass `cursor=cursor` to `places.browse_region(...)`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cli_place.py -k cursor -v`
Expected: PASS. Then `uv run pytest tests/test_cli_place.py -v`.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/cli/commands/place.py src/cwms_tools/cli/commands/region.py tests/test_cli_place.py
git commit -m "feat(cli): --cursor flag on place search and region browse"
```

### Task 10: Restructure `cwms-tools schema` into a structured contract

**Files:**
- Modify: `src/cwms_tools/cli/commands/schema.py:33-151`
- Test: `tests/test_cli_inspection.py` (schema-related tests)

This replaces each command's single prose `path` string with structured `arguments`/`options` records (name, type, default, enum, required, repeatable), a per-command `error_codes` list with exit codes, and a `latency_class`. Factor the fingerprint-relevant subset into `cli_contract_payload()` for Task 14.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_inspection.py`:

```python
import json
from typer.testing import CliRunner
from cwms_tools.cli.app import app

runner = CliRunner()


def test_schema_commands_are_structured():
    result = runner.invoke(app, ["schema"])  # machine mode auto-on under CliRunner
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    cmd = {c["path"]: c for c in doc["commands"]}["cwms-tools place search"]
    # structured args/options
    names = {o["name"] for o in cmd["options"]}
    assert {"--office", "--limit", "--cursor", "--detail"} <= names
    limit_opt = next(o for o in cmd["options"] if o["name"] == "--limit")
    assert limit_opt["type"] == "integer" and limit_opt["default"] == 50
    detail_opt = next(o for o in cmd["options"] if o["name"] == "--detail")
    assert detail_opt["enum"] == ["summary", "full"]
    office_opt = next(o for o in cmd["options"] if o["name"] == "--office")
    assert office_opt["repeatable"] is True
    # per-command error catalog + latency
    assert "invalid_cursor" in {e["code"] for e in cmd["error_codes"]}
    assert cmd["latency_class"] in {"local", "cached", "network", "slow", "async"}


def test_schema_value_get_marks_with_status_slow_path():
    result = runner.invoke(app, ["schema"])
    doc = json.loads(result.stdout)
    cmd = {c["path"]: c for c in doc["commands"]}["cwms-tools value get"]
    assert cmd["latency_class"] in {"network", "slow"}
    ws = next(o for o in cmd["options"] if o["name"] == "--with-status")
    assert ws["type"] == "boolean"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli_inspection.py -k schema -v`
Expected: FAIL — `KeyError: 'options'` / `"cwms-tools place search"` path no longer a prose string.

- [ ] **Step 3: Implement the structured schema**

In `src/cwms_tools/cli/commands/schema.py`, replace `_commands()` with structured records and add a `cli_contract_payload()` that returns the fingerprint-relevant subset. Use this shape per command (full content shown for `place search`; apply the same structure to every command):

```python
def _opt(name, type_, *, default=None, enum=None, required=False, repeatable=False, help=""):
    rec = {"name": name, "type": type_, "required": required, "repeatable": repeatable}
    if default is not None:
        rec["default"] = default
    if enum is not None:
        rec["enum"] = enum
    if help:
        rec["help"] = help
    return rec


def _arg(name, type_, *, required=True, variadic=False, help=""):
    return {"name": name, "type": type_, "required": required, "variadic": variadic, "help": help}


def _errs(*codes: str) -> list[dict[str, Any]]:
    from cwms_tools.core.errors import ErrorCode  # noqa: PLC0415
    out = []
    for c in codes:
        ec = ErrorCode(c)
        out.append({"code": c, "exit": exit_code_for(ec)})
    return out


def _commands() -> list[dict[str, Any]]:
    return [
        {"path": "cwms-tools whoami", "output_class": "record", "reads_stdin": False,
         "latency_class": "cached", "arguments": [], "options": [], "error_codes": []},
        {"path": "cwms-tools env", "output_class": "record", "reads_stdin": False,
         "latency_class": "local", "arguments": [], "options": [], "error_codes": []},
        {"path": "cwms-tools config show", "output_class": "record", "reads_stdin": False,
         "latency_class": "local", "arguments": [],
         "options": [_opt("--resolved", "boolean", default=False, required=True,
                          help="Show the merged effective configuration.")],
         "error_codes": _errs("usage_error")},
        {"path": "cwms-tools fingerprint", "output_class": "record", "reads_stdin": False,
         "latency_class": "cached", "arguments": [], "options": [], "error_codes": []},
        {"path": "cwms-tools schema", "output_class": "record", "reads_stdin": False,
         "latency_class": "local", "arguments": [], "options": [], "error_codes": []},
        {"path": "cwms-tools place search", "output_class": "list", "reads_stdin": False,
         "latency_class": "network",
         "arguments": [_arg("query", "string", help="Name fragment, case-insensitive.")],
         "options": [
             _opt("--office", "string", repeatable=True, help="Office code; repeat to fan out."),
             _opt("--parameter", "string", help="Filter to a published parameter."),
             _opt("--limit", "integer", default=50, help="Result cap; 0 = no cap."),
             _opt("--cursor", "string", help="Pagination cursor from prior next_cursor."),
             _opt("--detail", "string", default="summary", enum=["summary", "full"]),
         ],
         "error_codes": _errs("ghost_office", "invalid_cursor", "rate_limited",
                              "upstream_error", "usage_error")},
        {"path": "cwms-tools place describe", "output_class": "record", "reads_stdin": False,
         "latency_class": "network",
         "arguments": [_arg("spec", "string", help="OFFICE/NAME, e.g. NWDM/FTPK.")],
         "options": [_opt("--detail", "string", default="summary", enum=["summary", "full"])],
         "error_codes": _errs("ghost_office", "not_found", "rate_limited", "upstream_error",
                              "usage_error")},
        {"path": "cwms-tools place parameters", "output_class": "record", "reads_stdin": False,
         "latency_class": "network",
         "arguments": [_arg("spec", "string", help="OFFICE/NAME.")],
         "options": [],
         "error_codes": _errs("ghost_office", "not_found", "rate_limited", "upstream_error",
                              "usage_error")},
        {"path": "cwms-tools region browse", "output_class": "list", "reads_stdin": False,
         "latency_class": "network",
         "arguments": [],
         "options": [
             _opt("--office", "string", required=True, help="Office code (required)."),
             _opt("--south", "number"), _opt("--west", "number"),
             _opt("--north", "number"), _opt("--east", "number"),
             _opt("--state", "string", help="Two-letter US state code."),
             _opt("--limit", "integer", default=50, help="Result cap; 0 = no cap."),
             _opt("--cursor", "string", help="Pagination cursor from prior next_cursor."),
         ],
         "error_codes": _errs("ghost_office", "invalid_cursor", "rate_limited",
                              "upstream_error", "usage_error")},
        {"path": "cwms-tools value get", "output_class": "bulk-result", "reads_stdin": False,
         "latency_class": "slow", "supports_partial_failure": True,
         "partial_failure": "non-zero exit on any item failure; per-item errors inline",
         "arguments": [_arg("id_specs", "string", variadic=True,
                            help="One or more OFFICE/NAME/PARAMETER ids.")],
         "options": [
             _opt("--window-hours", "integer", default=24),
             _opt("--unit", "string", default="EN", enum=["EN", "SI"]),
             _opt("--with-status", "boolean", default=False,
                  help="Classify against Location Levels (slow; ~8s budget)."),
             _opt("--detail", "string", default="summary", enum=["summary", "full"]),
         ],
         "error_codes": _errs("ghost_office", "not_found", "rate_limited", "upstream_error",
                              "usage_error")},
        {"path": "cwms-tools value history", "output_class": "record", "reads_stdin": False,
         "latency_class": "slow",
         "arguments": [_arg("id_spec", "string", help="OFFICE/NAME/PARAMETER.")],
         "options": [
             _opt("--begin", "string", required=True, help="RFC3339 window start."),
             _opt("--end", "string", required=True, help="RFC3339 window end."),
             _opt("--unit", "string", default="EN", enum=["EN", "SI"]),
             _opt("--detail", "string", default="summary", enum=["summary", "full"]),
         ],
         "error_codes": _errs("ghost_office", "invalid_field", "not_found", "rate_limited",
                              "upstream_error", "usage_error")},
        {"path": "cwms-tools publisher for-parameter", "output_class": "record",
         "reads_stdin": False, "latency_class": "network",
         "arguments": [_arg("parameter", "string", help="Parameter code, e.g. Elev.")],
         "options": [_opt("--office", "string", repeatable=True, help="Office code; repeat.")],
         "error_codes": _errs("rate_limited", "upstream_error")},
        {"path": "cwms-tools mcp serve", "output_class": "stream", "reads_stdin": True,
         "latency_class": "async",
         "arguments": [],
         "options": [
             _opt("--transport", "string", default="stdio",
                  enum=["stdio", "streamable-http"]),
             _opt("--host", "string", default="127.0.0.1"),
             _opt("--port", "integer", default=8765),
         ],
         "error_codes": _errs("usage_error"),
         "notes": "stdio transport reserves stdout for the JSON-RPC channel"},
    ]


def cli_contract_payload() -> dict[str, Any]:
    """Fingerprint-relevant CLI contract subset (no fingerprint value, to avoid recursion)."""
    return {
        "commands": _commands(),
        "exit_codes": _exit_codes(),
        "machine_profile": _machine_profile(),
    }
```

Extract the existing `machine_profile` dict literal from `_schema_payload()` into a `_machine_profile()` helper, and have `_schema_payload()` build from `cli_contract_payload()`:

```python
def _schema_payload() -> dict[str, Any]:
    contract = cli_contract_payload()
    return {
        "name": "cwms-tools",
        "version": PKG_VERSION,
        "fingerprint_scope": fp.FINGERPRINT_SCOPE,
        "commands": contract["commands"],
        "exit_codes": contract["exit_codes"],
        "error_codes": sorted(c.value for c in ErrorCode),
        "env_inputs": _env_inputs(),
        "mcp_tools": TOOL_INVENTORY,
        "mcp_resources": RESOURCE_INVENTORY,
        "machine_profile": contract["machine_profile"],
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cli_inspection.py -k schema -v`
Expected: PASS. Fix any older schema assertions that expected the prose `path` strings (update them to the new structured shape).

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/cli/commands/schema.py tests/test_cli_inspection.py
git commit -m "feat(cli): structured schema with args/flags, per-command error catalog, latency class (C-1)"
```

---

## Commit 5 — Annotations, completions, latency metadata

### Task 11: Honest tool annotations (m-4)

**Files:**
- Modify: `src/cwms_tools/mcp/tools.py` (all `@mcp.tool(annotations=...)`)
- Modify: `src/cwms_tools/mcp/server.py:196`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
import asyncio
from cwms_tools.mcp.server import build_server

_CDA_TOOLS = {
    "cwms_search_places", "cwms_describe_place", "cwms_list_parameters",
    "cwms_get_value", "cwms_get_history", "cwms_browse_region",
    "cwms_publishers_for_parameter",
}


def test_cda_tools_declare_open_world_and_idempotent():
    async def go():
        mcp = build_server()
        return {t.name: t.to_mcp_tool() for t in await mcp.list_tools()}

    tools = asyncio.run(go())
    for name in _CDA_TOOLS:
        ann = tools[name].annotations
        assert ann.readOnlyHint is True
        assert ann.openWorldHint is True
        assert ann.idempotentHint is True
    overview = tools["cwms_get_overview_section"].annotations
    assert overview.openWorldHint is False
    assert overview.idempotentHint is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k open_world -v`
Expected: FAIL — `openWorldHint` is None.

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/tools.py`, extend every CDA tool's `annotations` dict with `"openWorldHint": True, "idempotentHint": True`. Example for `cwms_search_places`:

```python
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": True,
            "idempotentHint": True,
            "title": "Search places by name",
        },
    )
```

Apply the same three-hint set to `cwms_describe_place`, `cwms_list_parameters`, `cwms_browse_region`, `cwms_get_value`, `cwms_get_history`, and `cwms_publishers_for_parameter`.

In `src/cwms_tools/mcp/server.py`, update the overview tool annotation (it reads bundled local content, so `openWorldHint=False`):

```python
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "openWorldHint": False,
            "idempotentHint": True,
            "title": "Get overview section",
        }
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k open_world -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/mcp/tools.py src/cwms_tools/mcp/server.py tests/test_mcp_server.py
git commit -m "feat(mcp): set openWorldHint/idempotentHint honestly on all tools (m-4)"
```

### Task 12: Latency metadata in capabilities (m-6)

**Files:**
- Modify: `src/cwms_tools/mcp/resources.py:79-151`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
from cwms_tools.mcp.resources import capabilities_payload


def test_capabilities_declare_tool_latency():
    cap = capabilities_payload()
    lat = cap["tool_latency"]
    assert lat["cwms_get_value"] in {"network", "slow"}
    assert lat["cwms_get_history"] == "slow"
    assert lat["cwms_search_places"] == "network"
    assert lat["cwms_get_overview_section"] == "local"
    # every advertised tool has a latency class
    assert set(lat) == set(cap["tools"])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k latency -v`
Expected: FAIL — `KeyError: 'tool_latency'`.

- [ ] **Step 3: Implement**

In `src/cwms_tools/mcp/resources.py`, add a module constant and surface it in `capabilities_payload()`:

```python
#: Per-tool latency class (local | cached | network | slow). `slow` flags paths
#: that routinely exceed ~1s: the levels-classified value path and the
#: potentially 300k-point history pull.
TOOL_LATENCY: dict[str, str] = {
    "cwms_search_places": "network",
    "cwms_describe_place": "network",
    "cwms_list_parameters": "network",
    "cwms_get_value": "network",
    "cwms_get_history": "slow",
    "cwms_browse_region": "network",
    "cwms_publishers_for_parameter": "network",
    "cwms_get_overview_section": "local",
}
```

In `capabilities_payload()` return dict, add (after `"tool_error_codes": TOOL_ERROR_CODES,`):

```python
        "tool_latency": TOOL_LATENCY,
```

Note in the value of `cwms_get_value`: it is `network` for the default fast path; the slow `--with-status` path is documented in the tool description and `level_lookup_status`. Add `"cwms_get_overview_section"` etc. Also add `"TOOL_LATENCY"` to `__all__`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -k latency -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/mcp/resources.py tests/test_mcp_server.py
git commit -m "feat(mcp): structured per-tool latency metadata in capabilities (m-6)"
```

### Task 13: m-5 completions — DEFER (documented), FastMCP 3.3.1 has no completion hook

**Decision (confirmed live + by Codex):** FastMCP 3.3.1 exposes **no** completion decorator — `hasattr(FastMCP(name="x"), "completion")` is `False`, and the only completion entry point is the private `_mcp_server` low-level `Server.completion()`. Wiring completions through that private attribute (uncertain whether FastMCP's run loop even dispatches to it) is disproportionate plumbing for a **Minor** finding, and the `cwms://overview` index already gives agents every valid `{section_id}`/`{chunk_id}` value to build URIs from. So m-5 is **deferred**, not implemented — recorded transparently rather than faked.

This task is documentation-only (no fragile private-API code). It rides in commit 5.

- [ ] **Step 1: Record the deferral in the capability summary**

In `src/cwms_tools/mcp/resources.py`, inside `capabilities_payload()`'s return dict, add a `completions` block under the existing `fastmcp` key region:

```python
        "completions": {
            "supported": False,
            "reason": (
                "FastMCP 3.3.1 exposes no completion handler; the overview index "
                "is the discovery path for resource-template variables."
            ),
            "discover_section_ids_via": "cwms://overview",
        },
```

- [ ] **Step 2: Add a failing test that pins the documented fallback**

Add to `tests/test_mcp_server.py`:

```python
from cwms_tools.mcp.resources import capabilities_payload


def test_capabilities_document_completion_fallback():
    comp = capabilities_payload()["completions"]
    assert comp["supported"] is False
    assert comp["discover_section_ids_via"] == "cwms://overview"
```

- [ ] **Step 3: Run it**

Run: `uv run pytest tests/test_mcp_server.py -k completion_fallback -v`
Expected: PASS after Step 1 (write the test first, watch it fail on `KeyError: 'completions'`, then add the block).

- [ ] **Step 4: Note the deferral in the CHANGELOG known-limitations (done in Task 16)**

No code here — Task 16 step 2 records "m-5 (completion for resource-template variables) deferred: FastMCP 3.3.1 lacks a completion hook; track for the upgrade that adds one."

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/mcp/resources.py tests/test_mcp_server.py
git commit -m "docs(mcp): document completion-unsupported fallback; defer m-5 (FastMCP lacks hook)"
```

---

## Commit 6 — Fold CLI into the fingerprint + release (M-2)

### Task 14: Hash the CLI contract into the capability fingerprint

**Files:**
- Modify: `src/cwms_tools/core/fingerprint.py:48-73`
- Modify: `src/cwms_tools/mcp/contract.py:88-93`
- Test: `tests/test_fingerprint.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_fingerprint.py`:

```python
from cwms_tools.core import fingerprint
from cwms_tools.mcp.contract import canonical_fingerprint


def test_cli_contract_changes_move_the_fingerprint():
    base = fingerprint.compute(tools={}, resources=[], cli_contract={"commands": []})
    changed = fingerprint.compute(
        tools={}, resources=[], cli_contract={"commands": [{"path": "x"}]}
    )
    assert base != changed


def test_canonical_fingerprint_includes_cli_contract(monkeypatch):
    # Mutating the CLI schema must move the canonical fingerprint.
    import cwms_tools.cli.commands.schema as schema_cmd
    before = canonical_fingerprint()
    orig = schema_cmd.cli_contract_payload
    monkeypatch.setattr(
        schema_cmd, "cli_contract_payload",
        lambda: {**orig(), "commands": orig()["commands"] + [{"path": "cwms-tools probe"}]},
    )
    fingerprint_module_cache_clear()  # see note
    after = canonical_fingerprint()
    assert before != after
```

> Note: `canonical_fingerprint()` recomputes the hash on every call (only `tool_definitions()` is `@lru_cache`d, and the CLI contract is read fresh each call), so no cache reset is needed — delete the `fingerprint_module_cache_clear()` line. Keep the assertion.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_fingerprint.py -k cli_contract -v`
Expected: FAIL — `compute() got an unexpected keyword argument 'cli_contract'`.

- [ ] **Step 3: Implement**

In `src/cwms_tools/core/fingerprint.py`, add the parameter and fold it into the hashed payload:

```python
def compute(
    *,
    tools: dict[str, dict[str, Any]] | None = None,
    resources: list[dict[str, Any]] | None = None,
    cli_contract: dict[str, Any] | None = None,
) -> str:
    ...
    payload = {
        "cwms_tools": _cwms_tools_version(),
        "cwms_python": _cwms_python_version(),
        "tools": _sorted_tools(tools or {}),
        "resources": _sorted_resources(resources or []),
        "cli_contract": cli_contract or {},
        "error_codes": sorted(c.value for c in ErrorCode),
        "overview_sha256": overview.document_sha256(),
        "session": session_fingerprint(),
        "workarounds": active_workarounds(),
    }
```

Update the module docstring's numbered input list to include "8. The CLI command/flag/exit-code contract".

In `src/cwms_tools/mcp/contract.py`, inject the CLI contract via lazy import (preserving `core/`'s purity):

```python
def canonical_fingerprint() -> str:
    """The one capability fingerprint shared by every agent-visible surface."""
    from cwms_tools.cli.commands.schema import cli_contract_payload  # noqa: PLC0415

    return fingerprint.compute(
        tools=tool_definitions(),
        resources=RESOURCE_INVENTORY,
        cli_contract=cli_contract_payload(),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_fingerprint.py -v`
Expected: PASS. Confirm no import cycle: `uv run python -c "from cwms_tools.mcp.contract import canonical_fingerprint; print(canonical_fingerprint()[:12])"`.

- [ ] **Step 5: Commit**

```bash
git add src/cwms_tools/core/fingerprint.py src/cwms_tools/mcp/contract.py tests/test_fingerprint.py
git commit -m "feat(fingerprint): fold CLI contract into the capability fingerprint (M-2)"
```

### Task 15: Extend the fingerprint invariant tests for the v0.3.0 surface

**Files:**
- Modify: `tests/test_capability_fingerprint_snapshot.py`
- Test: same

**Note:** This test does **not** pin a literal digest (the digest is volatile across sessions because the User-Agent embeds the version). It pins the fingerprint *shape* (64-hex) and *invariants* (tools/resources/error codes are inputs). The shape/invariant tests stay green under additive changes — so there is nothing to "refresh." This task only **extends** the invariant pins to cover the new surface and confirms the suite passes.

- [ ] **Step 1: Confirm the existing invariants still hold**

Run: `uv run pytest tests/test_capability_fingerprint_snapshot.py -v`
Expected: PASS (additive changes don't break shape/invariant/cross-surface-equality tests). If any FAIL, stop and reconcile before extending.

- [ ] **Step 2: Write the failing extension test**

Add to `tests/test_capability_fingerprint_snapshot.py`:

```python
def test_invalid_cursor_is_a_fingerprinted_error_code():
    from cwms_tools.core.errors import ErrorCode
    assert "invalid_cursor" in {c.value for c in ErrorCode}


def test_cli_contract_is_a_fingerprint_input():
    # Two different CLI contracts must yield different digests (M-2 closed).
    base = fingerprint.compute(tools={}, resources=[], cli_contract={"commands": []})
    changed = fingerprint.compute(tools={}, resources=[], cli_contract={"commands": [{"path": "x"}]})
    assert base != changed
```

- [ ] **Step 3: Run to verify it passes**

Run: `uv run pytest tests/test_capability_fingerprint_snapshot.py -v`
Expected: PASS (Task 1 added the code; Task 14 added the `cli_contract` param).

- [ ] **Step 4: Commit**

```bash
git add tests/test_capability_fingerprint_snapshot.py
git commit -m "test: pin invalid_cursor and CLI-contract as fingerprint inputs (v0.3.0)"
```

### Task 16: Version bump, CHANGELOG, README, full gate

**Files:**
- Modify: `pyproject.toml` (version), `src/cwms_tools/__init__.py` (if `__version__` is hard-coded there)
- Modify: `CHANGELOG.md`
- Modify: `README.md` (pagination + new flags)

- [ ] **Step 1: Bump the version**

In `pyproject.toml` set `version = "0.3.0"`. Check `src/cwms_tools/__init__.py` — if `__version__` is a literal, bump it too; if it reads from package metadata, leave it.

- [ ] **Step 2: Write the CHANGELOG entry**

Add a `## [0.3.0]` section to `CHANGELOG.md`.
- **Added/Changed:** cursor pagination on `search`/`browse` (MCP tools + CLI `--cursor`), history `next_begin` continuation, `invalid_cursor` error code, `openWorldHint`/`idempotentHint` annotations, per-tool latency metadata in capabilities + CLI schema, structured CLI `schema` (args/flags/error-catalog/latency), CLI contract folded into the fingerprint, explicit `ok: true` success discriminator.
- **Breaking surface:** the fingerprint value moves; tool input/output schemas gained fields. Bump is intentional and additive — cached clients re-walk.
- **Known limitations:** m-5 (completion for resource-template variables) **deferred** — FastMCP 3.3.1 exposes no completion hook; agents discover valid `{section_id}` values via the `cwms://overview` index. M-3 (`isError` on tool failures) remains framework-limited; the `ok` discriminator is the documented contract until FastMCP surfaces `isError` alongside `structuredContent`.

- [ ] **Step 3: Update the README**

In `README.md`: document `--cursor` in the command examples (add a paged `region browse` example and mention `next_begin` for history continuation). The exit-code table already maps usage/validation to `2`; if it lists error codes, add one line clarifying that the `invalid_cursor` error code maps to CLI exit code `2` (input-validation class), consistent with `ErrorCode.INVALID_CURSOR`.

- [ ] **Step 4: Run the full gate**

```bash
uv run ruff format && uv run ruff check --fix
uv run ty check
uv run pytest --cov=cwms_tools -q
uv run prek run --all-files
```

Expected: format clean, ty no errors, all tests pass, coverage ≥ 95% (add tests for any newly-uncovered branch — e.g. `limit=0` + `cursor` interaction, `decode_cursor` version mismatch), prek hooks green.

- [ ] **Step 5: Probe the built surface end-to-end**

```bash
uv run cwms-tools schema | python -m json.tool | head -40   # structured commands present (piped stdout => machine mode)
uv run cwms-tools fingerprint                                # new value, scope schema-contract
# --machine/--json are root-callback options: place them BEFORE the subcommand.
uv run cwms-tools --machine place search L -o NWDM --cursor BADTOKEN; echo "exit=$?"  # invalid_cursor, exit 2, stderr
```

Expected: structured schema; fingerprint emitted; the bad-cursor probe prints the `invalid_cursor` envelope to stderr with exit `2` and clean stdout.

- [ ] **Step 6: Commit and open the PR**

```bash
git add pyproject.toml src/cwms_tools/__init__.py CHANGELOG.md README.md
git commit -m "release: v0.3.0 — agent-friendliness remediation (M-1,M-2,C-1,m-4..m-7)"
git push -u origin feat/agent-friendliness-v0.3.0
gh pr create --fill --base main
```

---

## M-3 disposition (no separate task)

M-3 (tool errors cannot set protocol `isError: true` under FastMCP 3.3.1) is **addressed, not fully fixed**:
- The explicit `ok: true` (Task 2) + existing `ok: false` envelope gives a symmetric, documented discriminator, and `cwms://capabilities` already documents it.
- Add a one-line tracking note in the CHANGELOG and a `# TODO(fastmcp-isError)` comment in `mcp/tools.py::_safe` pointing at the MCP SDK `CallToolResult` (which *can* carry `structuredContent` + `isError`) so the fix lands when FastMCP exposes it. Do not raise `ToolError` instead — that would strip the structured error contract, which is worse.

---

## Self-Review

**Spec coverage:**
- M-1 → Tasks 3,4,5 (list cursor) + 6 (history `next_begin`) + 7,9 (MCP/CLI wiring). ✓
- M-2 → Task 14 (+ snapshot 15). ✓
- M-3 → disposition note + Task 2 (`ok:true`). ✓
- C-1 → Task 10. ✓
- m-4 → Task 11. ✓
- m-5 → Task 13: **deferred (documented)** — FastMCP 3.3.1 has no completion hook (verified live: `hasattr(FastMCP(...), "completion") is False`). The `cwms://overview` index is the agent's discovery path for `{section_id}`; capabilities now declares `completions.supported = false` with the reason. Tracked for the FastMCP upgrade that adds the hook.
- m-6 → Task 12. ✓
- m-7 → Task 2. ✓
- Release hygiene (version/CHANGELOG/README/gate) → Task 16. ✓

**Placeholder scan:** none. Task 13 is now a concrete doc-only deferral (no speculative private-API code). All other code steps contain real, applicable diffs verified against current source.

**Type consistency:** `cursor` is `str | None` everywhere (core, MCP tool param, CLI option). `next_cursor`/`has_more` named identically across `models.py`, `places.py`, and tests. `next_begin` consistent across `timeseries.py`, `values.py`, `models.py`. `cli_contract_payload()` is defined in Task 10 and consumed in Task 14 under the same name. `request_hash`/`validate_continuation`/`encode_cursor`/`decode_cursor` signatures match between `pagination.py` (Task 3) and call sites (Tasks 4,5).

**Residual risks:** (1) cursor robustness to mid-session catalog cache eviction is bounded by the `total`-in-cursor check (`ensure_total`), which converts drift into a clean `invalid_cursor` rather than a silent skip/dupe — acceptable; (2) coverage may dip on new branches (cursor decode/version-mismatch, `limit=0`+cursor, unparseable `next_begin` seam) — Task 16 step 4 budgets for top-up tests; (3) m-5 stays open until FastMCP ships a completion hook — deferral is documented in capabilities + CHANGELOG, with `cwms://overview` as the working fallback.
