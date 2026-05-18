"""Tests for ts_id parsing, publisher ranking, and aggregation."""

from __future__ import annotations

import pytest

from cwms_tools.core import publishers


def test_parse_ts_id_decomposes_six_segments() -> None:
    parts = publishers.parse_ts_id("FOSS.Elev.Inst.15Minutes.0.Ccp-Rev")
    assert parts is not None
    assert parts.location == "FOSS"
    assert parts.parameter == "Elev"
    assert parts.type == "Inst"
    assert parts.interval == "15Minutes"
    assert parts.duration == "0"
    assert parts.version == "Ccp-Rev"
    assert parts.ts_id == "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"


@pytest.mark.parametrize("bad", ["", "no-dots", "a.b.c", "a.b.c.d.e", "a.b.c.d.e.f.g"])
def test_parse_ts_id_returns_none_for_bad_shapes(bad: str) -> None:
    assert publishers.parse_ts_id(bad) is None


def test_split_parameter_first_hyphen_only() -> None:
    assert publishers.split_parameter("Elev") == ("Elev", None)
    assert publishers.split_parameter("Elev-Forebay") == ("Elev", "Forebay")
    # cwms-overview.md §4.6: split on first hyphen only.
    assert publishers.split_parameter("Stor-Conservation Pool") == ("Stor", "Conservation Pool")
    assert publishers.split_parameter("%-Conservation Pool Full") == (
        "%",
        "Conservation Pool Full",
    )


def test_publisher_rank_exact_matches() -> None:
    assert publishers.publisher_rank("Best-MRBWM") == 100
    assert publishers.publisher_rank("Ccp-Rev") == 60
    assert publishers.publisher_rank("Raw-A2W") == 30
    assert publishers.publisher_rank("MANUAL") == 5


def test_publisher_rank_prefix_fallback() -> None:
    # `Best-` prefix (anything not exact-matched) wins over generic suffixes.
    assert publishers.publisher_rank("Best-NWDP") == 90
    # `Rev-` prefix:
    assert publishers.publisher_rank("Rev-SomethingNew") == 50


def test_publisher_rank_suffix_fallback() -> None:
    assert publishers.publisher_rank("WEIRD-REV") == 50
    assert publishers.publisher_rank("WEIRD-RAW") == 25
    assert publishers.publisher_rank("WEIRD-Computed") == 20


def test_publisher_rank_unknown_is_zero() -> None:
    assert publishers.publisher_rank("completely-novel-thing") == 0


def test_pick_canonical_prefers_higher_rank() -> None:
    candidates = [
        "FOSS.Elev.Inst.15Minutes.0.Raw-A2W",
        "FOSS.Elev.Inst.15Minutes.0.Best-MRBWM",
        "FOSS.Elev.Inst.15Minutes.0.MANUAL",
    ]
    assert publishers.pick_canonical(candidates) == "FOSS.Elev.Inst.15Minutes.0.Best-MRBWM"


def test_pick_canonical_filters_by_parameter() -> None:
    candidates = [
        "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
        "FOSS.Flow-In.Inst.15Minutes.0.Best-MRBWM",
    ]
    assert (
        publishers.pick_canonical(candidates, parameter="Elev")
        == "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev"
    )


def test_pick_canonical_returns_none_when_nothing_matches() -> None:
    assert publishers.pick_canonical([], parameter="Elev") is None
    assert (
        publishers.pick_canonical(["FOSS.Flow.Inst.15Minutes.0.MANUAL"], parameter="Elev") is None
    )


def test_aggregate_publishers_sorted_by_rank() -> None:
    ts = [
        "FOSS.Elev.Inst.15Minutes.0.MANUAL",
        "FOSS.Flow-In.Inst.15Minutes.0.Best-MRBWM",
        "FOSS.Flow-Out.Inst.15Minutes.0.Best-MRBWM",
        "FOSS.Elev.Inst.15Minutes.0.Ccp-Rev",
    ]
    facts = publishers.aggregate_publishers(ts)
    assert [f.publisher for f in facts] == ["Best-MRBWM", "Ccp-Rev", "MANUAL"]
    assert facts[0].ts_count == 2
    assert facts[0].parameters == ("Flow-In", "Flow-Out")


def test_parameter_counts_deduplicates_by_parameter() -> None:
    ts = [
        "FOSS.Elev.Inst.15Minutes.0.Best-MRBWM",
        "FOSS.Elev.Inst.1Hour.0.Ccp-Rev",
        "FOSS.Flow-Out.Inst.15Minutes.0.MANUAL",
    ]
    counts = publishers.parameter_counts(ts)
    assert counts == {"Elev": 2, "Flow-Out": 1}
