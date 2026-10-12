# Milestone 2 (0.8.1 — first-try usability) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the 13 open issues in GitHub milestone 2 so that an agent holding only `tools/list` and the three discovery resources gets from a place name to a correct first data call, with an honest status classifier and error envelopes that repair themselves.

**Architecture:** Seven feature branches, each a PR into `main`, in dependency order. Behavior changes land first (search latency, cold-cache discovery, self-repairing resolution, status classifier, history validation); agent-facing prose lands last so it describes settled behavior. All logic changes go in `core/` producers so the CLI and MCP surfaces inherit them; every wire change gets a CLI↔MCP parity test, a CHANGELOG line, and an intentional fingerprint move.

**Tech Stack:** Python 3.12+, uv, FastMCP 4 (MCP SDK v2), Typer, Pydantic v2, pytest + `responses`, ruff, ty, prek.

**Spec:** The 13 GitHub issues in milestone 2 (`gh issue list --milestone 2`): #96, #161, #162, #163, #164, #165, #166, #167, #168, #169, #170, #171, #172. Design decisions were reviewed with Codex (amicus consult job `191280b7f0e541afbaf91a86644aa9a6`, 2026-10-12); the decisions and the deviations from the issue text are recorded in **Design decisions** below. A second Codex pass on this document (job `4a43a223fb2246c194b2411a742a72a2`) found 17 executable defects (shadowed `offices` name, unbounded directory fetch, ambiguous alias repairs, wrong depth-parent fixture, test seams that would go online, mock regexes that could not discriminate requests, stale assertions, overbroad staging); every one is folded into the tasks below.

## Global Constraints

- `uv` for everything (`uv run pytest`, `uv run ruff check`, `uv run ruff format`, `uv run ty check`, `prek run --all-files` before pushing). Never edit `uv.lock`.
- Conventional Commits; branch names `fix/…`, `docs/…`, `chore/…`; never commit to `main`; never merge a PR (Brian merges); push after each commit.
- Self-assign each issue before working it: `gh issue edit <n> --add-assignee @me`.
- CLI and MCP must agree field-for-field in `summary` and `full` detail. Shaping lives in `core/shaping.py`; serialization in `core/_compact.py`. Extend `tests/test_cli_mcp_parity.py` for every field change.
- No new runtime dependencies. No background cache pre-warming.
- Every agent-visible prose or schema change moves the capability fingerprint; that is expected. Snapshot tests pin *inputs* (code lists, URIs), never the hash.
- `tests/test_error_code_contract.py` must stay set-equal: every advertised `(tool, code)` has a provocation and vice versa. The CLI `schema` command (`cli/commands/schema.py`) mirrors per-command error codes and must be updated alongside `mcp/resources.py::TOOL_ERROR_CODES`.
- Mock CDA with `responses`; never hit live CDA in unit tests. The `integration` marker exists for live smoke tests.
- Coverage target 95%+.
- Stage explicitly: `git add -u` (modified tracked files) plus each new file by name. Never stage everything: the working tree carries unrelated untracked skill directories and an older plan. Inspect `git status --short` before every commit.
- Merge barrier: a PR whose tasks build on an earlier PR (PR 3 on PR 2; PR 4-7 on PR 3's `RepairIntent`/office validation where they touch error paths) is branched from `main` only after the earlier PR has merged. Brian merges; do not stack branches.
- Office-code validation (PR 3) is opt-in in unit tests: a `conftest.py` autouse fixture stubs the directory lookup to the degraded fallback, and tests that exercise validation request the `validating_directory` fixture. Keep unit tests offline.

## Design decisions (agreed with Codex; deviations from issue text called out)

1. **#169 first.** Measured on live CDA (cProfile, `search_places("Fort Peck", office="NWDM", limit=1)`): cold 164.8 s, of which network 6.9 s and `geo.co_located` 157.6 s (10,527 rows × 10,527 candidates = 112 M haversine calls); warm 160.4 s, network 0.05 s. Fix is an exact spatial hash on Earth-centered (ECEF) coordinates with cell = radius, then the existing haversine check. A lat/lon degree grid was rejected: at 48° N two points 89 m apart fall two 0.001° longitude cells apart. No in-process derived cache (keep `core/cache.py` the only cache). Membership logic in `_apply_parameter_filter` stays before pagination (promoted siblings change `total_count`).
2. **#161/#164.** Keep "omit `office` = cached scope only" (no implicit fan-out). Make `partial: true` + `repair_hint` fire whenever `offices_skipped_for_budget` is non-empty, regardless of result count; the hint never carries a cursor. Order the curated list by priority (NWDM, NWDP, SWT, MVS, LRN, MVR, …) so the first follow-up reaches the richest offices; label it a curated priority order. Fix the latent cache-key mismatch in `publishers_index` (`_office_has_cache` probes `("ts_catalog", office, "")` but `catalog.get_timeseries_catalog` writes `("ts_catalog", office, "", "no-extents")`), so cached offices are actually discovered.
3. **#96/#163/#170 together.** `ghost_location` goes live only for a *confirmed* catalog record with no timeseries; a name absent from the office catalog is `not_found{field:"name"}` with a case-insensitive or fuzzy suggestion. Office codes are validated once per top-level producer against the cached office directory; an exact case-insensitive match is normalized transparently (`swt` → `SWT`), an unknown code is `invalid_field{field:"office"}`; validation is skipped when the directory fell back (never block on a degraded list). Repairs preserve the requested operation: core attaches an internal `RepairIntent` (argument overrides), and each surface resolves it to a same-tool retry with the caller's own options (window, unit, rollup, detail, with_status). Deviation from #96: for a depth-string ghost the primary repair is the same tool on the shallowest data-bearing child, with `cwms_get_profile` named in `repair.alternative`, rather than switching tools (a `get_history` must not repair to a `get_profile`).
4. **#162.** Classify by the most severe *recognized* class among crossed thresholds (flood > action > watch > nominal), never by alphabetical or purely numeric order; floor-type levels (inactive, bottom, streambed, dead, minimum) contribute `nominal`; conservation ceilings (Top of Conservation / Normal / Multipurpose, Rule/Guide Curve, Bottom of Flood) contribute `watch`; `unknown` when only unrecognized levels are crossed or no levels exist. Thresholds are evaluated at the **observation timestamp**. Duplicate rows are multiple effective dates per `level_id` (verified live on SWT/FOSS: 24 rows, 13 ids, e.g. `1900-01-01` and `1961-02-13` revisions of the same level); keep one applicable revision per id. Seasonal levels were evaluated at the revision date (a 1961 window); pass the revision date as `effective-date` and the observation time as the query window.
5. **#167.** Reject timezone-less timestamps (`invalid_field`) and `end <= begin` (`usage_error`) in shared core helpers, before any upstream call. **#168** is description-only; no auto-rollup.
6. **#166.** Keep the field name `last_data_timestamp`; describe it as the catalog-reported time with its real fallback (`latest-time`, else `last-update`) and the lag. **#165** states the budget and the timeout behavior without promising that one retry succeeds.
7. **#171.** The first-call contract goes into server `instructions` and a fingerprinted `first_call` block in `cwms://capabilities`; `region_hints` is added to the shared `OfficesGuidance` model (it has `extra="forbid"`) and the static office guidance becomes an explicit fingerprint input.
8. **#172.** Items 1, 3, 4 ship in 0.8.1. Item 2 (renaming `repair_hint.args`/`reason`) is **deferred to 0.9.0** as a coherent migration (search + publisher + error repairs); 0.8.1 files the follow-up issue and reuses the existing `SearchRepairHint` shape for the new continuation chain.

## Review Focus

Inputs the issues imply but no task's tests exercise by default; each line names the owning task where its test is added.

1. A location name containing regex metacharacters or a comma (`UBLW_S1-D21,0ft`) on the `not_found`/suggestion path must not break the catalog `like` or the suggestion matcher (Task 10 test `test_suggest_location_handles_comma_and_regex_names`).
2. Two co-located records with identical coordinates in different offices must still be siblings, and a record with `latitude: None` must be neither a sibling nor crash the index (Task 1 test `test_co_located_index_skips_rows_without_coordinates`).
3. A threshold list where two recognized levels share one value (Top of Conservation == Top of Normal) must classify deterministically (Task 17 test `test_classify_equal_valued_levels_is_deterministic`).
4. `begin_iso` with a `+05:30` offset and `end_iso` in `Z` that denote the same instant must be rejected as `usage_error`, not accepted as a one-point window (Task 19 test `test_history_rejects_equal_instants_across_offsets`).
5. An office code that is valid but *not* in the curated discovery list (e.g. `SPK`) must validate as known and must not be reported as skipped by the cold-cache repair chain (Task 11 test `test_require_known_office_accepts_directory_office_outside_curated_list`).

---

## PR 1 — `fix/169-search-latency` (#169)

### Task 0: Branch and assign

- [ ] **Step 0.1**

```bash
cd /Users/bdc/projects/cwms-tools && git checkout main && git pull
git checkout -b fix/169-search-latency
gh issue edit 169 --add-assignee @me
```

### Task 1: `geo.co_located_index` — exact spatial hash

**Files:**
- Modify: `src/cwms_tools/core/geo.py`
- Test: `tests/test_geo.py`

**Interfaces:**
- Produces: `geo.co_located_index(points: list[GeoPoint], *, radius_m: float = 100.0) -> dict[tuple[str, str], list[GeoPoint]]` — for every point, the other points within `radius_m` great-circle distance, in input order; identical to calling `co_located(p, points, radius_m=radius_m)` for each `p`.

- [ ] **Step 1.1: Write the failing tests**

Append to `tests/test_geo.py`:

```python
import random


def _brute(points, radius_m):
    return {(p.office_id, p.name): geo.co_located(p, points, radius_m=radius_m) for p in points}


def test_co_located_index_matches_brute_force_on_random_cluster() -> None:
    rng = random.Random(1234)
    pts = [
        geo.GeoPoint("X", f"P{i}", 48.0 + rng.uniform(-0.003, 0.003), -100.0 + rng.uniform(-0.005, 0.005))
        for i in range(300)
    ]
    assert geo.co_located_index(pts, radius_m=100.0) == _brute(pts, 100.0)


def test_co_located_index_catches_pair_that_straddles_degree_grid_cells() -> None:
    """Codex counterexample for a 0.001-degree grid: 89 m apart at 48 N but two
    longitude cells apart. The ECEF hash must still pair them."""
    a = geo.GeoPoint("X", "A", 48.0, -100.0001)
    b = geo.GeoPoint("X", "B", 48.0, -99.9989)
    assert geo.haversine_m(a.latitude, a.longitude, b.latitude, b.longitude) < 100.0
    idx = geo.co_located_index([a, b], radius_m=100.0)
    assert [p.name for p in idx[("X", "A")]] == ["B"]
    assert [p.name for p in idx[("X", "B")]] == ["A"]


@pytest.mark.parametrize(
    "lat,lon",
    [(0.0, 0.0), (0.0, 179.9999), (0.0, -179.9999), (89.9, 10.0), (-89.9, -10.0)],
)
def test_co_located_index_matches_brute_force_at_antimeridian_and_poles(lat, lon) -> None:
    pts = [
        geo.GeoPoint("X", "A", lat, lon),
        geo.GeoPoint("X", "B", lat + 0.0005, lon),
        geo.GeoPoint("X", "C", lat, lon - 0.0004 if lon > 0 else lon + 0.0004),
        geo.GeoPoint("X", "D", lat - 0.05, lon),
    ]
    assert geo.co_located_index(pts, radius_m=100.0) == _brute(pts, 100.0)


def test_co_located_index_preserves_input_order_and_excludes_self() -> None:
    pts = [
        geo.GeoPoint("NWDM", "BECR", 39.6331, -105.2931),
        geo.GeoPoint("NWO", "BECR", 39.633, -105.293),
        geo.GeoPoint("NWD", "BECR", 39.6332, -105.2932),
        geo.GeoPoint("SWT", "FOSS", 35.55, -98.97),
    ]
    idx = geo.co_located_index(pts)
    assert [p.office_id for p in idx[("NWO", "BECR")]] == ["NWDM", "NWD"]
    assert idx[("SWT", "FOSS")] == []


def test_co_located_index_skips_rows_without_coordinates() -> None:
    """Review focus #2: callers filter None coordinates before building GeoPoints;
    the index must accept an empty list and a single point without error."""
    assert geo.co_located_index([]) == {}
    only = geo.GeoPoint("X", "A", 1.0, 1.0)
    assert geo.co_located_index([only]) == {("X", "A"): []}
```

Add `import pytest` at the top of `tests/test_geo.py`.

- [ ] **Step 1.2: Run to verify failure**

Run: `uv run pytest tests/test_geo.py -q`
Expected: FAIL with `AttributeError: module 'cwms_tools.core.geo' has no attribute 'co_located_index'`.

- [ ] **Step 1.3: Implement**

Add to `src/cwms_tools/core/geo.py` after `co_located`:

```python
def _ecef_m(lat: float, lon: float) -> tuple[float, float, float]:
    """Earth-centered, Earth-fixed coordinates in meters on the mean sphere."""
    phi, lam = math.radians(lat), math.radians(lon)
    cos_phi = math.cos(phi)
    return (
        EARTH_RADIUS_M * cos_phi * math.cos(lam),
        EARTH_RADIUS_M * cos_phi * math.sin(lam),
        EARTH_RADIUS_M * math.sin(phi),
    )


def co_located_index(
    points: list[GeoPoint],
    *,
    radius_m: float = 100.0,
) -> dict[tuple[str, str], list[GeoPoint]]:
    """Every point's co-located siblings in one pass: `{(office_id, name): [...]}`.

    Equivalent to `co_located(p, points, radius_m=radius_m)` for each `p` —
    same members, same (input) order — but O(N + candidate pairs) instead of
    O(N²) (#169: a 10 k-row office took 160 s under the pairwise form).

    Points are hashed into a cubic grid over ECEF coordinates with cell size
    `radius_m`. The chord between two points is never longer than their
    great-circle distance, so any pair within `radius_m` along the surface is
    within `radius_m` on every axis and lands in the same or an adjacent cell.
    Only those 27 cells are compared, then the exact haversine test runs as
    before. A degree grid was rejected because its longitude cells shrink
    with latitude; this hash has no latitude, antimeridian, or pole cases.
    """
    cell = float(radius_m)
    keys: list[tuple[int, int, int]] = []
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for i, p in enumerate(points):
        x, y, z = _ecef_m(p.latitude, p.longitude)
        key = (math.floor(x / cell), math.floor(y / cell), math.floor(z / cell))
        keys.append(key)
        buckets.setdefault(key, []).append(i)

    out: dict[tuple[str, str], list[GeoPoint]] = {}
    for i, p in enumerate(points):
        kx, ky, kz = keys[i]
        hits: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in buckets.get((kx + dx, ky + dy, kz + dz), ()):
                        if j == i:
                            continue
                        q = points[j]
                        if q.office_id == p.office_id and q.name == p.name:
                            continue
                        if haversine_m(p.latitude, p.longitude, q.latitude, q.longitude) <= radius_m:
                            hits.append(j)
        hits.sort()  # input order, matching `co_located`
        out[(p.office_id, p.name)] = [points[j] for j in hits]
    return out
```

Add `"co_located_index"` to `__all__`.

- [ ] **Step 1.4: Run tests**

Run: `uv run pytest tests/test_geo.py -q`
Expected: PASS.

- [ ] **Step 1.5: Commit**

```bash
git add src/cwms_tools/core/geo.py tests/test_geo.py
git commit -m "perf(geo): add exact ECEF spatial-hash co-location index (#169)"
git push -u origin fix/169-search-latency
```

### Task 2: Use the index in `catalog.enrich_locations`

**Files:**
- Modify: `src/cwms_tools/core/catalog.py:301-334`
- Test: `tests/test_catalog.py`

- [ ] **Step 2.1: Write the failing test**

Append to `tests/test_catalog.py` (reuse the file's `configured`/`mocked` fixtures and `API_ROOT`):

```python
def test_enrich_locations_builds_co_location_in_one_pass(configured, mocked, monkeypatch) -> None:
    """#169: enrichment must not call the pairwise `co_located` per row."""
    from cwms_tools.core import geo

    rows = [
        {"office-id": "SWT", "name": f"S{i}", "latitude": 35.0 + i * 0.0001, "longitude": -98.0}
        for i in range(50)
    ]
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": rows}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    calls = {"pairwise": 0}
    real = geo.co_located

    def counting(*a, **k):
        calls["pairwise"] += 1
        return real(*a, **k)

    monkeypatch.setattr(catalog, "co_located", counting, raising=False)
    monkeypatch.setattr(geo, "co_located", counting)
    enriched = catalog.enrich_locations("SWT")
    assert calls["pairwise"] == 0
    # ~11 m spacing: every row is within 100 m of its 9 nearest neighbours.
    assert "S1" in next(r for r in enriched if r["name"] == "S0")["co_located"]
```

Check how `tests/test_catalog.py` imports `catalog` and `responses` (top of file) and match it.

- [ ] **Step 2.2: Run to verify failure**

Run: `uv run pytest tests/test_catalog.py -k one_pass -q`
Expected: FAIL (`calls["pairwise"] == 50`).

- [ ] **Step 2.3: Implement**

In `src/cwms_tools/core/catalog.py`, change the import `from cwms_tools.core.geo import GeoPoint, co_located` to `from cwms_tools.core.geo import GeoPoint, co_located_index`, and in `enrich_locations` replace

```python
    geopoints: list[GeoPoint] = [g for g in (_to_geopoint(r) for r in rows) if g is not None]
    geopoint_index = {(g.office_id, g.name): g for g in geopoints}
```

with

```python
    geopoints: list[GeoPoint] = [g for g in (_to_geopoint(r) for r in rows) if g is not None]
    # One-pass sibling index (#169): the pairwise form was O(N²) and took
    # 160 s on NWDM's 10 k rows even with the catalog on disk.
    siblings_by_key = co_located_index(geopoints)
```

and replace

```python
        target_gp = geopoint_index.get((office_id, name))
        siblings = (
            [g.name for g in co_located(target_gp, geopoints)] if target_gp is not None else []
        )
```

with

```python
        siblings = [g.name for g in siblings_by_key.get((office_id, name), [])]
```

- [ ] **Step 2.4: Run the catalog and places suites**

Run: `uv run pytest tests/test_catalog.py tests/test_places.py -q`
Expected: PASS (existing `test_enrich_locations_detects_co_located_siblings` still green).

- [ ] **Step 2.5: Commit**

```bash
git add src/cwms_tools/core/catalog.py tests/test_catalog.py
git commit -m "perf(catalog): build co-located siblings with the spatial index (#169)"
git push
```

### Task 3: Measure, relabel latency honestly, record the breakdown

**Files:**
- Create: `scripts/profile_search.py` (dev-only; not shipped)
- Modify: `src/cwms_tools/mcp/resources.py:100-111` (`TOOL_LATENCY`), `CHANGELOG.md`
- Test: `tests/test_capability_fingerprint_snapshot.py` (only if a latency label changes)

- [ ] **Step 3.1: Add the profiling script**

```python
"""Profile a cold and a warm office search against live CDA (#169). Dev-only.

Usage: uv run python scripts/profile_search.py NWDM "Fort Peck"
"""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path

from cwms_tools.core import places, session
from cwms_tools.core.cache import Cache, set_cache


def main(office: str, query: str) -> None:
    session.configure_session()
    set_cache(Cache(directory=Path(tempfile.mkdtemp(prefix="cwms-profile-"))))
    for label in ("cold", "warm"):
        prof = cProfile.Profile()
        t0 = time.perf_counter()
        prof.enable()
        resp = places.search_places(query, office=office, limit=1)
        prof.disable()
        print(f"== {label}: {time.perf_counter() - t0:.1f}s total_count={resp['total_count']}")
        out = io.StringIO()
        pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(12)
        print(out.getvalue())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

- [ ] **Step 3.2: Run it and record the numbers**

Run: `uv run python scripts/profile_search.py NWDM "Fort Peck"`
Expected: cold dominated by network (≈7 s on 2026-10-12), warm well under 2 s. Paste both totals and the top three cumulative rows into a comment on issue #169 (`gh issue comment 169 --body-file -`), alongside the pre-fix numbers (cold 164.8 s / warm 160.4 s, 112,342,100 haversine calls).

- [ ] **Step 3.3: Decide the latency label**

If warm NWDM search is under ~2 s and cold is network-bound, leave `TOOL_LATENCY` as `network`. If cold NWDM is still over ~10 s, change `cwms_search_places` and `cwms_browse_region` to `"slow"` in `TOOL_LATENCY` and note it in the CHANGELOG (fingerprint moves either way only if the label changes).

- [ ] **Step 3.4: CHANGELOG**

Under `## [Unreleased]` add a `### Fixed` section with:

```markdown
- `cwms_search_places` / `cwms_browse_region` (and the CLI twins) no longer spend
  minutes on large offices: co-located siblings are now found with a one-pass
  spatial index instead of a pairwise O(N²) scan. Measured on NWDM (10,527
  locations) a warm search dropped from 160 s to under 2 s; the cold cost is the
  catalog download itself (≈7 s) (#169).
```

- [ ] **Step 3.5: Full gate, commit, open the PR**

```bash
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q
prek run --all-files
git add scripts/profile_search.py CHANGELOG.md src/cwms_tools/mcp/resources.py
git commit -m "perf(search): record #169 profile and latency labels"
git push
gh pr create --fill --title "perf: one-pass co-location index for search/browse (#169)" --body "Closes #169. See CHANGELOG. Cold/warm profile before and after is on the issue.

🤖 Generated with Claude Code

https://claude.ai/code/session_015kfeiw3yomoQw34HgijkLw"
```

Report the PR URL; wait for CI and Copilot review per project norms; do not merge.

---

## PR 2 — `fix/161-search-cold-cache` (#161, #164)

### Task 4: Branch; curated office list becomes a priority order; one copy of it

**Files:**
- Modify: `src/cwms_tools/core/offices.py:87-111, 203-218`
- Modify: `src/cwms_tools/core/publishers_index.py:31-83` (delete `_KNOWN_LIKELY_OFFICES` and the `_l2.iterkeys` probe)
- Test: `tests/test_offices.py`

**Interfaces:**
- Produces: `offices.DISCOVERY_OFFICE_PRIORITY: tuple[str, ...]` (public constant) and `offices.discovery_office_candidates() -> list[str]` now returns that tuple in priority order (not sorted).

- [ ] **Step 4.1: Branch and assign**

```bash
git checkout main && git pull && git checkout -b fix/161-search-cold-cache
gh issue edit 161 --add-assignee @me && gh issue edit 164 --add-assignee @me
```

- [ ] **Step 4.2: Write the failing test**

Append to `tests/test_offices.py`:

```python
def test_discovery_office_candidates_is_a_priority_order() -> None:
    """#161: a fresh server follows the repair chain with a fan-out budget of
    ceil(workers/2) (4 by default). The first four must be the regional rollups
    and the two district offices with the most published projects so both
    reviewer failures (Fort Peck at NWDM, Foss at SWT) resolve on the first
    follow-up call — not an alphabetical slice (LRB, LRC, LRE, LRH)."""
    cands = offices.discovery_office_candidates()
    assert cands[:4] == ["NWDM", "NWDP", "SWT", "MVS"]
    assert cands == list(offices.DISCOVERY_OFFICE_PRIORITY)
    assert len(cands) == len(set(cands))
    # The degraded office-directory fallback keeps a stable sorted shape.
    assert [r["name"] for r in offices._fallback_records()] == sorted(cands)
```

- [ ] **Step 4.3: Run to verify failure**

Run: `uv run pytest tests/test_offices.py -k priority_order -q`
Expected: FAIL (`cands[:4] == ['LRB', 'LRC', 'LRE', 'LRH']`).

- [ ] **Step 4.4: Implement**

In `src/cwms_tools/core/offices.py` replace the `_FALLBACK_OFFICES` block (lines 87-111) with:

```python
#: Curated data-bearing offices in *priority order*: the regional rollups
#: first, then district-publishing offices roughly by how many published
#: projects they carried in the 2026-10-11 survey (cwms-overview.md §8:
#: NWDM 109, NWDP 95, SWT 49, LRN 28, MVR 25, MVS 15), then the remaining
#: MVD/SWD/LRD districts. This is a curated priority, not a measurement of
#: richness. It is the fan-out order of the name-first repair chain (#161),
#: the implicit candidate set `publishers_for_parameter` probes for cached
#: offices (#164), and the degraded office-directory fallback. One copy:
#: `publishers_index` used to carry a duplicate.
DISCOVERY_OFFICE_PRIORITY: tuple[str, ...] = (
    "NWDM",
    "NWDP",
    "SWT",
    "MVS",
    "LRN",
    "MVR",
    "MVP",
    "MVK",
    "MVM",
    "MVN",
    "SWL",
    "SWG",
    "SWF",
    "LRL",
    "LRH",
    "LRP",
    "LRB",
    "LRC",
    "LRE",
)
_FALLBACK_OFFICES: tuple[str, ...] = DISCOVERY_OFFICE_PRIORITY
```

Replace `discovery_office_candidates` (lines 203-213) body with `return list(DISCOVERY_OFFICE_PRIORITY)` and update its docstring: "…in curated priority order (see `DISCOVERY_OFFICE_PRIORITY`); the fan-out budget takes the first N on a cold start." Keep `_fallback_records` and `cached_offices_for_locations` sorted (they already call `sorted(...)`). Add `"DISCOVERY_OFFICE_PRIORITY"` to `__all__`.

In `src/cwms_tools/core/publishers_index.py`: delete `_KNOWN_LIKELY_OFFICES` (lines 60-83) and replace `_cached_offices` (lines 31-57) with:

```python
def _cached_offices() -> set[str]:
    """Offices whose unfiltered ts_catalog is already on disk.

    diskcache keys are hashed, so we cannot enumerate them; probe the curated
    candidate set (`offices.discovery_office_candidates()`) instead. Only
    offices that are *actually* cached come back — nothing is fetched here.
    """
    return {o for o in offices.discovery_office_candidates() if _office_has_cache(o)}
```

and add `from cwms_tools.core import catalog, concurrency, offices, publishers` to the imports (replacing the existing import line).

- [ ] **Step 4.5: Run tests**

Run: `uv run pytest tests/test_offices.py tests/test_publishers_index.py tests/test_places.py -q`
Expected: PASS (`test_search_places_empty_scope_returns_repair_hint` compares against the function, so order is not pinned there).

- [ ] **Step 4.6: Commit**

```bash
git add src/cwms_tools/core/offices.py src/cwms_tools/core/publishers_index.py tests/test_offices.py
git commit -m "fix(offices): curated discovery list is a priority order with one copy (#161, #164)"
git push -u origin fix/161-search-cold-cache
```

### Task 5: Fix the ts_catalog cache-presence probe (#164, latent bug found by Codex)

**Files:**
- Modify: `src/cwms_tools/core/catalog.py:72-120`
- Modify: `src/cwms_tools/core/publishers_index.py` (`_office_has_cache`)
- Test: `tests/test_publishers_index.py`

**Interfaces:**
- Produces: `catalog.ts_catalog_cached(office_id: str, *, like: str | None = None, include_extents: bool = False) -> bool` — true iff `get_timeseries_catalog` with the same arguments would be a cache hit.

- [ ] **Step 5.1: Write the failing test**

Append to `tests/test_publishers_index.py`:

```python
def test_publishers_for_parameter_discovers_offices_indexed_through_the_producer(
    configured,
) -> None:
    """#164 (Codex finding): `_office_has_cache` probed the key
    ('ts_catalog', office, '') but the catalog writes ('ts_catalog', office,
    '', 'no-extents'), so a genuinely indexed office was never seen as cached.
    Index SWT explicitly, then ask with `offices` omitted: SWT must be found
    from cache with NO second upstream call."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"),
            status=200,
        )
        publishers_index.publishers_for_parameter("Elev", offices=["SWT"])
        assert len(mocked.calls) == 1
        payload = publishers_index.publishers_for_parameter("Elev")
        assert len(mocked.calls) == 1, "cached office must not be refetched"
    assert payload["coverage"]["offices_requested"] == ["SWT"]
    assert payload["coverage"]["offices_indexed"] == ["SWT"]
    assert payload["coverage"]["complete"] is True
```

- [ ] **Step 5.2: Run to verify failure**

Run: `uv run pytest tests/test_publishers_index.py -k through_the_producer -q`
Expected: FAIL (`offices_requested == []`).

- [ ] **Step 5.3: Implement**

In `src/cwms_tools/core/catalog.py`, factor the key out of `get_timeseries_catalog`:

```python
def _ts_catalog_key(office_id: str, *, like: str | None, include_extents: bool) -> str:
    cfg = current_config()
    return build_cache_key(
        "ts_catalog",
        office_id,
        like or "",
        "extents" if include_extents else "no-extents",
        api_root=cfg.api_root,
    )


def ts_catalog_cached(
    office_id: str, *, like: str | None = None, include_extents: bool = False
) -> bool:
    """True iff `get_timeseries_catalog(office_id, like=, include_extents=)` is a cache hit.

    The single place that knows the ts_catalog key shape — probes elsewhere
    (`publishers_index`) drifted from it once (#164)."""
    return get_cache().get(_ts_catalog_key(office_id, like=like, include_extents=include_extents)) is not None
```

and make `get_timeseries_catalog` use `key = _ts_catalog_key(office_id, like=like, include_extents=include_extents)` (delete its inline `build_cache_key(...)` call and the now-unused `cfg` local). Add `"ts_catalog_cached"` to `__all__`.

In `publishers_index.py` replace `_office_has_cache` with:

```python
def _office_has_cache(office: str) -> bool:
    """Cheap check: is the office's unfiltered ts_catalog already on disk?"""
    return catalog.ts_catalog_cached(office)
```

and remove the now-unused `build_cache_key`/`current_config` imports if nothing else uses them (ruff will say).

- [ ] **Step 5.4: Run tests**

Run: `uv run pytest tests/test_publishers_index.py tests/test_catalog.py -q`
Expected: PASS.

- [ ] **Step 5.5: Commit**

```bash
git add src/cwms_tools/core/catalog.py src/cwms_tools/core/publishers_index.py tests/test_publishers_index.py
git commit -m "fix(publishers): probe the real ts_catalog cache key so indexed offices are discovered (#164)"
git push
```

### Task 6: `search_places` — followable repair chain whenever offices were skipped (#161)

**Files:**
- Modify: `src/cwms_tools/core/places.py:181-233, 297-319`
- Test: `tests/test_places.py`

**Interfaces:**
- Produces: on any search where `offices_skipped_for_budget` is non-empty, `partial: true`, a `partial_reasons` entry starting `offices_skipped_for_budget:`, and `repair_hint = {reason: "offices_skipped_for_budget", message, tool: "cwms_search_places", args: {query, office: <skipped list>[, parameter]}}` (no `cursor`, no `limit`).

- [ ] **Step 6.1: Write the failing tests**

Append to `tests/test_places.py`:

```python
def test_search_places_skipped_offices_always_yield_partial_and_a_followable_hint(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#161: the reviewer's second call searched 4 of 19 offices, got
    `partial:false` and no hint, and concluded Fort Peck was not in CWMS. Any
    budget-skipped office must set partial and hand back exactly the skipped
    list as the next `office` argument — regardless of whether rows came back."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=LOCATIONS_PAYLOAD, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TIMESERIES_PAYLOAD, status=200)
    payload = places.search_places("FOSS", office=["SWT", "NWDM", "NWDP"], parameter="Elev")
    assert payload["results"], "rows came back from the searched office"
    assert payload["offices_skipped_for_budget"] == ["NWDM", "NWDP"]
    assert payload["partial"] is True
    assert any(r.startswith("offices_skipped_for_budget:") for r in payload["partial_reasons"])
    hint = payload["repair_hint"]
    assert hint["reason"] == "offices_skipped_for_budget"
    assert hint["tool"] == "cwms_search_places"
    assert hint["args"] == {"query": "FOSS", "office": ["NWDM", "NWDP"], "parameter": "Elev"}


def test_search_places_cold_chain_reaches_every_curated_office(
    configured, mocked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow the hint chain from a cold cache until no office is skipped: every
    curated office must be searched exactly once, richest first."""
    monkeypatch.setattr(places, "_fanout_budget", lambda: 4)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json={"locations": []}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    first = places.search_places("Fort Peck")
    assert first["results"] == [] and first["partial"] is True
    assert first["repair_hint"]["reason"] == "no_offices_in_scope"
    searched: list[str] = []
    hint = first["repair_hint"]
    for _ in range(10):
        resp = places.search_places(**hint["args"])
        searched.extend(resp["offices_searched"])
        if "repair_hint" not in resp:
            assert resp["partial"] is False
            break
        hint = resp["repair_hint"]
        assert hint["reason"] == "offices_skipped_for_budget"
        assert "cursor" not in hint["args"] and "limit" not in hint["args"]
    assert searched[:4] == ["NWDM", "NWDP", "SWT", "MVS"]
    assert searched == offices.discovery_office_candidates()


def test_search_places_no_offices_in_scope_reason_does_not_tell_caller_to_omit_office(
    configured, mocked
) -> None:
    payload = places.search_places("Fort Peck")
    reason = next(r for r in payload["partial_reasons"] if r.startswith("no_offices_in_scope"))
    assert "omit" not in reason
    assert "repair_hint" in reason
```

- [ ] **Step 6.2: Run to verify failure**

Run: `uv run pytest tests/test_places.py -k "followable_hint or cold_chain or tell_caller" -q`
Expected: FAIL (`partial` missing / `repair_hint` missing / reason says "omit").

- [ ] **Step 6.3: Implement**

In `src/cwms_tools/core/places.py`:

1. In `_run_fanout` change the reason text to:

```python
    if not requested:
        reasons.append(
            "no_offices_in_scope: no office catalog is cached yet; follow `repair_hint` "
            "or pass `office` explicitly"
        )
    if skipped:
        reasons.append(
            f"offices_skipped_for_budget: {len(skipped)} office(s) not searched this call; "
            "follow `repair_hint` to continue"
        )
```

2. In `search_places`, replace the final hint block (lines 202-208) with:

```python
    # Name-first dead-end recovery (#24): only when the caller OMITTED `office`
    # and the implicit cached scope was empty. An explicit empty list is a
    # deliberate "search nothing" — never widen it.
    if office is None and decoded is None and not offices_searched:
        response["repair_hint"] = _empty_scope_repair_hint(query, parameter)
    # Budget continuation (#161): whenever offices were skipped — rows or no
    # rows — the chain must stay followable to exhaustion. The hint restarts a
    # fresh search over the skipped offices (no cursor: a cursor locks the
    # office set it was minted for).
    elif offices_skipped and decoded is None:
        response["repair_hint"] = _skipped_scope_repair_hint(query, parameter, offices_skipped)
    return response


def _skipped_scope_repair_hint(
    query: str, parameter: str | None, skipped: list[str]
) -> dict[str, Any]:
    args: dict[str, Any] = {"query": query, "office": list(skipped)}
    if parameter is not None:
        args["parameter"] = parameter
    return {
        "reason": "offices_skipped_for_budget",
        "message": (
            f"{len(skipped)} office(s) were not searched because of the per-call fan-out "
            "budget. Retry with `office` set to exactly this list to continue; repeat "
            "until `offices_skipped_for_budget` is empty."
        ),
        "tool": "cwms_search_places",
        "args": args,
    }
```

(`partial`/`partial_reasons` already flip to true because `_run_fanout` now appends a reason whenever `skipped` is non-empty.) Also make `_empty_scope_repair_hint`'s message say "No office catalog is cached yet, so nothing was searched. Retry with `office` set to this curated list (priority order; the budget takes the first few per call) and keep following `repair_hint` until `offices_skipped_for_budget` is empty."

3. Update the `search_places` docstring paragraph about `repair_hint` to describe both triggers.

- [ ] **Step 6.4: Run tests**

Run: `uv run pytest tests/test_places.py tests/test_mcp_tool_handlers.py tests/test_cli_place.py -q`
Expected: PASS. `test_search_places_caps_uncached_office_fanout_by_budget` and `test_search_places_skipped_offices_coexist_with_truncated_false` still pass (they do not assert `partial`).

- [ ] **Step 6.5: Commit**

```bash
git add src/cwms_tools/core/places.py tests/test_places.py
git commit -m "fix(search): keep the cold-cache repair chain followable to exhaustion (#161)"
git push
```

### Task 7: `publishers_for_parameter` — honest `complete`, cold-cache repair (#164)

**Files:**
- Modify: `src/cwms_tools/core/publishers_index.py:86-184`
- Modify: `src/cwms_tools/core/models.py` (`PublishersCoverage.complete` description)
- Test: `tests/test_publishers_index.py`

- [ ] **Step 7.1: Write the failing tests**

```python
def test_publishers_for_parameter_cold_cache_is_not_complete_and_names_curated_offices(
    configured,
) -> None:
    """#164: on a fresh server with `offices` omitted the index answers from
    nothing; `complete:true` read as 'nobody publishes Elev'."""
    payload = publishers_index.publishers_for_parameter("Elev")
    cov = payload["coverage"]
    assert cov["offices_requested"] == [] and cov["offices_indexed"] == []
    assert cov["complete"] is False
    repair = payload["repair"]
    assert repair["next_step"] == "index_curated_offices"
    assert repair["tool"] == "cwms_publishers_for_parameter"
    assert repair["arguments"] == {
        "parameter": "Elev",
        "offices": offices.discovery_office_candidates(),
    }


def test_publishers_for_parameter_complete_requires_an_indexed_office(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", status=500, body="err")
        payload = publishers_index.publishers_for_parameter("Elev", offices=["SWT"])
    assert payload["coverage"]["offices_indexed"] == []
    assert payload["coverage"]["complete"] is False
```

Add `from cwms_tools.core import offices, publishers_index, session` to the test imports.

- [ ] **Step 7.2: Run to verify failure**

Run: `uv run pytest tests/test_publishers_index.py -k "cold_cache or requires_an_indexed" -q`
Expected: first FAILS on `complete is False`; second passes already (error_skipped) — keep it as a pin.

- [ ] **Step 7.3: Implement**

In `publishers_for_parameter`:

```python
    complete = bool(indexed) and not skipped and not error_skipped
    ...
    repair = None
    if skipped:
        repair = RepairHint(
            next_step="retry_skipped_offices",
            tool="cwms_publishers_for_parameter",
            arguments={"parameter": parameter, "offices": skipped},
        ).model_dump(mode="json")
    elif offices is None and not requested:
        # Cold cache with `offices` omitted (#164): nothing is indexed yet.
        # Mirror `cwms_search_places`'s curated starting set.
        repair = RepairHint(
            next_step="index_curated_offices",
            tool="cwms_publishers_for_parameter",
            arguments={
                "parameter": parameter,
                "offices": offices_mod.discovery_office_candidates(),
            },
        ).model_dump(mode="json")
```

(The parameter is named `offices`, so import the module as `from cwms_tools.core import offices as offices_mod` — or rename the module alias consistently; ruff/ty must be clean.)

Update the docstring: "`offices=None` means the offices already in cache; on a cold cache that is nothing, `complete` is false and `repair` names the curated list to index first."

In `core/models.py` give `PublishersCoverage.complete` a `Field(description="True only when at least one office was indexed and none were skipped (budget or error). False on a cold cache with `offices` omitted — follow `repair`.")`.

- [ ] **Step 7.4: Run tests**

Run: `uv run pytest tests/test_publishers_index.py tests/test_cli_mcp_parity.py tests/test_mcp_tool_handlers.py -q`
Expected: PASS.

- [ ] **Step 7.5: Commit**

```bash
git add src/cwms_tools/core/publishers_index.py src/cwms_tools/core/models.py tests/test_publishers_index.py
git commit -m "fix(publishers): coverage.complete is false on a cold cache and names the curated offices (#164)"
git push
```

### Task 8: Descriptions, parity test, CHANGELOG, PR

**Files:**
- Modify: `src/cwms_tools/mcp/tools.py:189-194, 653-657`
- Modify: `src/cwms_tools/cli/commands/place.py:64-80`, `src/cwms_tools/cli/commands/publisher.py:33-41`
- Modify: `src/cwms_tools/cli/commands/schema.py` (`--office` help on `place search` and `publisher for-parameter`)
- Modify: `src/cwms_tools/core/models.py` (`SearchPlacesResponse.repair_hint` description)
- Test: `tests/test_cli_mcp_parity.py`

- [ ] **Step 8.1: Write the failing parity test**

Append to `tests/test_cli_mcp_parity.py`:

```python
def test_search_places_budget_repair_hint_parity_real_producer(configured, monkeypatch) -> None:
    """#161: the continuation hint must reach both surfaces identically."""
    _stub_catalog(monkeypatch)
    monkeypatch.setattr(places, "_fanout_budget", lambda: 1)
    argv = ["place", "search", "FOSS", "-o", "SWT", "-o", "NWDM", "-o", "NWDP"]
    cli_data = _cli_data(SEARCH, argv)
    mcp_data = _mcp_data("cwms_search_places", {"query": "FOSS", "office": ["SWT", "NWDM", "NWDP"]})
    assert cli_data == mcp_data
    assert cli_data["partial"] is True
    assert cli_data["repair_hint"]["args"]["office"] == ["NWDM", "NWDP"]
```

Note: `_stub_catalog` stubs `enrich_locations` so no office is "cached" on disk; the budget of 1 searches SWT only. If `_location_catalog_cached` makes all three uncached, the first (SWT) consumes the budget — assert matches.

Also append the cold-cache publisher repair parity (#164), driven through the real producer with an empty cache:

```python
def test_publishers_cold_cache_repair_parity_real_producer(configured) -> None:
    cli_data = _cli_data(PUBLISHERS, ["publisher", "for-parameter", "Elev"])
    mcp_data = _mcp_data("cwms_publishers_for_parameter", {"parameter": "Elev"})
    assert cli_data == mcp_data
    assert cli_data["coverage"]["complete"] is False
    assert cli_data["repair"]["next_step"] == "index_curated_offices"
```

- [ ] **Step 8.2: Run to verify failure, then implement prose**

Run: `uv run pytest tests/test_cli_mcp_parity.py -k budget_repair -q` — expected to pass already if Task 6 is correct (it is a pin, not a red test; confirm it passes).

MCP `office` param on `cwms_search_places` (tools.py):

```python
            "Office code(s). If unknown, omit it: on a fresh server the first call "
            "returns no rows and a `repair_hint`; keep following `repair_hint` until "
            "`offices_skipped_for_budget` is empty. If you know the region, pass the "
            "office directly (NWDM Missouri mainstem, NWDP Columbia/Snake, SWT "
            "Oklahoma, MVS St. Louis; full list at `cwms://offices`).",
```

MCP `offices` on `cwms_publishers_for_parameter`:

```python
            "Office codes to index. Omit to answer from already-cached offices only "
            "— on a cold start that is nothing (`coverage.complete:false`); follow "
            "`repair` to index the curated data-bearing offices.",
```

CLI `--office` help on `place search` and `publisher for-parameter` and the matching `schema.py` `_opt` help strings: same content in CLI phrasing (`--office`, `cwms-tools offices`). `SearchPlacesResponse.repair_hint` description: "Set when nothing was in scope (`reason: no_offices_in_scope`) or offices were budget-skipped (`reason: offices_skipped_for_budget`); pass `args` back verbatim to continue."

- [ ] **Step 8.3: CHANGELOG**

Under `## [Unreleased]` → `### Fixed`:

```markdown
- `cwms_search_places` / `place search`: a cold-cache name-first search is now
  followable to exhaustion. Whenever offices are skipped by the fan-out budget
  the response sets `partial: true` and carries a `repair_hint` whose
  `args.office` is exactly the skipped list; the curated office list is now a
  priority order (NWDM, NWDP, SWT, MVS, …) so the first follow-up reaches the
  data-rich offices. The `no_offices_in_scope` reason no longer tells the
  caller to omit a field they omitted (#161).
- `cwms_publishers_for_parameter` / `publisher for-parameter`:
  `coverage.complete` is false when no office was indexed, and a cold cache with
  `offices` omitted returns a `repair` naming the curated offices. A latent
  cache-key mismatch meant previously indexed offices were never recognised as
  cached; fixed (#164).
```

`### Changed`: "Capability fingerprint moves (parameter descriptions)."

- [ ] **Step 8.4: Gate, commit, PR**

```bash
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q && prek run --all-files
git add -u && git commit -m "docs(search): describe the cold-cache repair chain on both surfaces (#161, #164)"
git push
gh pr create --fill --title "fix: followable cold-cache discovery for search and publisher index (#161, #164)" --body "Closes #161. Closes #164.

🤖 Generated with Claude Code

https://claude.ai/code/session_015kfeiw3yomoQw34HgijkLw"
```

---

## PR 3 — `fix/96-self-repairing-resolution` (#96, #163, #170)

Order inside the PR matters: the repair plumbing (Task 9) and the shared catalog helpers (Task 10) come first, office validation (Task 11) next, and `ghost_location` goes live (Task 12) only after the name-existence check exists, so a misspelled name can never be reported as a ghost.

### Task 9: Branch; `RepairIntent` — same-operation repairs resolved at each surface

**Files:**
- Modify: `src/cwms_tools/core/errors.py`
- Modify: `src/cwms_tools/mcp/tools.py` (`_safe`, every `_repair_call` dict)
- Modify: `src/cwms_tools/cli/render.py`
- Test: `tests/test_errors.py`, `tests/test_mcp_tool_handlers.py`, `tests/test_cli_value.py`

**Interfaces:**
- Produces: `errors.RepairIntent(next_step: str, overrides: dict[str, Any], alternative: str | None = None)` (frozen dataclass); `CwmsToolsError.repair_intent: RepairIntent | None`; `CwmsToolsError.of(..., repair_intent=...)`; `errors.resolve_repair_intent(intent, *, tool, args) -> RepairHint`; `render.attach_repair_intent(err, *, tool, args, spec_key=None, spec_fields=None, spec_as_list=False) -> CwmsToolsError`.
- Contract: core raises with BOTH a generic `repair` (valid on its own, e.g. `cwms_search_places`) and an optional `repair_intent` (argument overrides for retrying the *same* operation). A surface that knows its own tool identity replaces `repair` with the resolved same-tool retry, keeping the caller's other options (window, unit, rollup, detail, with_status). The intent never appears on the wire.

- [ ] **Step 9.1: Branch and assign**

```bash
git checkout main && git pull && git checkout -b fix/96-self-repairing-resolution
for n in 96 163 170; do gh issue edit $n --add-assignee @me; done
```

- [ ] **Step 9.2: Write the failing tests**

Append to `tests/test_errors.py`:

```python
from cwms_tools.core.errors import RepairHint, RepairIntent, resolve_repair_intent


def test_repair_intent_resolves_to_same_tool_retry_with_overrides() -> None:
    intent = RepairIntent("use_published_parameter", {"parameter": "Elev"}, alternative="alt")
    hint = resolve_repair_intent(
        intent, tool="cwms_get_value", args={"office": "SWT", "name": "FOSS", "parameter": "elev", "unit": "SI"}
    )
    assert hint == RepairHint(
        next_step="use_published_parameter",
        tool="cwms_get_value",
        arguments={"office": "SWT", "name": "FOSS", "parameter": "Elev", "unit": "SI"},
        alternative="alt",
    )


def test_error_of_carries_intent_off_the_envelope() -> None:
    err = CwmsToolsError.of(
        ErrorCode.NOT_FOUND,
        "x",
        field="name",
        value="Foss",
        repair=RepairHint(next_step="search", tool="cwms_search_places", arguments={"query": "Foss"}),
        repair_intent=RepairIntent("retry_with_catalog_id", {"name": "FOSS"}),
    )
    assert err.repair_intent == RepairIntent("retry_with_catalog_id", {"name": "FOSS"})
    assert "repair_intent" not in err.envelope.model_dump(mode="json")
    assert err.envelope.repair is not None and err.envelope.repair.tool == "cwms_search_places"
```

Append to `tests/test_mcp_tool_handlers.py`:

```python
def test_get_history_resolves_repair_intent_to_same_tool_with_window_preserved(
    configured, monkeypatch
) -> None:
    """#96/#163 (Codex): a corrected-argument repair must retry THIS tool with the
    caller's window/unit/rollup, never switch to get_value."""
    from cwms_tools.core import values
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint, RepairIntent

    def boom(*a, **k):
        raise CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            "no",
            field="parameter",
            value="elev",
            repair=RepairHint(next_step="list", tool="cwms_list_parameters", arguments={}),
            repair_intent=RepairIntent("use_published_parameter", {"parameter": "Elev"}),
        )

    monkeypatch.setattr(values, "get_history", boom)
    server = build_server()
    args = {
        "office": "SWT",
        "name": "FOSS",
        "parameter": "elev",
        "begin_iso": "2026-05-17T00:00:00Z",
        "end_iso": "2026-05-18T00:00:00Z",
        "unit": "SI",
        "rollup": "daily",
    }
    result = _call(server, "cwms_get_history", args)
    repair = _branch(result.structured_content)["error"]["repair"]
    assert repair["tool"] == "cwms_get_history"
    assert repair["next_step"] == "use_published_parameter"
    assert repair["arguments"] == {**args, "parameter": "Elev", "detail": "summary"}
    # And the resolved repair is itself a valid call: replay it through the real
    # MCP parser and check the producer receives the corrected parameter.
    seen: dict = {}

    def record(office, name, parameter, **kwargs):
        seen.update(office=office, name=name, parameter=parameter, **kwargs)
        raise CwmsToolsError.of(ErrorCode.NOT_FOUND, "stop")

    monkeypatch.setattr(values, "get_history", record)
    _call(server, "cwms_get_history", repair["arguments"])
    assert seen["parameter"] == "Elev" and seen["unit"] == "SI" and seen["rollup"] == "daily"
```

Append to `tests/test_cli_value.py`:

```python
def test_value_get_batch_item_resolves_repair_intent_into_id_specs(monkeypatch) -> None:
    from cwms_tools.core import values
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint, RepairIntent

    def boom(*a, **k):
        raise CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            "no",
            field="name",
            value="Foss",
            repair=RepairHint(next_step="search", tool="cwms_search_places", arguments={}),
            repair_intent=RepairIntent("retry_with_catalog_id", {"name": "FOSS"}),
        )

    monkeypatch.setattr(values, "get_value", boom)
    result = runner.invoke(app, ["value", "get", "SWT/Foss/Elev", "--unit", "SI", "--with-status"])
    assert result.exit_code == 3
    item = json.loads(result.stdout)["results"][0]
    repair = item["error"]["repair"]
    assert repair["tool"] == "cwms-tools value get"
    assert repair["arguments"]["id_specs"] == ["SWT/FOSS/Elev"]
    assert repair["arguments"]["unit"] == "SI" and repair["arguments"]["with_status"] is True
    # The diagnostic names the CLI's real positional, not the MCP arg (#68 rule).
    assert item["error"]["details"]["field"] == "id_specs"
```

- [ ] **Step 9.3: Run to verify failure**

Run: `uv run pytest tests/test_errors.py tests/test_mcp_tool_handlers.py tests/test_cli_value.py -k "intent" -q`
Expected: FAIL (`ImportError: cannot import name 'RepairIntent'`).

- [ ] **Step 9.4: Implement core**

In `src/cwms_tools/core/errors.py` add after `RepairHint`:

```python
@dataclass(frozen=True)
class RepairIntent:
    """Internal, surface-agnostic repair: retry the SAME operation with these overrides.

    Core producers know what to change (`{"parameter": "Elev"}`, `{"name":
    "FOSS"}`) but not which surface call failed or what its other arguments
    were; the surface boundary (`mcp.tools._safe`, `cli.render.attach_repair_intent`)
    merges `overrides` into its own wire-format arguments and emits a public
    `RepairHint`. Never serialized: the envelope's `repair` stays a
    self-contained generic fallback for callers that cannot resolve intents.
    """

    next_step: str
    overrides: dict[str, Any]
    alternative: str | None = None


def resolve_repair_intent(
    intent: RepairIntent, *, tool: str, args: dict[str, Any]
) -> RepairHint:
    """Public same-tool retry hint: the caller's wire args with the intent's overrides applied."""
    return RepairHint(
        next_step=intent.next_step,
        tool=tool,
        arguments={**args, **intent.overrides},
        alternative=intent.alternative,
    )
```

(add `from dataclasses import dataclass` to the imports.) In `CwmsToolsError`:

```python
    def __init__(self, envelope: ErrorEnvelope, *, repair_intent: RepairIntent | None = None) -> None:
        super().__init__(envelope.message)
        self.envelope = envelope
        self.repair_intent = repair_intent
```

and give `of(...)` a `repair_intent: RepairIntent | None = None` keyword, returning `cls(envelope, repair_intent=repair_intent)`. Export `RepairIntent` and `resolve_repair_intent` in `__all__`.

- [ ] **Step 9.5: Implement the MCP boundary**

In `src/cwms_tools/mcp/tools.py` replace the body of `_safe`'s `except` with:

```python
    except CwmsToolsError as err:
        if _repair_call is not None:
            tool, call_args = _repair_call
            office_id = err.envelope.details.value if err.envelope.details else None
            if err.envelope.code is ErrorCode.GHOST_OFFICE and isinstance(office_id, str):
                err.envelope.repair = offices.ghost_office_repair(
                    office_id, tool=tool, args=call_args
                )
            elif err.repair_intent is not None:
                err.envelope.repair = resolve_repair_intent(
                    err.repair_intent, tool=tool, args=call_args
                )
        return error_ref(err)
```

Import `resolve_repair_intent` from `cwms_tools.core.errors`. Then add `"office": office` as the FIRST key of every `_repair_call` dict in the seven handlers (`cwms_describe_place`, `cwms_list_parameters`, `cwms_browse_region`, `cwms_get_value`, `cwms_get_history`, `cwms_get_profile`; for `cwms_search_places` add `**({"office": office} if office is not None else {})`). `ghost_office_repair` overwrites `office`, so the NW-stub repair output is unchanged. Update `_safe`'s docstring: the args now include `office`.

- [ ] **Step 9.6: Implement the CLI boundary**

In `src/cwms_tools/cli/render.py` add:

```python
def attach_repair_intent(
    error: CwmsToolsError,
    *,
    tool: str,
    args: dict[str, Any],
    spec_key: str | None = None,
    spec_fields: dict[str, str] | None = None,
    spec_as_list: bool = False,
) -> CwmsToolsError:
    """Resolve a core `RepairIntent` into a same-command retry (#96/#163).

    `args` are this command's own wire-format options (`--flag` names as
    keys). Commands that take a combined `OFFICE/NAME[/PARAMETER]` positional
    pass `spec_key` (the positional's name) and `spec_fields` — an ordered
    `{"office": ..., "name": ..., "parameter": ...}` — so an override on any
    part is re-joined into the positional; `spec_as_list` wraps it for
    `value get`'s variadic `id_specs`. A `ghost_office` error keeps its own
    rollup repair (`attach_ghost_office_*`). No-op when there is no intent.
    """
    intent = error.repair_intent
    if intent is None or error.envelope.code is ErrorCode.GHOST_OFFICE:
        return error
    if spec_key is None:
        arguments: dict[str, Any] = {**args, **intent.overrides}
    else:
        fields = dict(spec_fields or {})
        fields.update({k: v for k, v in intent.overrides.items() if k in fields})
        spec = "/".join(str(v) for v in fields.values())
        arguments = {**args, spec_key: [spec] if spec_as_list else spec}
    error.envelope.repair = RepairHint(
        next_step=intent.next_step,
        tool=tool,
        arguments=arguments,
        alternative=intent.alternative,
    )
    return error
```

Export it in `__all__`. Call it in each CLI command's `except CwmsToolsError` block, after the existing `rewrite_error_field`/`attach_ghost_office_*` calls and before `emit_error`. In the same blocks, extend the #68 field translation so a `name` or `parameter` diagnostic names the command's real positional: `place parameters` adds `rewrite_error_field(err, when="name", to="spec")` (describe already has it); `value get` adds `rewrite_error_field(err, when="name", to="id_specs")` and `rewrite_error_field(err, when="parameter", to="id_specs")`; `value history` and `value profile` add the same pair with `to="id_spec"`.

- `place search` (`cli/commands/place.py`): `attach_repair_intent(err, tool="cwms-tools place search", args={**repair_args, **({"office": list(office)} if office else {})})`.
- `place describe`: `attach_repair_intent(err, tool="cwms-tools place describe", args={**({"include_locks": True} if include_locks else {}), "detail": detail.value}, spec_key="spec", spec_fields={"office": office, "name": name})`.
- `place parameters`: same with `args={}`.
- `value get` batch loop (`cli/commands/value.py:141-166`): after the ghost-office branch, `attach_repair_intent(err, tool="cwms-tools value get", args={"window_hours": window_hours, "unit": unit.value, "with_status": with_status, "detail": detail.value}, spec_key="id_specs", spec_fields={"office": office, "name": name, "parameter": parameter}, spec_as_list=True)`.
- `value history`: `args={"begin": begin, "end": end, "unit": unit.value, "rollup": rollup.value, "detail": detail.value}`, `spec_key="id_spec"`, `spec_fields={"office": office, "name": name, "parameter": parameter}`.
- `value profile`: `args={"window_hours": window_hours, "unit": unit.value, "detail": detail.value}`, same spec fields.

- [ ] **Step 9.7: Run tests**

Run: `uv run pytest tests/test_errors.py tests/test_mcp_tool_handlers.py tests/test_cli_value.py tests/test_cli_place.py -q`
Expected: PASS.

- [ ] **Step 9.8: Commit**

```bash
git add src/cwms_tools/core/errors.py src/cwms_tools/mcp/tools.py src/cwms_tools/cli/render.py src/cwms_tools/cli/commands/place.py src/cwms_tools/cli/commands/value.py tests/
git commit -m "feat(errors): RepairIntent resolved to same-tool retries on both surfaces (#96, #163)"
git push -u origin fix/96-self-repairing-resolution
```

### Task 10: Shared catalog helpers — siblings, existence, suggestions (one implementation)

**Files:**
- Modify: `src/cwms_tools/core/catalog.py`
- Modify: `src/cwms_tools/core/places.py:448-466, 565-586` (delete `_sibling_data_at`, `_data_at_for_location`; call catalog)
- Test: `tests/test_catalog.py`, `tests/test_places.py`

**Interfaces:**
- Produces in `catalog`:
  - `sibling_data_at(siblings: list[str], by_name: dict[str, dict], parameter: str | None = None) -> list[str]` (moved verbatim from `places._sibling_data_at`).
  - `data_bearing_siblings(office_id, name, *, parameter=None, use_cache=True) -> list[str]` (moved from `places._data_at_for_location`, gains `parameter`).
  - `location_catalog_row(office_id, name, *, use_cache=True) -> dict | None` — exact-name row from the cached locations catalog.
  - `suggest_location(office_id, name, *, use_cache=True) -> tuple[str | None, list[str]]` — `(case-insensitive exact catalog id or None, up to 3 fuzzy catalog ids)` matched against `name`, `public-name`, `long-name`.
  - `location_not_found_error(office_id, name, *, use_cache=True) -> CwmsToolsError` — `not_found{field:"name"}` with suggestion, generic repair `cwms_search_places{query, office}`, and a `RepairIntent("retry_with_catalog_id", {"name": exact})` when an exact case-insensitive match exists.

- [ ] **Step 10.1: Write the failing tests**

Append to `tests/test_catalog.py`:

```python
SUGGEST_LOCATIONS = {
    "locations": [
        {"office-id": "SWT", "name": "FOSS", "public-name": "Foss Reservoir", "latitude": 35.55, "longitude": -98.97},
        {"office-id": "SWT", "name": "ARBU", "public-name": "Arbuckle Lake", "latitude": 34.4, "longitude": -97.0},
        {"office-id": "SWT", "name": "UBLW_S1-D21,0ft", "public-name": None, "latitude": 47.6, "longitude": -122.3},
    ]
}


def _arm_suggest(mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=SUGGEST_LOCATIONS, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)


def test_location_catalog_row_is_exact_and_case_sensitive(configured, mocked) -> None:
    _arm_suggest(mocked)
    assert catalog.location_catalog_row("SWT", "FOSS")["public-name"] == "Foss Reservoir"
    assert catalog.location_catalog_row("SWT", "Foss") is None


@pytest.mark.parametrize(
    "query,exact,fuzzy_contains",
    [
        ("Foss", "FOSS", []),  # case-insensitive id
        ("foss reservoir", "FOSS", []),  # case-insensitive public name
        ("Foss Lake", None, ["FOSS"]),  # not a ci match; fuzzy by first word
        ("Arbuckle", None, ["ARBU"]),
        ("Zzzz", None, []),
    ],
)
def test_suggest_location(configured, mocked, query, exact, fuzzy_contains) -> None:
    _arm_suggest(mocked)
    got_exact, fuzzy = catalog.suggest_location("SWT", query)
    assert got_exact == exact
    for name in fuzzy_contains:
        assert name in fuzzy


def test_suggest_location_ambiguous_alias_is_not_an_exact_match(configured, mocked) -> None:
    rows = {
        "locations": [
            {"office-id": "SWT", "name": "A", "public-name": "Shared", "latitude": 1.0, "longitude": 1.0},
            {"office-id": "SWT", "name": "B", "public-name": "Shared", "latitude": 2.0, "longitude": 2.0},
            {"office-id": "SWT", "name": "SHARED", "public-name": "Other", "latitude": 3.0, "longitude": 3.0},
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=rows, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    # The catalog id wins over the alias when both match.
    assert catalog.suggest_location("SWT", "shared") == ("SHARED", [])
    rows["locations"].pop()  # no id match: the alias is ambiguous → candidates only
    mocked.replace(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=rows, status=200)
    assert catalog.suggest_location("SWT", "shared", use_cache=False) == (None, ["A", "B"])


def test_suggest_location_handles_comma_and_regex_names(configured, mocked) -> None:
    """Review focus #1: ids with commas/regex metacharacters flow through the
    matcher untouched (no regex is built from the query here)."""
    _arm_suggest(mocked)
    assert catalog.suggest_location("SWT", "ublw_s1-d21,0ft") == ("UBLW_S1-D21,0ft", [])
    assert catalog.suggest_location("SWT", "(.*)") == (None, [])


def test_location_not_found_error_carries_suggestion_and_intent(configured, mocked) -> None:
    _arm_suggest(mocked)
    err = catalog.location_not_found_error("SWT", "Foss")
    env = err.envelope
    assert env.code is ErrorCode.NOT_FOUND
    assert env.details is not None and env.details.field == "name" and env.details.value == "Foss"
    assert "did you mean 'FOSS'" in env.message
    assert env.repair is not None and env.repair.tool == "cwms_search_places"
    assert env.repair.arguments == {"query": "Foss", "office": "SWT"}
    assert err.repair_intent is not None and err.repair_intent.overrides == {"name": "FOSS"}
    fuzzy_only = catalog.location_not_found_error("SWT", "Foss Lake")
    assert fuzzy_only.repair_intent is None
    assert "FOSS" in (fuzzy_only.envelope.details.reason or "")


def test_data_bearing_siblings_filters_by_parameter(configured, mocked) -> None:
    rows = {
        "locations": [
            {"office-id": "NWDP", "name": "GWLW", "latitude": 47.65, "longitude": -122.33},
            {"office-id": "NWDP", "name": "GWLW_S1-D3,0ft", "latitude": 47.6501, "longitude": -122.33},
            {"office-id": "NWDP", "name": "GWLW_S1-D13,0ft", "latitude": 47.6502, "longitude": -122.33},
        ]
    }
    ts = {
        "entries": [
            {"name": "GWLW_S1-D3,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
            {"name": "GWLW_S1-D13,0ft.Conc-DissolvedOxygen.Inst.1Hour.0.IRIDIUM-REV"},
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=rows, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts, status=200)
    assert catalog.data_bearing_siblings("NWDP", "GWLW") == ["GWLW_S1-D13,0ft", "GWLW_S1-D3,0ft"]
    assert catalog.data_bearing_siblings("NWDP", "GWLW", parameter="Temp-Water") == ["GWLW_S1-D3,0ft"]
    assert catalog.data_bearing_siblings("NWDP", "NOPE") == []
```

Add `from cwms_tools.core.errors import ErrorCode` to the test imports if absent.

- [ ] **Step 10.2: Run to verify failure**

Run: `uv run pytest tests/test_catalog.py -k "suggest or catalog_row or not_found_error or data_bearing" -q`
Expected: FAIL (`AttributeError`).

- [ ] **Step 10.3: Implement in `catalog.py`**

Add `import difflib` and `from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint, RepairIntent`. Append:

```python
def sibling_data_at(
    siblings: list[str],
    by_name: dict[str, dict[str, Any]],
    parameter: str | None = None,
) -> list[str]:
    """Filter `siblings` to those that publish data (and `parameter`, when set); sorted.

    The one sibling predicate shared by search/browse/describe/list_parameters
    and the value tools' ghost repair (#96) — moved here from `places` so it
    cannot fork (#45/#55/#56).
    """
    out: list[str] = []
    for s in siblings:
        info = by_name.get(s)
        if not info or info.get("parameter_count", 0) <= 0:
            continue
        if parameter is not None and parameter not in (info.get("parameters") or []):
            continue
        out.append(s)
    return sorted(out)


def data_bearing_siblings(
    office_id: str,
    name: str,
    *,
    parameter: str | None = None,
    use_cache: bool = True,
) -> list[str]:
    """Co-located siblings of `name` that publish data (optionally `parameter`).

    `[]` when `name` is not in the office catalog or no sibling publishes —
    callers that need to tell those apart check `location_catalog_row` first.
    """
    enriched = enrich_locations(office_id, use_cache=use_cache)
    by_name = {r["name"]: r for r in enriched}
    target = by_name.get(name)
    if target is None:
        return []
    return sibling_data_at(target.get("co_located") or [], by_name, parameter)


def location_catalog_row(
    office_id: str, name: str, *, use_cache: bool = True
) -> dict[str, Any] | None:
    """The exact-name (case-sensitive) locations-catalog row, or None if absent."""
    payload = get_locations_catalog(office_id, use_cache=use_cache)
    for row in _iter_location_entries(payload):
        if (row.get("name") or row.get("location-id")) == name:
            return row
    return None


def suggest_location(
    office_id: str, name: str, *, use_cache: bool = True
) -> tuple[str | None, list[str]]:
    """`(exact_ci_match, fuzzy)`: the catalog id whose id/public/long name equals
    `name` case-insensitively, else up to three fuzzy candidates (#163).

    Fuzzy = difflib close matches over every label (cutoff 0.6) plus labels
    whose first word equals the query's first word ("Foss Lake" → FOSS via
    "Foss Reservoir"). Pure string matching: no regex is built from `name`.
    """
    rows = _dedupe_rows_by_name(list(_iter_location_entries(get_locations_catalog(office_id, use_cache=use_cache))))
    folded = name.casefold().strip()
    ids: dict[str, str] = {}  # casefolded catalog id -> catalog id
    aliases: dict[str, list[str]] = {}  # casefolded public/long name -> catalog ids
    for row in rows:
        catalog_id = row.get("name") or row.get("location-id")
        if not isinstance(catalog_id, str):
            continue
        ids.setdefault(catalog_id.casefold(), catalog_id)
        for label in (row.get("public-name"), row.get("long-name")):
            if isinstance(label, str) and label:
                bucket = aliases.setdefault(label.casefold(), [])
                if catalog_id not in bucket:
                    bucket.append(catalog_id)
    # A catalog id beats an alias; an alias is a repairable match only when it
    # names exactly one record — two "Shared" public names must not become an
    # arbitrary same-tool retry (Codex review). Ambiguous aliases become fuzzy
    # candidates for discovery instead.
    if folded in ids:
        return ids[folded], []
    alias_hits = aliases.get(folded, [])
    if len(alias_hits) == 1:
        return alias_hits[0], []
    fuzzy: list[str] = list(alias_hits)
    labels: dict[str, str] = {**ids, **{k: v[0] for k, v in aliases.items()}}
    for key in difflib.get_close_matches(folded, list(labels), n=5, cutoff=0.6):
        if labels[key] not in fuzzy:
            fuzzy.append(labels[key])
    first = folded.split(" ", 1)[0]
    if len(first) >= 3:
        for key, catalog_id in labels.items():
            if key.split(" ", 1)[0] == first and catalog_id not in fuzzy:
                fuzzy.append(catalog_id)
    return None, fuzzy[:3]


def location_not_found_error(
    office_id: str, name: str, *, use_cache: bool = True
) -> CwmsToolsError:
    """`not_found{field:"name"}` for a name absent from the office catalog (#163).

    Distinct from a ghost: the record does not exist at all. Carries a
    case-insensitive suggestion as a same-tool `RepairIntent` when unique, and
    always a generic `cwms_search_places` repair.
    """
    exact, fuzzy = suggest_location(office_id, name, use_cache=use_cache)
    search = RepairHint(
        next_step="resolve_name_with_search",
        tool="cwms_search_places",
        arguments={"query": name, "office": office_id},
    )
    base = (
        "Location ids are case-sensitive catalog ids (e.g. FOSS, not 'Foss' or "
        "'Foss Lake'); resolve them with cwms_search_places."
    )
    if exact is not None:
        return CwmsToolsError.of(
            ErrorCode.NOT_FOUND,
            f"No location {name!r} in office {office_id}; did you mean {exact!r}?",
            field="name",
            value=name,
            reason=f"{base} {exact!r} matches case-insensitively; retry with name={exact!r}.",
            repair=search,
            repair_intent=RepairIntent("retry_with_catalog_id", {"name": exact}),
        )
    hint = f" Similar catalog ids: {', '.join(fuzzy)}." if fuzzy else ""
    return CwmsToolsError.of(
        ErrorCode.NOT_FOUND,
        f"No location {name!r} in office {office_id}.",
        field="name",
        value=name,
        reason=f"{base}{hint}",
        repair=search,
    )
```

Add the five names to `__all__`. In `places.py`: delete `_sibling_data_at` and `_data_at_for_location`; replace the three `_sibling_data_at(...)` calls with `catalog.sibling_data_at(...)` and the `list_parameters` call with `catalog.data_bearing_siblings(office, name, use_cache=use_cache)`.

- [ ] **Step 10.4: Verify no import cycle and run tests**

Run: `uv run python -c "import cwms_tools.core.timeseries, cwms_tools.core.places, cwms_tools.core.catalog" && uv run pytest tests/test_catalog.py tests/test_places.py -q`
Expected: PASS; all `data_at` tests unchanged.

- [ ] **Step 10.5: Commit**

```bash
git add src/cwms_tools/core/catalog.py src/cwms_tools/core/places.py tests/test_catalog.py
git commit -m "refactor(catalog): one home for sibling lookup, name existence, and suggestions (#96, #163)"
git push
```

### Task 11: Office validation once per producer (#163a)

**Files:**
- Modify: `src/cwms_tools/core/offices.py`
- Modify: `src/cwms_tools/core/places.py` (`_resolve_scope`, `describe_place`, `list_parameters`, `browse_region`)
- Modify: `src/cwms_tools/core/values.py` (`get_value`, `get_history`, `get_profile`)
- Modify: `src/cwms_tools/core/publishers_index.py` (loop)
- Modify: `src/cwms_tools/mcp/resources.py` (`TOOL_ERROR_CODES`), `src/cwms_tools/cli/commands/schema.py`
- Test: `tests/test_offices.py`, `tests/test_values.py`, `tests/test_places.py`, `tests/test_error_code_contract.py`

**Interfaces:**
- Produces: `offices.require_known_office(office: str) -> str` — returns the canonical code (case-normalized when a case-insensitive match exists); raises `ghost_office` for NW stubs matched case-insensitively (precedence preserved even during an outage) and `invalid_field{field:"office_id"}` (surface-translated to `office`) for unknown codes; returns the input unchanged when the directory is on its degraded fallback, and for `_DIRECTORY_RETRY_SECONDS` (60 s, measured from when the failed lookup *finished*, keyed by API root) afterwards.
- Produces: `offices.directory_for_validation() -> tuple[list[str], bool]` — the cached directory ids, else ONE bounded fetch (`timeout=(3.05, 10.0)` on `cwms.api.SESSION.get`, which cwms-python's `cwms.api.get` does not allow) that shares `list_offices`'s cache key; `(ids, False)` on success, `([], True)` on any failure. The test seam: `tests/conftest.py` stubs it to `([], True)` by default (validation off) and the `validating_directory` fixture restores it.
- Produces: `offices._reset_directory_state()` (test hook) clearing the cooldown map.
- Both `places._validate_offices` and the publisher loop deduplicate the *canonical* codes, first occurrence wins (`["SWT", "swt"]` → one SWT).

- [ ] **Step 11.1: Write the failing tests**

Append to `tests/test_offices.py` (reuse `_LIVE_SHAPE`, `configured`):

```python
# `_LIVE_SHAPE` holds only HQ/NWDM/NWO; validation tests need SWT and SPK too.
DIRECTORY = [
    {"office-id": "SWT", "long-name": "Tulsa District", "type": "DIS", "reports-to": "SWD"},
    {"office-id": "NWDM", "long-name": "Missouri River Basin", "type": "MSCR", "reports-to": "NWD"},
    {"office-id": "NWO", "long-name": "Omaha District", "type": "DIS", "reports-to": "NWD"},
    {"office-id": "SPK", "long-name": "Sacramento District", "type": "DIS", "reports-to": "SPD"},
]


def _arm_directory(mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}offices", json=DIRECTORY, status=200)


def test_require_known_office_accepts_and_normalizes_case(configured, validating_directory) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_directory(mocked)
        assert offices.require_known_office("SWT") == "SWT"
        assert offices.require_known_office("swt") == "SWT"
        assert len(mocked.calls) == 1, "second lookup is served from the shared cache key"


def test_require_known_office_rejects_unknown_code(configured, validating_directory) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_directory(mocked)
        with pytest.raises(CwmsToolsError) as ex:
            offices.require_known_office("XYZ")
    env = ex.value.envelope
    assert env.code is ErrorCode.INVALID_FIELD
    assert env.details is not None and env.details.field == "office_id" and env.details.value == "XYZ"
    assert "cwms://offices" in (env.details.reason or "")
    assert env.repair is not None and env.repair.tool == "cwms_list_offices"


@pytest.mark.parametrize("directory_status", [200, 503])
def test_require_known_office_keeps_nw_stub_precedence_even_offline(
    configured, validating_directory, directory_status
) -> None:
    """Lowercase stubs are ghost_office whether or not the directory is reachable."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        if directory_status == 200:
            _arm_directory(mocked)
        else:
            mocked.add(responses.GET, f"{API_ROOT}offices", status=503)
        with pytest.raises(CwmsToolsError) as ex:
            offices.require_known_office("nwo")
    assert ex.value.envelope.code is ErrorCode.GHOST_OFFICE
    assert ex.value.envelope.details.value == "NWO"


def test_require_known_office_skips_validation_on_fallback_and_backs_off(
    configured, validating_directory
) -> None:
    """Never block on a degraded directory, and don't re-hit a failing /offices
    on every producer call. cwms-python's adapter retries a 503 several times, so
    count attempts after the FIRST lookup and assert the second adds none."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", status=503)
        assert offices.require_known_office("XYZ") == "XYZ"
        attempts = len(mocked.calls)
        assert attempts >= 1
        assert offices.require_known_office("XYZ") == "XYZ"
        assert len(mocked.calls) == attempts
    # The cooldown is keyed by API root and clears on reset.
    offices._reset_directory_state()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_directory(mocked)
        assert offices.require_known_office("swt") == "SWT"


def test_require_known_office_accepts_directory_office_outside_curated_list(
    configured, validating_directory
) -> None:
    """Review focus #5: SPK is a real office that is not in the curated discovery list."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_directory(mocked)
        assert offices.require_known_office("SPK") == "SPK"
    assert "SPK" not in offices.discovery_office_candidates()
```

(Add `from cwms_tools.core.errors import CwmsToolsError, ErrorCode` to the imports.) Add to `tests/conftest.py`:

```python
from cwms_tools.core import offices


@pytest.fixture(autouse=True)
def _offline_office_directory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Office-code validation (#163) is opt-in for unit tests.

    By default the directory lookup reports the degraded fallback, so
    `require_known_office` passes codes through and no test gains a network
    dependency. Tests that exercise validation request `validating_directory`,
    which keeps the real lookup and a clean cooldown map.
    """
    offices._reset_directory_state()
    if "validating_directory" not in request.fixturenames:
        monkeypatch.setattr(offices, "directory_for_validation", lambda: ([], True))
    yield
    offices._reset_directory_state()


@pytest.fixture
def validating_directory() -> None:
    """Marker fixture: run this test with real office-code validation."""
    return None
```

Append to `tests/test_values.py`:

```python
def test_get_value_normalizes_office_case_and_rejects_unknown_office(
    configured, validating_directory
) -> None:
    directory = [{"office-id": "SWT", "long-name": "Tulsa District", "type": "DIS"}]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=directory, status=200)
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(
                ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
                points=[(datetime(2026, 5, 17, 18, tzinfo=UTC), 1648.21)],
            ),
            status=200,
        )
        payload = values.get_value("swt", "FOSS", "Elev")
        assert payload["office_id"] == "SWT"
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("XYZ", "FOSS", "Elev")
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD
    # No catalog call was made for the unknown office (cwms-python sends `office=`).
    assert not [c for c in mocked.calls if "office=XYZ" in (c.request.url or "")]
```

Append to `tests/test_places.py`:

```python
def test_search_places_invalid_office_is_partial_in_fanout_and_raises_alone(
    configured, mocked, validating_directory
) -> None:
    mocked.add(responses.GET, f"{API_ROOT}offices", json=[{"office-id": "SWT", "type": "DIS"}], status=200)
    _arm_all(mocked)
    payload = places.search_places("FOSS", office=["SWT", "swt", "XYZ"])
    assert payload["offices_searched"] == ["SWT"], "canonical codes are deduplicated"
    assert "XYZ: invalid_field" in payload["partial_reasons"]
    with pytest.raises(CwmsToolsError) as ex:
        places.search_places("FOSS", office="XYZ")
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD
```

Append to `tests/test_publishers_index.py`:

```python
def test_publishers_for_parameter_dedupes_normalized_offices_and_skips_invalid(
    configured, validating_directory
) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=[{"office-id": "SWT", "type": "DIS"}], status=200)
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json=_ts_catalog("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"),
            status=200,
        )
        payload = publishers_index.publishers_for_parameter("Elev", offices=["SWT", "swt", "XYZ"])
    cov = payload["coverage"]
    assert cov["offices_indexed"] == ["SWT"] and cov["offices_error_skipped"] == ["XYZ"]
    assert payload["ts_count"] == 1
```

The error-code contract provocations below also take `validating_directory`: add it as a parameter of `test_provocation_returns_advertised_code`.

In `tests/test_error_code_contract.py`, extend the provocation table so some provocations can arm their own mocks, then add `invalid_field` for the six tools:

```python
DIRECTORY = [{"office-id": "SWT", "long-name": "Tulsa District", "type": "DIS"}]


def _arm_directory(mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}offices", json=DIRECTORY, status=200)


# (tool, code) -> callable that registers the exact upstream mocks the provocation needs.
ARMED: dict[tuple[str, str], Callable[[responses.RequestsMock], None]] = {}
for tool in (
    "cwms_search_places",
    "cwms_describe_place",
    "cwms_list_parameters",
    "cwms_browse_region",
    "cwms_get_value",
    "cwms_get_profile",
):
    PROVOCATIONS[(tool, "invalid_field")] = ({**GOOD_ARGS[tool], "office": "XYZ"}, None)
    ARMED[(tool, "invalid_field")] = _arm_directory
```

and change `test_provocation_returns_advertised_code` so that when `(tool, code) in ARMED` it opens a `RequestsMock`, calls `ARMED[(tool, code)](mocked)`, then `_mock_all(mocked, status)` only if `status is not None`, then calls the tool. (`cwms_get_history` already advertises `invalid_field`; its existing provocation stands.)

- [ ] **Step 11.2: Run to verify failure**

Run: `uv run pytest tests/test_offices.py tests/test_values.py tests/test_places.py tests/test_error_code_contract.py -k "known_office or office_case or invalid_office or invalid_field" -q`
Expected: FAIL.

- [ ] **Step 11.3: Implement**

In `src/cwms_tools/core/offices.py`:

```python
import time

#: Bound on the validation-only directory fetch (connect, read). `cwms.api.get`
#: exposes no timeout and its adapter retries 6× with backoff, so this goes
#: through `cwms.api.SESSION.get` directly. The adapter's retries still apply
#: (they are per-session), so a hard outage costs one bounded retry sequence
#: per cooldown window, not one per producer call.
_DIRECTORY_TIMEOUT_S: tuple[float, float] = (3.05, 10.0)
#: After a fallback, skip validation for this long (measured from when the
#: failed lookup FINISHED, keyed by API root) instead of adding a failing
#: `/offices` round-trip to every producer call (#163 review). The producer's
#: own catalog call reports the outage.
_DIRECTORY_RETRY_SECONDS: float = 60.0
_directory_fallback: dict[str, float] = {}  # api_root -> monotonic time the fallback completed


def _reset_directory_state() -> None:
    """Test hook: clear the validation cooldown."""
    _directory_fallback.clear()


def _fetch_directory_bounded() -> list[dict[str, Any]]:
    """One bounded GET /offices through cwms-python's session (shares its retry adapter)."""
    headers = {"Accept": cwms_api.api_version_text(cwms_api.API_VERSION)}
    with cwms_api.SESSION.get("offices", headers=headers, timeout=_DIRECTORY_TIMEOUT_S) as resp:
        resp.raise_for_status()
        return _parse_office_records(resp.json())


def directory_for_validation() -> tuple[list[str], bool]:
    """`(office_ids, used_fallback)` for `require_known_office`.

    Cached directory first (the same cache key `list_offices` writes, so a
    `cwms://offices` read or a prior validation serves both); else one bounded
    fetch. Any failure is `([], True)` — never raise from a preflight.
    """
    cfg = current_config()
    cache: Cache | None = None
    key: str | None = None
    hit: Any = None
    try:
        cache = get_cache()
        key = build_cache_key("offices", "records", api_root=cfg.api_root)
        hit = cache.get(key)
    except Exception:
        cache, key, hit = None, None, None
    if isinstance(hit, list) and hit:
        return [r["name"] for r in hit], False
    try:
        records = _fetch_directory_bounded()
    except Exception:
        return [], True
    if not records:
        return [], True
    records.sort(key=lambda r: r["name"])
    if cache is not None and key is not None:
        with suppress(Exception):
            cache.set(key, records, ttl=cache.ttl_for("offices"))
    return [r["name"] for r in records], False


def require_known_office(office: str) -> str:
    """Return the canonical office code or raise (#163).

    NW district stubs keep their `ghost_office` precedence, matched
    case-insensitively and before any directory access, so the precedence
    survives an outage. A case-insensitive directory match is normalized
    (`swt` -> `SWT`) so a first call succeeds instead of round-tripping a
    repair. An unknown code is `invalid_field` (`field=office_id`,
    surface-translated to `office`) with a pointer at the directory. When the
    directory is on its degraded fallback the input passes through unchanged
    — never block on a degraded list — and validation stays off for
    `_DIRECTORY_RETRY_SECONDS` after that lookup finished.
    """
    upper = office.upper()
    if upper in NW_STUBS:
        raise ghost_office_error(upper)
    api_root = current_config().api_root
    since = _directory_fallback.get(api_root)
    if since is not None and time.monotonic() - since < _DIRECTORY_RETRY_SECONDS:
        return office
    ids, used_fallback = directory_for_validation()
    if used_fallback:
        _directory_fallback[api_root] = time.monotonic()  # after the lookup completed
        return office
    _directory_fallback.pop(api_root, None)
    if office in ids:
        return office
    canonical = {o.casefold(): o for o in ids}.get(office.casefold())
    if canonical is not None:
        return canonical
    raise CwmsToolsError.of(
        ErrorCode.INVALID_FIELD,
        f"Unknown office code {office!r}.",
        field="office_id",
        value=office,
        reason=(
            "Not a USACE office code in the CDA directory. Valid codes are listed at "
            "cwms://offices (cwms_list_offices / `cwms-tools offices`); in Northwestern "
            "Division use NWDM or NWDP, not the district stubs."
        ),
        repair=RepairHint(next_step="list_valid_offices", tool="cwms_list_offices", arguments={}),
    )
```

Add `require_known_office` and `directory_for_validation` to `__all__`. (`cwms_api.api_version_text` and `cwms_api.API_VERSION` exist in cwms-python 1.1.2's `cwms/api.py`; `cwms_api.SESSION` is rebound by `init_session`, so always read it through the module at call time. If `current_config()` cannot run before the session is configured, use `resolve_session_config().api_root` for the cooldown key instead.)

Wire it:

- `places._resolve_scope` (fresh path only, before `_run_fanout`):

```python
        requested, invalid_reasons = _validate_offices(requested)
        offices_searched, offices_skipped, partial_reasons = _run_fanout(requested)
        return None, 0, offices_searched, offices_skipped, [*invalid_reasons, *partial_reasons]
```

with

```python
def _validate_offices(requested: list[str]) -> tuple[list[str], list[str]]:
    """Canonicalize and deduplicate the requested offices (#163). A single
    invalid office raises (the agent gets the full envelope); in a fan-out it
    becomes a `"<office>: <code>"` partial reason and is dropped. Dedup runs on
    the CANONICAL code (`["SWT", "swt"]` is one office), first occurrence wins."""
    valid: list[str] = []
    reasons: list[str] = []
    for office in requested:
        try:
            canonical = offices.require_known_office(office)
        except CwmsToolsError as err:
            if len(requested) == 1:
                raise
            reasons.append(f"{office}: {err.envelope.code.value}")
            continue
        if canonical not in valid:
            valid.append(canonical)
    return valid, reasons
```

The four cursor tests in `tests/test_places.py` that stub `_run_fanout`/`_gather_enriched` without the `configured` fixture (`test_search_places_paginates_with_cursor` and its three siblings) run with the conftest offline seam, so `_validate_offices` passes codes through; they need no change.

  The cursor path keeps its locked (already-validated) list. `_gather_enriched`'s single-office re-raise stays for upstream errors.
- `places.describe_place`, `places.list_parameters`, `places.browse_region`: first statement `office = offices.require_known_office(office)`.
- `values.get_value`, `values.get_history`, `values.get_profile`: same (add `offices` to the `from cwms_tools.core import ...` line). In `get_history` it goes after the `rollup` check; in `get_profile` after the window check (cheap local validation first).
- `publishers_index.publishers_for_parameter`: the function's own parameter is named `offices`, so the module is imported as `offices_mod` (Task 7). Replace the existing dedup of `requested` with validate-then-dedup, before the budget loop:

```python
    requested_unique: list[str] = []
    error_skipped: list[str] = []
    for office in requested:
        try:
            canonical = offices_mod.require_known_office(office)
        except CwmsToolsError:
            error_skipped.append(office)
            continue
        if canonical not in requested_unique:
            requested_unique.append(canonical)
    requested = requested_unique
```

  (and drop the later `error_skipped: list[str] = []` initialisation so the list carries through). `coverage.offices_requested` echoes the canonical, deduplicated list.

Error catalogs: in `mcp/resources.py::TOOL_ERROR_CODES` add `"invalid_field"` (sorted position) to `cwms_search_places`, `cwms_describe_place`, `cwms_list_parameters`, `cwms_browse_region`, `cwms_get_value`, `cwms_get_profile`. In `cli/commands/schema.py` add `"invalid_field"` to the `_errs(...)` lists of `place search`, `place describe`, `place parameters`, `region browse`, `value get`, `value profile`. Publisher tool/command: unchanged (absorbed into `offices_error_skipped`).

- [ ] **Step 11.4: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS. Watch for tests that register `/offices` with `assert_all_requests_are_fired=True` or count calls; the directory fetch is one extra GET on the first producer call per test (cached afterwards). If a `responses` mock has `assert_all_requests_are_fired=True` and no `/offices` route, it still passes (unmatched URL → ConnectionError → fallback).

- [ ] **Step 11.5: Commit**

```bash
git add -u && git commit -m "fix(core): validate office codes once per producer; normalize case; invalid_field for unknown (#163)"
git push
```

### Task 12: `require_canonical_ts_id` cases A/B/C; `ghost_location` goes live (#96, #163b)

**Files:**
- Modify: `src/cwms_tools/core/timeseries.py:23-36, 121-146`
- Modify: `src/cwms_tools/mcp/resources.py:52` (`RESERVED_ERROR_CODES`), `TOOL_ERROR_CODES` for `cwms_get_value`/`cwms_get_history`
- Modify: `src/cwms_tools/cli/commands/schema.py` (`value get`, `value history` add `ghost_location`)
- Test: `tests/test_values.py`, `tests/test_capability_fingerprint_snapshot.py:246-262`, `tests/test_error_code_contract.py`

**Interfaces:**
- `timeseries.require_canonical_ts_id(office, location, parameter, *, use_cache=True) -> str` keeps its signature. Raises:
  - Case A `not_found{field:"parameter"}`: reason lists published codes; `repair` → `cwms_list_parameters`; `repair_intent("use_published_parameter", {"parameter": <match>})` when a case-insensitive or difflib (cutoff 0.6) match exists.
  - name absent: `catalog.location_not_found_error`.
  - Case B `ghost_location{field:"name"}`: reason lists data-bearing siblings; `repair` → `cwms_search_places{query, office, parameter}`; `repair_intent("read_data_bearing_sibling", {"name": <best>}, alternative=...)` where best = shallowest `<location>-D…` child publishing the parameter (alternative names `cwms_get_profile`), else the single flat sibling publishing it; no intent when several flat candidates exist.
  - Case C `ghost_location`: no data-bearing sibling; `repair` → `cwms_search_places{query, office}`.
- `timeseries.canonical_ts_id` is kept (used by nothing else? grep; delete if unused).

- [ ] **Step 12.1: Write the failing tests**

Append to `tests/test_values.py`:

```python
GWLW_LOCATIONS = {
    "locations": [
        {"office-id": "NWDP", "name": "GWLW_S1", "latitude": 47.65, "longitude": -122.33},
        {"office-id": "NWDP", "name": "GWLW_S1-D3,0ft", "latitude": 47.6501, "longitude": -122.33},
        {"office-id": "NWDP", "name": "GWLW_S1-D13,0ft", "latitude": 47.6502, "longitude": -122.33},
        # A neighbouring string whose children are co-located with GWLW_S1 but are
        # NOT its children: a ghost GWLW_S2 must not repair to a GWLW_S1-D… child.
        {"office-id": "NWDP", "name": "GWLW_S2", "latitude": 47.6503, "longitude": -122.33},
        {"office-id": "NWDP", "name": "LONELY", "latitude": 46.0, "longitude": -120.0},
    ]
}
GWLW_TS = {
    "entries": [
        {"name": "GWLW_S1-D3,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
        {"name": "GWLW_S1-D13,0ft.Temp-Water.Inst.1Hour.0.IRIDIUM-REV"},
    ]
}


def _arm_catalog(mocked, *, locations, ts_for_location, ts_full) -> None:
    # `ts_ids_for_location` scopes its request with a `like` query key; the
    # office-wide enrichment fetch has no `like` (it does carry
    # `timeseries-group-like`, so the regex anchors on `[?&]like=` — a bare
    # `like=` would match both). Register the scoped one first.
    mocked.add(
        responses.GET,
        re.compile(rf"{re.escape(API_ROOT)}catalog/TIMESERIES\?.*[?&]like=.*"),
        json=ts_for_location,
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=ts_full, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locations, status=200)


def test_case_a_not_found_lists_published_parameters_and_suggests_close_match(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(
            responses.GET,
            f"{API_ROOT}catalog/TIMESERIES",
            json={"entries": [{"name": "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"}, {"name": "FOSS.Stor.Inst.1Day.0.Ccp-Rev"}]},
            status=200,
        )
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("SWT", "FOSS", "Stro")
    err = ex.value
    assert err.envelope.code is ErrorCode.NOT_FOUND
    assert "Elev, Stor" in (err.envelope.details.reason or "")
    assert err.envelope.repair is not None and err.envelope.repair.tool == "cwms_list_parameters"
    assert err.repair_intent is not None
    assert err.repair_intent.overrides == {"parameter": "Stor"}


def test_case_a_wrong_case_parameter_is_an_exact_suggestion(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("SWT", "FOSS", "elev")
    assert ex.value.repair_intent.overrides == {"parameter": "Elev"}


def test_case_b_depth_string_ghost_points_at_shallowest_child_and_names_profile(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "GWLW_S1", "Temp-Water")
    err = ex.value
    assert err.envelope.code is ErrorCode.GHOST_LOCATION
    assert err.envelope.details.field == "name"
    assert "GWLW_S1-D3,0ft" in (err.envelope.details.reason or "")
    assert err.repair_intent.next_step == "read_data_bearing_sibling"
    assert err.repair_intent.overrides == {"name": "GWLW_S1-D3,0ft"}
    assert "cwms_get_profile" in (err.repair_intent.alternative or "")
    assert err.envelope.repair.tool == "cwms_search_places"


def test_case_b_neighbouring_string_children_are_not_this_strings_children(configured) -> None:
    """Codex #7: a nearby depth-tagged sibling is not evidence that get_profile on
    THIS name works. GWLW_S2 is a ghost next to GWLW_S1's children: list them,
    but no same-tool intent (two flat candidates) and no profile alternative."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "GWLW_S2", "Temp-Water")
    err = ex.value
    assert err.envelope.code is ErrorCode.GHOST_LOCATION
    assert err.repair_intent is None
    assert err.envelope.repair.tool == "cwms_search_places"
    assert "GWLW_S1-D3,0ft" in (err.envelope.details.reason or "")


def test_case_b_single_flat_sibling_is_the_intent(configured) -> None:
    locations = {
        "locations": [
            {"office-id": "NWDM", "name": "GARR-Garrison_Dam", "latitude": 47.5, "longitude": -101.4},
            {"office-id": "NWDM", "name": "GARR", "latitude": 47.5001, "longitude": -101.4},
        ]
    }
    ts_full = {"entries": [{"name": "GARR.Elev.Inst.1Hour.0.Best-MRBWM"}]}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=locations, ts_for_location={"entries": []}, ts_full=ts_full)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDM", "GARR-Garrison_Dam", "Elev")
    assert ex.value.envelope.code is ErrorCode.GHOST_LOCATION
    assert ex.value.repair_intent.overrides == {"name": "GARR"}


def test_case_c_barren_everywhere_is_ghost_with_search_repair(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full={"entries": []})
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "LONELY", "Elev")
    err = ex.value
    assert err.envelope.code is ErrorCode.GHOST_LOCATION
    assert err.repair_intent is None
    assert err.envelope.repair.tool == "cwms_search_places"
    assert err.envelope.repair.arguments == {"query": "LONELY", "office": "NWDP"}


def test_missing_name_is_not_found_not_ghost(configured) -> None:
    """#163: a misspelled/wrong-case name must never be promoted to ghost_location."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_value("NWDP", "gwlw_s1", "Temp-Water")
    err = ex.value
    assert err.envelope.code is ErrorCode.NOT_FOUND
    assert err.envelope.details.field == "name"
    assert err.repair_intent.overrides == {"name": "GWLW_S1"}


def test_get_history_inherits_ghost_location(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        _arm_catalog(mocked, locations=GWLW_LOCATIONS, ts_for_location={"entries": []}, ts_full=GWLW_TS)
        with pytest.raises(CwmsToolsError) as ex:
            values.get_history(
                "NWDP", "GWLW_S1", "Temp-Water",
                begin=datetime(2026, 5, 17, tzinfo=UTC), end=datetime(2026, 5, 18, tzinfo=UTC),
            )
    assert ex.value.envelope.code is ErrorCode.GHOST_LOCATION
```

In `tests/test_capability_fingerprint_snapshot.py::test_dead_error_codes_removed_and_reserved_codes_declared` change the reserved list to `["publisher_unavailable", "wrapper_bug"]` and assert `"ghost_location" in payload["error_codes"]`. In `tests/test_error_code_contract.py` add:

```python
GHOST_LOCATIONS = {
    "locations": [
        {"office-id": "SWT", "name": "FOSS", "latitude": 35.55, "longitude": -98.97},
    ]
}


def _arm_ghost(mocked) -> None:
    mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=GHOST_LOCATIONS, status=200)


for tool in ("cwms_get_value", "cwms_get_history"):
    PROVOCATIONS[(tool, "ghost_location")] = (GOOD_ARGS[tool], None)
    ARMED[(tool, "ghost_location")] = _arm_ghost
```

Add a CLI exit-code pin to `tests/test_cli_value.py`:

```python
def test_value_get_ghost_location_exits_12(monkeypatch) -> None:
    from cwms_tools.core import values
    from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint

    def boom(*a, **k):
        raise CwmsToolsError.of(
            ErrorCode.GHOST_LOCATION, "ghost", field="name", value="X",
            repair=RepairHint(next_step="s", tool="cwms_search_places", arguments={}),
        )

    monkeypatch.setattr(values, "get_value", boom)
    result = runner.invoke(app, ["value", "get", "SWT/X/Elev"])
    assert result.exit_code == 12
    assert json.loads(result.stdout)["results"][0]["error"]["code"] == "ghost_location"
```

- [ ] **Step 12.2: Run to verify failure**

Run: `uv run pytest tests/test_values.py -k "case_ or missing_name or inherits_ghost" -q`
Expected: FAIL (`NOT_FOUND` with the generic reason everywhere).

- [ ] **Step 12.3: Implement `timeseries.py`**

Replace `require_canonical_ts_id` (and delete `canonical_ts_id` if `grep -rn canonical_ts_id src tests` shows no other caller; otherwise keep it delegating):

```python
import difflib

from cwms_tools.core import catalog, depth, publishers
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint, RepairIntent


def require_canonical_ts_id(
    office: str,
    location: str,
    parameter: str,
    *,
    use_cache: bool = True,
) -> str:
    """The canonical ts_id for `(office, location, parameter)`, or a self-repairing error.

    One catalog fetch, three failure branches (#96/#163):
    A. the location publishes other parameters → `not_found` naming them, with
       a same-tool intent on the closest published code;
    B/C. the location has no timeseries → first confirm the catalog row exists
       (else `not_found{field:name}` with a spelling suggestion), then
       `ghost_location` pointing at data-bearing co-located siblings (B) or,
       when none exist, at `cwms_search_places` (C).
    """
    ts_ids = catalog.ts_ids_for_location(office, location, use_cache=use_cache)
    tsid = publishers.pick_canonical(ts_ids, parameter=parameter)
    if tsid is not None:
        return tsid
    if ts_ids:
        raise _parameter_not_found(office, location, parameter, ts_ids)
    if catalog.location_catalog_row(office, location, use_cache=use_cache) is None:
        raise catalog.location_not_found_error(office, location, use_cache=use_cache)
    raise _ghost_location_error(office, location, parameter, use_cache=use_cache)


def _closest_parameter(parameter: str, published: list[str]) -> str | None:
    """Case-insensitive exact match first (`elev` → `Elev`), then difflib."""
    for code in published:
        if code.casefold() == parameter.casefold():
            return code
    close = difflib.get_close_matches(parameter, published, n=1, cutoff=0.6)
    return close[0] if close else None


def _parameter_not_found(
    office: str, location: str, parameter: str, ts_ids: list[str]
) -> CwmsToolsError:
    published = sorted(publishers.parameter_counts(ts_ids))
    match = _closest_parameter(parameter, published)
    reason = f"This location publishes: {', '.join(published)}. Retry with one of these."
    if match is not None:
        reason += f" Did you mean {match!r}?"
    return CwmsToolsError.of(
        ErrorCode.NOT_FOUND,
        f"No published ts_id at {office}/{location} for parameter {parameter!r}.",
        field="parameter",
        value=parameter,
        reason=reason,
        repair=RepairHint(
            next_step="list_available_parameters",
            tool="cwms_list_parameters",
            arguments={"office": office, "name": location},
        ),
        repair_intent=(
            RepairIntent("use_published_parameter", {"parameter": match}) if match else None
        ),
    )


def _ghost_location_error(
    office: str, location: str, parameter: str, *, use_cache: bool
) -> CwmsToolsError:
    with_param = catalog.data_bearing_siblings(
        office, location, parameter=parameter, use_cache=use_cache
    )
    any_data = with_param or catalog.data_bearing_siblings(office, location, use_cache=use_cache)
    if not any_data:  # Case C: the genuine dead end
        return CwmsToolsError.of(
            ErrorCode.GHOST_LOCATION,
            f"{office}/{location} publishes no timeseries (ghost record), and no "
            "co-located record does either.",
            field="name",
            value=location,
            reason=(
                "The catalog row exists but carries no data, and nothing within "
                "~100 m publishes. Search for the place by name to find the "
                "data-bearing record."
            ),
            repair=RepairHint(
                next_step="search_for_data_bearing_place",
                tool="cwms_search_places",
                arguments={"query": location, "office": office},
            ),
        )
    # Case B: siblings carry the data. Prefer the shallowest depth child of THIS
    # string (so the intent is a child of the requested name, not a neighbour);
    # else a single flat sibling; several flat candidates → no same-tool intent,
    # the search repair lists them all with data_at.
    prefix = f"{location}-D"
    depth_children = sorted(
        (s for s in with_param if s.startswith(prefix) and depth.parse_depth(s) is not None),
        key=depth.depth_sort_key,
    )
    intent: RepairIntent | None = None
    if depth_children:
        intent = RepairIntent(
            "read_data_bearing_sibling",
            {"name": depth_children[0]},
            alternative=(
                f"cwms_get_profile with office={office!r}, name={location!r}, "
                f"parameter={parameter!r} reads every depth of this string in one call."
            ),
        )
    elif len(with_param) == 1:
        intent = RepairIntent("read_data_bearing_sibling", {"name": with_param[0]})
    listed = with_param or any_data
    scope = (
        f"publish {parameter!r}" if with_param else f"publish data (none publishes {parameter!r})"
    )
    return CwmsToolsError.of(
        ErrorCode.GHOST_LOCATION,
        f"{office}/{location} publishes no timeseries (ghost record); co-located "
        "records carry the data.",
        field="name",
        value=location,
        reason=f"Co-located records that {scope}: {', '.join(listed)}.",
        repair=RepairHint(
            next_step="search_for_data_bearing_place",
            tool="cwms_search_places",
            arguments={"query": location, "office": office, "parameter": parameter},
        ),
        repair_intent=intent,
    )
```

Contract updates: remove `"ghost_location"` from `RESERVED_ERROR_CODES` (update the comment: it is emitted by the value tools since 0.8.1); add `"ghost_location"` to `TOOL_ERROR_CODES["cwms_get_value"]` and `["cwms_get_history"]`; in `cli/commands/schema.py` add `"ghost_location"` to `value get` and `value history`. Update the `cwms_get_value` MCP docstring's error sentence if it lists codes.

- [ ] **Step 12.4: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS, including `test_every_advertised_code_has_a_provocation`, the snapshot test, and `test_get_value_raises_not_found_with_repair_when_parameter_absent` (its `repair.tool` is still `cwms_list_parameters`).

- [ ] **Step 12.5: Commit**

```bash
git add -u && git commit -m "feat(values): self-repairing not_found and live ghost_location on the value tools (#96, #163)"
git push
```

### Task 13: `list_parameters`/`describe_place` existence checks; describe `data_at` (#163b, #170)

**Files:**
- Modify: `src/cwms_tools/core/places.py` (`describe_place`, `list_parameters`)
- Modify: `src/cwms_tools/core/models.py` (`DescribePlaceResponse.data_at`)
- Test: `tests/test_places.py`, `tests/test_cli_mcp_parity.py`

- [ ] **Step 13.1: Write the failing tests**

Append to `tests/test_places.py`:

```python
def test_list_parameters_missing_name_is_not_found_with_suggestion(configured, mocked) -> None:
    _arm_all(mocked)
    mocked.replace(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json={"entries": []}, status=200)
    with pytest.raises(CwmsToolsError) as ex:
        places.list_parameters("SWT", "Foss")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    assert ex.value.envelope.details.field == "name"
    assert ex.value.repair_intent.overrides == {"name": "FOSS"}


def test_describe_place_404_name_carries_suggestion(configured, mocked) -> None:
    _arm_all(mocked)
    mocked.add(responses.GET, f"{API_ROOT}locations/Foss", json={"detail": "nope"}, status=404)
    with pytest.raises(CwmsToolsError) as ex:
        places.describe_place("SWT", "Foss")
    assert ex.value.envelope.code is ErrorCode.NOT_FOUND
    assert "did you mean 'FOSS'" in ex.value.envelope.message
    assert ex.value.repair_intent.overrides == {"name": "FOSS"}


def test_describe_place_ghost_carries_data_at(configured, mocked) -> None:
    """#170: describe on a ghost names the co-located records that publish."""
    locations = {
        "locations": [
            {"office-id": "NWDM", "name": "GARR-Garrison_Dam", "latitude": 47.5, "longitude": -101.4},
            {"office-id": "NWDM", "name": "GARR", "latitude": 47.5001, "longitude": -101.4},
        ]
    }
    mocked.add(responses.GET, f"{API_ROOT}catalog/LOCATIONS", json=locations, status=200)
    mocked.add(
        responses.GET,
        re.compile(rf"{re.escape(API_ROOT)}catalog/TIMESERIES\?.*[?&]like=.*"),
        json={"entries": []},
        status=200,
    )
    mocked.add(
        responses.GET,
        f"{API_ROOT}catalog/TIMESERIES",
        json={"entries": [{"name": "GARR.Elev.Inst.1Hour.0.Best-MRBWM"}]},
        status=200,
    )
    mocked.add(responses.GET, f"{API_ROOT}locations/GARR-Garrison_Dam", json={"office-id": "NWDM", "name": "GARR-Garrison_Dam"}, status=200)
    mocked.add(responses.GET, f"{API_ROOT}projects/GARR-Garrison_Dam", json={"detail": "no"}, status=404)
    payload = places.describe_place("NWDM", "GARR-Garrison_Dam")
    assert payload["parameter_count"] == 0
    assert payload["data_at"] == ["GARR"]
    _arm_all(mocked)
    assert places.describe_place("SWT", "FOSS")["data_at"] is None
```

(`import re` at the top of `tests/test_places.py` if absent.) In `tests/test_cli_mcp_parity.py` add `"data_at": None` to `DESCRIBE["fields"]` and append:

```python
@pytest.mark.parametrize("detail", ["summary", "full"])
def test_describe_place_data_at_parity(configured, monkeypatch, detail) -> None:
    raw = {**_producer_payload(DESCRIBE), "parameters": [], "parameter_count": 0, "publishers": [], "ts_ids": [], "data_at": ["GARR"]}
    monkeypatch.setattr(places, "describe_place", lambda *a, **k: copy.deepcopy(raw))
    cli_data = _cli_data(DESCRIBE, ["place", "describe", "SWT/FOSS", "--detail", detail])
    mcp_data = _mcp_data("cwms_describe_place", {"office": "SWT", "name": "FOSS", "detail": detail})
    assert cli_data == mcp_data and cli_data["data_at"] == ["GARR"]
```

- [ ] **Step 13.2: Run to verify failure**

Run: `uv run pytest tests/test_places.py tests/test_cli_mcp_parity.py -k "missing_name or 404_name or ghost_carries_data_at or data_at_parity" -q`
Expected: FAIL.

- [ ] **Step 13.3: Implement**

`places.list_parameters` after computing `ts_ids`:

```python
    if not ts_ids and catalog.location_catalog_row(office, name, use_cache=use_cache) is None:
        raise catalog.location_not_found_error(office, name, use_cache=use_cache)
```

`places.describe_place`:

```python
    try:
        location = locations.get_one(office, name, use_cache=use_cache)
    except CwmsToolsError as err:
        if err.envelope.code is ErrorCode.NOT_FOUND and err.envelope.details and err.envelope.details.field == "name":
            raise catalog.location_not_found_error(office, name, use_cache=use_cache) from err
        raise
    ...
    response["data_at"] = (
        catalog.data_bearing_siblings(office, name, use_cache=use_cache) if not ts_ids else None
    )
```

`core/models.py` `DescribePlaceResponse`: add after `last_data_timestamp`:

```python
    data_at: list[str] | None = Field(
        default=None,
        description=(
            "Only when parameter_count is 0: co-located records (within ~100 m) that "
            "publish data; describe or read one of these instead. Omitted otherwise."
        ),
    )
```

- [ ] **Step 13.4: Run tests, commit**

Run: `uv run pytest -q`
Expected: PASS.

```bash
git add -u && git commit -m "feat(places): name suggestions on list/describe; describe carries data_at for ghosts (#163, #170)"
git push
```

### Task 14: `name` descriptions, CHANGELOG, PR

**Files:**
- Modify: `src/cwms_tools/mcp/tools.py` (`name` on describe/list/get_value/get_history; `_OFFICE_HINT`)
- Modify: `src/cwms_tools/cli/commands/place.py`, `value.py` (argument help), `cli/commands/schema.py` (`spec`/`id_spec(s)` help)
- Modify: `CHANGELOG.md`

- [ ] **Step 14.1: Prose**

Add to `tools.py`:

```python
_NAME_HINT = (
    "Catalog id, case-sensitive (FOSS, not 'Foss' or 'Foss Lake'); get it from "
    "`cwms_search_places`. A depth-tagged sensor keeps its comma (UBLW_S1-D21,0ft)."
)
```

and use it for `name` on `cwms_describe_place`, `cwms_list_parameters`, `cwms_get_value`, `cwms_get_history` (keep `cwms_get_profile`'s string-specific text but add "case-sensitive catalog id"). Set `_OFFICE_HINT = "USACE office code (e.g. NWDM, SWT; case-insensitive). Discover valid codes at cwms://offices."`. CLI: `place describe/parameters` `spec` help → "OFFICE/NAME with the case-sensitive catalog id, e.g. SWT/FOSS (not SWT/Foss)"; `value get/history/profile` ids likewise; mirror in `schema.py` `_arg(...)` help strings.

- [ ] **Step 14.2: CHANGELOG**

Under `## [Unreleased]`:

```markdown
### Added
- `cwms_describe_place` / `place describe` carry `data_at` (co-located records that
  publish) when the described location is a ghost, matching search and browse (#170).

### Changed
- `ghost_location` is now a live error code (was reserved): `cwms_get_value` /
  `cwms_get_history` (and `value get` / `value history`, exit code 12 instead of
  3) return it for a catalog record that exists but publishes no timeseries. The
  envelope names the co-located records that do publish and repairs to the same
  tool on the best one (`repair.alternative` names `cwms_get_profile` for depth
  strings) (#96).
- Repairs now retry the *same* operation with the caller's options preserved
  (window, unit, rollup, detail, with_status); the CLI rebuilds its
  `OFFICE/NAME/PARAMETER` positional (#96, #163).
- Office codes are validated against the CDA directory on every tool; an exact
  case-insensitive match is normalized (`swt` → `SWT`), an unknown code returns
  `invalid_field{field: office}` instead of an empty ghost-looking success. The
  NW district stubs keep `ghost_office`. `invalid_field` is advertised on every
  office-taking tool (#163).
- A location name absent from the catalog returns `not_found{field: name}` with a
  case-insensitive or fuzzy suggestion ("did you mean FOSS") rather than a
  ghost-looking `ts_count: 0` (#163). `name` descriptions state that ids are
  case-sensitive catalog ids.
- `not_found` for a parameter lists the parameters the location does publish and
  suggests the closest code (#96).
- Capability fingerprint moves (error codes, descriptions, `data_at` field).
```

- [ ] **Step 14.3: Gate, commit, PR**

```bash
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q && prek run --all-files
git add -u && git commit -m "docs(tools): case-sensitive name/office hints; CHANGELOG for self-repairing resolution"
git push
gh pr create --fill --title "feat: self-repairing resolution errors, office/name validation, describe data_at (#96, #163, #170)" --body "Closes #96. Closes #163. Closes #170.

Deliberate contract changes (fingerprint moves): ghost_location reserved→live, invalid_field advertised on office-taking tools, data_at on describe. Deviation from #96's text: depth-string ghosts repair to the same tool on the shallowest child with cwms_get_profile as the alternative, so get_history never repairs to get_profile (Codex review).

🤖 Generated with Claude Code

https://claude.ai/code/session_015kfeiw3yomoQw34HgijkLw"
```

---

## PR 4 — `fix/162-status-classifier` (#162)

### Task 15: Branch; one applicable revision per `level_id`

**Files:**
- Modify: `src/cwms_tools/core/levels.py:89-114`
- Test: `tests/test_levels.py`

**Interfaces:**
- Produces: `levels.applicable_revisions(rows: list[dict], *, at: datetime) -> list[dict]` — for each distinct `level_id` (via `level_id_of`), the row with the latest `level-date <= at` whose `expiration-date` is null or `> at`; rows without a parsable `level-date` are skipped; ties on equal dates keep the first row in listing order; output preserves first-seen `level_id` order. `resolve_applicable_level` is reimplemented on top of it (pick the single row among the survivors with the latest date), behavior unchanged.

- [ ] **Step 15.1: Branch and assign**

```bash
git checkout main && git pull && git checkout -b fix/162-status-classifier
gh issue edit 162 --add-assignee @me
```

- [ ] **Step 15.2: Write the failing tests**

Append to `tests/test_levels.py`:

```python
AT = datetime(2026, 10, 12, 14, tzinfo=UTC)


def _row(lid: str, date: str, *, exp: str | None = None, specified: str = "X") -> dict:
    row = {"location-level-id": lid, "level-date": date, "specified-level-id": specified}
    if exp is not None:
        row["expiration-date"] = exp
    return row


def test_applicable_revisions_keeps_one_row_per_level_id() -> None:
    """#162: live SWT/FOSS returns 24 rows for 13 ids — a 1900-01-01 and a
    1961-02-13 revision of most levels — which doubled `thresholds_active`."""
    rows = [
        _row("FOSS.Elev.Inst.0.Top of Flood", "1900-01-01T06:00:00Z"),
        _row("FOSS.Elev.Inst.0.Top of Flood", "1961-02-13T06:00:00Z"),
        _row("FOSS.Elev.Inst.0.Streambed", "1961-02-13T06:00:00Z"),
        _row("FOSS.Elev.Inst.0.Future", "2030-01-01T00:00:00Z"),
        _row("FOSS.Elev.Inst.0.Expired", "2000-01-01T00:00:00Z", exp="2001-01-01T00:00:00Z"),
        _row("FOSS.Elev.Inst.0.Undated", ""),
    ]
    kept = levels.applicable_revisions(rows, at=AT)
    assert [(levels.level_id_of(r), r["level-date"]) for r in kept] == [
        ("FOSS.Elev.Inst.0.Top of Flood", "1961-02-13T06:00:00Z"),
        ("FOSS.Elev.Inst.0.Streambed", "1961-02-13T06:00:00Z"),
    ]


def test_applicable_revisions_equal_dates_keep_listing_order() -> None:
    a = _row("L", "2000-01-01T00:00:00Z", specified="first")
    b = _row("L", "2000-01-01T00:00:00Z", specified="second")
    assert levels.applicable_revisions([a, b], at=AT)[0]["specified-level-id"] == "first"
```

- [ ] **Step 15.3: Run to verify failure**

Run: `uv run pytest tests/test_levels.py -k applicable_revisions -q`
Expected: FAIL (`AttributeError`).

- [ ] **Step 15.4: Implement**

In `levels.py` add before `resolve_applicable_level`:

```python
def applicable_revisions(rows: list[dict[str, Any]], *, at: datetime) -> list[dict[str, Any]]:
    """One row per `level_id`: the revision in force at `at`.

    CDA's `/levels` listing returns every revision of a level (#162: FOSS had
    a 1900 and a 1961 row for most ids). Keep, per id, the latest `level-date`
    <= `at` whose `expiration-date` is null or > `at`. Undated rows are
    skipped; equal dates keep listing order; output keeps first-seen id order.
    """
    best: dict[str, tuple[datetime, dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        level_id = level_id_of(row)
        if level_id is None:
            continue
        level_date = _coerce_dt(row.get("level-date") or row.get("levelDate"))
        if level_date is None or level_date > at:
            continue
        exp = _coerce_dt(row.get("expiration-date") or row.get("expirationDate"))
        if exp is not None and exp <= at:
            continue
        current = best.get(level_id)
        if current is None:
            order.append(level_id)
            best[level_id] = (level_date, row)
        elif level_date > current[0]:
            best[level_id] = (level_date, row)
    return [best[level_id][1] for level_id in order]
```

and rewrite `resolve_applicable_level` as:

```python
    rows = list_levels(office, location=location, parameter=parameter)
    survivors = applicable_revisions(rows, at=at)
    if not survivors:
        return None
    return max(survivors, key=lambda r: _coerce_dt(r.get("level-date") or r.get("levelDate")) or at)
```

Add `"applicable_revisions"` to `__all__`.

- [ ] **Step 15.5: Run, commit**

Run: `uv run pytest tests/test_levels.py -q` → PASS (the two existing `resolve_applicable_level` tests stay green).

```bash
git add src/cwms_tools/core/levels.py tests/test_levels.py
git commit -m "fix(levels): keep one applicable revision per level id (#162)"
git push -u origin fix/162-status-classifier
```

### Task 16: Evaluate seasonal levels at the observation time, not the revision date

**Files:**
- Modify: `src/cwms_tools/core/levels.py:117-179, 286-345`
- Test: `tests/test_seasonal_workaround.py`

**Interfaces:**
- `levels.fetch_level_value(level_id, *, office, effective_date, unit="EN", at: datetime | None = None)`. `effective_date` stays the revision selector (`effective-date` query param); `at` (default `effective_date`) is the evaluation instant: the seasonal bypass queries `begin=at`, `end=at+1h`. Response gains `"evaluated_at": at.isoformat()`.

- [ ] **Step 16.1: Write the failing test**

Append to `tests/test_seasonal_workaround.py`:

```python
def test_seasonal_bypass_queries_the_evaluation_time_not_the_revision_date(configured) -> None:
    """#162 (Codex): a 1961 revision of an annual rule curve was being read at
    its 1961 date (February's value) regardless of today's date."""
    seasonal_config = {
        "location-level-id": "FOSS.Elev.Inst.0.Rule Curve",
        "office-id": "SWT",
        "specified-level-id": "Rule Curve",
        "level-date": "1961-02-13T06:00:00Z",
        "seasonal-values": [{"value": 1648.0, "offset-months": 0}],
        "interval-months": 12,
    }
    at = datetime(2026, 10, 12, 14, tzinfo=UTC)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, re.compile(rf"{API_ROOT}levels/.*"), json=seasonal_config, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{API_ROOT}levels/.*/timeseries.*"),
            json={"units": "ft", "values": [[int(at.timestamp() * 1000), 1640.0, 0]]},
            status=200,
        )
        result = levels.fetch_level_value(
            "FOSS.Elev.Inst.0.Rule Curve",
            office="SWT",
            effective_date=datetime(1961, 2, 13, 6, tzinfo=UTC),
            at=at,
            unit="EN",
        )
        ts_call = next(c for c in mocked.calls if "/timeseries" in (c.request.url or ""))
        url = ts_call.request.url or ""
    assert "effective-date=1961-02-13" in url
    assert "begin=2026-10-12T14" in url
    assert "end=2026-10-12T15" in url
    assert result["evaluated_at"] == at.isoformat()
```

(`responses` URL-encodes `:`; if the assertion fails on encoding, assert against `urllib.parse.unquote(url)`.)

- [ ] **Step 16.2: Run to verify failure**

Run: `uv run pytest tests/test_seasonal_workaround.py -k evaluation_time -q`
Expected: FAIL (`TypeError: unexpected keyword 'at'`).

- [ ] **Step 16.3: Implement**

`fetch_level_value`: add `at: datetime | None = None`; `at = at or effective_date`; pass `at=at` into `_seasonal_workaround`; add `"evaluated_at": at.isoformat()` to all three return dicts. `_seasonal_workaround(..., at: datetime)`: `window_end = at + timedelta(hours=1)`; params `"effective-date": effective_date.isoformat()`, `"begin": at.isoformat()`, `"end": window_end.isoformat()`. Update both docstrings ("`effective_date` selects the revision; `at` is when the threshold is evaluated — the observation timestamp").

- [ ] **Step 16.4: Run, commit**

Run: `uv run pytest tests/test_seasonal_workaround.py tests/test_level_error_classification.py tests/test_levels.py -q` → PASS.

```bash
git add -u && git commit -m "fix(levels): evaluate seasonal thresholds at the observation time (#162)"
git push
```

### Task 17: `classify` by most-severe recognized class

**Files:**
- Modify: `src/cwms_tools/core/levels.py:182-224`
- Modify: `src/cwms_tools/core/models.py:450-457` (`StatusClass` docstring)
- Test: `tests/test_levels.py`

**Interfaces:**
- `levels.classify(observation, thresholds) -> tuple[str, list[dict]]` keeps its signature. Each annotated threshold gains `"zone": "flood" | "action" | "watch" | "nominal" | None` (its recognized class; `None` = unrecognized name). `status_class` = max severity over crossed (`at`/`above`) recognized zones; `"unknown"` when `observation is None`, when `thresholds` is empty, or when only unrecognized levels are crossed; `"nominal"` when thresholds exist and none is crossed.
- `levels.level_zone(specified_level_id: str) -> str | None` — the vocabulary rule, public for tests.

- [ ] **Step 17.1: Write the failing tests**

Append to `tests/test_levels.py` (replace the existing three `test_classify_*` with this set; keep `test_classify_returns_unknown_for_no_observation`):

```python
def _t(name: str, value: float) -> dict:
    return {"specified_level_id": name, "value": value, "unit": "ft"}


FOSS_FT = [  # SWT/FOSS Elev vocabulary observed live 2026-10-12 (metres -> ft, approx.)
    _t("Streambed", 1563.0), _t("Top of Inactive", 1597.2), _t("Bottom of Conservation", 1597.2),
    _t("Bottom of Normal", 1597.2), _t("Top of Conservation", 1642.0), _t("Top of Normal", 1642.0),
    _t("Pool of Record", 1648.2), _t("24-hr Surveillance Begins", 1655.0), _t("Top of Flood", 1662.2),
    _t("Spillway Crest", 1668.6), _t("Maximum Pool", 1694.2), _t("Top of Dam", 1697.0),
]


@pytest.mark.parametrize(
    "name,zone",
    [
        ("Top of Flood", "flood"), ("Flood Stage", "flood"), ("Bottom of Flood", "watch"),
        ("Action Stage", "action"), ("24-hr Surveillance Begins", "action"),
        ("Watch Stage", "watch"), ("Top of Conservation", "watch"), ("Rule Curve", "watch"),
        ("Top of Normal", "watch"), ("Top of Multipurpose", "watch"),
        ("Top of Inactive", "nominal"), ("Bottom of Conservation", "nominal"),
        ("Streambed", "nominal"), ("Dead Pool", "nominal"), ("Minimum Pool", "nominal"),
        ("Pool of Record", None), ("Spillway Crest", None), ("Top of Dam", None),
    ],
)
def test_level_zone_vocabulary(name, zone) -> None:
    assert levels.level_zone(name) == zone


def test_classify_inside_conservation_zone_is_nominal() -> None:
    """The reviewer's Foss case: 4.4 ft below Top of Conservation read 'watch'."""
    status, annotated = levels.classify(1637.6, FOSS_FT)
    assert status == "nominal"
    crossed = [t["specified_level_id"] for t in annotated if t["relation"] != "below"]
    assert set(crossed) == {"Streambed", "Top of Inactive", "Bottom of Conservation", "Bottom of Normal"}
    assert all(t["zone"] == "nominal" for t in annotated if t["relation"] != "below")


def test_classify_in_flood_pool_is_watch_then_action_then_flood() -> None:
    assert levels.classify(1645.0, FOSS_FT)[0] == "watch"   # above Top of Conservation
    assert levels.classify(1656.0, FOSS_FT)[0] == "action"  # above 24-hr Surveillance Begins
    assert levels.classify(1663.0, FOSS_FT)[0] == "flood"   # above Top of Flood


def test_classify_unrecognized_level_cannot_downgrade_a_recognized_crossing() -> None:
    """Codex counterexample: Flood Stage=10, unknown=20, observation 25 must be flood."""
    status, _ = levels.classify(25.0, [_t("Flood Stage", 10.0), _t("Mystery", 20.0)])
    assert status == "flood"


def test_classify_only_unrecognized_crossed_is_unknown() -> None:
    assert levels.classify(25.0, [_t("Mystery", 20.0), _t("Flood Stage", 30.0)])[0] == "unknown"


def test_classify_no_thresholds_is_unknown_not_nominal() -> None:
    assert levels.classify(5.0, [])[0] == "unknown"


def test_classify_below_everything_is_nominal() -> None:
    status, annotated = levels.classify(1500.0, FOSS_FT)
    assert status == "nominal"
    assert all(t["relation"] == "below" for t in annotated)


def test_classify_equal_valued_levels_is_deterministic() -> None:
    """Review focus #3: Top of Conservation == Top of Normal; ordering of the
    annotated list and the class must not depend on dict/listing order."""
    a = [_t("Top of Normal", 1642.0), _t("Top of Conservation", 1642.0)]
    b = list(reversed(a))
    sa, la = levels.classify(1642.0, a)
    sb, lb = levels.classify(1642.0, b)
    assert sa == sb == "watch"
    assert [t["specified_level_id"] for t in la] == [t["specified_level_id"] for t in lb]
```

- [ ] **Step 17.2: Run to verify failure**

Run: `uv run pytest tests/test_levels.py -k "zone or classify" -q`
Expected: FAIL.

- [ ] **Step 17.3: Implement**

Replace `classify` in `levels.py`:

```python
# Vocabulary → zone. Order matters: a "Bottom of Flood" is the conservation
# ceiling (watch), not a floor; floors are checked before "flood" so "Bottom
# of Flood Control" never reads as a flood crossing. Everything else is an
# unrecognized name (zone None) — it never downgrades a recognized crossing.
_CEILING_TERMS = (
    "top of conservation", "top of constant conservation", "top of multipurpose",
    "top of normal", "top of joint", "rule curve", "guide curve", "bottom of flood",
)
_FLOOR_TERMS = ("inactive", "bottom", "streambed", "dead", "invert", "minimum")
_FLOOD_TERMS = ("flood",)
_ACTION_TERMS = ("action", "warning", "surveillance")
_WATCH_TERMS = ("watch", "monitor")
_SEVERITY = {"nominal": 0, "watch": 1, "action": 2, "flood": 3}


def level_zone(specified_level_id: str) -> str | None:
    """The zone a crossed level implies, from its name; None when unrecognized."""
    name = specified_level_id.casefold()
    if any(term in name for term in _CEILING_TERMS):
        return "watch"
    if any(term in name for term in _FLOOR_TERMS):
        return "nominal"
    if any(term in name for term in _FLOOD_TERMS):
        return "flood"
    if any(term in name for term in _ACTION_TERMS):
        return "action"
    if any(term in name for term in _WATCH_TERMS):
        return "watch"
    return None


def classify(
    observation: float | None,
    thresholds: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Classify an observation against the active thresholds (#162).

    Returns `(status_class, annotated_thresholds)`. Each threshold is annotated
    with `relation` (above/at/below), `delta`, and `zone` (`level_zone`).
    `status_class` is the most severe zone among crossed (at/above) recognized
    levels — flood > action > watch > nominal — so a crossed floor or an
    unrecognized level can never mask a crossed flood stage. `unknown` when
    there is no observation, no thresholds, or only unrecognized levels were
    crossed; `nominal` when thresholds exist and none is crossed. Annotated
    thresholds are sorted by value descending, then name, for determinism.
    """
    if observation is None:
        return "unknown", thresholds
    annotated: list[dict[str, Any]] = []
    for t in thresholds:
        value = t.get("value")
        if not isinstance(value, (int, float)):
            continue
        relation = "above" if observation > value else "below" if observation < value else "at"
        annotated.append(
            {
                **t,
                "relation": relation,
                "delta": observation - value,
                "zone": level_zone(str(t.get("specified_level_id", ""))),
            }
        )
    annotated.sort(key=lambda t: (-t["value"], str(t.get("specified_level_id", ""))))
    if not annotated:
        return "unknown", annotated
    crossed = [t for t in annotated if t["relation"] in {"above", "at"}]
    if not crossed:
        return "nominal", annotated
    recognized = [t["zone"] for t in crossed if t["zone"] is not None]
    if not recognized:
        return "unknown", annotated
    return max(recognized, key=lambda z: _SEVERITY[z]), annotated
```

Export `level_zone`. In `core/models.py` add `zone: str | None = Field(default=None, description="Zone this level implies when crossed: flood|action|watch|nominal; null for an unrecognized name.")` to `ActiveThreshold`, and set the `StatusClass` docstring to:

```python
    """Heuristic status on `cwms_get_value` with `with_status`.

    The most severe zone among the thresholds the observation is at or above:
    flood (name contains 'flood'), action ('action', 'warning', 'surveillance'),
    watch ('watch', 'monitor', or a conservation ceiling such as Top of
    Conservation / Top of Normal / Rule Curve / Bottom of Flood). Floor-type
    levels (inactive, bottom, streambed, dead, minimum) count as nominal.
    'unknown' when no levels exist, the lookup was skipped or failed, or only
    unrecognized level names were crossed. Read `thresholds_active` (each with
    its `zone`) for the specific band.
    """
```

- [ ] **Step 17.4: Run, commit**

Run: `uv run pytest tests/test_levels.py tests/test_models.py tests/test_output_schema.py -q` → PASS.

```bash
git add -u && git commit -m "fix(levels): classify by most-severe recognized zone, not alphabetical name (#162)"
git push
```

### Task 18: Wire `get_value` — dedup, observation-time evaluation, tests, CHANGELOG, PR

**Files:**
- Modify: `src/cwms_tools/core/values.py:77-91, 486-587`
- Modify: `src/cwms_tools/core/shaping.py:114-125` (summary keeps `zone`; nothing to strip)
- Test: `tests/test_values.py`, `tests/test_cli_mcp_parity.py` (`VALUE` case gains `"zone": None` on the threshold)

- [ ] **Step 18.1: Write the failing tests**

Append to `tests/test_values.py`:

```python
def _levels_listing(*rows) -> dict:
    return {"levels": list(rows)}


def _level(lid: str, specified: str, date: str) -> dict:
    return {"location-level-id": lid, "specified-level-id": specified, "office-id": "SWT", "level-date": date}


def test_get_value_dedupes_level_revisions_and_classifies_nominal_inside_conservation(configured) -> None:
    """#162 end to end: two revisions per id, floor levels crossed → one row per
    id and `nominal`, with the value fetched at the newest revision."""
    listing = _levels_listing(
        _level("FOSS.Elev.Inst.0.Top of Flood", "Top of Flood", "1900-01-01T06:00:00Z"),
        _level("FOSS.Elev.Inst.0.Top of Flood", "Top of Flood", "1961-02-13T06:00:00Z"),
        _level("FOSS.Elev.Inst.0.Top of Conservation", "Top of Conservation", "1961-02-13T06:00:00Z"),
        _level("FOSS.Elev.Inst.0.Top of Inactive", "Top of Inactive", "1961-02-13T06:00:00Z"),
    )
    values_by_id = {"Top of Flood": 1662.2, "Top of Conservation": 1642.0, "Top of Inactive": 1597.2}

    def level_value(request):
        name = next(k for k in values_by_id if k in request.url.replace("%20", " "))
        return (200, {}, json.dumps({"constant-value": values_by_id[name], "level-units-id": "ft"}))

    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", points=[(datetime(2026, 10, 12, 14, tzinfo=UTC), 1637.6)]),
            status=200,
        )
        mocked.add(responses.GET, f"{API_ROOT}levels", json=listing, status=200)
        mocked.add_callback(responses.GET, re.compile(rf"{API_ROOT}levels/.*"), callback=level_value)
        payload = values.get_value("SWT", "FOSS", "Elev", classify_against_levels=True)
        level_reads = [c for c in mocked.calls if re.search(r"/levels/.+", c.request.url or "")]
    assert payload["status_class"] == "nominal"
    assert payload["level_lookup_status"] == "computed"
    ids = [t["level_id"] for t in payload["thresholds_active"]]
    assert len(ids) == len(set(ids)) == 3
    assert len(level_reads) == 3, "one value read per level id, at the newest revision"
    assert next(t for t in payload["thresholds_active"] if t["specified_level_id"] == "Top of Inactive")["zone"] == "nominal"


def test_get_value_with_no_levels_is_unknown_and_unavailable(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}catalog/TIMESERIES", json=TS_CATALOG, status=200)
        mocked.add(
            responses.GET,
            re.compile(rf"{re.escape(_ts_url())}\?.*"),
            json=_ts_payload(ts_id="FOSS.Elev.Inst.15Minutes.0.Ccp-Rev", points=[(datetime(2026, 5, 17, 18, tzinfo=UTC), 1648.21)]),
            status=200,
        )
        mocked.add(responses.GET, f"{API_ROOT}levels", json={"levels": []}, status=200)
        payload = values.get_value("SWT", "FOSS", "Elev", classify_against_levels=True)
    assert payload["status_class"] == "unknown"
    assert payload["level_lookup_status"] == "unavailable"
    assert payload["thresholds_active"] == []
```

Delete the now-contradicted `test_get_value_classifies_nominal_when_with_status_enabled` (it asserted `nominal` with no levels). Add `import json` to the test module. In `tests/test_level_error_classification.py` change `test_thresholds_skip_a_level_whose_value_fetch_fails` to expect `([], "unknown")` (no usable threshold is `unknown`, not `nominal`); its `_resolve()` helper keeps working because `at` gets a default (below). In the parity `VALUE` case set the threshold's `"zone": "flood"` and `"specified_level_id": "Flood Stage"` — a non-null value, so parity fails if either surface drops `zone` (null would be stripped on both and prove nothing); also add a `"zone": "nominal"` second threshold row. (The `level_reads` count is the check that each id is read once at its newest revision.)

- [ ] **Step 18.2: Run to verify failure**

Run: `uv run pytest tests/test_values.py -k "dedupes or no_levels_is_unknown" -q`
Expected: FAIL (6 threshold rows; `nominal` for no levels).

- [ ] **Step 18.3: Implement**

In `values.get_value` pass the observation time into the threshold lookup:

```python
    observed_at = _coerce_dt(timestamp) or datetime.now(tz=UTC)
    ...
        thresholds_active, status_class, level_lookup_status = _resolve_thresholds_with_timeout(
            timeout=budget, office=office, location=location, parameter=parameter,
            observation=observation, unit=series.get("unit", unit), at=observed_at,
        )
```

Thread `at` through `_resolve_thresholds_with_timeout(..., at: datetime)` and `_resolve_thresholds(..., at: datetime | None = None)` — the latter defaults to now so the existing `_resolve()` helper in `tests/test_level_error_classification.py` keeps its signature; in `_resolve_thresholds` replace the row loop head with:

```python
    at = at or datetime.now(tz=UTC)
    try:
        rows = levels.applicable_revisions(
            levels.list_levels(office, location=location, parameter=parameter), at=at
        )
    except CwmsToolsError:
        return [], "unknown"
    candidates = []
    for row in rows:
        level_id = levels.level_id_of(row)
        specified = row.get("specified-level-id") or row.get("specifiedLevelId")
        if not isinstance(level_id, str) or not isinstance(specified, str):
            continue
        revision = _coerce_dt(row.get("level-date") or row.get("levelDate")) or at
        try:
            level_value = levels.fetch_level_value(
                level_id, office=office, effective_date=revision, at=at, unit=unit
            )
        except CwmsToolsError:
            continue
        ...
```

Update `get_value`'s docstring: thresholds are the revisions in force at the observation timestamp and are evaluated there; `status_class` semantics per `StatusClass`. `_resolve_thresholds_with_timeout`'s "`unavailable`" branch (`not thresholds and status == "unknown"`) is now reached by design for "no levels defined".

- [ ] **Step 18.4: Full suite, CHANGELOG, PR**

Run: `uv run pytest -q` → PASS (the parity VALUE case, `test_cli_value.py::test_value_get_with_status_runs_classification` — check its expectation; with `{"levels": []}` it must now expect `unknown`/`unavailable`).

CHANGELOG `### Fixed`:

```markdown
- `cwms_get_value` / `value get --with-status`: `status_class` no longer reads
  `watch` for a pool inside its conservation zone. The class is now the most
  severe recognized zone among crossed levels (flood > action > watch >
  nominal; floor-type levels such as Top of Inactive or Streambed count as
  nominal; conservation ceilings as watch), never the alphabetically-last
  name. Each `thresholds_active` row carries its `zone`. With no levels
  defined the class is `unknown` with `level_lookup_status: unavailable`
  (was `nominal`). Duplicate rows are gone: one applicable revision per
  level id (CDA lists every historical revision). Seasonal levels are now
  evaluated at the observation timestamp instead of at their revision date
  (#162). Fingerprint moves (schema descriptions, new `zone` field).
```

```bash
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q && prek run --all-files
git add -u && git commit -m "fix(values): one revision per level, evaluate at observation time, honest status_class (#162)"
git push
gh pr create --fill --title "fix: honest status_class and de-duplicated thresholds (#162)" --body "Closes #162.

🤖 Generated with Claude Code

https://claude.ai/code/session_015kfeiw3yomoQw34HgijkLw"
```

---

## PR 5 — `fix/167-history-window-validation` (#167, #168)

### Task 19: Branch; shared window parsing and validation in core

- [ ] **Step 19.0: Branch and assign**

```bash
git checkout main && git pull && git checkout -b fix/167-history-window-validation
gh issue edit 167 --add-assignee @me && gh issue edit 168 --add-assignee @me
```

**Files:**
- Modify: `src/cwms_tools/core/values.py` (new helpers; `get_history` calls `require_window`)
- Modify: `src/cwms_tools/mcp/tools.py:528-551`, `src/cwms_tools/cli/commands/value.py:244-246, 346-363`
- Modify: `src/cwms_tools/mcp/resources.py` (`cwms_get_history` adds `usage_error`)
- Test: `tests/test_values.py`, `tests/test_mcp_tool_handlers.py`, `tests/test_cli_value.py`, `tests/test_error_code_contract.py`

**Interfaces:**
- `values.parse_rfc3339(value: str, *, field: str) -> datetime` — raises `invalid_field{field}` for unparsable input and for a parsed value with no timezone (reason: "RFC3339 with timezone; append Z for UTC, e.g. 2026-10-05T00:00:00Z").
- `values.require_window(begin: datetime, end: datetime, *, begin_field: str = "begin", end_field: str = "end") -> None` — raises `invalid_field` for a naive datetime (datetime callers), `usage_error{field: end_field}` when `end <= begin` (compared as instants), reason naming both values.
- `get_history` calls `require_window(begin, end)` before any upstream call.

- [ ] **Step 19.1: Write the failing tests**

Append to `tests/test_values.py`:

```python
def test_parse_rfc3339_rejects_naive_and_garbage() -> None:
    with pytest.raises(CwmsToolsError) as ex:
        values.parse_rfc3339("2026-10-05", field="begin_iso")
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD
    assert ex.value.envelope.details.field == "begin_iso"
    assert "append Z" in (ex.value.envelope.details.reason or "")
    with pytest.raises(CwmsToolsError):
        values.parse_rfc3339("not-a-date", field="end_iso")
    assert values.parse_rfc3339("2026-10-05T00:00:00Z", field="x") == datetime(2026, 10, 5, tzinfo=UTC)


def test_history_rejects_inverted_window_before_any_upstream_call(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        with pytest.raises(CwmsToolsError) as ex:
            values.get_history(
                "SWT", "FOSS", "Elev",
                begin=datetime(2026, 5, 18, tzinfo=UTC), end=datetime(2026, 5, 17, tzinfo=UTC),
            )
        assert len(mocked.calls) == 0
    env = ex.value.envelope
    assert env.code is ErrorCode.USAGE_ERROR
    assert env.details.field == "end"
    assert "2026-05-18" in (env.details.reason or "") and "2026-05-17" in (env.details.reason or "")


def test_history_rejects_equal_instants_across_offsets() -> None:
    """Review focus #4: +05:30 and Z spellings of the same instant are an empty window."""
    with pytest.raises(CwmsToolsError) as ex:
        values.require_window(
            datetime.fromisoformat("2026-05-17T05:30:00+05:30"),
            datetime.fromisoformat("2026-05-17T00:00:00+00:00"),
            begin_field="begin_iso", end_field="end_iso",
        )
    assert ex.value.envelope.code is ErrorCode.USAGE_ERROR and ex.value.envelope.details.field == "end_iso"


def test_require_window_rejects_naive_datetime() -> None:
    with pytest.raises(CwmsToolsError) as ex:
        values.require_window(datetime(2026, 5, 17), datetime(2026, 5, 18, tzinfo=UTC))
    assert ex.value.envelope.code is ErrorCode.INVALID_FIELD and ex.value.envelope.details.field == "begin"
```

Append to `tests/test_mcp_tool_handlers.py`:

```python
@pytest.mark.parametrize(
    "args,code,field",
    [
        ({"begin_iso": "2026-10-05", "end_iso": "2026-10-06T00:00:00Z"}, "invalid_field", "begin_iso"),
        ({"begin_iso": "2026-10-06T00:00:00Z", "end_iso": "2026-10-05T00:00:00Z"}, "usage_error", "end_iso"),
        ({"begin_iso": "2026-10-05T00:00:00Z", "end_iso": "2026-10-05T00:00:00Z"}, "usage_error", "end_iso"),
    ],
)
def test_get_history_window_validation(configured, args, code, field) -> None:
    server = build_server()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        result = _call(server, "cwms_get_history", {"office": "SWT", "name": "FOSS", "parameter": "Elev", **args})
        assert len(mocked.calls) == 0
    err = _branch(result.structured_content)["error"]
    assert err["code"] == code and err["details"]["field"] == field
```

Append to `tests/test_cli_value.py`:

```python
@pytest.mark.parametrize(
    "begin,end,code,field",
    [
        ("2026-10-05", "2026-10-06T00:00:00Z", "invalid_field", "begin"),
        ("2026-10-06T00:00:00Z", "2026-10-05T00:00:00Z", "usage_error", "end"),
    ],
)
def test_value_history_window_validation(begin, end, code, field) -> None:
    result = runner.invoke(app, ["value", "history", "SWT/FOSS/Elev", "--begin", begin, "--end", end])
    assert result.exit_code == 2
    err = json.loads(result.stderr)["error"]
    assert err["code"] == code and err["details"]["field"] == field
```

In `tests/test_error_code_contract.py` add `PROVOCATIONS[("cwms_get_history", "usage_error")] = ({**GOOD_ARGS["cwms_get_history"], "end_iso": GOOD_ARGS["cwms_get_history"]["begin_iso"]}, None)`.

- [ ] **Step 19.2: Run to verify failure**

Run: `uv run pytest tests/test_values.py tests/test_mcp_tool_handlers.py tests/test_cli_value.py tests/test_error_code_contract.py -k "rfc3339 or inverted or equal_instants or naive or window_validation or usage_error" -q`
Expected: FAIL.

- [ ] **Step 19.3: Implement**

In `values.py`:

```python
_TZ_HINT = "RFC3339 with timezone; append Z for UTC, e.g. 2026-10-05T00:00:00Z"


def parse_rfc3339(value: str, *, field: str) -> datetime:
    """Parse a surface timestamp argument; reject garbage and timezone-less input (#167)."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CwmsToolsError.of(
            ErrorCode.INVALID_FIELD, f"Could not parse {field} as RFC3339: {exc}",
            field=field, value=value, reason=_TZ_HINT,
        ) from exc
    if parsed.tzinfo is None:
        raise CwmsToolsError.of(
            ErrorCode.INVALID_FIELD, f"{field} has no timezone: {value!r}.",
            field=field, value=value, reason=f"No timezone was given, so the instant is ambiguous. {_TZ_HINT}",
        )
    return parsed


def require_window(
    begin: datetime, end: datetime, *, begin_field: str = "begin", end_field: str = "end"
) -> None:
    """Reject naive datetimes and empty/inverted windows before any upstream call (#167)."""
    for name, dt in ((begin_field, begin), (end_field, end)):
        if dt.tzinfo is None:
            raise CwmsToolsError.of(
                ErrorCode.INVALID_FIELD, f"{name} has no timezone.",
                field=name, value=dt.isoformat(), reason=_TZ_HINT,
            )
    if end <= begin:
        raise CwmsToolsError.of(
            ErrorCode.USAGE_ERROR,
            f"{end_field} must be after {begin_field}.",
            field=end_field,
            value=end.isoformat(),
            reason=(
                f"Got {begin_field}={begin.isoformat()} and {end_field}={end.isoformat()}; "
                "an empty or inverted window would return no data, not an error."
            ),
        )
```

`get_history`: call `require_window(begin, end)` right after the `rollup` check (before `offices.require_known_office`). MCP handler: replace the two `try/except ValueError` blocks with

```python
        try:
            begin = values.parse_rfc3339(begin_iso, field="begin_iso")
            end = values.parse_rfc3339(end_iso, field="end_iso")
            values.require_window(begin, end, begin_field="begin_iso", end_field="end_iso")
        except CwmsToolsError as err:
            return error_ref(err)
```

CLI `history`: replace `_parse_iso` with `values.parse_rfc3339(begin, field="begin")` / `("end")` wrapped in `try/except CwmsToolsError as err: emit_error(err)`, then `values.require_window(begin_dt, end_dt, begin_field="begin", end_field="end")` in the same try. Delete `_parse_iso`. Add `"usage_error"` to `TOOL_ERROR_CODES["cwms_get_history"]`. Export the two helpers in `values.__all__`.

- [ ] **Step 19.4: Run, commit**

Run: `uv run pytest -q` → PASS (`test_get_history_handler_rejects_bad_begin_iso`/`end_iso` and `test_value_history_rejects_bad_datetimes` keep passing: unparsable input is still `invalid_field`).

```bash
git add -u && git commit -m "fix(history): reject timezone-less and inverted windows on both surfaces (#167)"
git push -u origin fix/167-history-window-validation
```

### Task 20: Steer long raw windows toward rollup (#168); CHANGELOG; PR

**Files:**
- Modify: `src/cwms_tools/mcp/tools.py:508-513, 516-527`, `src/cwms_tools/cli/commands/value.py:211-224`, `src/cwms_tools/cli/commands/schema.py` (`--rollup` help), `src/cwms_tools/core/models.py` (`HistoryResponse.rollup` description)

- [ ] **Step 20.1: Prose (no behavior change)**

MCP `rollup` description:

```python
            "Downsample. 'raw' (default) returns every point and is capped at "
            "5,000 (30 days of 15-minute data is ~2,900 points, ~220 KB); for "
            "windows longer than about two days use 'hourly' or 'daily' — "
            "per-bucket min/max/mean/count in `buckets`, exempt from the raw "
            "cap (the upstream fetch cap can still apply; see `truncated`). "
            "`summary` is always present whatever the rollup.",
```

Same guidance in the tool docstring's second paragraph, the CLI `--rollup` help, the `schema.py` `_opt("--rollup", ...)` help, and `HistoryResponse.rollup`'s description ("the applied rollup; the server never changes it").

- [ ] **Step 20.2: CHANGELOG, gate, PR**

```markdown
### Fixed
- `cwms_get_history` / `value history`: an inverted or empty window
  (`end <= begin`) is now `usage_error` naming both fields instead of an empty
  `ok: true`; a timezone-less timestamp is rejected as `invalid_field` with the
  fix spelled out, matching the error text (#167). `usage_error` is advertised
  on `cwms_get_history`.
### Changed
- `rollup` descriptions steer windows longer than about two days to
  `hourly`/`daily` and state the raw cap and typical size (#168). Fingerprint
  moves.
```

```bash
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q && prek run --all-files
git add -u && git commit -m "docs(history): steer long raw windows toward rollup (#168)"
git push
gh pr create --fill --title "fix: history window validation and rollup steer (#167, #168)" --body "Closes #167. Closes #168.

🤖 Generated with Claude Code

https://claude.ai/code/session_015kfeiw3yomoQw34HgijkLw"
```

---

## PR 6 — `docs/171-first-call-contract` (#171, #165, #166)

### Task 21: Branch; first-call contract in `instructions` and `cwms://capabilities`

**Files:**
- Modify: `src/cwms_tools/mcp/server.py:51-67`, `src/cwms_tools/mcp/resources.py:164-267, 270-323`
- Test: `tests/test_capability_fingerprint_snapshot.py`, `tests/test_mcp_server.py`

- [ ] **Step 21.1: Branch and assign**

```bash
git checkout main && git pull && git checkout -b docs/171-first-call-contract
for n in 171 165 166; do gh issue edit $n --add-assignee @me; done
```

- [ ] **Step 21.2: Write the failing tests**

Append to `tests/test_capability_fingerprint_snapshot.py`:

```python
def test_instructions_and_capabilities_state_the_first_call_contract() -> None:
    """#171: an agent must not have to infer office+catalog-name, case
    sensitivity, the tool chain, or the no-forecasts scope from trial calls."""
    text = server_instructions()
    for needle in ("office", "case-sensitive", "cwms_search_places", "cwms_get_value", "forecast"):
        assert needle in text, needle
    first_call = capability_contract_payload()["first_call"]
    assert first_call["requires"] == ["office", "name"]
    assert "case-sensitive" in first_call["name"].casefold()
    assert first_call["typical_chain"][0] == "cwms_search_places"
    assert first_call["one_shot"] == "cwms_get_value"
    assert "forecast" in first_call["not_available"].lower()
    # Fingerprinted: it is agent-selection prose.
    assert "first_call" in capability_contract_payload()
```

- [ ] **Step 21.3: Implement**

`INSTRUCTIONS` in `server.py`: insert after the first sentence:

```python
    "First-call contract: every place-specific tool (`cwms_describe_place`, "
    "`cwms_list_parameters`, `cwms_get_value`, `cwms_get_history`, `cwms_get_profile`) "
    "needs an `office` code AND the case-sensitive catalog `name` (FTPK, not "
    "'Fort Peck'; FOSS, not 'Foss Lake'); `parameter` codes are case-sensitive "
    "(Elev, Flow-In). Typical chain: `cwms_search_places` (name → office + "
    "catalog id) → `cwms_describe_place` or `cwms_list_parameters` → "
    "`cwms_get_value` / `cwms_get_history`. With office and id already known, "
    "`cwms_get_value` alone is the one-shot path. No forecasts: this server "
    "reads observations and thresholds only. "
```

`capability_contract_payload()` gains, after `"does_not"`:

```python
        "first_call": {
            "requires": ["office", "name"],
            "office": "USACE office code (cwms://offices; NW Division uses NWDM/NWDP).",
            "name": "Case-sensitive catalog id (FOSS, not 'Foss' or 'Foss Lake'); from cwms_search_places.",
            "parameter": "Case-sensitive code (Elev, Flow-In); from cwms_list_parameters.",
            "typical_chain": [
                "cwms_search_places",
                "cwms_describe_place | cwms_list_parameters",
                "cwms_get_value | cwms_get_history",
            ],
            "one_shot": "cwms_get_value",
            "not_available": "Forecasts (see does_not).",
        },
```

and `capabilities_payload()` copies it through (`"first_call": contract["first_call"]`). Check `tests/test_mcp_server.py` for a pinned capabilities key list and add `first_call`.

- [ ] **Step 21.4: Run, commit**

Run: `uv run pytest tests/test_capability_fingerprint_snapshot.py tests/test_mcp_server.py tests/test_fastmcp_capabilities.py -q` → PASS.

```bash
git add -u && git commit -m "docs(mcp): state the first-call contract in instructions and capabilities (#171)"
git push -u origin docs/171-first-call-contract
```

### Task 22: `region_hints` on `cwms://offices`, fingerprinted

**Files:**
- Modify: `src/cwms_tools/core/offices.py` (static guidance), `src/cwms_tools/core/models.py:720-725`, `src/cwms_tools/mcp/resources.py:326-354`, `src/cwms_tools/mcp/contract.py:159-182`, `src/cwms_tools/core/fingerprint.py:64-110`
- Test: `tests/test_offices.py`, `tests/test_capability_fingerprint_snapshot.py`, `tests/test_cli_offices.py`

**Interfaces:**
- `offices.REGION_HINTS: tuple[dict, ...]` of `{"office": str, "region": str, "examples": list[str]}`; `offices.static_guidance() -> dict` returning `{nw_regional_rollup, nw_district_stubs, nw_rollup_targets, region_hints}` (the prose currently inlined in `resources.offices_payload`).
- `models.RegionHint(office, region, examples)`; `OfficesGuidance.region_hints: list[RegionHint]`.
- `fingerprint.compute(..., office_guidance: dict | None = None)` — new input; `contract.canonical_fingerprint()` passes `offices.static_guidance()`.

- [ ] **Step 22.1: Write the failing tests**

Append to `tests/test_offices.py`:

```python
def test_offices_payload_carries_region_hints(configured) -> None:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{API_ROOT}offices", json=_LIVE_SHAPE, status=200)
        from cwms_tools.mcp.resources import offices_payload

        payload = offices_payload()
    hints = {h["office"]: h for h in payload["guidance"]["region_hints"]}
    assert hints["SWT"]["region"].startswith("Tulsa District")
    assert "FOSS" in hints["SWT"]["examples"]
    assert hints["NWDM"]["examples"][0] == "FTPK"
    from cwms_tools.core.models import OfficesResponse

    OfficesResponse.model_validate(payload)  # the shared model accepts the new block
```

Append to `tests/test_capability_fingerprint_snapshot.py`:

```python
def test_office_guidance_is_a_fingerprint_input() -> None:
    base = fingerprint.compute(tools={}, resources=[], office_guidance={"region_hints": []})
    changed = fingerprint.compute(
        tools={}, resources=[], office_guidance={"region_hints": [{"office": "SWT"}]}
    )
    assert base != changed
```

- [ ] **Step 22.2: Run to verify failure, then implement**

In `offices.py`:

```python
#: Static region → office hints for first-call office selection (#171). Only
#: offices the bundled overview has evidence for; examples are catalog ids an
#: agent can pass to `cwms_get_value` directly. Fingerprinted via
#: `static_guidance()` — editing this changes what an agent does next.
REGION_HINTS: tuple[dict[str, Any], ...] = (
    {"office": "NWDM", "region": "Missouri River mainstem (MT, ND, SD, NE): Fort Peck, Garrison, Oahe, Big Bend, Fort Randall, Gavins Point", "examples": ["FTPK", "GARR", "OAHE", "GAPT"]},
    {"office": "NWDP", "region": "Columbia and Snake rivers (WA, OR, ID): Bonneville, The Dalles, John Day, McNary, Grand Coulee, Lower Granite; Seattle-area lakes", "examples": ["BON", "TDA", "JDA", "MCN", "GCL", "LWG", "GWLW_S1"]},
    {"office": "SWT", "region": "Tulsa District (OK, KS, north TX) flood-control reservoirs and locks", "examples": ["FOSS", "WEBB"]},
    {"office": "MVS", "region": "St. Louis District (MO, IL): middle Mississippi gages and lakes", "examples": ["Carlyle Lk"]},
    {"office": "LRN", "region": "Nashville District (TN, KY): Cumberland River projects", "examples": ["BARKLEY"]},
    {"office": "MVR", "region": "Rock Island District (IA, IL, MO): upper Mississippi locks and dams", "examples": ["IL02"]},
    {"office": "SPK", "region": "Sacramento District (CA, NV)", "examples": []},
)


def static_guidance() -> dict[str, Any]:
    """The static, agent-facing guidance block of `cwms://offices` (fingerprinted)."""
    return {
        "nw_regional_rollup": (
            "In Northwestern Division, query the regional rollups — NWDM (Missouri River) "
            "or NWDP (Pacific Northwest). The NW district offices (NWO, NWK, NWS, NWP, NWW) "
            "are catalog stubs that publish no operational data in CDA."
        ),
        "nw_district_stubs": sorted(NW_ROLLUP_TARGETS),
        "nw_rollup_targets": dict(NW_ROLLUP_TARGETS),
        "region_hints": [dict(h) for h in REGION_HINTS],
    }
```

`resources.offices_payload()` uses `"guidance": offices.static_guidance()`. `models.py`: add `class RegionHint(CompactDumpMixin, BaseModel)` with `office: str`, `region: str`, `examples: list[str]`, and `region_hints: list[RegionHint] = Field(default_factory=list, description="Static region → office hints with example catalog ids; a starting point, not the full directory.")` on `OfficesGuidance`. `fingerprint.compute`: add `office_guidance: dict[str, Any] | None = None` and `"office_guidance": office_guidance or {}` in the payload (document as input 12 in the module docstring). `contract.canonical_fingerprint()`: `office_guidance=offices.static_guidance()` (import `offices` from core). The `cwms://offices` resource docstring (server.py:220-230) also gets its #172.3 cleanup here — drop the executor paragraph; say "Includes `region_hints` (region → office with example ids)."

- [ ] **Step 22.3: Run, commit**

Run: `uv run pytest tests/test_offices.py tests/test_cli_offices.py tests/test_capability_fingerprint_snapshot.py tests/test_mcp_server.py -q` → PASS.

```bash
git add -u && git commit -m "feat(offices): region_hints on cwms://offices, fingerprinted static guidance (#171)"
git push
```

### Task 23: `with_status` wording (#165) and `last_data_timestamp` descriptions (#166)

**Files:**
- Modify: `src/cwms_tools/mcp/tools.py:445-459`, `src/cwms_tools/cli/commands/value.py:85-98, 107-111`, `src/cwms_tools/cli/commands/schema.py` (`--with-status` help, `value get` notes)
- Modify: `src/cwms_tools/core/models.py` (`PlaceSummary`, `DescribePlaceResponse`, `ListParametersResponse` `last_data_timestamp`)
- Test: `tests/test_models.py`

- [ ] **Step 23.1: Write the failing test**

Append to `tests/test_models.py`:

```python
import pytest

from cwms_tools.core import models as M


@pytest.mark.parametrize("model", [M.PlaceSummary, M.DescribePlaceResponse, M.ListParametersResponse])
def test_last_data_timestamp_is_described_as_a_lagging_catalog_extent(model) -> None:
    desc = model.model_fields["last_data_timestamp"].description or ""
    assert "catalog" in desc and "lag" in desc and "cwms_get_value" in desc
```

- [ ] **Step 23.2: Implement prose**

Shared constant in `models.py`:

```python
_LAST_DATA_DESC = (
    "Latest observation time the CDA timeseries catalog reports for this "
    "location (its `latest-time` extent, else `last-update`). Catalog extents "
    "can lag live data by weeks and may be absent for a location that does "
    "publish; use `cwms_get_value` for freshness."
)
```

applied as `Field(default=None, description=_LAST_DATA_DESC)` on the three models. MCP `with_status`:

```python
            "Also classify against CWMS Location Levels (one /levels listing plus "
            "one read per level; observed ~1.5 s on SWT/FOSS, longer on large "
            "offices). Bounded by an 8 s budget: on timeout the value is still "
            "returned with `level_lookup_status: 'timed_out'` and no thresholds; a "
            "later call may succeed once the listing is cached. Default off.",
```

Tool docstring: replace "is slow and often times out" with "adds the threshold lookup (see `with_status`)". CLI `--with-status` help and the `schema.py` `value get` option help/notes: same facts in CLI phrasing; remove "reliably slow".

- [ ] **Step 23.3: CHANGELOG, gate, PR**

```markdown
### Changed
- Server `instructions` and `cwms://capabilities` (`first_call`) state the
  first-call contract: office code + case-sensitive catalog name, case-sensitive
  parameter codes, the typical tool chain, the one-shot path, and that there are
  no forecasts. `cwms://offices` carries `region_hints` (region → office with
  example ids); the static office guidance is now a fingerprint input (#171).
- `with_status` / `--with-status` describe the measured cost and the timeout
  behavior instead of "often times out" (#165).
- `last_data_timestamp` is described as the CDA catalog extent, which can lag
  live data by weeks (#166). Field name unchanged.
- Capability fingerprint moves (prose and the new `region_hints` field).
```

```bash
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q && prek run --all-files
git add -u && git commit -m "docs: honest with_status and last_data_timestamp descriptions (#165, #166)"
git push
gh pr create --fill --title "docs: first-call contract, region hints, honest with_status/last_data_timestamp (#171, #165, #166)" --body "Closes #171. Closes #165. Closes #166.

🤖 Generated with Claude Code

https://claude.ai/code/session_015kfeiw3yomoQw34HgijkLw"
```

---

## PR 7 — `chore/172-wire-polish` (#172 items 1, 3, 4; item 2 deferred)

### Task 24: Branch; passthrough-DTO descriptions (item 1)

**Files:**
- Modify: `src/cwms_tools/core/models.py` (`DescribePlaceResponse.location`, `.project`, `.locks`; `PlaceSummary.raw`)
- Test: `tests/test_models.py`

- [ ] **Step 24.1**

```bash
git checkout main && git pull && git checkout -b chore/172-wire-polish && gh issue edit 172 --add-assignee @me
```

Test:

```python
def test_passthrough_dto_fields_say_they_are_verbatim_kebab_case() -> None:
    for model, field in [
        (M.DescribePlaceResponse, "location"), (M.DescribePlaceResponse, "project"),
        (M.DescribePlaceResponse, "locks"), (M.PlaceSummary, "raw"),
    ]:
        desc = model.model_fields[field].description or ""
        assert "kebab-case" in desc, (model.__name__, field)
```

Implement, one accurate description per field (upstream key spelling is preserved everywhere; normalization and detail pruning differ):
- `location`: "Upstream Location DTO; keys keep CDA's kebab-case spelling (`office-id`, `public-name`), not the envelope's snake_case. detail=summary keeps the triage subset; full is verbatim."
- `project`: keep the existing text and prepend "Upstream Project DTO with kebab-case keys (not normalized to snake_case)."
- `locks`: "Lock records normalized from the upstream DTO: kebab-case keys preserved, sentinel (-9999) dimensions nulled and named in `invalid_fields`, a failed lookup is `{name, error}`; detail=summary keeps the dimension subset."
- `PlaceSummary.raw`: prepend "Verbatim upstream Location DTO with kebab-case keys; " to its existing text.
Commit `docs(models): say which passthrough fields keep kebab-case keys and which are normalized (#172)`.

### Task 25: Overview index summaries (item 4) and fingerprinting them

**Files:**
- Modify: `src/cwms_tools/core/overview.py:83-102`, `src/cwms_tools/core/fingerprint.py`
- Test: `tests/test_overview.py`, `tests/test_capability_fingerprint_snapshot.py`

- [ ] **Step 25.1: Write the failing tests**

```python
def test_summaries_are_prose_not_rules_or_link_lists() -> None:
    by_id = {s.section_id: s.summary for s in overview.all_sections()}
    assert by_id["reference-where-to-look-for-specific-things"] == (
        "Reference section: Reference — where to look for specific things."
    )
    assert by_id["sources"].startswith("Reference section:")
    for section_id, summary in by_id.items():
        assert summary and summary != "---", section_id
        assert not summary.startswith(("-", "|", "#")), section_id
```

and in the snapshot test:

```python
def test_overview_summaries_are_a_fingerprint_input(monkeypatch) -> None:
    from cwms_tools.core import overview as ov

    base = fingerprint.compute(tools={}, resources=[])
    first = ov.all_sections()[0]
    monkeypatch.setattr(ov, "all_sections", lambda: [first.__class__(**{**first.__dict__, "summary": "changed"})])
    assert fingerprint.compute(tools={}, resources=[]) != base
```

(Check how `OverviewSection` is constructed — it is a dataclass in `overview.py`; use `dataclasses.replace(first, summary="changed")` if so.)

- [ ] **Step 25.2: Implement**

Rewrite `_first_summary(body, *, title)` to work on blank-line-separated paragraphs instead of individual stripped lines (the Sources section's list has indented continuation lines under `- Key modules:` that the old line filter let through as prose): split `body` on blank lines; a paragraph is a *list block* if its first non-blank line starts with `- `, `* `, `+ `, or `<digits>. `; skip paragraphs that are headings (`#`), tables (`|`), horizontal rules (`---`/`***`), list blocks (every continuation line of the block goes with it), or fenced code; the first remaining paragraph yields the first ≤3 sentences as today; when none remains return `f"Reference section: {title}."`. Update `_build_section` to pass `title`. Add a unit test that feeds the literal multiline Sources list (copy the `- Key modules:` block from `cwms-overview.md`) plus a trailing prose paragraph and asserts the prose wins, and one with only the list asserting the fallback. In `fingerprint.compute` add `"overview_summaries": {s.section_id: s.summary for s in overview.all_sections()}` to the payload and document it as input 13. Run `uv run pytest tests/test_overview.py tests/test_capability_fingerprint_snapshot.py tests/test_mcp_server.py -q`; commit `fix(overview): real index summaries for reference-only sections; fingerprint them (#172)`.

### Task 26: Defer item 2; CHANGELOG; PR

- [ ] **Step 26.1: File the 0.9.0 follow-up**

```bash
gh issue create --title "0.9.0: unify repair shapes (repair_hint.args/reason → arguments/next_step) across search, publisher, and error envelopes" --label enhancement --body "Deferred from #172 (Codex review): renaming \`SearchRepairHint\` fields is a breaking wire change that does not belong in a patch release. Plan one coherent migration for 0.9.0: \`SearchPlacesResponse.repair_hint\` and \`PublishersForParameterResponse.repair\` adopt the error \`RepairHint\` shape (\`next_step\`, \`tool\`, \`arguments\`, \`alternative\`, plus \`message\`), \`COMPACT_ERROR_SCHEMA\` in \`mcp/output_schema.py\` and its field-set tests are updated, a \`deprecations\` entry announces the removal, and the CHANGELOG calls out the break. Until then both shapes stay as documented."
```

Add the resulting issue number to `capabilities.deprecation_policy`? No — nothing is deprecated yet; just link it in the CHANGELOG line.

- [ ] **Step 26.2: CHANGELOG, gate, PR**

```markdown
### Changed
- Passthrough upstream DTOs (`location`, `project`, `locks`, `raw`) are
  documented as verbatim kebab-case records; envelope fields stay snake_case
  (#172).
- `cwms://offices` resource description no longer mentions internal executor
  details (#172).
- `cwms://overview` index summaries for the reference and sources sections are
  now real one-line summaries instead of "---" and a link list; summaries are a
  fingerprint input (#172).
- Unifying the success-side `repair_hint` shape with the error `repair` shape is
  deferred to 0.9.0 as a breaking change (#<new issue>).
```

```bash
uv run ruff format && uv run ruff check && uv run ty check && uv run pytest -q && prek run --all-files
git add -u && git commit -m "chore(wire): passthrough-DTO notes, offices prose, overview summaries (#172)"
git push
gh pr create --fill --title "chore: wire-shape polish (#172)" --body "Closes #172 (item 2 deferred to the linked 0.9.0 issue).

🤖 Generated with Claude Code

https://claude.ai/code/session_015kfeiw3yomoQw34HgijkLw"
```

---

## After all seven PRs

- Each PR: wait for `CI success`, request/evaluate the Copilot review (Brian requests it manually if the auto-request does not fire), address findings, report green. Brian merges.
- `## [Unreleased]` then becomes `## [0.8.1]`; the release checklist in AGENTS.md applies (tag, PyPI via CI, `uvx` smoke test with 0.8.0 as the control). The version bump must also regenerate `manifest.json` — `tests/test_manifest.py` pins its version to `pyproject.toml` and the installed package.
- Re-run the reviewer's seven first-try questions against the published 0.8.1 with an in-process FastMCP client and note the first-call success count on milestone 2 before closing it.
