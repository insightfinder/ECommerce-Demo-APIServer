from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.concurrency import run_in_threadpool

from app.db import queries
from app.db.errors import DbError
from app.db.pool import Db2Pool
from app.logging_setup import current_request_id
from app.routes.deps import get_pool

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(pool: Db2Pool = Depends(get_pool)):
    """Readiness check that verifies database connectivity.
    
    Async endpoint that properly yields control to the event loop while
    acquiring database connections, preventing connection pool exhaustion.
    """
    try:
        async with pool.async_connection() as conn:
            await run_in_threadpool(queries.ping, conn)
    except DbError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "error": "db_error",
                "sqlcode": exc.sqlcode,
                "sqlstate": exc.sqlstate,
                "request_id": current_request_id(),
            },
        )
    return {"status": "ready"}


@router.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
