from fastapi import Request

from app.db.pool import Db2Pool


async def get_pool(request: Request) -> Db2Pool:
    """Returns the database connection pool instance.
    
    Use this dependency in sync endpoints that are run in thread pools.
    """
    return request.app.state.pool
