"""Office discovery — primary source `cwms.api.get("offices")` with caching.

Backs two surfaces:

- the `cwms://offices` MCP resource (and its office-code discovery role),
  via `list_offices()` which returns full office records; and
- the optional-`office` mode of `cwms_search_places`: when an agent asks
  "what is the water temperature at Fremont Bridge?" without naming an
  office, the tool fans out across offices already cached this session.

This module supplies the office list both upstream-fresh and as a degraded
fallback for the "no offices cached, no upstream" cold start. The fanout
itself is in `core/places.py:search_places` and mirrors the budgeted
pattern from `core/publishers_index.py` so we never expand to the full
~68-office surface implicitly.
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any
from urllib.parse import urljoin

import requests
from cwms import api as cwms_api
from requests.adapters import HTTPAdapter

from cwms_tools.core.cache import Cache, build_cache_key, get_cache
from cwms_tools.core.errors import CwmsToolsError, ErrorCode, RepairHint
from cwms_tools.core.session import current_config

# NW Division district stubs — catalog stubs that publish no operational
# data in CDA (cwms-overview.md §6.1). Canonical home for this map (#69):
# previously triplicated across `core.catalog`, `core.locations`, and
# `mcp.resources` — those now import it from here.
NW_STUBS: frozenset[str] = frozenset({"NWO", "NWK", "NWS", "NWP", "NWW"})
NW_ROLLUP_TARGETS: dict[str, str] = {
    "NWO": "NWDM",
    "NWK": "NWDM",
    "NWS": "NWDP",
    "NWP": "NWDP",
    "NWW": "NWDP",
}


def nw_rollup_target(office_id: str) -> str:
    """The regional rollup office that publishes data for an NW district stub."""
    return NW_ROLLUP_TARGETS.get(office_id, "NWDM")


def ghost_office_error(office_id: str) -> CwmsToolsError:
    """Build the canonical `ghost_office` error (no `repair` attached).

    Single home for this envelope (#69: `NW_STUBS`/`ghost_office_repair`
    already live here) so `core.locations` and `core.catalog` — both of which
    short-circuit NW-stub offices — emit byte-identical guidance instead of
    two copies that can drift. The surface boundary (`mcp.tools._safe`,
    `cli.render`) attaches the same-tool retry repair via `ghost_office_repair`.
    """
    return CwmsToolsError.of(
        ErrorCode.GHOST_OFFICE,
        f"Office {office_id} publishes no operational data; use the regional rollup.",
        field="office_id",
        value=office_id,
        reason=(
            "NW Division districts (NWO, NWK, NWS, NWP, NWW) are catalog stubs. "
            "Use NWDM (Missouri) or NWDP (Pacific NW) instead. The "
            "`cwms://offices` resource lists every valid office code."
        ),
    )


def ghost_office_repair(office_id: str, *, tool: str, args: dict[str, Any]) -> RepairHint:
    """Build a same-call retry repair hint: `office` swapped to the rollup target.

    `tool`/`args` are the FAILING call's own name and original (wire-format)
    arguments; an `office` in `args` is overwritten with the rollup target, so
    callers may include it or leave it out. The caller (an MCP tool handler or
    CLI command) is the only thing that knows which surface call is retrying, so it
    supplies its own identity here rather than this being hardcoded to a
    single fallback tool (#69: the repair no longer silently switches from,
    say, `cwms_get_value` to `cwms_browse_region`, discarding the agent's
    `name`/`parameter` and forcing re-orchestration)."""
    return RepairHint(
        next_step="retry_with_rollup_office",
        tool=tool,
        arguments={**args, "office": nw_rollup_target(office_id)},
    )


def ghost_offices_repair(
    offices_arg: list[str], *, tool: str, args: dict[str, Any], key: str
) -> RepairHint:
    """`ghost_office_repair` for a tool whose office argument is a list (#183).

    Each NW stub in `offices_arg` becomes its rollup; other codes are kept, and
    the result is deduplicated in order (`[NWO, NWK, NWS]` -> `[NWDM, NWDP]`).
    `key` is the surface's own argument name (`offices` on MCP, `office` for
    the CLI's repeatable `--office`)."""
    rewritten: list[str] = []
    for office in offices_arg:
        target = nw_rollup_target(office.upper()) if office.upper() in NW_STUBS else office
        if target not in rewritten:
            rewritten.append(target)
    return RepairHint(
        next_step="retry_with_rollup_office",
        tool=tool,
        arguments={**args, key: rewritten},
    )


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

# CDA `/offices` `type` codes → human label. Unknown codes pass through as
# themselves (see `_type_label`) so a new upstream code degrades gracefully
# rather than vanishing. Codes verified live 2026-06-10; see cwms-overview.md §4.1.
_TYPE_LABELS: dict[str, str] = {
    "HQ": "corps headquarters",
    "MSC": "division headquarters",
    "MSCR": "division regional",
    "DIS": "district",
    "FOA": "field operating activity",
    "UNK": "unknown",
}


def _type_label(code: str) -> str:
    return _TYPE_LABELS.get(code, code)


def list_offices(*, use_cache: bool = True) -> tuple[list[dict[str, Any]], bool]:
    """Return `(office_records, used_fallback)`.

    Each record is a normalized dict with `name` and, when upstream
    supplies them, `long_name`, `type` (raw CDA code), `type_label`
    (human label), and `reports_to`. Records are sorted by `name`.

    Primary source is `cwms.api.get("offices")` cached under the `"offices"`
    namespace (7-day TTL, see `core/cache.py`). On upstream failure or an
    empty/unrecognized payload, returns the documented degraded fallback as
    name-only records with `used_fallback=True`, so a caller can surface
    `partial: true` to the agent. Cache access (init, read, write) is
    best-effort: a broken cache (unwritable directory, corrupted store) must
    not turn a working upstream fetch into an unstructured crash for callers
    that advertise this as a never-raises path (#78) — `cwms_list_offices`,
    the `cwms://offices` resource, and the CLI `offices` command all rely on
    it degrading gracefully instead.
    """
    cfg = current_config()
    cache: Cache | None = None
    key: str | None = None
    if use_cache:
        try:
            cache = get_cache()
            key = build_cache_key("offices", "records", api_root=cfg.api_root)
            hit = cache.get(key)
        except Exception:
            cache = None
            key = None
        else:
            if isinstance(hit, list) and hit:
                return [dict(r) for r in hit], False
    try:
        raw = cwms_api.get("offices")
    except Exception:
        return _fallback_records(), True
    records = _parse_office_records(raw)
    if not records:
        return _fallback_records(), True
    records.sort(key=lambda r: r["name"])
    if cache is not None and key is not None:
        with suppress(Exception):
            cache.set(key, records, ttl=cache.ttl_for("offices"))
    return [dict(r) for r in records], False


#: Bound on the validation-only directory fetch (connect, read). `cwms.api.get`
#: exposes no timeout and its session retries 6x with backoff, so the preflight
#: uses its own non-retrying session: one attempt per cooldown window.
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


_preflight_session: requests.Session | None = None


def _preflight_session_for(source: requests.Session) -> requests.Session:
    """A plain, non-retrying session carrying cwms-python's default headers (auth)."""
    global _preflight_session  # noqa: PLW0603
    if _preflight_session is None:
        sess = requests.Session()
        adapter = HTTPAdapter(max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _preflight_session = sess
    _preflight_session.headers.update(source.headers)
    return _preflight_session


def _fetch_directory_bounded() -> list[dict[str, Any]]:
    """One attempt, no retries, at GET /offices.

    The producer's own call still goes through cwms-python's retrying session;
    this preflight must not inherit its `Retry(total=6)` backoff.
    """
    source = cwms_api.SESSION
    url = urljoin(cwms_api.return_base_url(), "offices")
    headers = {"Accept": cwms_api.api_version_text(cwms_api.API_VERSION)}
    sess = _preflight_session_for(source)
    with sess.get(url, headers=headers, timeout=_DIRECTORY_TIMEOUT_S) as resp:
        resp.raise_for_status()
        return _parse_office_records(resp.json())


def directory_for_validation() -> tuple[list[str], bool]:
    """`(office_ids, used_fallback)` for `require_known_office`.

    Cached directory first (the same cache key `list_offices` writes, so a
    `cwms://offices` read or a prior validation serves both); else one bounded
    fetch. Any cache or network failure is `([], True)`: this never raises on
    those (a preflight must not turn an outage into an error).
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
    directory is on its degraded fallback the input is upper-cased without
    directory validation (CWMS codes are uppercase) -- never block on a
    degraded list -- and validation stays off for `_DIRECTORY_RETRY_SECONDS`
    after that lookup finished.
    """
    upper = office.upper()
    if upper in NW_STUBS:
        raise ghost_office_error(upper)
    api_root = current_config().api_root
    since = _directory_fallback.get(api_root)
    if since is not None and time.monotonic() - since < _DIRECTORY_RETRY_SECONDS:
        return upper
    ids, used_fallback = directory_for_validation()
    if used_fallback:
        _directory_fallback[api_root] = time.monotonic()  # after the lookup completed
        return upper
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


def list_office_ids(*, use_cache: bool = True) -> tuple[list[str], bool]:
    """Return `(office_ids, used_fallback)` — names only, derived from `list_offices`."""
    records, used_fallback = list_offices(use_cache=use_cache)
    return [r["name"] for r in records], used_fallback


def cached_offices_for_locations() -> list[str]:
    """Return office IDs whose unfiltered locations catalog is already cached.

    Used as the default fanout scope when an agent omits `office` from
    `cwms_search_places`. The check probes the cache for the unfiltered
    `location_catalog` key per candidate office; this is cheap because
    `cache.get` short-circuits on miss.

    Candidate set is the same heuristic slice the fallback uses, since
    diskcache keys are SHA-256 hashed and we cannot iterate them directly.
    """
    # Local import: `catalog` imports this module (`NW_STUBS`), so a module-level
    # import here would be circular.
    from cwms_tools.core import catalog  # noqa: PLC0415

    return sorted(o for o in _FALLBACK_OFFICES if catalog.location_catalog_cached(o))


def discovery_office_candidates() -> list[str]:
    """The curated data-bearing office set used to seed name-first discovery.

    When an agent searches by bare name with no office in scope, this is the
    concrete list `cwms_search_places` names in its `repair_hint` (issue #24):
    the same heuristic slice `cached_offices_for_locations` probes — regional
    rollups (NWDM/NWDP) plus the district-publishing offices — so the agent
    gets a realistic, mostly data-bearing starting set rather than the full
    ~68-office surface. Returned in curated priority order (see
    `DISCOVERY_OFFICE_PRIORITY`); the fan-out budget takes the first N on a
    cold start.
    """
    return list(DISCOVERY_OFFICE_PRIORITY)


def _fallback_records() -> list[dict[str, Any]]:
    """Name-only records for the degraded fallback path (no upstream metadata)."""
    return [{"name": name} for name in sorted(_FALLBACK_OFFICES)]


def _parse_office_records(raw: Any) -> list[dict[str, Any]]:
    """Tolerate the common CDA shapes for the offices payload.

    Live CDA returns a JSON array of `{name, long-name, type, reports-to}`;
    some wrappers nest it under `offices`/`entries`/`items`.
    """
    if isinstance(raw, list):
        items: list[Any] = raw
    elif isinstance(raw, dict):
        candidate: Any = raw.get("offices") or raw.get("entries") or raw.get("items") or []
        items = candidate if isinstance(candidate, list) else []
    else:
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, str):
            out.append({"name": it})
            continue
        if not isinstance(it, dict):
            continue
        name = it.get("office-id") or it.get("id") or it.get("name")
        if not isinstance(name, str):
            continue
        record: dict[str, Any] = {"name": name}
        long_name = it.get("long-name") or it.get("longName")
        if isinstance(long_name, str):
            record["long_name"] = long_name
        code = it.get("type")
        if isinstance(code, str) and code:
            record["type"] = code
            record["type_label"] = _type_label(code)
        reports_to = it.get("reports-to") or it.get("reportsTo")
        if isinstance(reports_to, str):
            record["reports_to"] = reports_to
        out.append(record)
    return out


#: Static region -> office hints for first-call office selection (#171). Only
#: offices the bundled overview has evidence for; examples are data-bearing
#: catalog ids an agent can pass to `cwms_get_value` directly. Fingerprinted via
#: `static_guidance()`: editing this changes what an agent does next.
REGION_HINTS: tuple[dict[str, Any], ...] = (
    {
        "office": "NWDM",
        "region": (
            "Missouri River mainstem (MT, ND, SD, NE): Fort Peck, Garrison, Oahe, "
            "Big Bend, Fort Randall, Gavins Point"
        ),
        "examples": ["FTPK", "GARR", "OAHE", "GAPT"],
    },
    {
        "office": "NWDP",
        "region": (
            "Columbia and Snake rivers (WA, OR, ID): Bonneville, The Dalles, John Day, "
            "McNary, Grand Coulee, Lower Granite"
        ),
        "examples": ["BON", "TDA", "JDA", "MCN", "GCL", "LWG"],
    },
    {
        "office": "SWT",
        "region": "Tulsa District (OK, KS, north TX) flood-control reservoirs and locks",
        "examples": ["FOSS", "WEBB"],
    },
    {
        "office": "MVS",
        "region": "St. Louis District (MO, IL): middle Mississippi gages and lakes",
        "examples": ["Carlyle Lk"],
    },
    {
        "office": "LRN",
        "region": "Nashville District (TN, KY): Cumberland River projects",
        "examples": [],
    },
    {
        "office": "MVR",
        "region": "Rock Island District (IA, IL, MO): upper Mississippi locks and dams",
        "examples": ["IL02"],
    },
    {"office": "SPK", "region": "Sacramento District (CA, NV)", "examples": []},
)


def static_guidance() -> dict[str, Any]:
    """The static, agent-facing guidance block of `cwms://offices` (fingerprinted)."""
    return {
        "nw_regional_rollup": (
            "In Northwestern Division, query the regional rollups — NWDM "
            "(Missouri River) or NWDP (Pacific Northwest). The NW district "
            "offices (NWO, NWK, NWS, NWP, NWW) are catalog stubs that "
            "publish no operational data in CDA."
        ),
        "nw_district_stubs": sorted(NW_ROLLUP_TARGETS),
        "nw_rollup_targets": dict(NW_ROLLUP_TARGETS),
        "region_hints": [{**h, "examples": list(h["examples"])} for h in REGION_HINTS],
    }


__all__ = [
    "DISCOVERY_OFFICE_PRIORITY",
    "NW_ROLLUP_TARGETS",
    "NW_STUBS",
    "REGION_HINTS",
    "cached_offices_for_locations",
    "directory_for_validation",
    "discovery_office_candidates",
    "ghost_office_error",
    "ghost_office_repair",
    "ghost_offices_repair",
    "list_office_ids",
    "list_offices",
    "nw_rollup_target",
    "require_known_office",
    "static_guidance",
]
