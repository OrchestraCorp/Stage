"""Server access logging retains paths/status without OAuth hints, codes or state."""

import logging

from app.core.middleware import AccessQueryFilter, install_safe_access_logging


def test_oauth_logout_hint_and_state_are_removed_before_formatting() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %s',
        ("127.0.0.1:1234", "GET", "/oauth/logout?id_token_hint=private-token&state=private-state", "1.1", 200),
        None,
    )
    assert AccessQueryFilter().filter(record)
    assert record.getMessage() == '127.0.0.1:1234 - "GET /oauth/logout HTTP/1.1" 200'
    assert "private" not in record.getMessage()


def test_logging_filter_preserves_non_query_paths_and_handles_other_records() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %s',
        ("peer", "GET", "/oauth/authorize", "1.1", 400),
        None,
    )
    assert AccessQueryFilter().filter(record) and "/oauth/authorize HTTP/1.1" in record.getMessage()
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "server status %s", ("ok",), None)
    assert AccessQueryFilter().filter(other) and other.getMessage() == "server status ok"


def test_logging_filter_installation_is_idempotent() -> None:
    install_safe_access_logging()
    install_safe_access_logging()
    assert sum(isinstance(item, AccessQueryFilter) for item in logging.getLogger("uvicorn.access").filters) == 1
