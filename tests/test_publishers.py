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


# --------------------------------------------------------------------------
# similar_parameters — shared by get_value's not_found and search hints (#97)
# --------------------------------------------------------------------------


def test_similar_parameters_case_slip_is_the_only_suggestion() -> None:
    published = ["ELEV-X", "Elev", "Elev-Forebay", "Flow-In"]
    assert publishers.similar_parameters("elev", published, expand_base=True) == ["Elev"]


def test_similar_parameters_difflib_near_miss() -> None:
    assert publishers.similar_parameters("Stro", ["Elev", "Flow-In", "Stor"]) == ["Stor"]


def test_similar_parameters_base_expansion_catches_low_ratio_subs() -> None:
    # Elev -> Elev-Forebay scores 0.5 in difflib, under the 0.6 cutoff.
    published = ["Elev-Forebay", "Elev-Tailwater", "Flow-In"]
    assert publishers.similar_parameters("Elev", published) == []
    assert publishers.similar_parameters("Elev", published, expand_base=True) == [
        "Elev-Forebay",
        "Elev-Tailwater",
    ]


def test_similar_parameters_base_expansion_only_for_bare_input() -> None:
    published = ["Temp-Air", "Temp-Water", "Temp-Water-Surface"]
    out = publishers.similar_parameters("Temp-Waterr", published, expand_base=True)
    assert out[0] == "Temp-Water"
    # A hyphenated input never base-expands to every `Temp-*`.
    assert out == publishers.similar_parameters("Temp-Waterr", published)


def test_similar_parameters_no_close_candidate() -> None:
    assert publishers.similar_parameters("Xyzzy", ["Elev", "Stor"], expand_base=True) == []


def test_similar_parameters_dedupes_orders_and_caps() -> None:
    # difflib hits first (ranked), then base hits sorted; a code in both appears once.
    published = [f"Elev-{s}" for s in ("A", "B", "C", "D", "E", "F")] + ["Elv"]
    out = publishers.similar_parameters("Elev", published, n=3, expand_base=True, cap=5)
    # difflib ranks Elv, Elev-F, Elev-E; base expansion adds Elev-A.. but
    # Elev-E/-F appear only once, at their difflib position.
    assert out == ["Elv", "Elev-F", "Elev-E", "Elev-A", "Elev-B"]


def test_similar_parameters_semantic_alternative_is_kept() -> None:
    # Advisory, not a correction: a different quantity can still be offered.
    assert publishers.similar_parameters("Temp-Water", ["Elev", "Temp-Air"]) == ["Temp-Air"]
