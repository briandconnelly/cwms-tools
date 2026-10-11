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
