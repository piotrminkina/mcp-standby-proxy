import asyncio

import pytest

from mcp_standby_proxy.state import BackendState, StateMachine, TRANSITIONS
from mcp_standby_proxy.errors import StateError


def test_initial_state_is_cold() -> None:
    sm = StateMachine()
    assert sm.state == BackendState.COLD


async def test_valid_transition_cold_to_starting() -> None:
    sm = StateMachine()
    async with sm.lock:
        await sm.transition(BackendState.STARTING)
    assert sm.state == BackendState.STARTING


async def test_invalid_transition_cold_to_active_raises() -> None:
    sm = StateMachine()
    async with sm.lock:
        with pytest.raises(StateError):
            await sm.transition(BackendState.ACTIVE)


async def test_all_valid_transitions() -> None:
    """All transitions defined in TRANSITIONS table should succeed."""
    for from_state, targets in TRANSITIONS.items():
        for target in targets:
            sm = StateMachine()
            # Set state directly for testing
            sm._state = from_state
            async with sm.lock:
                await sm.transition(target)
            assert sm.state == target


async def test_invalid_transitions_raise_state_error() -> None:
    """Transitions not in the table should raise StateError."""
    # COLD cannot go to HEALTHY
    sm = StateMachine()
    async with sm.lock:
        with pytest.raises(StateError):
            await sm.transition(BackendState.HEALTHY)


async def test_wait_for_resolves_when_state_reached() -> None:
    sm = StateMachine()

    async def _transition_later() -> None:
        await asyncio.sleep(0.01)
        async with sm.lock:
            await sm.transition(BackendState.STARTING)

    asyncio.create_task(_transition_later())
    result = await sm.wait_for(BackendState.STARTING)
    assert result == BackendState.STARTING


async def test_wait_for_timeout_raises() -> None:
    sm = StateMachine()
    with pytest.raises(asyncio.TimeoutError):
        await sm.wait_for(BackendState.ACTIVE, timeout=0.05)


async def test_wait_for_multiple_states_resolves_first() -> None:
    sm = StateMachine()

    async def _transition_later() -> None:
        await asyncio.sleep(0.01)
        async with sm.lock:
            await sm.transition(BackendState.STARTING)

    asyncio.create_task(_transition_later())
    result = await sm.wait_for(BackendState.ACTIVE, BackendState.STARTING)
    assert result == BackendState.STARTING


async def test_wait_for_already_in_target_state() -> None:
    sm = StateMachine()
    result = await sm.wait_for(BackendState.COLD)
    assert result == BackendState.COLD


async def test_state_property_reads_correctly_after_transitions() -> None:
    sm = StateMachine()
    assert sm.state == BackendState.COLD

    async with sm.lock:
        await sm.transition(BackendState.STARTING)
    assert sm.state == BackendState.STARTING

    async with sm.lock:
        await sm.transition(BackendState.FAILED)
    assert sm.state == BackendState.FAILED

    async with sm.lock:
        await sm.transition(BackendState.COLD)
    assert sm.state == BackendState.COLD


async def test_concurrent_wait_for_all_wake_up() -> None:
    """Multiple concurrent wait_for calls should all wake up on the same transition."""
    sm = StateMachine()
    results: list[BackendState] = []

    async def _waiter() -> None:
        state = await sm.wait_for(BackendState.STARTING)
        results.append(state)

    tasks = [asyncio.create_task(_waiter()) for _ in range(5)]
    await asyncio.sleep(0.01)

    async with sm.lock:
        await sm.transition(BackendState.STARTING)

    await asyncio.gather(*tasks)
    assert len(results) == 5
    assert all(r == BackendState.STARTING for r in results)


async def test_state_error_contains_from_and_to() -> None:
    sm = StateMachine()
    async with sm.lock:
        with pytest.raises(StateError) as exc_info:
            await sm.transition(BackendState.ACTIVE)
    err = exc_info.value
    assert err.from_state == BackendState.COLD
    assert err.to_state == BackendState.ACTIVE


def test_lock_is_exposed() -> None:
    sm = StateMachine()
    assert isinstance(sm.lock, asyncio.Lock)
