"""Pydantic v2 models for cwms-tools.

Two model tiers:

1. **Task-response models** — normalized response shapes this layer
   guarantees. Used directly as MCP tool `outputSchema`s and CLI JSON
   payloads. Strip nulls/defaults in summary mode.
2. **DTO facades** — thin Pydantic models keyed to the upstream CWMS Java
   DTOs (`Location`, `Project`, `LocationLevel`, ...). `extra="allow"` so
   unknown upstream fields pass through unchanged; new upstream fields are
   non-breaking. Surface only at `detail=full` under a nested `raw` field.

Every class/field docstring here is serialized verbatim into every tool's
outputSchema an MCP client preloads (#67) — keep them wire-appropriate (short,
agent-facing) and put maintainer-only rationale in a regular comment instead.
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
    """'summary' (compact, default) or 'full' (verbose fields + quality codes)."""

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


# No `endpoints_called`/`cached` here (#70, deliberate — see #67 on why this
# docstring stays terse: it's serialized into every tool's outputSchema). A
# single tool call can span multiple sub-calls, each independently cached or
# not against a different upstream endpoint, so a flat list/bool would be
# either silently incomplete or ambiguous — worse than not advertising it.
# `core.errors.SourceInfo` (the error-envelope's `source`) is unaffected: it
# records the one endpoint that actually failed, with no such ambiguity.
class SourceMeta(CompactDumpMixin, BaseModel):
    """Provenance attached to every successful tool response."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str
    workaround: str | None = None
    upstream_status: int | None = Field(
        default=None,
        description="Set on a recovered partial-success sub-call; omitted otherwise.",
    )


class ErrorRef(BaseModel):
    """The in-band `{ok: false, error: {...}}` envelope returned by tool handlers.

    `error` is the full `ErrorEnvelope` so the published outputSchema documents
    the failure contract (code, details, repair, temporary, retry_after_ms,
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
# Task-response models — extra="forbid" (#74). These used to tolerate
# extras as an "upgrade hatch" for the dict layout core/places.py and
# core/values.py already build, but combined with the capability fingerprint
# gap (#71, now closed) that meant a producer field could appear or drift
# with no schema validation and no fingerprint movement — the outputSchema
# advertised `additionalProperties: true` on every success branch. Every
# field a producer actually emits is now declared explicitly (including
# detail=full-only diagnostics like `ActiveThreshold.source_workaround` and
# `PublishersForParameterResponse.observed_publishers_by_office`); an
# undeclared field is a real bug, not a tolerated extra. DTO facades above
# this line (CdaLocation, CdaProject) are a different tier and keep
# extra="allow" by design — they wrap arbitrary upstream JSON.
# --------------------------------------------------------------------------


# Parses depth-tagged sensor ids (#27), e.g. `GWLW_S1-D3,0ft` (comma is a
# decimal point: 3.0 ft, not 0 ft) and `BECR-D042,5m` (42.5 m).
class SensorDepth(CompactDumpMixin, BaseModel):
    """Structured depth parsed from a depth-tagged sensor id."""

    model_config = ConfigDict(extra="forbid")

    value: float = Field(description="Sensor depth below the surface.")
    unit: str = Field(description="Depth unit: 'ft' or 'm'.")


class PlaceSummary(CompactDumpMixin, BaseModel):
    """One result from `cwms_search_places` / `cwms_browse_region`."""

    model_config = ConfigDict(extra="forbid")

    office_id: str
    name: str
    public_name: str | None = None
    location_kind: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    depth: SensorDepth | None = Field(
        default=None,
        description="Parsed sensor depth (e.g. GWLW_S1-D3,0ft → 3.0 ft); omitted otherwise.",
    )
    parameter_count: int = 0
    parameters: list[str] = Field(
        default_factory=list,
        description="Parameter codes published here (e.g. Temp-Water, Elev). Empty for ghosts.",
    )
    publishers: list[str] = Field(default_factory=list)
    last_data_timestamp: str | None = None
    co_located: list[str] = Field(default_factory=list)
    data_at: list[str] = Field(
        default_factory=list,
        description=(
            "Co-located siblings with data, when this row is a ghost; try "
            "`cwms_list_parameters` on each."
        ),
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Unfiltered upstream location DTO; detail=full only (search, not browse).",
    )


# Distinct from `cwms_tools.core.errors.RepairHint` (the `{tool, args}` shape
# inside error envelopes, #24) so the two never collide in imports or typing.
class SearchRepairHint(CompactDumpMixin, BaseModel):
    """A retryable next call to recover from a dead-end search (e.g. no office in scope)."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    message: str
    tool: str
    args: dict[str, Any]


class SearchPlacesResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_search_places`."""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    query: str
    office: str | list[str] | None = None
    offices_searched: list[str] = Field(default_factory=list)
    offices_skipped_for_budget: list[str] = Field(
        default_factory=list,
        description=(
            "Offices over the per-call fanout budget; pass back in `office` to "
            "widen. Signals scope incompleteness — orthogonal to `truncated`/"
            "`has_more`, which only describe row completeness within offices "
            "already searched."
        ),
    )
    parameter: str | None = None
    nearby_non_matching_count: int | None = Field(
        default=None,
        description="Rows dropped for not publishing `parameter`, when set. Omitted otherwise.",
    )
    partial: bool = False
    partial_reasons: list[str] = Field(default_factory=list)
    repair_hint: SearchRepairHint | None = Field(
        default=None,
        description=(
            "Set when the call couldn't be served (e.g. no office in scope); "
            "names a concrete retry — pass `args.office` back to widen."
        ),
    )
    results: list[PlaceSummary]
    total_count: int = Field(
        default=0,
        description="Total matches before the `limit` cap was applied.",
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Always false: `limit` never makes rows unrecoverable here — page "
            "through the rest via `has_more`/`next_cursor` instead."
        ),
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
        description="Opaque next-page cursor; pass back as `cursor`. Omitted when has_more=false.",
    )
    source: SourceMeta


class PublisherFingerprint(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    publisher: str
    rank: int
    ts_count: int
    parameters: list[str]


class DescribePlaceResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_describe_place`."""

    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")

    publisher: str
    rank: int
    parameters: list[str]
    ts_count: int


class ListParametersResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_list_parameters`."""

    model_config = ConfigDict(extra="forbid")

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
            "Repair hint: co-located siblings that publish data, when this "
            "location is a ghost (ts_count == 0). Omitted otherwise."
        ),
    )
    source: SourceMeta


class BrowseRegionResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_browse_region`."""

    model_config = ConfigDict(extra="forbid")

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
            "Ghost rows (parameter_count == 0) in the full match set (`total_count`), "
            "not just returned rows; do not compute as `result_count - ghost_count`."
        ),
    )
    total_count: int = Field(
        default=0,
        description="Total matches before the `limit` cap was applied.",
    )
    truncated: bool = Field(
        default=False,
        description=(
            "Always false: `limit` never makes rows unrecoverable here — page "
            "through the rest via `has_more`/`next_cursor` instead."
        ),
    )
    limit: int | None = Field(
        default=None,
        description="The applied result cap (null means no cap).",
    )
    truncation_hint: str | None = Field(
        default=None,
        description="How to narrow the browse or page further when `has_more` is true.",
    )
    has_more: bool = Field(
        default=False,
        description="True when more results exist beyond this page; fetch with `next_cursor`.",
    )
    next_cursor: str | None = Field(
        default=None,
        description="Opaque next-page cursor; pass back as `cursor`. Omitted when has_more=false.",
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


class LevelLookupStatus(StrEnum):
    """Threshold-classification outcome on `cwms_get_value` responses."""

    SKIPPED = "skipped"
    COMPUTED = "computed"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


class ActiveThreshold(CompactDumpMixin, BaseModel):
    """One applicable threshold and the relation of the current value to it."""

    model_config = ConfigDict(extra="forbid")

    specified_level_id: str
    level_id: str | None = Field(default=None, description="Present only at detail=full.")
    value: float
    unit: str
    relation: Literal["above", "at", "below"]
    delta: float | None = None
    source_workaround: str | None = None


class ValueWithContextResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_get_value`."""

    _keep_null: ClassVar[frozenset[str]] = frozenset({"value", "timestamp"})
    model_config = ConfigDict(extra="forbid")

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
    level_lookup_status: LevelLookupStatus
    truncated: bool = False
    truncation_hint: str | None = None
    source: SourceMeta


class HistoryPoint(CompactDumpMixin, BaseModel):
    _keep_null: ClassVar[frozenset[str]] = frozenset({"value", "timestamp"})
    model_config = ConfigDict(extra="forbid")

    timestamp: str | None = None
    value: float | None = None
    quality: int | None = None


class HistorySummary(CompactDumpMixin, BaseModel):
    """Window-level reduction so 'how has X changed?' needs no client-side math."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(
        description="Observations with both a numeric value and a timestamp (others excluded).",
    )
    first: float = Field(description="Earliest observation by timestamp.")
    last: float = Field(description="Latest observation by timestamp.")
    min: float
    max: float
    mean: float
    delta: float = Field(description="`last - first` over the window.")


class HistoryBucket(CompactDumpMixin, BaseModel):
    """One server-side rollup bucket (per UTC hour or day)."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str = Field(description="RFC3339 UTC bucket start (half-open interval).")
    min: float
    max: float
    mean: float
    count: int = Field(
        description="Observations in this bucket (numeric value + parseable timestamp).",
    )


class HistoryResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_get_history`."""

    # Keep `summary` even when null so the key is always present in the response
    # (it is null only when the window holds no numeric observations); callers
    # can rely on `summary` existing rather than probing for it.
    _keep_null: ClassVar[frozenset[str]] = frozenset({"summary"})
    model_config = ConfigDict(extra="forbid")

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
            "Applied downsample: 'raw' (points in `values`, capped — see "
            "`truncated`) or 'hourly'/'daily' (aggregates in `buckets`)."
        ),
    )
    summary: HistorySummary | None = Field(
        description=(
            "First/last/min/max/mean/delta/count over the window; null only when "
            "no numeric observations exist."
        ),
    )
    values: list[HistoryPoint] = Field(
        description=(
            "Raw points; empty under 'hourly'/'daily' rollup (see `buckets`). "
            "Capped under 'raw' — see `truncated`."
        ),
    )
    buckets: list[HistoryBucket] | None = Field(
        default=None,
        description="Per-bucket aggregates when `rollup` is 'hourly'/'daily'; omitted for 'raw'.",
    )
    value_count: int = Field(
        description=(
            "Raw point count for the window; may exceed `len(values)` when "
            "rollup buckets or truncation apply."
        ),
    )
    truncated: bool = False
    truncation_hint: str | None = None
    next_begin: str | None = Field(
        default=None,
        description=(
            "When `truncated`, the RFC3339 `begin` for the next request to "
            "continue with no gap/dup. Omitted otherwise."
        ),
    )
    source: SourceMeta


class ProfileSensor(CompactDumpMixin, BaseModel):
    """One depth sensor's latest reading in a `cwms_get_profile` result."""

    _keep_null: ClassVar[frozenset[str]] = frozenset({"value", "timestamp"})
    model_config = ConfigDict(extra="forbid")

    name: str
    depth: SensorDepth
    value: float | None = None
    unit: str | None = None
    timestamp: str | None = None
    publisher: str | None = None
    ts_id: str | None = None
    error: str | None = Field(
        default=None,
        description=(
            "Error code if this one sensor could not be read; the rest of the profile stands."
        ),
    )


class ProfileResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_get_profile` — a depth string read in one call."""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    office_id: str
    name: str = Field(description="The parent 'string' location (e.g. GWLW_S1).")
    parameter: str
    unit: str = Field(
        description=(
            "Actual unit of the sensor readings (e.g. degF), from the first "
            "successful sensor — not the requested EN/SI system unless all fail."
        ),
    )
    sensor_count: int
    profile: list[ProfileSensor] = Field(
        description="Depth sensors sorted shallow→deep, each with structured depth + latest value.",
    )
    note: str | None = Field(
        default=None,
        description="Present only when no depth-tagged sensors matched; explains how to recover.",
    )
    source: SourceMeta


class PublisherCoverage(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    publisher: str
    rank: int
    locations_known: int
    freshness: str | None = None


class PublishersCoverage(CompactDumpMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    offices_requested: list[str]
    offices_indexed: list[str]
    offices_skipped_for_budget: list[str] = Field(
        default_factory=list,
        description="Offices skipped by the fanout budget; re-run with these in `offices`.",
    )
    offices_error_skipped: list[str] = Field(
        default_factory=list,
        description=(
            "Offices skipped by a catalog fetch error (distinct from budget "
            "skips); retrying may help."
        ),
    )
    complete: bool


class PublishersForParameterResponse(CompactDumpMixin, BaseModel):
    """Response shape for `cwms_publishers_for_parameter`."""

    # `serialize_by_alias`: the one field below needs to round-trip under its
    # literal underscore-prefixed wire name, which pydantic forbids as a
    # Python attribute name outright — the alias IS the real field name here,
    # not cosmetic, so serialization must honor it without a `by_alias=True`
    # at every dump call site.
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    ok: Literal[True] = True
    parameter: str
    publishers: list[PublisherCoverage]
    publisher_count: int
    ts_count: int
    coverage: PublishersCoverage
    repair: dict[str, Any] | None = None
    observed_publishers_by_office: dict[str, list[str]] | None = Field(
        default=None,
        alias="_observed_publishers_by_office",
        description="Internal diagnostic: publisher sightings per office; detail=full only.",
    )
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
    "LevelLookupStatus",
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
