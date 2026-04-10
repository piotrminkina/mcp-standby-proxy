import asyncio
from enum import Enum

from mcp_standby_proxy.errors import StateError


class BackendState(Enum):
    COLD = "cold"
    STARTING = "starting"
    HEALTHY = "healthy"
    ACTIVE = "active"
    FAILED = "failed"
    STOPPING = "stopping"


# Valid transitions: from state -> set of allowed target states
TRANSITIONS: dict[BackendState, set[BackendState]] = {
    BackendState.COLD:     {BackendState.STARTING},
    BackendState.STARTING: {BackendState.HEALTHY, BackendState.FAILED, BackendState.STOPPING},
    BackendState.HEALTHY:  {BackendState.ACTIVE, BackendState.FAILED, BackendState.STOPPING},
    BackendState.ACTIVE:   {BackendState.STOPPING, BackendState.FAILED},
    BackendState.FAILED:   {BackendState.COLD},
    BackendState.STOPPING: {BackendState.COLD},
}


class StateMachine:
    """Async state machine for backend lifecycle management.

    Transitions are validated and serialized via an asyncio.Lock that callers
    must hold for the duration of a multi-step transition (e.g. start + healthcheck).
    """

    def __init__(self) -> None:
        self._state = BackendState.COLD
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition()

    @property
    def state(self) -> BackendState:
        """Current state (atomic enum read, no lock required)."""
        return self._state

    async def transition(self, target: BackendState) -> None:
        """Transition to target state.

        Must be called with the lock held externally.
        Raises StateError if the transition is not valid.
        """
        allowed = TRANSITIONS.get(self._state, set())
        if target not in allowed:
            raise StateError(self._state, target)
        self._state = target
        async with self._condition:
            self._condition.notify_all()

    async def wait_for(
        self,
        *states: BackendState,
        timeout: float | None = None,
    ) -> BackendState:
        """Wait until the state machine enters one of the given states.

        Returns the state that was reached. Raises asyncio.TimeoutError if
        timeout elapses before reaching any of the target states.
        """
        state_set = set(states)

        async def _wait() -> BackendState:
            async with self._condition:
                await self._condition.wait_for(lambda: self._state in state_set)
                return self._state

        if timeout is not None:
            return await asyncio.wait_for(_wait(), timeout=timeout)
        return await _wait()

    @property
    def lock(self) -> asyncio.Lock:
        """Expose the lock for external callers that need to serialize transitions."""
        return self._lock
