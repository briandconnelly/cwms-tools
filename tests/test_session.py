"""Tests for session configuration and User-Agent construction."""

from __future__ import annotations

import pytest

from cwms_tools.core import session


def test_user_agent_includes_cwms_tools_and_cwms_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CWMS_TOOLS_USER_AGENT_EXTRA", raising=False)
    monkeypatch.setenv("CWMS_TOOLS_REPO_URL", "https://example.test/cwms-tools")
    ua = session.build_user_agent()
    assert ua.startswith("cwms-tools/")
    assert "cwms-python/" in ua
    assert "https://example.test/cwms-tools" in ua


def test_user_agent_appends_extra_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CWMS_TOOLS_USER_AGENT_EXTRA", "orgname-deploy-42")
    ua = session.build_user_agent()
    assert ua.endswith("orgname-deploy-42")


def test_resolve_session_config_normalizes_api_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CWMS_TOOLS_API_ROOT", "https://example.test/api")
    cfg = session.resolve_session_config()
    assert cfg.api_root.endswith("/")


def test_pool_connections_scales_with_workers() -> None:
    cfg = session.resolve_session_config()
    # Plan rule: max(2 * workers, 16)
    from cwms_tools.core.concurrency import MAX_WORKERS

    assert cfg.pool_connections == max(2 * MAX_WORKERS, 16)


def test_configure_session_sets_user_agent_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CWMS_TOOLS_OPERATOR_EMAIL", raising=False)
    cfg = session.configure_session()
    import cwms.api

    assert cwms.api.SESSION.headers.get("User-Agent") == cfg.user_agent


def test_configure_session_sets_from_header_when_email_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CWMS_TOOLS_OPERATOR_EMAIL", "ops@example.test")
    # Reset the internal state so a fresh config is built.
    session._state["config"] = None
    session.configure_session()
    import cwms.api

    assert cwms.api.SESSION.headers.get("From") == "ops@example.test"


def test_session_fingerprint_is_dict_with_expected_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CWMS_TOOLS_OPERATOR_EMAIL", raising=False)
    session._state["config"] = None
    fp = session.session_fingerprint()
    assert set(fp.keys()) == {"api_root", "user_agent", "pool_connections", "has_operator_email"}
    assert fp["has_operator_email"] is False


def test_configure_session_drops_cwms_api_log_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cwms-python's `cwms/api.py` calls bare `logging.error(...)`, which
    routes to the root logger and lands on stderr. After we wrap those
    failures ourselves into structured envelopes, the upstream log line is
    just noise. The filter targets `record.pathname` so it doesn't catch
    unrelated libraries' api.py modules."""
    import logging

    import cwms.api as cwms_api

    session._state["config"] = None
    session._remove_cwms_api_log_filter()
    session.configure_session()
    records: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CaptureHandler(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        # Simulate a cwms.api log call by constructing a record whose
        # pathname points at the upstream module's file.
        rec = logging.LogRecord(
            name="root",
            level=logging.ERROR,
            pathname=cwms_api.__file__,
            lineno=99,
            msg="CDA Error: response=<Response [406]>",
            args=None,
            exc_info=None,
        )
        root.handle(rec)
    finally:
        root.removeHandler(handler)

    assert records == [], "cwms.api log writes must be dropped"


def test_log_filter_does_not_drop_other_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity-check: only cwms/api.py is silenced. A record originating
    from an unrelated path must pass through."""
    import logging

    session._state["config"] = None
    session._remove_cwms_api_log_filter()
    session.configure_session()
    records: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CaptureHandler(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        rec = logging.LogRecord(
            name="root",
            level=logging.ERROR,
            pathname="/some/unrelated/library/api.py",
            lineno=42,
            msg="unrelated error",
            args=None,
            exc_info=None,
        )
        root.handle(rec)
    finally:
        root.removeHandler(handler)

    assert len(records) == 1
    assert records[0].msg == "unrelated error"
