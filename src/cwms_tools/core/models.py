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

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------


class Detail(str, Enum):
    """The `detail` toggle accepted by every tool that returns more than a scalar."""

    SUMMARY = "summary"
    FULL = "full"


class SourceMeta(BaseModel):
    """Provenance attached to every tool response (success path)."""

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


# --------------------------------------------------------------------------
# DTO facades — extra=allow so unknown upstream fields pass through.
# Re-cast just the fields we read explicitly; everything else lives under the
# implicit `__pydantic_extra__` collection.
# --------------------------------------------------------------------------


class CdaLocation(BaseModel):
    """Facade over the upstream Location DTO (cwms-overview.md §4.2)."""

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
    """Facade over the upstream Project DTO (cwms-overview.md §4.2)."""

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
    """The 6-segment ts_id decomposed (cwms-overview.md §4.3)."""

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
# Task-response models
# --------------------------------------------------------------------------


class PlaceSummary(BaseModel):
    """One result from `cwms_search_places`."""

    model_config = ConfigDict(extra="forbid")

    office_id: str
    name: str
    public_name: str | None = None
    location_kind: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    parameter_count: int = Field(
        description="Number of distinct parameters with ≥1 active ts_id; 0 = ghost.",
    )
    publishers: list[str] = Field(
        default_factory=list,
        description="Distinct publishers (version segments) at this location.",
    )
    last_data_timestamp: str | None = Field(
        default=None,
        description="RFC3339 UTC timestamp of most recent observed datum, or null.",
    )
    co_located: list[str] = Field(
        default_factory=list,
        description="Other location ids within ~100m of this one.",
    )


class SearchPlacesResult(BaseModel):
    """Response shape for `cwms_search_places`."""

    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[PlaceSummary]
    truncated: bool = False
    truncation_hint: str | None = None
    source: SourceMeta


class StatusClass(str, Enum):
    """Inline status classification on `cwms_get_value` responses (summary detail)."""

    NOMINAL = "nominal"
    WATCH = "watch"
    ACTION = "action"
    FLOOD = "flood"
    UNKNOWN = "unknown"


class ActiveThreshold(BaseModel):
    """One applicable threshold and the relation of the current value to it."""

    model_config = ConfigDict(extra="forbid")

    specified_level_id: str
    value: float
    unit: str
    relation: Literal["above", "at", "below"]
    delta: float | None = None  # signed difference (observation - threshold)


class ValueWithContext(BaseModel):
    """Response shape for `cwms_get_value` (single id, summary detail)."""

    model_config = ConfigDict(extra="forbid")

    ts_id: str
    office_id: str
    location: str
    parameter: str
    publisher: str
    value: float | None
    unit: str
    timestamp: str | None  # RFC3339 UTC
    status_class: StatusClass
    thresholds_active: list[ActiveThreshold] = Field(default_factory=list)
    raw: dict[str, Any] | None = None  # populated only at detail=full
    source: SourceMeta


__all__ = [
    "ActiveThreshold",
    "CdaLocation",
    "CdaProject",
    "Detail",
    "PlaceSummary",
    "SearchPlacesResult",
    "SourceMeta",
    "StatusClass",
    "TsIdParts",
    "ValueWithContext",
]
