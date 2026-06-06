"""Tests for the per-manager vManage session pool (no live vManage)."""

import threading
import time

import pytest
import requests
from sastre_mcp.session_pool import (
    ManagerSessionPool,
    SessionPoolTimeout,
    reset_pools,
    run_with_session,
)


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeRest:
    """Minimal stand-in for cisco_sdwan.base.rest_api.Rest."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.logged_out = False

    def logout(self) -> bool:
        self.logged_out = True
        return True


def _counting_factory() -> tuple[list[FakeRest], callable]:
    created: list[FakeRest] = []

    def factory() -> FakeRest:
        rest = FakeRest()
        created.append(rest)
        return rest

    return created, factory


def _pool(factory, *, max_size=2, max_idle_secs=600, acquire_timeout_secs=5):
    return ManagerSessionPool(
        "primary",
        "fp",
        factory,
        max_size=max_size,
        max_idle_secs=max_idle_secs,
        acquire_timeout_secs=acquire_timeout_secs,
    )


def test_session_is_reused_across_calls():
    created, factory = _counting_factory()
    pool = _pool(factory)

    pool.run(lambda api: "a")
    pool.run(lambda api: "b")

    assert len(created) == 1, "second call should reuse the idle session"
    assert created[0].logged_out is False


def test_returns_operation_result():
    _, factory = _counting_factory()
    pool = _pool(factory)
    assert pool.run(lambda api: 42) == 42


def test_concurrency_is_bounded_by_max_size():
    created, factory = _counting_factory()
    pool = _pool(factory, max_size=2)

    start = threading.Barrier(3)
    release = threading.Event()
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def op(api):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        release.wait(timeout=5)
        with lock:
            in_flight -= 1
        return None

    def worker():
        start.wait(timeout=5)
        pool.run(op)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    start.wait(timeout=5)
    # Give both workers a moment to enter the operation under the semaphore.
    time.sleep(0.2)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert peak <= 2
    assert len(created) <= 2


def test_acquire_timeout_raises_when_all_busy():
    _, factory = _counting_factory()
    pool = _pool(factory, max_size=1, acquire_timeout_secs=0.2)

    holding = threading.Event()
    release = threading.Event()

    def holder(api):
        holding.set()
        release.wait(timeout=5)
        return None

    t = threading.Thread(target=lambda: pool.run(holder))
    t.start()
    assert holding.wait(timeout=5)
    try:
        with pytest.raises(SessionPoolTimeout):
            pool.run(lambda api: None)
    finally:
        release.set()
        t.join(timeout=5)


def test_idle_session_evicted_after_max_idle():
    created, factory = _counting_factory()
    pool = _pool(factory, max_size=1, max_idle_secs=300)

    pool.run(lambda api: None)
    first = created[0]
    # Age the idle entry past the threshold.
    pool._idle[0].last_used = time.monotonic() - 10_000

    pool.run(lambda api: None)

    assert len(created) == 2, "stale idle session should be replaced"
    assert first.logged_out is True
    assert first.session.closed is True


def test_idle_eviction_disabled_when_zero():
    created, factory = _counting_factory()
    pool = _pool(factory, max_size=1, max_idle_secs=0)

    pool.run(lambda api: None)
    pool._idle[0].last_used = time.monotonic() - 10_000
    pool.run(lambda api: None)

    assert len(created) == 1, "max_idle_secs=0 disables idle eviction"


def test_stale_reused_session_retried_once_with_fresh_session():
    created, factory = _counting_factory()
    pool = _pool(factory, max_size=1)

    pool.run(lambda api: None)
    first = created[0]

    calls = {"n": 0}

    def flaky(api):
        calls["n"] += 1
        if api is first:
            raise requests.exceptions.ConnectionError("stale")
        return "ok"

    assert pool.run(flaky) == "ok"
    assert calls["n"] == 2, "should retry once after the reused session fails"
    assert len(created) == 2
    assert first.session.closed is True


def test_failing_session_not_returned_to_pool():
    created, factory = _counting_factory()
    pool = _pool(factory, max_size=1)

    def boom(api):
        raise requests.exceptions.ConnectionError("down")

    with pytest.raises(requests.exceptions.ConnectionError):
        pool.run(boom)

    assert pool._idle == [], "failed session must not be pooled"
    assert created[0].session.closed is True


def test_non_retryable_error_propagates_without_retry():
    created, factory = _counting_factory()
    pool = _pool(factory, max_size=1)

    # Prime an idle session so the next run reuses it.
    pool.run(lambda api: None)

    def raises_value(api):
        raise ValueError("not transport related")

    with pytest.raises(ValueError):
        pool.run(raises_value)

    # ValueError is not a stale-session error, so no fresh retry is attempted.
    assert len(created) == 1


def test_close_all_closes_idle_sessions():
    created, factory = _counting_factory()
    pool = _pool(factory, max_size=1)
    pool.run(lambda api: None)

    pool.close_all()

    assert created[0].session.closed is True
    assert pool._idle == []


def test_registry_reuses_pool_for_same_fingerprint():
    reset_pools()
    created, factory = _counting_factory()

    def run_once():
        return run_with_session(
            key="primary",
            fingerprint="fp",
            factory=factory,
            max_size=2,
            max_idle_secs=600,
            acquire_timeout_secs=5,
            operation=lambda api: "x",
        )

    run_once()
    run_once()
    assert len(created) == 1, "same fingerprint should reuse the pooled session"
    reset_pools()


def test_registry_rebuilds_pool_on_fingerprint_change():
    reset_pools()
    created, factory = _counting_factory()

    run_with_session(
        key="primary",
        fingerprint="fp1",
        factory=factory,
        max_size=2,
        max_idle_secs=600,
        acquire_timeout_secs=5,
        operation=lambda api: None,
    )
    first = created[0]

    run_with_session(
        key="primary",
        fingerprint="fp2",
        factory=factory,
        max_size=2,
        max_idle_secs=600,
        acquire_timeout_secs=5,
        operation=lambda api: None,
    )

    assert first.session.closed is True, "old pool's sessions should be closed on rebuild"
    assert len(created) == 2
    reset_pools()


def test_reset_pools_closes_everything():
    reset_pools()
    created, factory = _counting_factory()
    run_with_session(
        key="primary",
        fingerprint="fp",
        factory=factory,
        max_size=1,
        max_idle_secs=600,
        acquire_timeout_secs=5,
        operation=lambda api: None,
    )

    reset_pools()

    assert created[0].session.closed is True
