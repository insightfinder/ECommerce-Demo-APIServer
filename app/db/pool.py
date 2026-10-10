import asyncio
import logging
import queue
import threading
import time
from contextlib import asynccontextmanager, contextmanager

import ibm_db
from prometheus_client import Counter, Gauge, Histogram
from starlette.concurrency import run_in_threadpool

from app.db.errors import DbError, is_communication_error, parse_db2_error
from app.logging_setup import add_pool_acquire_time

log = logging.getLogger("apiserver.db.pool")

APPLICATION_NAME = "apiserver"

POOL_IN_USE = Gauge("apiserver_db_pool_in_use", "Connections currently checked out of the pool")
POOL_IDLE = Gauge("apiserver_db_pool_idle", "Idle connections currently held by the pool")
POOL_WAITERS = Gauge("apiserver_db_pool_waiters", "Threads currently waiting to acquire a connection")
POOL_MAX_SIZE = Gauge("apiserver_db_pool_max_size", "Configured maximum number of checked-out connections")
POOL_ACQUIRE_SECONDS = Histogram(
    "apiserver_db_pool_acquire_seconds",
    "Time spent acquiring a connection from the pool",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
POOL_CONNECTIONS_CREATED = Counter(
    "apiserver_db_pool_connections_created_total", "Physical Db2 connections opened"
)
POOL_CONNECTIONS_CLOSED = Counter(
    "apiserver_db_pool_connections_closed_total", "Physical Db2 connections closed"
)
POOL_ACQUIRE_TIMEOUTS = Counter(
    "apiserver_db_pool_acquire_timeouts_total", "Acquire attempts that timed out waiting for a connection"
)


class PoolTimeout(Exception):
    def __init__(self, timeout: float):
        super().__init__(f"timed out after {timeout}s waiting for a database connection")
        self.timeout = timeout


class Db2Pool:
    def __init__(self, dsn: str, min_size: int, max_size: int, acquire_timeout: float):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.acquire_timeout = acquire_timeout
        self._idle: queue.LifoQueue = queue.LifoQueue()
        self._sem = threading.BoundedSemaphore(max_size)
        self._lock = threading.Lock()
        self._in_use = 0
        POOL_MAX_SIZE.set(max_size)
        self._update_gauges()

    def start(self) -> None:
        """Pre-create min_size connections."""
        for _ in range(self.min_size):
            self._idle.put(self._connect())
        self._update_gauges()

    def close(self) -> None:
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            self._close(conn)
        self._update_gauges()

    def _connect(self):
        try:
            conn = ibm_db.connect(self.dsn, "", "")
            ibm_db.set_option(conn, {ibm_db.SQL_ATTR_INFO_APPLNAME: APPLICATION_NAME}, 1)
        except Exception as exc:
            info = parse_db2_error(ibm_db.conn_errormsg() or str(exc))
            log.error(
                "db connect failed",
                extra={"query": "connect", "sqlcode": info.sqlcode, "sqlstate": info.sqlstate, "reason": info.reason},
                exc_info=True,
            )
            raise DbError("connect", info) from exc
        POOL_CONNECTIONS_CREATED.inc()
        log.debug("db connection created")
        return conn

    def _close(self, conn) -> None:
        try:
            ibm_db.close(conn)
        except Exception:
            log.warning("error closing db connection", exc_info=True)
        POOL_CONNECTIONS_CLOSED.inc()
        log.debug("db connection closed")

    def _update_gauges(self) -> None:
        POOL_IN_USE.set(self._in_use)
        POOL_IDLE.set(self._idle.qsize())

    def acquire(self):
        start = time.perf_counter()
        POOL_WAITERS.inc()
        try:
            acquired = self._sem.acquire(timeout=self.acquire_timeout)
        finally:
            POOL_WAITERS.dec()
        if not acquired:
            elapsed = time.perf_counter() - start
            POOL_ACQUIRE_SECONDS.observe(elapsed)
            add_pool_acquire_time(elapsed)
            POOL_ACQUIRE_TIMEOUTS.inc()
            raise PoolTimeout(self.acquire_timeout)
        try:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = self._connect()
        except BaseException:
            self._sem.release()
            raise
        with self._lock:
            self._in_use += 1
        self._update_gauges()
        elapsed = time.perf_counter() - start
        POOL_ACQUIRE_SECONDS.observe(elapsed)
        add_pool_acquire_time(elapsed)
        return conn

    def release(self, conn) -> None:
        if self._idle.qsize() < self.min_size:
            self._idle.put(conn)
        else:
            self._close(conn)
        self._checked_in()

    def discard(self, conn) -> None:
        self._close(conn)
        self._checked_in()

    def _checked_in(self) -> None:
        with self._lock:
            self._in_use -= 1
        self._update_gauges()
        self._sem.release()

    @contextmanager
    def connection(self):
        conn = self.acquire()
        broken = False
        try:
            yield conn
        except BaseException as exc:
            broken = is_communication_error(exc)
            raise
        finally:
            if broken:
                self.discard(conn)
            else:
                self.release(conn)

    @asynccontextmanager
    async def async_connection(self):
        """Async-safe context manager for acquiring and releasing connections.
        
        This version uses run_in_threadpool to avoid blocking the event loop
        during connection acquisition, which is critical for high-concurrency scenarios.
        """
        conn = await run_in_threadpool(self.acquire)
        broken = False
        try:
            yield conn
        except BaseException as exc:
            broken = is_communication_error(exc)
            raise
        finally:
            if broken:
                await run_in_threadpool(self.discard, conn)
            else:
                await run_in_threadpool(self.release, conn)
