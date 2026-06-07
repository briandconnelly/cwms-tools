import pytest

from cwms_tools.core import pagination
from cwms_tools.core.errors import CwmsToolsError, ErrorCode


def test_roundtrip_encode_decode():
    payload = {
        "v": 1,
        "kind": "search_places",
        "off": 50,
        "req": "abc",
        "offices": ["NWDM"],
        "total": 120,
    }
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
    cur = {
        "v": 1,
        "kind": "search_places",
        "off": 50,
        "req": "abc",
        "offices": ["NWDM"],
        "total": 120,
    }
    assert pagination.validate_continuation(cur, kind="search_places", req="abc") == 50
    with pytest.raises(CwmsToolsError) as e1:
        pagination.validate_continuation(cur, kind="search_places", req="DIFFERENT")
    assert e1.value.envelope.code is ErrorCode.INVALID_CURSOR
    with pytest.raises(CwmsToolsError):
        pagination.validate_continuation(cur, kind="browse_region", req="abc")
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
    for bad in (
        {"offices": "NWDM"},
        {"offices": [1, 2]},
        {"offices": ["A"] * (pagination.MAX_CURSOR_OFFICES + 1)},
        {},
    ):
        with pytest.raises(CwmsToolsError) as exc:
            pagination.coerce_offices(bad)
        assert exc.value.envelope.code is ErrorCode.INVALID_CURSOR
