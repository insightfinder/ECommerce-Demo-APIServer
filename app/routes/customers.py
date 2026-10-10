from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from app.db import queries
from app.db.pool import Db2Pool
from app.routes.deps import get_pool

router = APIRouter(prefix="/api", tags=["customers"])


@router.get("/customers")
async def list_customers(limit: int = Query(50, ge=1, le=1000), pool: Db2Pool = Depends(get_pool)):
    """List customers with pagination.
    
    Async endpoint that properly yields control to the event loop while
    acquiring database connections, preventing connection pool exhaustion.
    """
    async with pool.async_connection() as conn:
        items = await run_in_threadpool(queries.list_customers, conn, limit)
    return {"items": items}
