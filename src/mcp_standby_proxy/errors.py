class ProxyError(Exception):
    """Base exception for all proxy errors."""


class ConfigError(ProxyError):
    """Configuration loading or validation failed."""


class LifecycleError(ProxyError):
    """Backend lifecycle operation failed (start/stop/healthcheck)."""


class StartError(LifecycleError):
    """Start command failed."""

    def __init__(self, exit_code: int | None, stderr: str) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"Start command failed with exit code {exit_code}: {stderr}")


class HealthcheckError(LifecycleError):
    """Healthcheck exceeded max attempts or timed out."""

    def __init__(self, attempts: int, last_error: str = "") -> None:
        self.attempts = attempts
        self.last_error = last_error
        msg = f"Healthcheck failed after {attempts} attempt(s)"
        if last_error:
            msg += f": {last_error}"
        super().__init__(msg)


class TransportError(ProxyError):
    """Transport connection or communication failed."""


class CacheError(ProxyError):
    """Cache read/write or version mismatch."""


class StateError(ProxyError):
    """Invalid state transition attempted."""

    def __init__(self, from_state: object, to_state: object) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Invalid state transition: {from_state} -> {to_state}")
