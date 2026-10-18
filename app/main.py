import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, load_settings
from app.db.errors import DbError
from app.db.pool import Db2Pool, PoolTimeout
from app.errors import ApiError
from app.logging_setup import (
    RequestContext,
    current_request_id,
    reset_request_context,
    set_request_context,
    setup_logging,
)
from app.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS
from app.routes import customers, health, orders, products

log = logging.getLogger("apiserver")
access_log = logging.getLogger("apiserver.access")

REQUEST_ID_HEADER = "x-request-id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ObservabilityMiddleware:
    """Request ID propagation, HTTP metrics and one access log line per request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == REQUEST_ID_HEADER.encode():
                incoming = value.decode("latin-1")
                break
        request_id = incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        ctx = RequestContext(request_id=request_id)
        token = set_request_context(ctx)

        status = 500
        started = False

        async def send_wrapper(message):
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(REQUEST_ID_HEADER.encode(), request_id.encode())]
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error")
            if not started:
                response = JSONResponse(
                    status_code=500,
                    content={"error": "internal_error", "message": "internal server error", "request_id": request_id},
                )
                await response(scope, receive, send_wrapper)
            status = 500
        finally:
            duration = time.perf_counter() - start
            route = scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            method = scope["method"]
            HTTP_REQUESTS.labels(method=method, route=route_path, status=str(status)).inc()
            HTTP_REQUEST_DURATION.labels(method=method, route=route_path).observe(duration)
            # 5xx (e.g. 503 from pool/lock timeouts) is logged at ERROR so failures stand out.
            access_log.log(
                logging.ERROR if status >= 500 else logging.INFO,
                "request completed",
                extra={
                    "method": method,
                    "route": route_path,
                    "status": status,
                    "duration_ms": round(duration * 1000, 3),
                    "pool_acquire_ms": round(ctx.pool_acquire_ms, 3),
                    "db_ms": round(ctx.db_ms, 3),
                },
            )
            reset_request_context(token)


def _error_body(error: str, **fields) -> dict:
    return {"error": error, **fields, "request_id": current_request_id()}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PoolTimeout)
    async def pool_timeout_handler(request: Request, exc: PoolTimeout):
        log.error("db pool acquire timed out", extra={"timeout_seconds": exc.timeout})
        return JSONResponse(
            status_code=503,
            content=_error_body("db_pool_timeout", message=str(exc), timeout_seconds=exc.timeout),
        )

    @app.exception_handler(DbError)
    async def db_error_handler(request: Request, exc: DbError):
        # Already logged at ERROR where it was raised (app.db.queries / app.db.pool).
        if exc.is_lock_timeout:
            return JSONResponse(
                status_code=503,
                content=_error_body("db_lock_timeout", sqlcode=exc.sqlcode, reason=exc.reason, query=exc.query),
            )
        return JSONResponse(
            status_code=500,
            content=_error_body("db_error", sqlcode=exc.sqlcode, sqlstate=exc.sqlstate, query=exc.query),
        )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.error, message=exc.message, **exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        error = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(error, message=exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_body("validation_error", details=jsonable_encoder(exc.errors())),
        )


def create_app(settings: Settings | None = None, pool: Db2Pool | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_level, settings.log_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_pool = pool or Db2Pool(
            settings.dsn,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            acquire_timeout=settings.pool_acquire_timeout,
        )
        app.state.pool = db_pool
        if pool is None:
            try:
                await run_in_threadpool(db_pool.start)
            except Exception:
                log.warning("could not pre-create pool connections; connections will be opened on demand")
        log.info(
            "apiserver started",
            extra={
                "db2_host": settings.db2_host,
                "db2_port": settings.db2_port,
                "db2_database": settings.db2_database,
                "pool_min_size": settings.pool_min_size,
                "pool_max_size": settings.pool_max_size,
                "pool_acquire_timeout": settings.pool_acquire_timeout,
            },
        )
        yield
        await run_in_threadpool(db_pool.close)
        log.info("apiserver stopped")

    app = FastAPI(title="E-Commerce Demo API", lifespan=lifespan)
    app.add_middleware(ObservabilityMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(products.router)
    app.include_router(customers.router)
    app.include_router(orders.router)
    return app


app = create_app()


def run() -> None:
    settings = load_settings()
    uvicorn.run(app, host="0.0.0.0", port=settings.app_port, log_config=None, access_log=False)


if __name__ == "__main__":
    run()
