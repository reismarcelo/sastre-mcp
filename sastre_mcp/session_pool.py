"""Thread-safe, per-manager pooling of cisco_sdwan ``Rest`` sessions.

``cisco_sdwan.Rest`` wraps a ``requests.Session``, which is not safe for
concurrent use by multiple threads. Show tasks run in worker threads (via
``asyncio.to_thread``), so a fresh ``Rest(...)`` per call would perform a full
vManage login/logout on every request and, under load, could open an unbounded
number of short-lived sessions against a single SD-WAN Manager.

This module keeps a small pool of reusable, logged-in sessions per manager. The
pool:

- bounds the number of concurrent sessions per manager (a ``BoundedSemaphore``
  acts as a connection limiter so a burst of tool calls cannot open unbounded
  vManage sessions),
- reuses idle sessions to avoid a full login on every request,
- evicts sessions that have been idle long enough to risk a server-side
  session timeout, and
- discards sessions on error instead of returning them to the pool, retrying a
  reused session once with a fresh login when it appears stale.

A leased session is removed from the idle set for the duration of the call and
only returned afterwards, so a given ``Rest`` is never used by two threads at
once.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

import requests
from cisco_sdwan.base.rest_api import Rest, RestAPIException

logger = logging.getLogger(__name__)

T = TypeVar("T")

# A reused (pooled) session may have expired server-side; these errors trigger a
# single retry with a freshly created session. Show tasks are read-only, so the
# retry is safe to repeat.
_STALE_SESSION_ERRORS = (requests.exceptions.RequestException, RestAPIException)


class SessionPoolTimeout(Exception):
    """Raised when no session becomes available within the configured timeout."""


def _safe_close(rest: Rest) -> None:
    """Best-effort logout + close; never raises."""
    try:
        if rest.session is not None:
            try:
                rest.logout()
            except Exception:
                logger.debug("error logging out pooled session", exc_info=True)
            rest.session.close()
    except Exception:
        logger.debug("error closing pooled session", exc_info=True)


class _PooledSession:
    __slots__ = ("last_used", "rest")

    def __init__(self, rest: Rest) -> None:
        self.rest = rest
        self.last_used = time.monotonic()


class ManagerSessionPool:
    """Bounded, thread-safe pool of logged-in ``Rest`` sessions for one manager."""

    def __init__(
        self,
        name: str,
        fingerprint: str,
        factory: Callable[[], Rest],
        *,
        max_size: int,
        max_idle_secs: int,
        acquire_timeout_secs: float,
    ) -> None:
        self.name = name
        self.fingerprint = fingerprint
        self.max_size = max_size
        self.max_idle_secs = max_idle_secs
        self.acquire_timeout_secs = acquire_timeout_secs
        self._factory = factory
        self._semaphore = threading.BoundedSemaphore(max_size)
        self._lock = threading.Lock()
        self._idle: list[_PooledSession] = []

    def _pop_idle(self) -> Rest | None:
        """Return a reusable idle session, discarding any that are too stale."""
        now = time.monotonic()
        stale: list[Rest] = []
        with self._lock:
            while self._idle:
                pooled = self._idle.pop()
                if self.max_idle_secs and (now - pooled.last_used) > self.max_idle_secs:
                    stale.append(pooled.rest)
                    continue
                rest = pooled.rest
                break
            else:
                rest = None
        # Close evicted sessions outside the lock (network I/O).
        for old in stale:
            _safe_close(old)
        return rest

    def _return_idle(self, rest: Rest) -> None:
        with self._lock:
            self._idle.append(_PooledSession(rest))

    def run(self, operation: Callable[[Rest], T]) -> T:
        """Run ``operation`` with a pooled (or fresh) session, bounding concurrency."""
        if not self._semaphore.acquire(timeout=self.acquire_timeout_secs):
            raise SessionPoolTimeout(
                f"No available session to manager '{self.name}' within {self.acquire_timeout_secs}s"
            )
        rest: Rest | None = None
        try:
            rest = self._pop_idle()
            if rest is not None:
                try:
                    result = operation(rest)
                except _STALE_SESSION_ERRORS as exc:
                    logger.debug(
                        "pooled session to '%s' failed (%s); retrying with a fresh session",
                        self.name,
                        type(exc).__name__,
                    )
                    _safe_close(rest)
                    rest = None
                    rest = self._factory()
                    result = operation(rest)
            else:
                rest = self._factory()
                result = operation(rest)
        except BaseException:
            if rest is not None:
                _safe_close(rest)
            raise
        else:
            self._return_idle(rest)
            return result
        finally:
            self._semaphore.release()

    def close_all(self) -> None:
        """Close every idle session. Leased sessions close themselves on return."""
        with self._lock:
            idle = self._idle
            self._idle = []
        for pooled in idle:
            _safe_close(pooled.rest)


_pools: dict[str, ManagerSessionPool] = {}
_pools_lock = threading.Lock()


def _get_pool(
    key: str,
    fingerprint: str,
    factory: Callable[[], Rest],
    *,
    max_size: int,
    max_idle_secs: int,
    acquire_timeout_secs: float,
) -> ManagerSessionPool:
    """Return the pool for ``key``, rebuilding it if its configuration changed."""
    with _pools_lock:
        existing = _pools.get(key)
        if (
            existing is not None
            and existing.fingerprint == fingerprint
            and existing.max_size == max_size
            and existing.max_idle_secs == max_idle_secs
            and existing.acquire_timeout_secs == acquire_timeout_secs
        ):
            return existing
        pool = ManagerSessionPool(
            key,
            fingerprint,
            factory,
            max_size=max_size,
            max_idle_secs=max_idle_secs,
            acquire_timeout_secs=acquire_timeout_secs,
        )
        _pools[key] = pool
    # Close the superseded pool's idle sessions outside the registry lock.
    if existing is not None:
        existing.close_all()
    return pool


def run_with_session[T](
    *,
    key: str,
    fingerprint: str,
    factory: Callable[[], Rest],
    max_size: int,
    max_idle_secs: int,
    acquire_timeout_secs: float,
    operation: Callable[[Rest], T],
) -> T:
    """Run ``operation`` against a pooled session for manager ``key``."""
    pool = _get_pool(
        key,
        fingerprint,
        factory,
        max_size=max_size,
        max_idle_secs=max_idle_secs,
        acquire_timeout_secs=acquire_timeout_secs,
    )
    return pool.run(operation)


def reset_pools() -> None:
    """Close and drop all pools (for shutdown and tests)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close_all()
