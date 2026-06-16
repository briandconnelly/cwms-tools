"""Pydantic v2 models for cwms-tools.

Two model tiers:

1. **Task-response models** — normalized response shapes this layer
   guarantees. Used directly as MCP tool `outputSchema`s and CLI JSON
   payloads. Strip nulls/defaults in summary mode.
2. **DTO facades** — thin Pydantic models keyed to the upstream CWMS Java
   DTOs (`Location`, `Project`, `LocationLevel`, ...). `extra="allow"` so
   unknown upstream fields pass through unchanged; new upstream fields are
   non-breaking. Surface only at `detail=full` under a nested `raw` field.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cwms_tools.core._compact import CompactDumpMixin
from cwms_tools.core.errors import (
    ErrorEnvelope,  # noqa: TC001 — runtime import: Pydantic resolves ErrorRef.error annotation at class-build time
)

if TYPE_CHECKING:
    from cwms_tools.core.errors import CwmsToolsError

# --------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------


class Detail(StrEnum):
    """Response density: 'summary' is the compact default; 'full' includes verbose upstream fields and per-point quality codes where applicable."""  # noqa: E501

    SUMMARY = "summary"
    FULL = "full"


class Unit(StrEnum):
    """Unit system. 'EN' is English (ft, cfs); 'SI' is metric (m, cms)."""

    EN = "EN"
    SI = "SI"


class Rollup(StrEnum):
    """History downsample mode: 'raw' returns every point; 'hourly'/'daily' return per-bucket min/max/mean/count aggregates."""  # noqa: E501

    RAW = "raw"
    HOURLY = "hourly"
    DAILY = "daily"


class SourceMeta(CompactDumpMixin, BaseModel):
    """Provenance attached to every successful tool response."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(description="Capability fingerprint at call time.")
    workaround: str | None = Field(
        default=None,
        description="Identifier of any active cwms-python bug workaround invoked.",
    )
    endpoints_called: list[str] = Field(default_factory=list)
    cached: bool = Field(
        default=False,
        description="True if the response was served wholly from cache.",
    )
    upstream_status: int | None = Field(
        default=None,
        description=(
            "Upstream HTTP status code from a recovered partial-success path. "
            "Set on responses where a sub-call returned a non-2xx that we "
            "handled into a partial response (e.g. project lookup 404 for a "
            "non-project location). Omitted on normal success paths."
        ),
    )


class ErrorRef(BaseModel):
    """The in-band `{ok: false, error: {...}}` envelope returned by tool handlers.

    `error` is the full `ErrorEnvelope` so the published outputSchema documents
    the failure contract (code, field, repair, retryable, retry_after_ms,
    request_id) instead of an opaque object.
    """

    model_config = ConfigDict(extra="forbid")

    ok: Literal[False] = False
    error: ErrorEnvelope

    @classmethod
    def from_error(cls, err: CwmsToolsError) -> ErrorRef:
        """Build the in-band error shape from a `CwmsToolsError`. The single
        source of this conversion for every MCP tool, so all tool errors look
        identical. The envelope is deep-copied so callers may mutate `ref.error`
        (e.g. stamping the capability fingerprint) without aliasing the
        exception's envelope."""
        return cls(error=err.envelope.model_copy(deep=True))


# --------------------------------------------------------------------------
# DTO facades — extra=allow so unknown upstream fields pass through.
# --------------------------------------------------------------------------


class CdaLocation(BaseModel):
    """Facade over the upstream CWMS Location DTO."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    office_id: str = Field(alias="office-id")
    location_kind: str | None = Field(default=None, alias="location-kind")
    location_type: str | None = Field(default=None, alias="location-type")
    timezone_name: str | None = Field(default=None, alias="timezone-name")
    horizontal_datum: str | None = Field(default=None, alias="horizontal-datum")
    latitude: float | None = None
    longitude: float | None = None
    published_latitude: float | None = Field(default=None, alias="published-latitude")
    published_longitude: float | None = Field(default=None, alias="published-longitude")
    nation: str | None = None
    state_initial: str | None = Field(default=None, alias="state-initial")
    county_name: str | None = Field(default=None, alias="county-name")
    nearest_city: str | None = Field(default=None, alias="nearest-city")
    public_name: str | None = Field(default=None, alias="public-name")
    long_name: str | None = Field(default=None, alias="long-name")
    description: str | None = None
    active: bool | None = None


class CdaProject(BaseModel):
    """Facade over the upstream CWMS Project DTO."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    location: CdaLocation | None = None
    federal_cost: float | None = Field(default=None, alias="federal-cost")
    non_federal_cost: float | None = Field(default=None, alias="non-federal-cost")
    cost_year: int | None = Field(default=None, alias="cost-year")
    cost_unit: str | None = Field(default=None, alias="cost-unit")
    authorizing_law: str | None = Field(default=None, alias="authorizing-law")
    project_owner: str | None = Field(default=None, alias="project-owner")
    hydropower_desc: str | None = Field(default=None, alias="hydropower-desc")
    project_remarks: str | None = Field(default=None, alias="project-remarks")


class TsIdParts(BaseModel):
    """The six dotted segments of a CWMS timeseries id (location, parameter,
    type, interval, duration, version)."""

    model_config = ConfigDict(extra="forbid")

    location: str
    parameter: str
    type: str
    interval: str
    duration: str
    version: str  # the publisher

    @property
    def ts_id(self) -> str:
        return ".".join(
            [self.location, self.parameter, self.type, self.interval, self.duration, self.version]
        )


# --------------------------------------------------------------------------
# Task-response models — extra="allow" tolerates the dict layout we already
# build in core/places.py and core/values.py without forcing a refactor of
# those producers. The schemas FastMCP derives still document every field
# we promise; extras are an upgrade hatch, not silent drift.
# --------------------------------------------------------------------------


class PlaceSummary(CompactDumpMixin, BaseModel):
    """One result from `cwms_search_places` / `cwms_browse_region`."""

    model_config = ConfigDict(extra="allow")

    office_id: str
    name: str
    public_name: str | None = None
    location_kind: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    parameter_count: int = 0
    parameters: list[str] = Field(
        default_factory=list,
        description=(
            "Distinct CWMS parameter codes published at this location "
            "(e.g. Temp-Water, Stage, Elev). Empty for barren/ghost rows."
        ),
    )
    publishers: list[str] = Field(default_factory=list)
    last_data_timestamp: str | None = None
    co_located: list[str] = Field(default_factory=list)
    data_at: list[str] = Field(
        default_factory=list,
        description=(
            "Repair hint. Names of co-located siblings that publish data when "
            "this row is barren (parameter_count == 0). Try `cwms_list_parameters` "
            "on each. Empty when this row already has data or when no data-bearing "
            "sibling exists."
        ),
    )


class SearchRepairHint(CompactDumpMixin, BaseModel):
    """A copy-paste-retryable next call an agent can make to recover from a
    dead-end search response (e.g. a bare-name search with no office in scope, #24).

    Named distinctly from `cwms_tools.core.errors.RepairHint` (the `{tool, args}`
    shape carried inside error envelopes) so the two never collide in imports or
    typing — this one is a richer, response-level recovery hint."""

    model_config = ConfigDict(extra="allow")

    reason: str = Field(description="Machine code for why the call could not be served.")
    message: str = Field(description="Human-readable guidance for the retry.")
    tool: str = Field(description="The tool to call next.")
    args: dict[str, Any] = Field(
        description="Concrete, ready-to-use arguments for the retry call.",
    )


class SearchPlacesResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_search_places`."""

    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    query: str
    office: str | list[str] | None = None
    offices_searched: list[str] = Field(default_factory=list)
    offices_skipped_for_budget: list[str] = Field(
        default_factory=list,
        description=(
            "Offices in the requested list that exceeded the per-call fanout "
            "budget. Pass these back in `office` to widen the search."
        ),
    )
    parameter: str | None = None
    nearby_non_matching_count: int | None = Field(
        default=None,
        description=(
            "When `parameter` is set, the number of data-bearing rows dropped "
            "because they don't publish that parameter. Omitted otherwise."
        ),
    )
    partial: bool = False
    partial_reasons: list[str] = Field(default_factory=list)
    repair_hint: SearchRepairHint | None = Field(
        default=None,
        description=(
            "Present only when the search could not be served as asked (e.g. no "
            "office in scope). Names a concrete retry call — pass `args.office` "
            "back to widen the search. Omitted on a normal result."
        ),
    )
    results: list[PlaceSummary]
    total_count: int = Field(
        default=0,
        description="Total matches before the `limit` cap was applied.",
    )
    truncated: bool = Field(
        default=False,
        description="True when `limit` clipped the results; `total_count` holds the full size.",
    )
    limit: int | None = Field(
        default=None,
        description="The applied result cap (null means no cap).",
    )
    has_more: bool = Field(
        default=False,
        description="True when more results exist beyond this page; fetch with `next_cursor`.",
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque cursor for the next page. Pass back as `cursor`. "
            "Omitted when has_more is false."
        ),
    )
    source: SourceMeta


class PublisherFingerprint(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="allow")

    publisher: str
    rank: int
    ts_count: int
    parameters: list[str]


class DescribePlaceResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_describe_place`."""

    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    office_id: str
    name: str
    location: dict[str, Any]
    project: dict[str, Any] | None = None
    partial: bool
    partial_reasons: list[str]
    parameters: list[str]
    parameter_count: int
    publishers: list[PublisherFingerprint]
    ts_ids: list[str]
    last_data_timestamp: str | None = None
    source: SourceMeta


class PublisherAtPlace(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="allow")

    publisher: str
    rank: int
    parameters: list[str]
    ts_count: int


class ListParametersResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_list_parameters`."""

    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    office_id: str
    name: str
    ts_count: int
    by_publisher: list[PublisherAtPlace]
    all_parameters: list[str]
    last_data_timestamp: str | None = None
    data_at: list[str] | None = Field(
        default=None,
        description=(
            "Repair hint. Names of co-located siblings that publish data when "
            "this location is barren (ts_count == 0). Omitted when the location is "
            "data-bearing (no repair needed)."
        ),
    )
    source: SourceMeta


class BrowseRegionResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_browse_region`."""

    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    office: str
    bbox: dict[str, float] | None = None
    state: str | None = None
    result_count: int = Field(
        default=0,
        description="Number of rows actually returned in `results` (after the `limit` cap).",
    )
    ghost_count: int = Field(
        default=0,
        description=(
            "Ghost rows (parameter_count == 0) among the FULL match set, i.e. out of "
            "`total_count` — not just the returned rows. Data-bearing rows sort first, "
            "so a capped browse may return zero ghosts while this stays > 0. Do not "
            "compute `result_count - ghost_count`."
        ),
    )
    total_count: int = Field(
        default=0,
        description="Total matches before the `limit` cap was applied.",
    )
    truncated: bool = Field(
        default=False,
        description="True when `limit` clipped the results; `total_count` holds the full size.",
    )
    limit: int | None = Field(
        default=None,
        description="The applied result cap (null means no cap).",
    )
    truncation_hint: str | None = Field(
        default=None,
        description="How to narrow or widen the browse when `truncated` is true.",
    )
    has_more: bool = Field(
        default=False,
        description="True when more results exist beyond this page; fetch with `next_cursor`.",
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque cursor for the next page. Pass back as `cursor`. "
            "Omitted when has_more is false."
        ),
    )
    results: list[PlaceSummary]
    source: SourceMeta


class StatusClass(StrEnum):
    """Inline status classification on `cwms_get_value` responses."""

    NOMINAL = "nominal"
    WATCH = "watch"
    ACTION = "action"
    FLOOD = "flood"
    UNKNOWN = "unknown"


class ActiveThreshold(CompactDumpMixin, BaseModel):
    """One applicable threshold and the relation of the current value to it."""

    model_config = ConfigDict(extra="allow")

    specified_level_id: str
    value: float
    unit: str
    relation: Literal["above", "at", "below"]
    delta: float | None = None


class ValueWithContextResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_get_value`."""

    _keep_null: ClassVar[frozenset[str]] = frozenset({"value", "timestamp"})
    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    ts_id: str
    office_id: str
    location: str
    parameter: str
    publisher: str | None = None
    value: float | None = None
    unit: str
    timestamp: str | None = None
    status_class: StatusClass
    thresholds_active: list[ActiveThreshold]
    truncated: bool = False
    truncation_hint: str | None = None
    source: SourceMeta


class HistoryPoint(CompactDumpMixin, BaseModel):
    _keep_null: ClassVar[frozenset[str]] = frozenset({"value", "timestamp"})
    model_config = ConfigDict(extra="allow")

    timestamp: str | None = None
    value: float | None = None
    quality: int | None = None


class HistorySummary(CompactDumpMixin, BaseModel):
    """Window-level reduction so 'how has X changed?' needs no client-side math."""

    model_config = ConfigDict(extra="allow")

    count: int = Field(
        description=(
            "Number of observations with a numeric value AND a timestamp considered "
            "for the summary (timestamp-less numeric points are excluded)."
        ),
    )
    first: float = Field(description="Earliest observation by timestamp.")
    last: float = Field(description="Latest observation by timestamp.")
    min: float
    max: float
    mean: float
    delta: float = Field(description="`last - first` over the window.")


class HistoryBucket(CompactDumpMixin, BaseModel):
    """One server-side rollup bucket (per UTC hour or day)."""

    model_config = ConfigDict(extra="allow")

    timestamp: str = Field(description="RFC3339 UTC bucket start (half-open interval).")
    min: float
    max: float
    mean: float
    count: int = Field(
        description=(
            "Observations aggregated into this bucket — each with a numeric value and "
            "a parseable RFC3339 timestamp (others are skipped)."
        ),
    )


class HistoryResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_get_history`."""

    # Keep `summary` even when null so the key is always present in the response
    # (it is null only when the window holds no numeric observations); callers
    # can rely on `summary` existing rather than probing for it.
    _keep_null: ClassVar[frozenset[str]] = frozenset({"summary"})
    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    ts_id: str
    office_id: str
    location: str
    parameter: str
    publisher: str | None = None
    unit: str
    begin: str
    end: str
    rollup: Rollup = Field(
        description=(
            "Applied downsample mode: 'raw' (every point in `values`), or "
            "'hourly'/'daily' (per-bucket aggregates in `buckets`; `values` is empty). "
            "Always present (required)."
        ),
    )
    summary: HistorySummary | None = Field(
        description=(
            "Window-level first/last/min/max/mean/delta/count over non-null "
            "observations. The key is always present (required); its value is null "
            "only when the window holds no numeric observations."
        ),
    )
    values: list[HistoryPoint] = Field(
        description=(
            "Raw points (timestamp + value). Always present (required) but empty "
            "under 'hourly'/'daily' rollup, where the aggregates are in `buckets`."
        ),
    )
    buckets: list[HistoryBucket] | None = Field(
        default=None,
        description="Per-bucket aggregates when `rollup` is 'hourly'/'daily'; omitted for 'raw'.",
    )
    value_count: int = Field(
        description=(
            "Number of raw points in the window. This is always the raw count, "
            "even under `rollup='hourly'/'daily'` where `values` is empty and the "
            "aggregates are in `buckets` — so `value_count` may exceed `len(values)`."
        ),
    )
    truncated: bool = False
    truncation_hint: str | None = None
    next_begin: str | None = Field(
        default=None,
        description=(
            "When `truncated` is true, the RFC3339 timestamp to use as `begin` on the "
            "next request to continue the window with no duplicate/skipped point. "
            "Omitted otherwise."
        ),
    )
    source: SourceMeta


class PublisherCoverage(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="allow")

    publisher: str
    rank: int
    locations_known: int
    freshness: str | None = None


class PublishersCoverage(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="allow")

    offices_requested: list[str]
    offices_indexed: list[str]
    offices_skipped_for_budget: list[str] = Field(
        default_factory=list,
        description=(
            "Offices not indexed because the per-call fanout budget was exhausted. "
            "Re-run with these in `offices` to continue the index deterministically."
        ),
    )
    offices_error_skipped: list[str] = Field(
        default_factory=list,
        description=(
            "Offices skipped because their catalog fetch errored (e.g. upstream_error, "
            "rate_limited). Distinct from budget skips: retrying may help, but these "
            "did not simply hit the budget."
        ),
    )
    complete: bool


class PublishersForParameterResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_publishers_for_parameter`."""

    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    parameter: str
    publishers: list[PublisherCoverage]
    publisher_count: int
    ts_count: int
    coverage: PublishersCoverage
    repair: dict[str, Any] | None = None
    source: SourceMeta


__all__ = [
    "ActiveThreshold",
    "BrowseRegionResponse",
    "CdaLocation",
    "CdaProject",
    "DescribePlaceResponse",
    "Detail",
    "ErrorRef",
    "HistoryPoint",
    "HistoryResponse",
    "ListParametersResponse",
    "PlaceSummary",
    "PublisherAtPlace",
    "PublisherCoverage",
    "PublisherFingerprint",
    "PublishersCoverage",
    "PublishersForParameterResponse",
    "SearchPlacesResponse",
    "SourceMeta",
    "StatusClass",
    "TsIdParts",
    "ValueWithContextResponse",
]
