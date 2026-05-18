"""ts_id parsing, publisher ranking, and a bounded publisher registry.

The publisher (version segment of a ts_id) is the single most informative
signal in CWMS — it tells you whether a series is live, which operational
team produced it, and what other parameters likely exist at the same
location (cwms-overview.md §3.1, §6.3).

This module is the single source of truth for:

- Decomposing a 6-segment ts_id into its parts.
- Ranking publishers so a canonical "best" is selected for any (location,
  parameter) pair.
- Aggregating publisher coverage across a set of ts_ids.

It is pure logic — no I/O.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final

from cwms_tools.core.models import TsIdParts

# Publisher-rank table. Higher rank wins when picking the canonical ts_id for a
# (location, parameter). Loosely follows cwms-overview.md §6.3 — `Best-*` over
# `*-REV` over `*-RAW`; `Computed`/`MANUAL` are last-resort. Unknown publishers
# default to 0, between RAW and REV, so a brand-new publisher doesn't shadow
# anything we've explicitly ranked.
_RANK_TABLE: Final[dict[str, int]] = {
    # Trusted blends — top of the pile.
    "Best-MRBWM": 100,
    "Best-": 90,  # any other "Best-*" publisher (prefix match below)
    # Revised series — preferred over RAW.
    "REV": 60,  # generic "*-REV" tail
    "Ccp-Rev": 60,
    "MVDhist-rev": 55,
    "Rev-Ccp": 55,
    "Rev-Regi": 55,
    "CBT-REV": 60,
    "IRIDIUM-REV": 55,
    "NWSRADIO-REV": 50,
    "MIXED-REV": 45,
    "MIXED-COMPUTED-REV": 40,
    # Raw / live feeds.
    "Raw-A2W": 30,
    "CBT-RAW": 25,
    "IRIDIUM-RAW": 20,
    "NWSRADIO-RAW": 15,
    # Computed pipelines.
    "CENWP-CALC": 35,  # NWDP Portland District calc — first-class computed
    "Metvue-Computed": 25,
    "Computed": 10,
    "MANUAL": 5,
    "REGI": 5,
    "Regi": 5,
}

#: Prefix-based fallbacks. Checked in order if no exact match wins.
_PREFIX_RANKS: Final[list[tuple[str, int]]] = [
    ("Best-", 90),
    ("Rev-", 50),
    ("Raw-", 30),
]

#: Suffix-based fallbacks. Checked after exact and prefix.
_SUFFIX_RANKS: Final[list[tuple[str, int]]] = [
    ("-REV", 50),
    ("-RAW", 25),
    ("-Computed", 20),
    ("-CALC", 30),
]


@dataclass(frozen=True)
class PublisherFacts:
    """Aggregate facts about one publisher's coverage at a location or scope."""

    publisher: str
    rank: int
    ts_count: int
    parameters: tuple[str, ...]  # sorted, deduped


def parse_ts_id(ts_id: str) -> TsIdParts | None:
    """Decompose a 6-segment ts_id. Returns None if the shape is wrong."""
    parts = ts_id.split(".")
    if len(parts) != 6:
        return None
    return TsIdParts(
        location=parts[0],
        parameter=parts[1],
        type=parts[2],
        interval=parts[3],
        duration=parts[4],
        version=parts[5],
    )


def split_parameter(parameter: str) -> tuple[str, str | None]:
    """Split a CWMS parameter on the *first* hyphen — see cwms-overview.md §4.6.

    `Elev-Forebay` -> (`Elev`, `Forebay`); `%-Conservation Pool Full` ->
    (`%`, `Conservation Pool Full`); `Elev` -> (`Elev`, None).
    """
    head, sep, tail = parameter.partition("-")
    if not sep:
        return parameter, None
    return head, tail


def publisher_rank(publisher: str) -> int:
    """Return the rank for a publisher; unknown publishers get 0."""
    if publisher in _RANK_TABLE:
        return _RANK_TABLE[publisher]
    for prefix, rank in _PREFIX_RANKS:
        if publisher.startswith(prefix):
            return rank
    for suffix, rank in _SUFFIX_RANKS:
        if publisher.endswith(suffix):
            return rank
    return 0


def pick_canonical(
    ts_ids: list[str],
    *,
    parameter: str | None = None,
) -> str | None:
    """Pick the canonical (highest-ranked) ts_id from a candidate list.

    If `parameter` is given, only ts_ids whose parameter segment matches are
    considered. Ties are broken by alphabetical ts_id so the choice is
    deterministic.
    """
    candidates: list[tuple[int, str]] = []
    for tsid in ts_ids:
        parts = parse_ts_id(tsid)
        if parts is None:
            continue
        if parameter is not None and parts.parameter != parameter:
            continue
        candidates.append((publisher_rank(parts.version), tsid))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: (-kv[0], kv[1]))
    return candidates[0][1]


def aggregate_publishers(ts_ids: list[str]) -> list[PublisherFacts]:
    """Return one `PublisherFacts` per distinct publisher in `ts_ids`.

    Sorted by descending rank, then by publisher name for determinism.
    """
    bucket: dict[str, set[str]] = {}
    for tsid in ts_ids:
        parts = parse_ts_id(tsid)
        if parts is None:
            continue
        bucket.setdefault(parts.version, set()).add(parts.parameter)
    facts = [
        PublisherFacts(
            publisher=publisher,
            rank=publisher_rank(publisher),
            ts_count=sum(1 for t in ts_ids if (p := parse_ts_id(t)) and p.version == publisher),
            parameters=tuple(sorted(parameters)),
        )
        for publisher, parameters in bucket.items()
    ]
    facts.sort(key=lambda f: (-f.rank, f.publisher))
    return facts


def parameter_counts(ts_ids: list[str]) -> dict[str, int]:
    """Return a Counter of distinct parameters across the ts_ids."""
    return dict(
        Counter(parts.parameter for ts_id in ts_ids if (parts := parse_ts_id(ts_id)) is not None)
    )


__all__ = [
    "PublisherFacts",
    "aggregate_publishers",
    "parameter_counts",
    "parse_ts_id",
    "pick_canonical",
    "publisher_rank",
    "split_parameter",
]
