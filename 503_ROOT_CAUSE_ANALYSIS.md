# HTTP 503 Root Cause Analysis: GET /api/products Endpoint

## Executive Summary

**Root Cause Identified**: Connection pool resource leak in the `GET /api/products/{id}` endpoint caused by missing connection release on the 404 error path.

**Impact**: Every lookup of a missing product leaks one database connection from the pool. Once the pool is exhausted, all subsequent database requests timeout and return HTTP 503 (Service Unavailable).

**Status**: **CRITICAL** - Active production issue causing service unavailability

---

## Incident Metrics Analysis

```
Status: 503
Duration: 76.684 ms
pool_acquire_ms: 10.648 ms
db_ms: 0.0 ms
```

### Key Observations

1. **Pool acquisition time (10.648 ms)** is the dominant factor in the 76.684 ms total duration
2. **Database execution time (0.0 ms)** indicates the database is not being queried - the issue is pre-query
3. **High pool_acquire_ms relative to db_ms** is the smoking gun pointing to pool exhaustion
4. The 10.648 ms is close to the `POOL_ACQUIRE_TIMEOUT` (default: 5ms from config), indicating a timeout

---

## Root Cause: Connection Leak in GET /api/products/{id}

### Location: `app/routes/products.py:26-37`

```python
@router.get("/products/{id}")
def get_product(id: int, pool: Db2Pool = Depends(get_pool)):
    conn = pool.acquire()
    product = queries.get_product(conn, id)
    if product is None:
        # BUG: the connection acquired above is never released on this path, so
        # every lookup of a missing product leaks one pool connection. Once
        # POOL_MAX_SIZE connections have leaked, every DB request waits
        # POOL_ACQUIRE_TIMEOUT and then fails with 503 db_pool_timeout.
        raise ApiError(404, "product_not_found", f"product {id} not found", product_id=id)
    pool.release(conn)
    return product
```

### The Bug

**Line 28**: `conn = pool.acquire()` - Acquires a connection from the pool
**Lines 29-30**: Query executes successfully, but returns no rows (product not found)
**Lines 31-35**: `ApiError` is raised WITHOUT releasing the connection
**Line 36**: `pool.release(conn)` is never reached when product is None

### Why This Causes 503

1. **Initial Request** (product_id=999):
   - `conn = pool.acquire()` → Connection acquired (pool size decreases)
   - `get_product()` returns None
   - `ApiError` raised
   - Connection never released → Connection stays marked as "in use"

2. **Subsequent Requests**:
   - Pool has fewer available connections
   - Each new 404 leaks another connection
   - Eventually pool is exhausted (all connections marked "in use")

3. **When Pool Exhausted**:
   - `pool.acquire()` calls `self._sem.acquire(timeout=self.acquire_timeout)`
   - With default `POOL_ACQUIRE_TIMEOUT=0.005` seconds (5ms), timeout occurs
   - `PoolTimeout` exception raised
   - Exception handler returns 503 status code

### Evidence in Code

**Pool Implementation** (`app/db/pool.py:99-126`):
```python
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
        raise PoolTimeout(self.acquire_timeout)  # ← Returns 503
```

**Exception Handler** (`app/main.py:106-112`):
```python
@app.exception_handler(PoolTimeout)
async def pool_timeout_handler(request: Request, exc: PoolTimeout):
    log.error("db pool acquire timed out", extra={"timeout_seconds": exc.timeout})
    return JSONResponse(
        status_code=503,  # ← Status 503
        content=_error_body("db_pool_timeout", message=str(exc), timeout_seconds=exc.timeout),
    )
```

---

## Dependency Analysis

### Direct Dependencies of GET /api/products/{id}

```
GET /api/products/{id}
├── FastAPI framework
│   └── Depends(get_pool)
│       └── app.state.pool (Db2Pool instance)
├── app.db.queries.get_product()
│   ├── ibm_db.prepare()
│   ├── ibm_db.execute()
│   ├── ibm_db.fetch_assoc() (loop)
│   └── ibm_db.free_stmt()
├── Pool connection release (MISSING on 404 path)
│   ├── pool.release()
│   │   ├── self._idle.put(conn)
│   │   └── self._checked_in()
│   │       ├── self._in_use -= 1
│   │       └── self._sem.release()
│   └── pool.discard() (for communication errors)
└── Exception handling (FastAPI / Starlette)
```

### Downstream Dependencies Affected

When pool is exhausted, ALL database operations are affected:
- **GET /api/products** - Connection list endpoint
- **GET /api/products?category=X** - Filtered list endpoint
- **GET /api/categories** - Categories endpoint
- **GET /api/customers** - Customers endpoint
- **GET /api/orders** - Orders endpoint
- **POST /api/orders** - Order creation endpoint
- **GET /healthz** - Health check (does NOT use pool)
- **GET /readyz** - Readiness check (uses pool)

---

## Comparison: Correct vs. Broken Implementations

### Correct Pattern (Lines 11-23): GET /api/products

```python
@router.get("/products")
def list_products(
    category: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    pool: Db2Pool = Depends(get_pool),
):
    with pool.connection() as conn:  # ← Uses context manager
        if category:
            items = queries.list_products_by_category(conn, category, limit, offset)
        else:
            items = queries.list_products(conn, limit, offset)
    return {"items": items, "limit": limit, "offset": offset}
```

**Why it works**: `pool.connection()` is a context manager that ensures release/discard in finally block (`app/db/pool.py:145-158`):
```python
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
            self.discard(conn)  # ← Always executed
        else:
            self.release(conn)  # ← Always executed
```

### Broken Pattern (Lines 26-37): GET /api/products/{id}

```python
@router.get("/products/{id}")
def get_product(id: int, pool: Db2Pool = Depends(get_pool)):
    conn = pool.acquire()  # ← Manual acquire, no context manager
    product = queries.get_product(conn, id)
    if product is None:
        raise ApiError(404, ...)  # ← Exception bypasses release
    pool.release(conn)  # ← Unreachable on 404 path
    return product
```

**Why it fails**: Manual acquire/release without try-finally protection

---

## Incident Progression Timeline

### Phase 1: Early Requests (Pool Healthy)
- User requests: `GET /api/products/999` (non-existent product)
- Response: 404 product_not_found
- Pool State: Connection leaked (in_use count = 1)

### Phase 2: Multiple 404 Requests (Pool Degradation)
```
Request 1: GET /api/products/999 → 404, leak 1 connection (in_use: 1/20)
Request 2: GET /api/products/888 → 404, leak 1 connection (in_use: 2/20)
Request 3: GET /api/products/777 → 404, leak 1 connection (in_use: 3/20)
...
Request 20: GET /api/products/111 → 404, leak 1 connection (in_use: 20/20)
```

### Phase 3: Pool Exhausted (503 Cascade)
```
Request 21: ANY GET/POST with DB → acquire() waits POOL_ACQUIRE_TIMEOUT (5ms)
            → timeout → PoolTimeout exception → 503 db_pool_timeout
Request 22: ANY GET/POST with DB → 503 db_pool_timeout
Request 23: ANY GET/POST with DB → 503 db_pool_timeout
...
Service Unavailable until pool reset or connections expire
```

---

## Configuration Impact

### Default Pool Configuration (app/config.py:27-40)

```python
pool_min_size=int(env.get("POOL_MIN_SIZE", "2")),        # Default: 2
pool_max_size=int(env.get("POOL_MAX_SIZE", "20")),       # Default: 20
pool_acquire_timeout=float(env.get("POOL_ACQUIRE_TIMEOUT", "0.005")),  # Default: 5ms
```

### Impact Analysis

| Setting | Default | Effect |
|---------|---------|--------|
| `POOL_MAX_SIZE` | 20 | After 20 failed GET /products/{id} requests, service down |
| `POOL_ACQUIRE_TIMEOUT` | 5ms | Incident metrics show ~10.648ms (2x timeout), request waits and fails |
| `POOL_MIN_SIZE` | 2 | Smaller pool = faster exhaustion |

The default configuration means:
- **Only 20 requests with invalid product IDs** can be served before cascading 503 failures
- This is an extremely low tolerance for this common scenario

---

## Exception Handling Path Analysis

### How 503 Response is Generated

```
GET /api/products/{id}
    ↓
pool.acquire() called (line 28)
    ↓
BoundedSemaphore.acquire(timeout=0.005)
    ↓
[If product_id not found]
    ↓
ApiError(404, ...) raised (line 35)
    ↓
pool.release(conn) NOT executed (line 36 unreachable)
    ↓
Connection remains in "in_use" state
    ↓
[Repeated for 20 requests with missing products]
    ↓
Pool exhausted (all 20 semaphore slots occupied)
    ↓
Next request: acquire() → waits 5ms → timeout
    ↓
PoolTimeout exception raised (app/db/pool.py:111)
    ↓
@app.exception_handler(PoolTimeout) catches it
    ↓
Returns JSONResponse(status_code=503, ...)
```

### Exception Handlers Involved

1. **PoolTimeout Handler** (`app/main.py:106-112`):
   - Triggered when semaphore acquire times out
   - Returns 503 with error code "db_pool_timeout"

2. **ApiError Handler** (`app/main.py:127-132`):
   - Handles the 404 error
   - But connection leak already occurred by this point

3. **Middleware** (`app/main.py:36-98`):
   - Logs request with pool_acquire_ms metrics
   - Observes the high pool_acquire_ms value (indicating timeout)

---

## Detection Metrics

### Prometheus Metrics Indicating the Issue

From `app/db/pool.py`:

1. **POOL_IN_USE** (Gauge):
   - Tracks connections currently checked out
   - Rising trend without corresponding release = leak detected

2. **POOL_ACQUIRE_TIMEOUTS** (Counter):
   - Incremented every time acquire() times out
   - Non-zero after pool exhaustion

3. **POOL_ACQUIRE_SECONDS** (Histogram):
   - Observes connection acquisition time
   - Shows spike in acquisition time when pool near capacity

4. **POOL_CONNECTIONS_CREATED vs POOL_CONNECTIONS_CLOSED**:
   - If created >> closed, connections not being returned

### Query to Detect the Issue
```promql
apiserver_db_pool_in_use > apiserver_db_pool_max_size - 2
```
Would show pool near capacity

---

## Impact Summary

### Affected Scenarios

1. **Lookup of Non-Existent Products** ✗ (ROOT CAUSE)
   - Every 404 response leaks exactly 1 connection

2. **Healthy Operations** ✓ (No Leaks)
   - `GET /api/products` - Uses context manager
   - `GET /api/categories` - Uses context manager
   - `GET /api/customers` - Uses context manager
   - `POST /api/orders` - Uses context manager properly

3. **Other GET /products/{id} Operations** ✗ (SECONDARY IMPACT)
   - When pool exhausted, existing product lookups also fail with 503

### Service Degradation

- **Probability of 503**: P(all 20 connections leaked) = N 404s
- **Recovery Time**: Until pool is reset or DB connections timeout
- **Cascade Effect**: Any endpoint needing DB becomes unavailable

---

## Root Cause Categories Verification

### 1. Exception Handling Returns 503 ✓ CONFIRMED
- **File**: `app/main.py:106-112`
- **Handler**: `pool_timeout_handler`
- **Condition**: Pool exhausted, acquire times out
- **Status Code**: 503

### 2. Circuit Breaker or Timeout Logic ✓ CONFIRMED
- **File**: `app/db/pool.py:99-111`
- **Mechanism**: `BoundedSemaphore.acquire(timeout=self.acquire_timeout)`
- **Timeout**: Default 5ms
- **Effect**: Acts as built-in circuit breaker via semaphore

### 3. Dependency Health Checks ✗ NOT INVOLVED
- **Readiness Check** (`/readyz`): Checks DB connectivity, not pool health
- **Health Check** (`/healthz`): Does NOT use pool
- No explicit pool health check triggering 503

### 4. Resource Limit Checks ✓ CONFIRMED
- **File**: `app/db/pool.py:49-53`
- **Mechanism**: `BoundedSemaphore(max_size=20)`
- **Limit**: Default max_size=20 connections
- **Breach**: Leak causes all 20 slots to be consumed

### 5. Recent Code Changes
- **File**: `app/routes/products.py:26-37`
- **Issue**: Comment explicitly documents the bug
- **Status**: Known but unfixed

---

## Remediation Priority

| Severity | Item | Fix Time |
|----------|------|----------|
| CRITICAL | Fix connection leak in GET /api/products/{id} | 5 min |
| HIGH | Add connection release guarantee (try-finally) | 10 min |
| HIGH | Add integration test for 404 path | 15 min |
| MEDIUM | Increase POOL_ACQUIRE_TIMEOUT for production | 2 min |
| MEDIUM | Add pool leak detection monitoring | 30 min |

---

## Verification

### Test Case: test_pool_timeout_maps_to_503 (Lines 156-167 in test_routes.py)

```python
def test_pool_timeout_maps_to_503(make_client):
    client = make_client(pool_max_size=1, pool_acquire_timeout=0.05)
    held = client.app.state.pool.acquire()
    try:
        resp = client.get("/api/customers")
    finally:
        client.app.state.pool.release(held)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "db_pool_timeout"
    assert body["request_id"]
```

This test confirms the 503 response mechanism works correctly when pool is exhausted.

### Missing Test Case: Leak Detection

No test exists for the GET /api/products/{id} leak scenario. The code comment at line 31-34 acknowledges the bug but lacks test coverage.

---

## Incident Metrics Decoded

```
Request to GET /api/products/INVALID_ID:

Timing Breakdown:
├── pool_acquire_ms: 10.648
│   └── Waiting for semaphore slot (near capacity)
├── db_ms: 0.0
│   └── Database not reached (pool timeout before reaching DB)
├── Request processing: 76.684 - 10.648 = 66.036
│   ├── Framework overhead
│   ├── Exception handling
│   └── Response serialization
└── Total: 76.684 ms

Analysis:
- pool_acquire_ms (10.648) ≈ 2 × POOL_ACQUIRE_TIMEOUT (5ms)
  → Indicates timeout occurred, waiter retried or waited longer
- db_ms (0.0) << pool_acquire_ms
  → Bottleneck is resource acquisition, not database
- Pattern matches connection pool exhaustion profile
```

---

## Conclusion

The HTTP 503 responses on GET /api/products endpoint are caused by a **critical connection pool resource leak** in the `GET /api/products/{id}` endpoint handler. Every request that results in a 404 "product not found" response fails to release the database connection back to the pool. After approximately 20 such requests, the pool is completely exhausted, causing all subsequent database operations to timeout and return HTTP 503 (Service Unavailable).

**Root Cause**: Missing connection release in exception path at `app/routes/products.py:35`

**Fix Required**: Wrap manual acquire/release with try-finally, or use the `pool.connection()` context manager instead.

**Urgency**: CRITICAL - Affects production availability
