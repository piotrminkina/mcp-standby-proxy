import pytest

from mcp_standby_proxy.errors import (
    CacheError,
    ConfigError,
    HealthcheckError,
    LifecycleError,
    ProxyError,
    StartError,
    StateError,
    TransportError,
)


def test_proxy_error_is_base() -> None:
    assert issubclass(ProxyError, Exception)


def test_config_error_inherits_proxy_error() -> None:
    err = ConfigError("bad config")
    assert isinstance(err, ProxyError)
    assert isinstance(err, ConfigError)


def test_lifecycle_error_inherits_proxy_error() -> None:
    err = LifecycleError("failed")
    assert isinstance(err, ProxyError)
    assert isinstance(err, LifecycleError)


def test_start_error_inherits_lifecycle_error() -> None:
    err = StartError(exit_code=1, stderr="command not found")
    assert isinstance(err, ProxyError)
    assert isinstance(err, LifecycleError)
    assert isinstance(err, StartError)


def test_start_error_str_contains_context() -> None:
    err = StartError(exit_code=2, stderr="permission denied")
    msg = str(err)
    assert "2" in msg
    assert "permission denied" in msg


def test_start_error_none_exit_code() -> None:
    err = StartError(exit_code=None, stderr="timed out")
    assert err.exit_code is None
    assert "None" in str(err)


def test_healthcheck_error_inherits_lifecycle_error() -> None:
    err = HealthcheckError(attempts=30)
    assert isinstance(err, ProxyError)
    assert isinstance(err, LifecycleError)
    assert isinstance(err, HealthcheckError)


def test_healthcheck_error_str_contains_attempts() -> None:
    err = HealthcheckError(attempts=5, last_error="connection refused")
    msg = str(err)
    assert "5" in msg
    assert "connection refused" in msg


def test_healthcheck_error_without_last_error() -> None:
    err = HealthcheckError(attempts=10)
    assert "10" in str(err)
    assert err.last_error == ""


def test_transport_error_inherits_proxy_error() -> None:
    err = TransportError("stream closed")
    assert isinstance(err, ProxyError)
    assert isinstance(err, TransportError)


def test_cache_error_inherits_proxy_error() -> None:
    err = CacheError("write failed")
    assert isinstance(err, ProxyError)
    assert isinstance(err, CacheError)


def test_state_error_inherits_proxy_error() -> None:
    err = StateError("COLD", "ACTIVE")
    assert isinstance(err, ProxyError)
    assert isinstance(err, StateError)


def test_state_error_str_contains_states() -> None:
    err = StateError("COLD", "ACTIVE")
    msg = str(err)
    assert "COLD" in msg
    assert "ACTIVE" in msg


def test_all_errors_share_common_ancestor() -> None:
    errors = [
        ConfigError("x"),
        LifecycleError("x"),
        StartError(1, "x"),
        HealthcheckError(1),
        TransportError("x"),
        CacheError("x"),
        StateError("A", "B"),
    ]
    for err in errors:
        assert isinstance(err, ProxyError), f"{type(err)} is not a ProxyError"


def test_start_error_stores_fields() -> None:
    err = StartError(exit_code=42, stderr="some error")
    assert err.exit_code == 42
    assert err.stderr == "some error"


def test_healthcheck_error_stores_fields() -> None:
    err = HealthcheckError(attempts=7, last_error="timeout")
    assert err.attempts == 7
    assert err.last_error == "timeout"


def test_state_error_stores_fields() -> None:
    err = StateError("STARTING", "COLD")
    assert err.from_state == "STARTING"
    assert err.to_state == "COLD"


@pytest.mark.parametrize("exc_class,args", [
    (ConfigError, ("msg",)),
    (TransportError, ("msg",)),
    (CacheError, ("msg",)),
])
def test_simple_errors_str(exc_class: type, args: tuple) -> None:
    err = exc_class(*args)
    assert str(err) == "msg"
