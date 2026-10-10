from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from app.db import queries
from app.db.pool import Db2Pool
from app.errors import ApiError
from app.routes.deps import get_pool

router = APIRouter(prefix="/api", tags=["products"])


@router.get("/products")
async def list_products(
    category: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    pool: Db2Pool = Depends(get_pool),
):
    """List products with optional category filter.
    
    Async endpoint that properly yields control to the event loop while
    acquiring database connections, preventing connection pool exhaustion.
    """
    async with pool.async_connection() as conn:
        if category:
            items = await run_in_threadpool(queries.list_products_by_category, conn, category, limit, offset)
        else:
            items = await run_in_threadpool(queries.list_products, conn, limit, offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/products/{id}")
async def get_product(id: int, pool: Db2Pool = Depends(get_pool)):
    """Get a single product by ID.
    
    Async endpoint that properly yields control to the event loop while
    acquiring database connections, preventing connection pool exhaustion.
    """
    async with pool.async_connection() as conn:
        product = await run_in_threadpool(queries.get_product, conn, id)
        if product is None:
            raise ApiError(404, "product_not_found", f"product {id} not found", product_id=id)
        return product


@router.get("/categories")
async def list_categories(pool: Db2Pool = Depends(get_pool)):
    """List all product categories.
    
    Async endpoint that properly yields control to the event loop while
    acquiring database connections, preventing connection pool exhaustion.
    """
    async with pool.async_connection() as conn:
        items = await run_in_threadpool(queries.list_categories, conn)
    return {"items": items}
